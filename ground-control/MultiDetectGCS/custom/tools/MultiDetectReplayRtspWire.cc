#include "MultiDetectReplayRtspWire.h"

#include <QByteArrayView>
#include <QCryptographicHash>

#include <algorithm>
#include <cstring>
#include <limits>
#include <sstream>
#include <type_traits>

namespace MultiDetectReplayRtsp {
namespace {

template<typename T>
[[nodiscard]] T readLittleEndian(std::span<const std::uint8_t> bytes, std::size_t offset)
{
    static_assert(std::is_unsigned_v<T>);
    T value = 0;
    for (std::size_t index = 0; index < sizeof(T); ++index) {
        value |= static_cast<T>(bytes[offset + index]) << (index * 8U);
    }
    return value;
}

template<typename T, std::size_t Size>
void writeLittleEndian(std::array<std::uint8_t, Size>& bytes, std::size_t offset, T value)
{
    static_assert(std::is_unsigned_v<T>);
    for (std::size_t index = 0; index < sizeof(T); ++index) {
        bytes[offset + index] = static_cast<std::uint8_t>((value >> (index * 8U)) & static_cast<T>(0xFFU));
    }
}

[[nodiscard]] bool allZero(const std::array<std::uint8_t, 32>& value)
{
    return std::all_of(value.cbegin(), value.cend(), [](std::uint8_t byte) { return byte == 0; });
}

[[nodiscard]] std::string numberMismatch(const char* field, std::uint64_t actual, std::uint64_t expected)
{
    std::ostringstream stream;
    stream << field << " mismatch: got " << actual << ", expected " << expected;
    return stream.str();
}

}  // namespace

std::array<std::uint8_t, kHeaderBytes> encodeHeader(const Header& header)
{
    std::array<std::uint8_t, kHeaderBytes> bytes{};
    std::copy(kMagic.cbegin(), kMagic.cend(), bytes.begin());
    writeLittleEndian(bytes, 8, kVersion);
    writeLittleEndian(bytes, 10, static_cast<std::uint16_t>(header.type));
    writeLittleEndian(bytes, 12, kHeaderBytes);
    writeLittleEndian(bytes, 16, header.sequence);
    writeLittleEndian(bytes, 24, header.capturedAtNs);
    writeLittleEndian(bytes, 32, header.width);
    writeLittleEndian(bytes, 36, header.height);
    writeLittleEndian(bytes, 40, header.stride);
    writeLittleEndian(bytes, 44, header.pixelFormat);
    writeLittleEndian(bytes, 48, header.payloadBytes);
    std::copy(header.sha256.cbegin(), header.sha256.cend(), bytes.begin() + 56);
    return bytes;
}

bool decodeHeader(std::span<const std::uint8_t> bytes, Header& header, std::string& error)
{
    if (bytes.size() != kHeaderBytes) {
        error = numberMismatch("header byte count", bytes.size(), kHeaderBytes);
        return false;
    }
    if (!std::equal(kMagic.cbegin(), kMagic.cend(), bytes.begin())) {
        error = "wire magic mismatch";
        return false;
    }

    const auto version = readLittleEndian<std::uint16_t>(bytes, 8);
    if (version != kVersion) {
        error = numberMismatch("wire version", version, kVersion);
        return false;
    }

    const auto rawType = readLittleEndian<std::uint16_t>(bytes, 10);
    if (rawType != static_cast<std::uint16_t>(MessageType::Frame)
        && rawType != static_cast<std::uint16_t>(MessageType::EndOfStream)) {
        error = "unsupported wire message type: " + std::to_string(rawType);
        return false;
    }

    const auto headerBytes = readLittleEndian<std::uint32_t>(bytes, 12);
    if (headerBytes != kHeaderBytes) {
        error = numberMismatch("header_bytes", headerBytes, kHeaderBytes);
        return false;
    }

    Header decoded;
    decoded.type = static_cast<MessageType>(rawType);
    decoded.sequence = readLittleEndian<std::uint64_t>(bytes, 16);
    decoded.capturedAtNs = readLittleEndian<std::uint64_t>(bytes, 24);
    decoded.width = readLittleEndian<std::uint32_t>(bytes, 32);
    decoded.height = readLittleEndian<std::uint32_t>(bytes, 36);
    decoded.stride = readLittleEndian<std::uint32_t>(bytes, 40);
    decoded.pixelFormat = readLittleEndian<std::uint32_t>(bytes, 44);
    decoded.payloadBytes = readLittleEndian<std::uint64_t>(bytes, 48);
    std::copy_n(bytes.begin() + 56, decoded.sha256.size(), decoded.sha256.begin());

    if (decoded.width == 0 || decoded.width > kMaxWidth) {
        error = "width outside the strict range 1.." + std::to_string(kMaxWidth);
        return false;
    }
    if (decoded.height == 0 || decoded.height > kMaxHeight) {
        error = "height outside the strict range 1.." + std::to_string(kMaxHeight);
        return false;
    }
    if (decoded.payloadBytes > kMaxPayloadBytes) {
        error = "payload exceeds the hard " + std::to_string(kMaxPayloadBytes) + " byte limit";
        return false;
    }

    header = decoded;
    error.clear();
    return true;
}

std::array<std::uint8_t, 32> sha256(std::span<const std::uint8_t> bytes)
{
    QCryptographicHash hash(QCryptographicHash::Sha256);
    hash.addData(QByteArrayView(reinterpret_cast<const char*>(bytes.data()), static_cast<qsizetype>(bytes.size())));
    const QByteArray digest = hash.result();
    std::array<std::uint8_t, 32> result{};
    std::memcpy(result.data(), digest.constData(), result.size());
    return result;
}

ReadStatus readMessage(std::istream& stream, Message& message, std::string& error)
{
    std::array<std::uint8_t, kHeaderBytes> rawHeader{};
    stream.read(reinterpret_cast<char*>(rawHeader.data()), static_cast<std::streamsize>(rawHeader.size()));
    const std::streamsize headerRead = stream.gcount();
    if (headerRead == 0) {
        if (stream.eof()) {
            error = "stdin closed before an explicit EOS message";
            return ReadStatus::CleanEndOfFile;
        }
        error = "failed to read a wire header from stdin";
        return ReadStatus::IoError;
    }
    if (headerRead != static_cast<std::streamsize>(rawHeader.size())) {
        error = numberMismatch("truncated header bytes", static_cast<std::uint64_t>(headerRead), rawHeader.size());
        return ReadStatus::TruncatedHeader;
    }

    Header header;
    if (!decodeHeader(rawHeader, header, error)) {
        return ReadStatus::InvalidHeader;
    }

    std::vector<std::uint8_t> payload(static_cast<std::size_t>(header.payloadBytes));
    if (!payload.empty()) {
        stream.read(reinterpret_cast<char*>(payload.data()), static_cast<std::streamsize>(payload.size()));
        const std::streamsize payloadRead = stream.gcount();
        if (payloadRead != static_cast<std::streamsize>(payload.size())) {
            const auto actualBytes = static_cast<std::uint64_t>(std::max<std::streamsize>(payloadRead, 0));
            error = numberMismatch("truncated payload bytes", actualBytes, payload.size());
            return ReadStatus::TruncatedPayload;
        }
    }

    message.header = header;
    message.payload = std::move(payload);
    error.clear();
    return ReadStatus::Ok;
}

bool validRtspMount(std::string_view mount, std::string& error)
{
    if (mount.size() < 2 || mount.front() != '/' || mount.back() == '/') {
        error = "RTSP mount must be an absolute non-root path with no trailing slash";
        return false;
    }

    std::size_t componentStart = 1;
    for (std::size_t index = 1; index <= mount.size(); ++index) {
        if (index != mount.size() && mount[index] != '/') {
            const auto character = static_cast<unsigned char>(mount[index]);
            const bool unreserved = (character >= 'a' && character <= 'z')
                || (character >= 'A' && character <= 'Z') || (character >= '0' && character <= '9')
                || character == '.' || character == '_' || character == '~' || character == '-';
            if (!unreserved) {
                error = "RTSP mount contains a character outside the RFC3986 unreserved set";
                return false;
            }
            continue;
        }

        const std::string_view component = mount.substr(componentStart, index - componentStart);
        if (component.empty()) {
            error = "RTSP mount contains an empty path component";
            return false;
        }
        if (component == "." || component == "..") {
            error = "RTSP mount cannot contain dot or dot-dot path components";
            return false;
        }
        componentStart = index + 1;
    }

    error.clear();
    return true;
}

bool validH264Geometry(std::uint32_t width, std::uint32_t height, std::string& error)
{
    SequenceValidator validator(width, height);
    if (!validator.validConfiguration(error)) {
        return false;
    }
    if ((width & 1U) != 0 || (height & 1U) != 0) {
        error = "H.264 width and height must both be even";
        return false;
    }
    error.clear();
    return true;
}

SequenceValidator::SequenceValidator(std::uint32_t width, std::uint32_t height)
    : _width(width)
    , _height(height)
{
    const std::uint64_t stride = static_cast<std::uint64_t>(_width) * 3ULL;
    const std::uint64_t frameBytes = stride * static_cast<std::uint64_t>(_height);
    if (stride <= std::numeric_limits<std::uint32_t>::max() && frameBytes <= kMaxPayloadBytes) {
        _stride = static_cast<std::uint32_t>(stride);
        _frameBytes = frameBytes;
    }
}

bool SequenceValidator::validConfiguration(std::string& error) const
{
    if (_width == 0 || _width > kMaxWidth) {
        error = "configured width outside the strict range 1.." + std::to_string(kMaxWidth);
        return false;
    }
    if (_height == 0 || _height > kMaxHeight) {
        error = "configured height outside the strict range 1.." + std::to_string(kMaxHeight);
        return false;
    }
    if (_stride == 0 || _frameBytes == 0 || _frameBytes > kMaxPayloadBytes) {
        error = "configured BGR24 frame exceeds the hard " + std::to_string(kMaxPayloadBytes) + " byte limit";
        return false;
    }
    error.clear();
    return true;
}

bool SequenceValidator::validate(Message& message, std::string& error)
{
    if (_sawEndOfStream) {
        error = "message received after EOS";
        return false;
    }
    if (message.header.sequence != _nextSequence) {
        error = numberMismatch("sequence", message.header.sequence, _nextSequence);
        return false;
    }
    if (message.header.width != _width) {
        error = numberMismatch("width", message.header.width, _width);
        return false;
    }
    if (message.header.height != _height) {
        error = numberMismatch("height", message.header.height, _height);
        return false;
    }
    if (message.header.stride != _stride) {
        error = numberMismatch("stride", message.header.stride, _stride);
        return false;
    }
    if (message.header.pixelFormat != kPixelFormatBgr24) {
        error = numberMismatch("pixel_format", message.header.pixelFormat, kPixelFormatBgr24);
        return false;
    }

    if (message.header.type == MessageType::Frame) {
        if (message.header.payloadBytes != _frameBytes) {
            error = numberMismatch("payload_bytes", message.header.payloadBytes, _frameBytes);
            return false;
        }
        if (message.payload.size() != _frameBytes) {
            error = numberMismatch("actual payload byte count", message.payload.size(), _frameBytes);
            return false;
        }
        if (_hasFrame && message.header.capturedAtNs <= _lastCapturedAtNs) {
            error = "captured_at_ns must be strictly increasing";
            return false;
        }
        if (sha256(message.payload) != message.header.sha256) {
            error = "payload SHA-256 mismatch";
            return false;
        }

        _lastCapturedAtNs = message.header.capturedAtNs;
        _hasFrame = true;
        ++_nextSequence;
        error.clear();
        return true;
    }

    if (message.header.payloadBytes != 0 || !message.payload.empty()) {
        error = "EOS must have an empty payload";
        return false;
    }
    if (!allZero(message.header.sha256)) {
        error = "EOS SHA-256 must contain 32 zero bytes";
        return false;
    }
    const std::uint64_t expectedCapture = _hasFrame ? _lastCapturedAtNs : 0;
    if (message.header.capturedAtNs != expectedCapture) {
        error = numberMismatch("EOS captured_at_ns", message.header.capturedAtNs, expectedCapture);
        return false;
    }

    _sawEndOfStream = true;
    error.clear();
    return true;
}

}  // namespace MultiDetectReplayRtsp
