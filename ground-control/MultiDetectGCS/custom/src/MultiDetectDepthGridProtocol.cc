#include "MultiDetectDepthGridProtocol.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QDataStream>
#include <QtCore/QIODevice>
#include <QtCore/QMessageAuthenticationCode>

namespace {

constexpr qsizetype kHeaderBytes = 50;
constexpr qsizetype kAuthenticationBytes = 16;
constexpr quint8 kProtocolVersion = 1;
constexpr quint16 kMaximumFragments = 4096;
constexpr quint64 kAssemblyTimeoutMs = 1000;

bool constantTimeEqual(const QByteArray& left, const QByteArray& right)
{
    if (left.size() != right.size()) {
        return false;
    }
    quint8 difference = 0;
    for (qsizetype index = 0; index < left.size(); ++index) {
        difference |= static_cast<quint8>(left.at(index)) ^ static_cast<quint8>(right.at(index));
    }
    return difference == 0;
}

}  // namespace

MultiDetectDepthGridProtocol::MultiDetectDepthGridProtocol(const QByteArray& hmacKey) : _hmacKey(hmacKey) {}

void MultiDetectDepthGridProtocol::reset()
{
    _pendingSequence = 0;
    _pendingSequenceValid = false;
    _pendingFirstAtMs = 0;
    _pendingMetadata = {};
    _pendingFragments.clear();
}

std::optional<MultiDetectDepthGridFrame> MultiDetectDepthGridProtocol::ingest(const QByteArray& datagram,
                                                                             quint64 receivedAtMs, QString* error)
{
    Fragment fragment;
    if (!_decode(datagram, &fragment, error)) {
        return std::nullopt;
    }
    if (_pendingSequenceValid && receivedAtMs > _pendingFirstAtMs + kAssemblyTimeoutMs) {
        reset();
    }
    if (!_pendingSequenceValid || fragment.sequence != _pendingSequence) {
        if (_pendingSequenceValid && !_newerSequence(fragment.sequence, _pendingSequence)) {
            _setError(error, QStringLiteral("stale depth-grid sequence"));
            return std::nullopt;
        }
        reset();
        _pendingSequenceValid = true;
        _pendingSequence = fragment.sequence;
        _pendingFirstAtMs = receivedAtMs;
        _pendingMetadata = fragment;
    }
    const Fragment& expected = _pendingMetadata;
    if (fragment.width != expected.width || fragment.height != expected.height || fragment.count != expected.count ||
        fragment.minimumDepthMm != expected.minimumDepthMm ||
        fragment.maximumDepthMm != expected.maximumDepthMm || fragment.uncompressedSize != expected.uncompressedSize ||
        fragment.compressedSize != expected.compressedSize || fragment.rawCrc32 != expected.rawCrc32 ||
        fragment.logarithmicEncoding != expected.logarithmicEncoding) {
        reset();
        _setError(error, QStringLiteral("inconsistent depth-grid fragment metadata"));
        return std::nullopt;
    }
    _pendingFragments.insert(fragment.index, fragment.payload);
    if (_pendingFragments.size() != fragment.count) {
        return std::nullopt;
    }
    QByteArray compressed;
    compressed.reserve(fragment.compressedSize);
    for (int index = 0; index < fragment.count; ++index) {
        if (!_pendingFragments.contains(index)) {
            return std::nullopt;
        }
        compressed.append(_pendingFragments.value(index));
    }
    if (compressed.size() != fragment.compressedSize) {
        reset();
        _setError(error, QStringLiteral("depth-grid compressed size mismatch"));
        return std::nullopt;
    }
    const QByteArray raw = qUncompress(compressed);
    if (raw.size() != fragment.uncompressedSize || _crc32(raw) != fragment.rawCrc32) {
        reset();
        _setError(error, QStringLiteral("depth-grid decompression or checksum failed"));
        return std::nullopt;
    }
    MultiDetectDepthGridFrame frame;
    frame.sequence = fragment.sequence;
    frame.sentAtMs = fragment.sentAtMs;
    frame.width = fragment.width;
    frame.height = fragment.height;
    frame.minimumDepthM = static_cast<double>(fragment.minimumDepthMm) / 1000.0;
    frame.maximumDepthM = static_cast<double>(fragment.maximumDepthMm) / 1000.0;
    frame.logarithmicEncoding = fragment.logarithmicEncoding;
    frame.quantizedDepth = raw;
    reset();
    return frame;
}

bool MultiDetectDepthGridProtocol::_decode(const QByteArray& datagram, Fragment* fragment, QString* error) const
{
    if (!configured()) {
        _setError(error, QStringLiteral("depth-grid protocol is not configured"));
        return false;
    }
    if (datagram.size() < kHeaderBytes + kAuthenticationBytes) {
        _setError(error, QStringLiteral("truncated depth-grid datagram"));
        return false;
    }
    const QByteArray authenticated = datagram.left(datagram.size() - kAuthenticationBytes);
    const QByteArray observedTag = datagram.right(kAuthenticationBytes);
    const QByteArray expectedTag = QMessageAuthenticationCode::hash(authenticated, _hmacKey, QCryptographicHash::Sha256)
                                       .left(kAuthenticationBytes);
    if (!constantTimeEqual(observedTag, expectedTag)) {
        _setError(error, QStringLiteral("depth-grid authentication failed"));
        return false;
    }
    QDataStream stream(authenticated);
    stream.setByteOrder(QDataStream::BigEndian);
    char magic[4]{};
    quint8 version = 0;
    quint8 flags = 0;
    quint16 headerSize = 0;
    quint16 payloadSize = 0;
    if (stream.readRawData(magic, 4) != 4) {
        _setError(error, QStringLiteral("truncated depth-grid header"));
        return false;
    }
    stream >> version >> flags >> headerSize >> fragment->sequence >> fragment->sentAtMs >> fragment->width >>
        fragment->height >> fragment->index >> fragment->count >> fragment->minimumDepthMm >>
        fragment->maximumDepthMm >> fragment->uncompressedSize >> fragment->compressedSize >> fragment->rawCrc32 >>
        payloadSize;
    if (QByteArray(magic, 4) != QByteArrayLiteral("MDPD") || version != kProtocolVersion ||
        headerSize != kHeaderBytes || (flags & ~0x01U) || stream.status() != QDataStream::Ok) {
        _setError(error, QStringLiteral("unsupported depth-grid header"));
        return false;
    }
    fragment->logarithmicEncoding = (flags & 0x01U) != 0;
    if (fragment->width < 1 || fragment->height < 1 || fragment->width > 4096 || fragment->height > 4096 ||
        static_cast<quint32>(fragment->width) * fragment->height != fragment->uncompressedSize ||
        fragment->count < 1 || fragment->count > kMaximumFragments || fragment->index >= fragment->count ||
        fragment->compressedSize < 5 || fragment->compressedSize > fragment->uncompressedSize * 2U + 4U ||
        payloadSize != authenticated.size() - headerSize) {
        _setError(error, QStringLiteral("invalid depth-grid fragment geometry"));
        return false;
    }
    fragment->payload = authenticated.mid(headerSize, payloadSize);
    return true;
}

quint32 MultiDetectDepthGridProtocol::_crc32(const QByteArray& bytes)
{
    quint32 crc = 0xFFFFFFFFU;
    for (const char value : bytes) {
        crc ^= static_cast<quint8>(value);
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1U) ^ (0xEDB88320U & (0U - (crc & 1U)));
        }
    }
    return ~crc;
}

bool MultiDetectDepthGridProtocol::_newerSequence(quint32 candidate, quint32 reference)
{
    return static_cast<qint32>(candidate - reference) > 0;
}

void MultiDetectDepthGridProtocol::_setError(QString* error, const QString& value)
{
    if (error != nullptr) {
        *error = value;
    }
}
