#include "MultiDetectOperatorProtocol.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QMessageAuthenticationCode>
#include <QtCore/QSet>
#include <QtCore/QStringList>
#include <QtCore/QUuid>
#include <QtCore/QVariantList>
#include <algorithm>
#include <cmath>
#include <iterator>
#include <limits>
#include <utility>

namespace {

constexpr char kMagic[] = {'M', 'D'};
constexpr quint8 kProtocolVersion = 1;
constexpr int kHeaderBytes = 20;
constexpr int kAuthenticationTagBytes = 16;
constexpr int kSafetyRuleCount = 19;

class Reader final
{
public:
    explicit Reader(const QByteArray& data) : _data(data) {}

    int remaining() const { return _data.size() - _offset; }

    bool atEnd() const { return _offset == _data.size(); }

    bool readU8(quint8* value)
    {
        if (remaining() < 1) {
            return false;
        }
        *value = static_cast<quint8>(_data.at(_offset++));
        return true;
    }

    bool readU16(quint16* value)
    {
        quint8 high = 0;
        quint8 low = 0;
        if (!readU8(&high) || !readU8(&low)) {
            return false;
        }
        *value = static_cast<quint16>((static_cast<quint16>(high) << 8) | low);
        return true;
    }

    bool readI16(qint16* value)
    {
        quint16 raw = 0;
        if (!readU16(&raw)) {
            return false;
        }
        *value = static_cast<qint16>(raw);
        return true;
    }

    bool readU32(quint32* value)
    {
        quint16 high = 0;
        quint16 low = 0;
        if (!readU16(&high) || !readU16(&low)) {
            return false;
        }
        *value = (static_cast<quint32>(high) << 16) | low;
        return true;
    }

    bool readI32(qint32* value)
    {
        quint32 raw = 0;
        if (!readU32(&raw)) {
            return false;
        }
        *value = static_cast<qint32>(raw);
        return true;
    }

    bool readU64(quint64* value)
    {
        quint32 high = 0;
        quint32 low = 0;
        if (!readU32(&high) || !readU32(&low)) {
            return false;
        }
        *value = (static_cast<quint64>(high) << 32) | low;
        return true;
    }

    bool readBytes(int count, QByteArray* value)
    {
        if (count < 0 || remaining() < count) {
            return false;
        }
        *value = _data.mid(_offset, count);
        _offset += count;
        return true;
    }

private:
    const QByteArray& _data;
    int _offset = 0;
};

void appendU8(QByteArray& data, quint8 value)
{
    data.append(static_cast<char>(value));
}

void appendU16(QByteArray& data, quint16 value)
{
    appendU8(data, static_cast<quint8>(value >> 8));
    appendU8(data, static_cast<quint8>(value));
}

void appendI16(QByteArray& data, qint16 value)
{
    appendU16(data, static_cast<quint16>(value));
}

void appendU32(QByteArray& data, quint32 value)
{
    appendU16(data, static_cast<quint16>(value >> 16));
    appendU16(data, static_cast<quint16>(value));
}

void appendU64(QByteArray& data, quint64 value)
{
    appendU32(data, static_cast<quint32>(value >> 32));
    appendU32(data, static_cast<quint32>(value));
}

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

bool fail(QString* error, const QString& message)
{
    if (error) {
        *error = message;
    }
    return false;
}

QString uuidString(const QByteArray& bytes)
{
    return QUuid::fromRfc4122(bytes).toString(QUuid::WithoutBraces);
}

QString trackingState(quint8 value)
{
    switch (value) {
        case 1:
            return QStringLiteral("INITIALIZING");
        case 2:
            return QStringLiteral("TRACKING");
        case 3:
            return QStringLiteral("LOST");
        case 4:
            return QStringLiteral("CANCELLED");
        case 5:
            return QStringLiteral("REJECTED");
        default:
            return {};
    }
}

QString missionPhase(quint8 value)
{
    static const QStringList phases = {
        QStringLiteral("STANDBY"),
        QStringLiteral("NAVIGATING"),
        QStringLiteral("SEARCHING"),
        QStringLiteral("TARGET_CONFIRMED"),
        QStringLiteral("AWAITING_AUTHORIZATION"),
        QStringLiteral("DEPLOYMENT_READY"),
        QStringLiteral("DEPLOYING"),
        QStringLiteral("VERIFYING_RELEASE"),
        QStringLiteral("EGRESS"),
        QStringLiteral("RETURN_REQUESTED"),
        QStringLiteral("TERMINATED"),
        QStringLiteral("FAULT"),
    };
    return (value >= 1 && value <= phases.size()) ? phases.at(value - 1) : QString();
}

QString releaseWindow(quint8 value)
{
    switch (value) {
        case 0:
            return QStringLiteral("UNAVAILABLE");
        case 1:
            return QStringLiteral("UNAVAILABLE");
        case 2:
            return QStringLiteral("WAIT");
        case 3:
            return QStringLiteral("READY");
        default:
            return {};
    }
}

QString authorizationState(quint8 value)
{
    switch (value) {
        case 0:
            return QStringLiteral("NONE");
        case 1:
            return QStringLiteral("PENDING");
        case 2:
            return QStringLiteral("APPROVED");
        default:
            return {};
    }
}

QString patrolPhase(quint8 value)
{
    static const QStringList phases = {
        QStringLiteral("PATROL"),   QStringLiteral("DETECTED"), QStringLiteral("LOCKED_MONITOR"),
        QStringLiteral("TRACKING"), QStringLiteral("OCCLUDED"), QStringLiteral("REACQUIRING"),
        QStringLiteral("LOST"),
    };
    return (value >= 1 && value <= phases.size()) ? phases.at(value - 1) : QString();
}

QString unifiedTrackState(quint8 value)
{
    static const QStringList states = {
        QStringLiteral("DETECTED"), QStringLiteral("LOCKED"),      QStringLiteral("TRACKING"),
        QStringLiteral("OCCLUDED"), QStringLiteral("REACQUIRING"), QStringLiteral("RECOVERED"),
        QStringLiteral("LOST"),
    };
    return (value >= 1 && value <= states.size()) ? states.at(value - 1) : QString();
}

QString sceneContextState(quint8 value)
{
    switch (value) {
        case 1:
            return QStringLiteral("VALID");
        case 2:
            return QStringLiteral("INVALID");
        case 3:
            return QStringLiteral("STALE");
        default:
            return {};
    }
}

QString returnDirection(quint8 value)
{
    switch (value) {
        case 1:
            return QStringLiteral("LEFT");
        case 2:
            return QStringLiteral("RIGHT");
        case 3:
            return QStringLiteral("ROUTE_REQUIRED");
        default:
            return {};
    }
}

QString advisoryValidity(quint8 value)
{
    switch (value) {
        case 1:
            return QStringLiteral("VALID");
        case 2:
            return QStringLiteral("DEGRADED");
        case 3:
            return QStringLiteral("INVALID");
        default:
            return {};
    }
}

double decodeRatio(quint8 value)
{
    return value == 255 ? -1.0 : static_cast<double>(value) / 254.0;
}

double decodeBearing(qint16 value)
{
    return value == std::numeric_limits<qint16>::min() ? std::numeric_limits<double>::quiet_NaN()
                                                       : static_cast<double>(value) / 100.0;
}

double decodeDistance(quint16 value)
{
    return value == std::numeric_limits<quint16>::max() ? std::numeric_limits<double>::quiet_NaN()
                                                        : static_cast<double>(value) / 10.0;
}

double decodeSignedDistance(qint16 value)
{
    return value == std::numeric_limits<qint16>::min() ? std::numeric_limits<double>::quiet_NaN()
                                                       : static_cast<double>(value) / 10.0;
}

double decodeUnsignedBearing(quint16 value)
{
    return value == std::numeric_limits<quint16>::max() ? std::numeric_limits<double>::quiet_NaN()
                                                        : static_cast<double>(value) / 100.0;
}

double decodeUnsignedCentidegrees(quint16 value)
{
    return value == std::numeric_limits<quint16>::max() ? std::numeric_limits<double>::quiet_NaN()
                                                        : static_cast<double>(value) / 100.0;
}

double decodeSignedDistance32(qint32 value)
{
    return value == std::numeric_limits<qint32>::min() ? std::numeric_limits<double>::quiet_NaN()
                                                       : static_cast<double>(value) / 10.0;
}

QStringList decodeRegistryMask(quint32 mask, const QStringList& registry)
{
    QStringList values;
    for (int index = 0; index < registry.size(); ++index) {
        if (mask & (static_cast<quint32>(1) << index)) {
            values.append(registry.at(index));
        }
    }
    return values;
}

int populationCount(quint32 value)
{
    int count = 0;
    while (value) {
        value &= value - 1;
        ++count;
    }
    return count;
}

quint16 encodeCoordinate(double value)
{
    return static_cast<quint16>(std::llround(std::clamp(value, 0.0, 1.0) * 65535.0));
}

quint8 rotationCode(int degrees)
{
    switch (degrees) {
        case 0:
            return 0;
        case 90:
            return 1;
        case 180:
            return 2;
        case 270:
            return 3;
        default:
            return 255;
    }
}

}  // namespace

MultiDetectOperatorProtocol::MultiDetectOperatorProtocol(QByteArray hmacKey, QString streamId)
    : _hmacKey(std::move(hmacKey)), _streamId(std::move(streamId)), _streamHash(hash32(_streamId))
{}

bool MultiDetectOperatorProtocol::decode(const QByteArray& payload, DecodedPacket* packet, QString* error) const
{
    if (!packet) {
        return fail(error, QStringLiteral("decode output is null"));
    }
    *packet = {};
    if (!configured()) {
        return fail(error, QStringLiteral("operator-link key or stream is not configured"));
    }
    if (payload.size() < kHeaderBytes + kAuthenticationTagBytes) {
        return fail(error, QStringLiteral("operator-link payload is truncated"));
    }
    if (payload.size() > kMaximumPayloadBytes) {
        return fail(error, QStringLiteral("operator-link payload exceeds TUNNEL capacity"));
    }

    Reader header(payload.left(kHeaderBytes));
    QByteArray magic;
    quint8 version = 0;
    quint8 typeValue = 0;
    quint8 flags = 0;
    quint8 reserved = 0;
    quint32 sequence = 0;
    quint64 sentAtMs = 0;
    quint16 bodyLength = 0;
    if (!header.readBytes(2, &magic) || !header.readU8(&version) || !header.readU8(&typeValue) ||
        !header.readU8(&flags) || !header.readU8(&reserved) || !header.readU32(&sequence) ||
        !header.readU64(&sentAtMs) || !header.readU16(&bodyLength) || !header.atEnd()) {
        return fail(error, QStringLiteral("operator-link header is malformed"));
    }
    if (magic != QByteArray(kMagic, 2)) {
        return fail(error, QStringLiteral("operator-link magic does not match"));
    }
    if (version != kProtocolVersion) {
        return fail(error, QStringLiteral("operator-link version is unsupported"));
    }
    if (flags != 0 || reserved != 0) {
        return fail(error, QStringLiteral("operator-link flags are unsupported"));
    }
    if (payload.size() != kHeaderBytes + bodyLength + kAuthenticationTagBytes) {
        return fail(error, QStringLiteral("operator-link body length does not match"));
    }

    const QByteArray signedBytes = payload.first(payload.size() - kAuthenticationTagBytes);
    const QByteArray suppliedTag = payload.last(kAuthenticationTagBytes);
    const QByteArray expectedTag = QMessageAuthenticationCode::hash(signedBytes, _hmacKey, QCryptographicHash::Sha256)
                                       .first(kAuthenticationTagBytes);
    if (!constantTimeEqual(suppliedTag, expectedTag)) {
        return fail(error, QStringLiteral("operator-link authentication failed"));
    }

    const MessageType type = static_cast<MessageType>(typeValue);
    if (typeValue < static_cast<quint8>(MessageType::TargetSelection) ||
        typeValue > static_cast<quint8>(MessageType::TargetGeolocationStatus)) {
        return fail(error, QStringLiteral("operator-link message type is unknown"));
    }

    const QByteArray body = payload.mid(kHeaderBytes, bodyLength);
    Reader reader(body);
    QVariantMap fields;

    if (type == MessageType::TargetSelection) {
        QByteArray commandBytes;
        QByteArray sessionBytes;
        quint8 action = 0;
        quint32 streamHash = 0;
        quint16 width = 0;
        quint16 height = 0;
        quint8 rotation = 0;
        quint16 ttlMs = 0;
        quint8 bboxPresent = 0;
        quint16 x1 = 0, y1 = 0, x2 = 0, y2 = 0;
        quint8 framePresent = 0;
        quint64 frameHash = 0;
        if (!reader.readBytes(16, &commandBytes) || !reader.readBytes(16, &sessionBytes) || !reader.readU8(&action) ||
            !reader.readU32(&streamHash) || !reader.readU16(&width) || !reader.readU16(&height) ||
            !reader.readU8(&rotation) || !reader.readU16(&ttlMs) || !reader.readU8(&bboxPresent) ||
            !reader.readU16(&x1) || !reader.readU16(&y1) || !reader.readU16(&x2) || !reader.readU16(&y2) ||
            !reader.readU8(&framePresent) || !reader.readU64(&frameHash) || !reader.atEnd()) {
            return fail(error, QStringLiteral("target-selection body has an invalid size"));
        }
        if (streamHash != _streamHash || action < 1 || action > 7 || bboxPresent > 1 || framePresent > 1 ||
            rotation > 3 || width == 0 || height == 0 || ttlMs == 0) {
            return fail(error, QStringLiteral("target-selection content is invalid"));
        }
        if ((action == 3 && bboxPresent != 0) || (action != 3 && bboxPresent != 1) ||
            (bboxPresent && (x2 <= x1 || y2 <= y1))) {
            return fail(error, QStringLiteral("target-selection bounding box is invalid"));
        }
        fields.insert(QStringLiteral("commandId"), uuidString(commandBytes));
        fields.insert(QStringLiteral("sessionId"), uuidString(sessionBytes));
        fields.insert(QStringLiteral("action"), action == 1   ? QStringLiteral("SELECT")
                                                : action == 2 ? QStringLiteral("SWITCH")
                                                : action == 3 ? QStringLiteral("CANCEL")
                                                : action == 4 ? QStringLiteral("SELECT_TRK")
                                                : action == 5 ? QStringLiteral("PROMOTE_LCK")
                                                : action == 6 ? QStringLiteral("DEMOTE_TRK")
                                                              : QStringLiteral("CANCEL_TRK"));
        fields.insert(QStringLiteral("width"), width);
        fields.insert(QStringLiteral("height"), height);
        fields.insert(QStringLiteral("rotation"), static_cast<int>(rotation) * 90);
        fields.insert(QStringLiteral("ttlMs"), ttlMs);
        fields.insert(QStringLiteral("bboxValid"), bboxPresent == 1);
        fields.insert(QStringLiteral("x1"), static_cast<double>(x1) / 65535.0);
        fields.insert(QStringLiteral("y1"), static_cast<double>(y1) / 65535.0);
        fields.insert(QStringLiteral("x2"), static_cast<double>(x2) / 65535.0);
        fields.insert(QStringLiteral("y2"), static_cast<double>(y2) / 65535.0);
        fields.insert(QStringLiteral("frameId"), framePresent ? hashedIdentifier(frameHash) : QString());
    } else if (type == MessageType::SelectionAck) {
        QByteArray commandBytes;
        quint8 accepted = 0;
        quint8 reason = 0;
        quint32 acknowledgedSequence = 0;
        if (!reader.readBytes(16, &commandBytes) || !reader.readU8(&accepted) || !reader.readU8(&reason) ||
            !reader.readU32(&acknowledgedSequence) || !reader.atEnd() || accepted > 1) {
            return fail(error, QStringLiteral("selection-ack body is invalid"));
        }
        fields.insert(QStringLiteral("commandId"), uuidString(commandBytes));
        fields.insert(QStringLiteral("accepted"), accepted == 1);
        fields.insert(QStringLiteral("reason"), reason);
        fields.insert(QStringLiteral("acknowledgedSequence"), acknowledgedSequence);
    } else if (type == MessageType::TrackStatus) {
        quint64 statusId = 0;
        QByteArray commandBytes;
        quint8 stateValue = 0;
        quint32 streamHash = 0;
        quint16 width = 0, height = 0;
        quint8 rotation = 0, targetPresent = 0;
        quint64 targetId = 0;
        quint8 bboxPresent = 0;
        quint16 x1 = 0, y1 = 0, x2 = 0, y2 = 0;
        quint8 labelLength = 0;
        QByteArray labelBytes;
        quint8 confidence = 0, quality = 0;
        quint64 frameId = 0;
        quint16 frameAgeMs = 0;
        qint16 bearing = 0;
        quint16 distance = 0;
        if (!reader.readU64(&statusId) || !reader.readBytes(16, &commandBytes) || !reader.readU8(&stateValue) ||
            !reader.readU32(&streamHash) || !reader.readU16(&width) || !reader.readU16(&height) ||
            !reader.readU8(&rotation) || !reader.readU8(&targetPresent) || !reader.readU64(&targetId) ||
            !reader.readU8(&bboxPresent) || !reader.readU16(&x1) || !reader.readU16(&y1) || !reader.readU16(&x2) ||
            !reader.readU16(&y2) || !reader.readU8(&labelLength) || !reader.readBytes(16, &labelBytes) ||
            !reader.readU8(&confidence) || !reader.readU8(&quality) || !reader.readU64(&frameId) ||
            !reader.readU16(&frameAgeMs) || !reader.readI16(&bearing) || !reader.readU16(&distance) ||
            !reader.atEnd()) {
            return fail(error, QStringLiteral("track-status body has an invalid size"));
        }
        const QString state = trackingState(stateValue);
        if (state.isEmpty() || streamHash != _streamHash || targetPresent > 1 || bboxPresent > 1 || rotation > 3 ||
            labelLength > 16 || width == 0 || height == 0 || (bboxPresent && (x2 <= x1 || y2 <= y1))) {
            return fail(error, QStringLiteral("track-status content is invalid"));
        }
        const QString label = QString::fromUtf8(labelBytes.first(labelLength));
        if (label.toUtf8() != labelBytes.first(labelLength)) {
            return fail(error, QStringLiteral("track-status label is not valid UTF-8"));
        }
        fields.insert(QStringLiteral("statusId"), hashedIdentifier(statusId));
        fields.insert(QStringLiteral("selectionCommandId"), uuidString(commandBytes));
        fields.insert(QStringLiteral("state"), state);
        fields.insert(QStringLiteral("streamId"), _streamId);
        fields.insert(QStringLiteral("width"), width);
        fields.insert(QStringLiteral("height"), height);
        fields.insert(QStringLiteral("rotation"), static_cast<int>(rotation) * 90);
        fields.insert(QStringLiteral("targetPresent"), targetPresent == 1);
        fields.insert(QStringLiteral("targetId"), targetPresent ? hashedIdentifier(targetId) : QString());
        fields.insert(QStringLiteral("bboxValid"), bboxPresent == 1);
        fields.insert(QStringLiteral("x1"), static_cast<double>(x1) / 65535.0);
        fields.insert(QStringLiteral("y1"), static_cast<double>(y1) / 65535.0);
        fields.insert(QStringLiteral("x2"), static_cast<double>(x2) / 65535.0);
        fields.insert(QStringLiteral("y2"), static_cast<double>(y2) / 65535.0);
        fields.insert(QStringLiteral("label"), label);
        fields.insert(QStringLiteral("confidence"), decodeRatio(confidence));
        fields.insert(QStringLiteral("trackingQuality"), decodeRatio(quality));
        fields.insert(QStringLiteral("sourceFrameId"), hashedIdentifier(frameId));
        fields.insert(QStringLiteral("sourceAgeMs"), frameAgeMs);
        fields.insert(QStringLiteral("relativeBearingDeg"), decodeBearing(bearing));
        fields.insert(QStringLiteral("estimatedRangeM"), decodeDistance(distance));
    } else if (type == MessageType::MissionStatus) {
        quint64 statusId = 0, missionId = 0, targetId = 0, slotId = 0;
        quint8 phaseValue = 0, windowValue = 0, safetyValue = 0, authorizationValue = 0;
        quint16 remainingPayloads = 0, totalPayloads = 0;
        quint8 targetPresent = 0, slotPresent = 0, confidence = 0;
        qint16 bearing = 0, crossTrack = 0, alongTrack = 0;
        quint16 distance = 0, releaseLead = 0;
        if (!reader.readU64(&statusId) || !reader.readU64(&missionId) || !reader.readU8(&phaseValue) ||
            !reader.readU8(&windowValue) || !reader.readU8(&safetyValue) || !reader.readU8(&authorizationValue) ||
            !reader.readU16(&remainingPayloads) || !reader.readU16(&totalPayloads) || !reader.readU8(&targetPresent) ||
            !reader.readU64(&targetId) || !reader.readU8(&slotPresent) || !reader.readU64(&slotId) ||
            !reader.readU8(&confidence) || !reader.readI16(&bearing) || !reader.readU16(&distance) ||
            !reader.readI16(&crossTrack) || !reader.readI16(&alongTrack) || !reader.readU16(&releaseLead) ||
            !reader.atEnd()) {
            return fail(error, QStringLiteral("mission-status body has an invalid size"));
        }
        const QString phase = missionPhase(phaseValue);
        const QString window = releaseWindow(windowValue);
        const QString authorization = authorizationState(authorizationValue);
        if (phase.isEmpty() || window.isEmpty() || authorization.isEmpty() || safetyValue > 2 || targetPresent > 1 ||
            slotPresent > 1 || remainingPayloads > totalPayloads) {
            return fail(error, QStringLiteral("mission-status content is invalid"));
        }
        fields.insert(QStringLiteral("statusId"), hashedIdentifier(statusId));
        fields.insert(QStringLiteral("missionId"), hashedIdentifier(missionId));
        fields.insert(QStringLiteral("phase"), phase);
        fields.insert(QStringLiteral("releaseWindow"), window);
        fields.insert(QStringLiteral("safetyKnown"), safetyValue != 0);
        fields.insert(QStringLiteral("safetyAllowed"), safetyValue == 2);
        fields.insert(QStringLiteral("authorizationState"), authorization);
        fields.insert(QStringLiteral("remainingPayloadCount"), remainingPayloads);
        fields.insert(QStringLiteral("totalPayloadCount"), totalPayloads);
        fields.insert(QStringLiteral("targetId"), targetPresent ? hashedIdentifier(targetId) : QString());
        fields.insert(QStringLiteral("activePayloadSlot"), slotPresent ? hashedIdentifier(slotId) : QString());
        fields.insert(QStringLiteral("targetConfidence"), decodeRatio(confidence));
        fields.insert(QStringLiteral("relativeBearingDeg"), decodeBearing(bearing));
        fields.insert(QStringLiteral("estimatedRangeM"), decodeDistance(distance));
        fields.insert(QStringLiteral("crossTrackErrorM"), decodeSignedDistance(crossTrack));
        fields.insert(QStringLiteral("alongTrackErrorM"), decodeSignedDistance(alongTrack));
        fields.insert(QStringLiteral("releaseLeadDistanceM"), decodeDistance(releaseLead));
    } else if (type == MessageType::SafetyStatus) {
        quint64 statusId = 0, missionId = 0, rulesetId = 0, targetId = 0;
        quint8 targetPresent = 0, registryVersion = 0;
        quint32 presentMask = 0, passMask = 0, denyMask = 0, unknownMask = 0;
        if (!reader.readU64(&statusId) || !reader.readU64(&missionId) || !reader.readU64(&rulesetId) ||
            !reader.readU8(&targetPresent) || !reader.readU64(&targetId) || !reader.readU32(&presentMask) ||
            !reader.readU32(&passMask) || !reader.readU32(&denyMask) || !reader.readU32(&unknownMask) ||
            !reader.readU8(&registryVersion) || !reader.atEnd()) {
            return fail(error, QStringLiteral("safety-status body has an invalid size"));
        }
        const quint32 verdictUnion = passMask | denyMask | unknownMask;
        const quint32 registeredMask = (static_cast<quint32>(1) << kSafetyRuleCount) - 1;
        if (registryVersion != 1 || targetPresent != 1 || verdictUnion != presentMask || (passMask & denyMask) ||
            (passMask & unknownMask) || (denyMask & unknownMask) || (presentMask & ~registeredMask)) {
            return fail(error, QStringLiteral("safety-status masks are invalid"));
        }
        fields.insert(QStringLiteral("statusId"), hashedIdentifier(statusId));
        fields.insert(QStringLiteral("missionId"), hashedIdentifier(missionId));
        fields.insert(QStringLiteral("rulesetId"), hashedIdentifier(rulesetId));
        fields.insert(QStringLiteral("targetId"), hashedIdentifier(targetId));
        fields.insert(QStringLiteral("presentCount"), populationCount(presentMask));
        fields.insert(QStringLiteral("passCount"), populationCount(passMask));
        fields.insert(QStringLiteral("denyCount"), populationCount(denyMask));
        fields.insert(QStringLiteral("unknownCount"), populationCount(unknownMask));
        fields.insert(QStringLiteral("allowed"), presentMask != 0 && passMask == presentMask);
        fields.insert(QStringLiteral("registryVersion"), registryVersion);
    } else if (type == MessageType::PatrolStatus) {
        quint64 statusId = 0, missionId = 0, targetId = 0, frameId = 0;
        quint8 phaseValue = 0, targetStateValue = 0, statusFlags = 0;
        quint8 directionValue = 0, validityValue = 0;
        quint16 x1 = 0, y1 = 0, x2 = 0, y2 = 0;
        quint8 labelLength = 0, confidence = 0, quality = 0;
        QByteArray labelBytes;
        quint16 totalTracks = 0, lockedTracks = 0, frameAgeMs = 0;
        quint16 evidenceAge = 0, turnRadius = 0;
        if (!reader.readU64(&statusId) || !reader.readU64(&missionId) || !reader.readU8(&phaseValue) ||
            !reader.readU8(&targetStateValue) || !reader.readU8(&statusFlags) || !reader.readU8(&directionValue) ||
            !reader.readU8(&validityValue) || !reader.readU64(&targetId) || !reader.readU16(&x1) ||
            !reader.readU16(&y1) || !reader.readU16(&x2) || !reader.readU16(&y2) || !reader.readU8(&labelLength) ||
            !reader.readBytes(16, &labelBytes) || !reader.readU8(&confidence) || !reader.readU8(&quality) ||
            !reader.readU16(&totalTracks) || !reader.readU16(&lockedTracks) || !reader.readU64(&frameId) ||
            !reader.readU16(&frameAgeMs) || !reader.readU16(&evidenceAge) || !reader.readU16(&turnRadius) ||
            !reader.atEnd()) {
            return fail(error, QStringLiteral("patrol-status body has an invalid size"));
        }

        constexpr quint8 kTargetPresent = 1 << 0;
        constexpr quint8 kBboxPresent = 1 << 1;
        constexpr quint8 kReturnPresent = 1 << 2;
        constexpr quint16 kUnavailable = std::numeric_limits<quint16>::max();
        const bool targetPresent = (statusFlags & kTargetPresent) != 0;
        const bool bboxPresent = (statusFlags & kBboxPresent) != 0;
        const bool returnPresent = (statusFlags & kReturnPresent) != 0;
        const QString phase = patrolPhase(phaseValue);
        const QString state = targetStateValue == 0 ? QString() : unifiedTrackState(targetStateValue);
        const QString direction = directionValue == 0 ? QString() : returnDirection(directionValue);
        const QString validity = validityValue == 0 ? QString() : advisoryValidity(validityValue);
        if (statusFlags & ~static_cast<quint8>(0b111) || phase.isEmpty() || targetStateValue > 7 ||
            directionValue > 3 || validityValue > 3 || labelLength > 16 || lockedTracks > totalTracks ||
            statusId == 0 || missionId == 0 || frameId == 0) {
            return fail(error, QStringLiteral("patrol-status content is invalid"));
        }
        if (bboxPresent && (!targetPresent || x2 <= x1 || y2 <= y1)) {
            return fail(error, QStringLiteral("patrol-status bounding box is invalid"));
        }
        if (!bboxPresent && (x1 != 0 || y1 != 0 || x2 != 0 || y2 != 0)) {
            return fail(error, QStringLiteral("patrol-status absent bounding box is not zeroed"));
        }
        if (!targetPresent && (targetId != 0 || targetStateValue != 0 || bboxPresent || labelLength != 0 ||
                               confidence != 255 || quality != 255 || returnPresent)) {
            return fail(error, QStringLiteral("patrol-status target metadata has no primary target"));
        }
        if (targetPresent && targetId == 0) {
            return fail(error, QStringLiteral("patrol-status primary target is invalid"));
        }
        if (phase == QStringLiteral("PATROL") && targetPresent) {
            return fail(error, QStringLiteral("PATROL status cannot contain a primary target"));
        }
        if (returnPresent != (!direction.isEmpty() && !validity.isEmpty()) ||
            (returnPresent &&
             (phase != QStringLiteral("LOST") || !targetPresent || evidenceAge == kUnavailable || turnRadius == 0)) ||
            (!returnPresent && (directionValue != 0 || validityValue != 0 || evidenceAge != kUnavailable ||
                                turnRadius != kUnavailable))) {
            return fail(error, QStringLiteral("patrol-status return-observe metadata is inconsistent"));
        }
        const QByteArray labelPadding = labelBytes.mid(labelLength);
        if (std::any_of(labelPadding.cbegin(), labelPadding.cend(), [](char value) { return value != '\0'; })) {
            return fail(error, QStringLiteral("patrol-status label padding is not zeroed"));
        }
        const QString label = QString::fromUtf8(labelBytes.first(labelLength));
        if (label.toUtf8() != labelBytes.first(labelLength)) {
            return fail(error, QStringLiteral("patrol-status label is not valid UTF-8"));
        }

        fields.insert(QStringLiteral("statusId"), hashedIdentifier(statusId));
        fields.insert(QStringLiteral("missionId"), hashedIdentifier(missionId));
        fields.insert(QStringLiteral("phase"), phase);
        fields.insert(QStringLiteral("targetPresent"), targetPresent);
        fields.insert(QStringLiteral("primaryTargetId"), targetPresent ? hashedIdentifier(targetId) : QString());
        fields.insert(QStringLiteral("targetState"), state);
        fields.insert(QStringLiteral("bboxValid"), bboxPresent);
        fields.insert(QStringLiteral("x1"), static_cast<double>(x1) / 65535.0);
        fields.insert(QStringLiteral("y1"), static_cast<double>(y1) / 65535.0);
        fields.insert(QStringLiteral("x2"), static_cast<double>(x2) / 65535.0);
        fields.insert(QStringLiteral("y2"), static_cast<double>(y2) / 65535.0);
        fields.insert(QStringLiteral("label"), label);
        fields.insert(QStringLiteral("confidence"), decodeRatio(confidence));
        fields.insert(QStringLiteral("trackingQuality"), decodeRatio(quality));
        fields.insert(QStringLiteral("totalTrackCount"), totalTracks);
        fields.insert(QStringLiteral("lockedTrackCount"), lockedTracks);
        fields.insert(QStringLiteral("sourceFrameId"), hashedIdentifier(frameId));
        fields.insert(QStringLiteral("sourceAgeMs"), frameAgeMs);
        fields.insert(QStringLiteral("returnDirection"), direction);
        fields.insert(QStringLiteral("returnValidity"), validity);
        fields.insert(QStringLiteral("returnEvidenceAgeS"),
                      returnPresent ? static_cast<double>(evidenceAge) / 10.0 : -1.0);
        fields.insert(QStringLiteral("estimatedMinimumTurnRadiusM"),
                      returnPresent && turnRadius != kUnavailable ? static_cast<double>(turnRadius) / 10.0 : -1.0);
        fields.insert(QStringLiteral("operatorConfirmationRequired"), true);
        fields.insert(QStringLiteral("sitlValidationRequired"), true);
        fields.insert(QStringLiteral("advisoryOnly"), true);
        fields.insert(QStringLiteral("flightControlEnabled"), false);
    } else if (type == MessageType::RangeStatus) {
        quint64 statusId = 0, targetId = 0, calibrationId = 0, frameId = 0;
        quint8 validityValue = 0, statusFlags = 0, consistency = 0;
        quint32 reasonMask = 0;
        quint16 sourceMask = 0, rejectedSourceMask = 0;
        quint16 slantRange = 0, groundRange = 0, slantLow = 0, slantHigh = 0;
        quint16 groundLow = 0, groundHigh = 0;
        qint16 relativeBearing = 0;
        quint16 absoluteBearing = 0, bearingSigma = 0;
        qint32 northOffset = 0, eastOffset = 0;
        quint16 frameAgeMs = 0, dataFreshness = 0;
        QByteArray contributionOne, contributionTwo, contributionThree;
        if (!reader.readU64(&statusId) || !reader.readU64(&targetId) || !reader.readU64(&calibrationId) ||
            !reader.readU64(&frameId) || !reader.readU8(&validityValue) || !reader.readU8(&statusFlags) ||
            !reader.readU32(&reasonMask) || !reader.readU16(&sourceMask) || !reader.readU16(&rejectedSourceMask) ||
            !reader.readU16(&slantRange) || !reader.readU16(&groundRange) || !reader.readU16(&slantLow) ||
            !reader.readU16(&slantHigh) || !reader.readU16(&groundLow) || !reader.readU16(&groundHigh) ||
            !reader.readI16(&relativeBearing) || !reader.readU16(&absoluteBearing) || !reader.readU16(&bearingSigma) ||
            !reader.readI32(&northOffset) || !reader.readI32(&eastOffset) || !reader.readU16(&frameAgeMs) ||
            !reader.readU16(&dataFreshness) || !reader.readU8(&consistency) || !reader.readBytes(6, &contributionOne) ||
            !reader.readBytes(6, &contributionTwo) || !reader.readBytes(6, &contributionThree) || !reader.atEnd()) {
            return fail(error, QStringLiteral("range-status body has an invalid size"));
        }

        static const QStringList reasonRegistry = {
            QStringLiteral("pose_stale_or_from_future"),
            QStringLiteral("target_image_stale_or_from_future"),
            QStringLiteral("pose_image_time_skew_exceeded"),
            QStringLiteral("duplicate_vertical_source"),
            QStringLiteral("vertical_reference_unavailable_or_stale"),
            QStringLiteral("vertical_references_inconsistent"),
            QStringLiteral("vertical_reference_outlier_rejected"),
            QStringLiteral("target_ray_does_not_intersect_ground_safely"),
            QStringLiteral("camera_ground_intersection_out_of_range"),
            QStringLiteral("duplicate_direct_range_source"),
            QStringLiteral("laser_target_mismatch"),
            QStringLiteral("vio_target_mismatch"),
            QStringLiteral("laser_absolute_scale_invalid"),
            QStringLiteral("vio_absolute_scale_invalid"),
            QStringLiteral("laser_stale_or_from_future"),
            QStringLiteral("vio_stale_or_from_future"),
            QStringLiteral("laser_out_of_range"),
            QStringLiteral("vio_out_of_range"),
            QStringLiteral("absolute_range_sources_inconsistent"),
            QStringLiteral("absolute_range_outlier_rejected"),
            QStringLiteral("single_absolute_range_method"),
            QStringLiteral("multimodal_range_consistent"),
            QStringLiteral("primary_target_snapshot_unavailable"),
            QStringLiteral("primary_target_not_freshly_observed"),
            QStringLiteral("pixhawk_pose_or_timestamp_unavailable"),
            QStringLiteral("pixhawk_agl_unavailable"),
            QStringLiteral("attitude_position_time_skew_exceeded"),
            QStringLiteral("target_not_freshly_observed"),
            QStringLiteral("direct_degraded_metric_range"),
            QStringLiteral("vertical_reference_unavailable"),
            QStringLiteral("direct_range_unavailable"),
        };
        static const QStringList sourceRegistry = {
            QStringLiteral("pixhawk_agl"),    QStringLiteral("dem_gps"),
            QStringLiteral("ground_plane"),   QStringLiteral("camera_ground"),
            QStringLiteral("laser"),          QStringLiteral("vio"),
            QStringLiteral("monocular_size"), QStringLiteral("monocular_metric"),
            QStringLiteral("rgb_slam"),
        };
        constexpr quint16 kUnavailable = std::numeric_limits<quint16>::max();
        constexpr quint32 kReasonMask = (static_cast<quint32>(1) << 31) - 1;
        constexpr quint16 kSourceMask = (static_cast<quint16>(1) << 9) - 1;
        static const QStringList vehicleProfiles = {
            QStringLiteral("auto"),
            QStringLiteral("fixed-wing"),
            QStringLiteral("multirotor"),
        };
        static const QStringList navigationStates = {
            QStringLiteral("unknown"),   QStringLiteral("vision-only"), QStringLiteral("gps-aided"),
            QStringLiteral("local-ned"), QStringLiteral("airspeed-dr"),
        };
        static const QStringList motionRegimes = {
            QStringLiteral("unknown"), QStringLiteral("static"),     QStringLiteral("low-speed"),
            QStringLiteral("cruise"),  QStringLiteral("high-speed"),
        };
        const quint8 vehicleCode = statusFlags & 0x03U;
        const quint8 navigationCode = (statusFlags >> 2U) & 0x07U;
        const quint8 motionCode = (statusFlags >> 5U) & 0x07U;
        const QString validity = advisoryValidity(validityValue);
        const bool slantIntervalAvailable = slantLow != kUnavailable || slantHigh != kUnavailable;
        const bool groundIntervalAvailable = groundLow != kUnavailable || groundHigh != kUnavailable;
        if (validity.isEmpty() || vehicleCode >= vehicleProfiles.size() || navigationCode >= navigationStates.size() ||
            motionCode >= motionRegimes.size() || reasonMask == 0 || (reasonMask & ~kReasonMask) ||
            (sourceMask & ~kSourceMask) || (rejectedSourceMask & ~kSourceMask) || (sourceMask & rejectedSourceMask) ||
            consistency == 255 || statusId == 0 || targetId == 0 || calibrationId == 0 || frameId == 0 ||
            sentAtMs < frameAgeMs) {
            return fail(error, QStringLiteral("range-status content is invalid"));
        }
        if ((slantIntervalAvailable &&
             (slantLow == kUnavailable || slantHigh == kUnavailable || slantLow > slantHigh)) ||
            (groundIntervalAvailable &&
             (groundLow == kUnavailable || groundHigh == kUnavailable || groundLow > groundHigh))) {
            return fail(error, QStringLiteral("range-status confidence interval is invalid"));
        }
        if (validity == QStringLiteral("INVALID") && (slantRange != kUnavailable || groundRange != kUnavailable ||
                                                      slantIntervalAvailable || groundIntervalAvailable)) {
            return fail(error, QStringLiteral("invalid range-status cannot contain distance"));
        }
        QVariantList sourceContributions;
        const auto decodeContribution = [&](const QByteArray& encoded) {
            if (encoded == QByteArray(6, '\0')) {
                return true;
            }
            const quint8 sourceIndex = static_cast<quint8>(encoded.at(0));
            const quint8 weight = static_cast<quint8>(encoded.at(1));
            const quint16 range =
                (static_cast<quint16>(static_cast<quint8>(encoded.at(2))) << 8U) | static_cast<quint8>(encoded.at(3));
            const quint16 sigma =
                (static_cast<quint16>(static_cast<quint8>(encoded.at(4))) << 8U) | static_cast<quint8>(encoded.at(5));
            if (sourceIndex == 0 || sourceIndex > sourceRegistry.size() || weight == 255 || range == kUnavailable ||
                sigma == kUnavailable || sigma == 0 || !(sourceMask & (static_cast<quint16>(1) << (sourceIndex - 1)))) {
                return false;
            }
            QVariantMap contribution;
            contribution.insert(QStringLiteral("source"), sourceRegistry.at(sourceIndex - 1));
            contribution.insert(QStringLiteral("rangeM"), static_cast<double>(range) / 10.0);
            contribution.insert(QStringLiteral("sigmaM"), static_cast<double>(sigma) / 10.0);
            contribution.insert(QStringLiteral("weight"), static_cast<double>(weight) / 254.0);
            sourceContributions.append(contribution);
            return true;
        };
        if (!decodeContribution(contributionOne) || !decodeContribution(contributionTwo) ||
            !decodeContribution(contributionThree)) {
            return fail(error, QStringLiteral("range-status contribution is invalid"));
        }

        fields.insert(QStringLiteral("statusId"), hashedIdentifier(statusId));
        fields.insert(QStringLiteral("targetId"), hashedIdentifier(targetId));
        fields.insert(QStringLiteral("calibrationId"), hashedIdentifier(calibrationId));
        fields.insert(QStringLiteral("sourceFrameId"), hashedIdentifier(frameId));
        fields.insert(QStringLiteral("validity"), validity);
        fields.insert(QStringLiteral("reasons"), decodeRegistryMask(reasonMask, reasonRegistry));
        fields.insert(QStringLiteral("sources"), decodeRegistryMask(sourceMask, sourceRegistry));
        fields.insert(QStringLiteral("rejectedSources"), decodeRegistryMask(rejectedSourceMask, sourceRegistry));
        fields.insert(QStringLiteral("slantRangeM"), decodeDistance(slantRange));
        fields.insert(QStringLiteral("groundRangeM"), decodeDistance(groundRange));
        fields.insert(QStringLiteral("slantRangeLowM"), decodeDistance(slantLow));
        fields.insert(QStringLiteral("slantRangeHighM"), decodeDistance(slantHigh));
        fields.insert(QStringLiteral("groundRangeLowM"), decodeDistance(groundLow));
        fields.insert(QStringLiteral("groundRangeHighM"), decodeDistance(groundHigh));
        fields.insert(QStringLiteral("relativeBearingDeg"), decodeBearing(relativeBearing));
        fields.insert(QStringLiteral("absoluteBearingDeg"), decodeUnsignedBearing(absoluteBearing));
        fields.insert(QStringLiteral("bearingSigmaDeg"), decodeUnsignedCentidegrees(bearingSigma));
        fields.insert(QStringLiteral("northOffsetM"), decodeSignedDistance32(northOffset));
        fields.insert(QStringLiteral("eastOffsetM"), decodeSignedDistance32(eastOffset));
        fields.insert(QStringLiteral("sourceAgeMs"), frameAgeMs);
        fields.insert(QStringLiteral("dataFreshnessS"), decodeDistance(dataFreshness));
        fields.insert(QStringLiteral("sensorConsistency"), decodeRatio(consistency));
        fields.insert(QStringLiteral("sourceContributions"), sourceContributions);
        fields.insert(QStringLiteral("fusionProfile"), QStringLiteral("outdoor-multimodal-v1"));
        fields.insert(QStringLiteral("vehicleProfile"), vehicleProfiles.at(vehicleCode));
        fields.insert(QStringLiteral("navigationState"), navigationStates.at(navigationCode));
        fields.insert(QStringLiteral("motionRegime"), motionRegimes.at(motionCode));
        fields.insert(QStringLiteral("advisoryOnly"), true);
        fields.insert(QStringLiteral("flightControlEnabled"), false);
        fields.insert(QStringLiteral("physicalReleaseEnabled"), false);
    } else if (type == MessageType::TargetGeolocationStatus) {
        quint64 targetId = 0, frameId = 0;
        quint8 availableValue = 0, reasonValue = 0;
        qint32 latitudeE7 = 0, longitudeE7 = 0;
        quint16 horizontalSigma = 0, frameAgeMs = 0;
        if (!reader.readU64(&targetId) || !reader.readU64(&frameId) || !reader.readU8(&availableValue) ||
            !reader.readU8(&reasonValue) || !reader.readI32(&latitudeE7) || !reader.readI32(&longitudeE7) ||
            !reader.readU16(&horizontalSigma) || !reader.readU16(&frameAgeMs) || !reader.atEnd()) {
            return fail(error, QStringLiteral("target-geolocation-status body has an invalid size"));
        }
        static const QStringList reasonRegistry = {
            QStringLiteral("gps_qualified"),
            QStringLiteral("gps_navigation_not_qualified"),
            QStringLiteral("target_offset_unavailable"),
            QStringLiteral("geolocation_input_invalid"),
            QStringLiteral("target_uncertainty_out_of_wire_range"),
            QStringLiteral("target_range_invalid"),
        };
        constexpr qint32 kCoordinateUnavailable = std::numeric_limits<qint32>::min();
        constexpr quint16 kUnavailable = std::numeric_limits<quint16>::max();
        const bool available = availableValue == 1;
        const bool latitudePresent = latitudeE7 != kCoordinateUnavailable;
        const bool longitudePresent = longitudeE7 != kCoordinateUnavailable;
        const bool coordinatesPresent = latitudePresent && longitudePresent;
        const bool coordinatesUnavailable = !latitudePresent && !longitudePresent;
        if (targetId == 0 || frameId == 0 || availableValue > 1 || reasonValue == 0 ||
            static_cast<int>(reasonValue) > reasonRegistry.size() || sentAtMs < frameAgeMs ||
            (!coordinatesPresent && !coordinatesUnavailable)) {
            return fail(error, QStringLiteral("target-geolocation-status content is invalid"));
        }
        if ((available &&
             (reasonValue != 1 || !coordinatesPresent || horizontalSigma == kUnavailable || latitudeE7 < -900000000 ||
              latitudeE7 > 900000000 || longitudeE7 < -1800000000 || longitudeE7 > 1800000000)) ||
            (!available && (reasonValue == 1 || coordinatesPresent || horizontalSigma != kUnavailable))) {
            return fail(error, QStringLiteral("target-geolocation-status availability is inconsistent"));
        }
        fields.insert(QStringLiteral("targetId"), hashedIdentifier(targetId));
        fields.insert(QStringLiteral("sourceFrameId"), hashedIdentifier(frameId));
        fields.insert(QStringLiteral("available"), available);
        fields.insert(QStringLiteral("reason"), reasonRegistry.at(reasonValue - 1));
        fields.insert(QStringLiteral("sourceAgeMs"), frameAgeMs);
        if (available) {
            fields.insert(QStringLiteral("latitudeDeg"), static_cast<double>(latitudeE7) / 10000000.0);
            fields.insert(QStringLiteral("longitudeDeg"), static_cast<double>(longitudeE7) / 10000000.0);
            fields.insert(QStringLiteral("horizontalSigmaM"), static_cast<double>(horizontalSigma) / 10.0);
        }
        fields.insert(QStringLiteral("advisoryOnly"), true);
        fields.insert(QStringLiteral("flightControlEnabled"), false);
        fields.insert(QStringLiteral("physicalReleaseEnabled"), false);
    } else if (type == MessageType::ReleaseStatus) {
        quint64 targetId = 0, rangeTargetId = 0, rangeFrameId = 0, calibrationId = 0;
        quint8 timingValue = 0, statusFlags = 0, consistency = 0;
        quint32 reasonMask = 0;
        qint32 targetNorth = 0, targetEast = 0, impactNorth = 0, impactEast = 0;
        qint32 alongError = 0, crossError = 0;
        quint16 ellipseMajor = 0, ellipseMinor = 0;
        qint16 ellipseOrientation = 0;
        quint16 groundRange = 0, rangeLow = 0, rangeHigh = 0;
        quint16 descentTime = 0, leadDistance = 0;
        if (!reader.readU64(&targetId) || !reader.readU64(&rangeTargetId) || !reader.readU64(&rangeFrameId) ||
            !reader.readU64(&calibrationId) || !reader.readU8(&timingValue) || !reader.readU8(&statusFlags) ||
            !reader.readU32(&reasonMask) || !reader.readI32(&targetNorth) || !reader.readI32(&targetEast) ||
            !reader.readI32(&impactNorth) || !reader.readI32(&impactEast) || !reader.readI32(&alongError) ||
            !reader.readI32(&crossError) || !reader.readU16(&ellipseMajor) || !reader.readU16(&ellipseMinor) ||
            !reader.readI16(&ellipseOrientation) || !reader.readU16(&groundRange) || !reader.readU16(&rangeLow) ||
            !reader.readU16(&rangeHigh) || !reader.readU16(&descentTime) || !reader.readU16(&leadDistance) ||
            !reader.readU8(&consistency) || !reader.atEnd()) {
            return fail(error, QStringLiteral("release-status body has an invalid size"));
        }

        static const QStringList reasonRegistry = {
            QStringLiteral("target_class_not_eligible"),
            QStringLiteral("multimodal_range_evidence_unavailable"),
            QStringLiteral("range_target_class_mismatch"),
            QStringLiteral("range_target_spatial_binding_failed"),
            QStringLiteral("multimodal_range_evidence_stale"),
            QStringLiteral("multimodal_range_not_valid"),
            QStringLiteral("multimodal_range_consistency_too_low"),
            QStringLiteral("multimodal_range_freshness_invalid"),
            QStringLiteral("multimodal_range_geometry_incomplete"),
            QStringLiteral("ballistic_telemetry_unavailable"),
            QStringLiteral("ballistic_telemetry_out_of_domain"),
            QStringLiteral("ballistic_telemetry_stale_or_from_future"),
            QStringLiteral("airspeed_groundspeed_wind_inconsistent"),
            QStringLiteral("ballistic_integration_failed"),
            QStringLiteral("impact_uncertainty_exceeds_limit"),
            QStringLiteral("target_outside_cross_track_corridor"),
            QStringLiteral("before_release_window"),
            QStringLiteral("release_window_passed"),
            QStringLiteral("multimodal_release_window_ready"),
            QStringLiteral("required_telemetry_unavailable"),
            QStringLiteral("required_telemetry_out_of_domain"),
            QStringLiteral("target_outside_calibrated_ground_projection"),
            QStringLiteral("release_window_ready"),
        };
        constexpr quint16 kUnavailable = std::numeric_limits<quint16>::max();
        constexpr qint16 kSignedUnavailable = std::numeric_limits<qint16>::min();
        constexpr qint32 kSigned32Unavailable = std::numeric_limits<qint32>::min();
        constexpr quint32 kReasonMask = (static_cast<quint32>(1) << 23) - 1;
        const bool bindingPresent = (statusFlags & 0x01U) != 0;
        const bool rangeIntervalAvailable = rangeLow != kUnavailable || rangeHigh != kUnavailable;
        QString timing;
        switch (timingValue) {
            case 1:
                timing = QStringLiteral("INVALID");
                break;
            case 2:
                timing = QStringLiteral("TOO_EARLY");
                break;
            case 3:
                timing = QStringLiteral("WINDOW");
                break;
            case 4:
                timing = QStringLiteral("TOO_LATE");
                break;
            default:
                break;
        }
        if (timing.isEmpty() || (statusFlags & ~0x01U) || reasonMask == 0 || (reasonMask & ~kReasonMask) ||
            targetId == 0 || calibrationId == 0 || bindingPresent != (rangeTargetId != 0 && rangeFrameId != 0) ||
            (!bindingPresent && (rangeTargetId != 0 || rangeFrameId != 0)) ||
            (rangeIntervalAvailable &&
             (rangeLow == kUnavailable || rangeHigh == kUnavailable || rangeLow > rangeHigh))) {
            return fail(error, QStringLiteral("release-status content is invalid"));
        }
        const bool completeGeometry =
            bindingPresent && targetNorth != kSigned32Unavailable && targetEast != kSigned32Unavailable &&
            impactNorth != kSigned32Unavailable && impactEast != kSigned32Unavailable &&
            alongError != kSigned32Unavailable && crossError != kSigned32Unavailable && ellipseMajor != kUnavailable &&
            ellipseMinor != kUnavailable && ellipseOrientation != kSignedUnavailable && groundRange != kUnavailable &&
            rangeLow != kUnavailable && rangeHigh != kUnavailable && descentTime != kUnavailable &&
            leadDistance != kUnavailable && consistency != 255;
        if (timing == QStringLiteral("WINDOW") && !completeGeometry) {
            return fail(error, QStringLiteral("release WINDOW lacks bound impact geometry"));
        }

        fields.insert(QStringLiteral("targetId"), hashedIdentifier(targetId));
        fields.insert(QStringLiteral("calibrationId"), hashedIdentifier(calibrationId));
        fields.insert(QStringLiteral("timingStatus"), timing);
        fields.insert(QStringLiteral("reasons"), decodeRegistryMask(reasonMask, reasonRegistry));
        fields.insert(QStringLiteral("rangeBindingPresent"), bindingPresent);
        fields.insert(QStringLiteral("impactAvailable"),
                      impactNorth != kSigned32Unavailable && impactEast != kSigned32Unavailable);
        fields.insert(QStringLiteral("ellipseAvailable"), ellipseMajor != kUnavailable &&
                                                              ellipseMinor != kUnavailable &&
                                                              ellipseOrientation != kSignedUnavailable);
        fields.insert(QStringLiteral("rangeIntervalAvailable"), rangeIntervalAvailable);
        fields.insert(QStringLiteral("rangeTargetId"), bindingPresent ? hashedIdentifier(rangeTargetId) : QString());
        fields.insert(QStringLiteral("rangeFrameId"), bindingPresent ? hashedIdentifier(rangeFrameId) : QString());
        fields.insert(QStringLiteral("targetNorthOffsetM"), decodeSignedDistance32(targetNorth));
        fields.insert(QStringLiteral("targetEastOffsetM"), decodeSignedDistance32(targetEast));
        fields.insert(QStringLiteral("impactNorthOffsetM"), decodeSignedDistance32(impactNorth));
        fields.insert(QStringLiteral("impactEastOffsetM"), decodeSignedDistance32(impactEast));
        fields.insert(QStringLiteral("alongTrackErrorM"), decodeSignedDistance32(alongError));
        fields.insert(QStringLiteral("crossTrackErrorM"), decodeSignedDistance32(crossError));
        fields.insert(QStringLiteral("errorEllipseMajorM"), decodeDistance(ellipseMajor));
        fields.insert(QStringLiteral("errorEllipseMinorM"), decodeDistance(ellipseMinor));
        fields.insert(QStringLiteral("errorEllipseOrientationDeg"), decodeBearing(ellipseOrientation));
        fields.insert(QStringLiteral("estimatedGroundRangeM"), decodeDistance(groundRange));
        fields.insert(QStringLiteral("groundRangeLowM"), decodeDistance(rangeLow));
        fields.insert(QStringLiteral("groundRangeHighM"), decodeDistance(rangeHigh));
        fields.insert(QStringLiteral("payloadDescentTimeS"), decodeDistance(descentTime));
        fields.insert(QStringLiteral("releaseLeadDistanceM"), decodeDistance(leadDistance));
        fields.insert(QStringLiteral("rangeSensorConsistency"), decodeRatio(consistency));
        fields.insert(QStringLiteral("advisoryOnly"), true);
        fields.insert(QStringLiteral("flightControlEnabled"), false);
        fields.insert(QStringLiteral("physicalReleaseEnabled"), false);
    } else if (type == MessageType::ApproachChallenge) {
        quint64 challengeToken = 0, targetToken = 0, issuedAtMs = 0, expiresAtMs = 0;
        quint32 targetRevision = 0;
        QByteArray selectionBytes;
        quint8 pending = 0;
        if (!reader.readU64(&challengeToken) || !reader.readU64(&targetToken) || !reader.readU32(&targetRevision) ||
            !reader.readBytes(16, &selectionBytes) || !reader.readU64(&issuedAtMs) || !reader.readU64(&expiresAtMs) ||
            !reader.readU8(&pending) || !reader.atEnd() || challengeToken == 0 || targetToken == 0 || pending != 1 ||
            expiresAtMs <= issuedAtMs) {
            return fail(error, QStringLiteral("approach challenge is invalid"));
        }
        const QString selectionId = uuidString(selectionBytes);
        if (QUuid(selectionId).isNull()) {
            return fail(error, QStringLiteral("approach challenge selection binding is invalid"));
        }
        fields.insert(QStringLiteral("challengeToken"), QString::number(challengeToken));
        fields.insert(QStringLiteral("targetToken"), QString::number(targetToken));
        fields.insert(QStringLiteral("targetRevision"), targetRevision);
        fields.insert(QStringLiteral("selectionCommandId"), selectionId);
        fields.insert(QStringLiteral("issuedAtMs"), QVariant::fromValue(issuedAtMs));
        fields.insert(QStringLiteral("expiresAtMs"), QVariant::fromValue(expiresAtMs));
        fields.insert(QStringLiteral("pending"), true);
        fields.insert(QStringLiteral("metadataOnly"), true);
        fields.insert(QStringLiteral("directPixhawkWrite"), false);
    } else if (type == MessageType::ApproachConfirmation) {
        quint64 commandToken = 0, sessionToken = 0, challengeToken = 0, targetToken = 0;
        quint32 targetRevision = 0;
        QByteArray selectionBytes;
        quint16 ttlMs = 0, durationMs = 0;
        quint8 completion = 0, continuous = 0;
        if (!reader.readU64(&commandToken) || !reader.readU64(&sessionToken) || !reader.readU64(&challengeToken) ||
            !reader.readU64(&targetToken) || !reader.readU32(&targetRevision) ||
            !reader.readBytes(16, &selectionBytes) || !reader.readU16(&ttlMs) || !reader.readU16(&durationMs) ||
            !reader.readU8(&completion) || !reader.readU8(&continuous) || !reader.atEnd() || commandToken == 0 ||
            sessionToken == 0 || challengeToken == 0 || targetToken == 0 || ttlMs == 0 || durationMs == 0 ||
            completion == 255 || continuous > 1) {
            return fail(error, QStringLiteral("approach confirmation is invalid"));
        }
        const QString selectionId = uuidString(selectionBytes);
        if (QUuid(selectionId).isNull()) {
            return fail(error, QStringLiteral("approach confirmation selection binding is invalid"));
        }
        fields.insert(QStringLiteral("commandToken"), QString::number(commandToken));
        fields.insert(QStringLiteral("sessionToken"), QString::number(sessionToken));
        fields.insert(QStringLiteral("challengeToken"), QString::number(challengeToken));
        fields.insert(QStringLiteral("targetToken"), QString::number(targetToken));
        fields.insert(QStringLiteral("targetRevision"), targetRevision);
        fields.insert(QStringLiteral("selectionCommandId"), selectionId);
        fields.insert(QStringLiteral("ttlMs"), ttlMs);
        fields.insert(QStringLiteral("slideDurationMs"), durationMs);
        fields.insert(QStringLiteral("completionFraction"), decodeRatio(completion));
        fields.insert(QStringLiteral("continuous"), continuous == 1);
        fields.insert(QStringLiteral("metadataOnly"), true);
        fields.insert(QStringLiteral("directPixhawkWrite"), false);
    } else if (type == MessageType::ApproachAck) {
        quint64 commandToken = 0;
        quint8 accepted = 0, reason = 0;
        quint32 acknowledgedSequence = 0;
        if (!reader.readU64(&commandToken) || !reader.readU8(&accepted) || !reader.readU8(&reason) ||
            !reader.readU32(&acknowledgedSequence) || !reader.atEnd() || commandToken == 0 || accepted > 1 ||
            (accepted == 1 && reason != 0) || (accepted == 0 && reason == 0)) {
            return fail(error, QStringLiteral("approach acknowledgement is invalid"));
        }
        fields.insert(QStringLiteral("commandToken"), QString::number(commandToken));
        fields.insert(QStringLiteral("accepted"), accepted == 1);
        fields.insert(QStringLiteral("reason"), reason);
        fields.insert(QStringLiteral("acknowledgedSequence"), acknowledgedSequence);
    } else if (type == MessageType::ApproachStatus) {
        quint64 targetId = 0;
        quint32 targetRevision = 0, reasonMask = 0;
        quint8 phaseValue = 0, statusFlags = 0;
        qint16 yawError = 0, pitchError = 0, yawAdvice = 0, pitchAdvice = 0;
        qint16 bankAdvice = 0, climbAdvice = 0;
        quint16 groundRange = 0, confirmationTtl = 0;
        if (!reader.readU64(&targetId) || !reader.readU32(&targetRevision) || !reader.readU8(&phaseValue) ||
            !reader.readU32(&reasonMask) || !reader.readI16(&yawError) || !reader.readI16(&pitchError) ||
            !reader.readI16(&yawAdvice) || !reader.readI16(&pitchAdvice) || !reader.readI16(&bankAdvice) ||
            !reader.readI16(&climbAdvice) || !reader.readU16(&groundRange) || !reader.readU16(&confirmationTtl) ||
            !reader.readU8(&statusFlags) || !reader.atEnd()) {
            return fail(error, QStringLiteral("approach status body has an invalid size"));
        }
        static const QStringList phases = {
            QStringLiteral("SEARCH"),         QStringLiteral("TARGET_LOCKED"), QStringLiteral("SLIDE_CONFIRM_REQUIRED"),
            QStringLiteral("CORRIDOR_VALID"), QStringLiteral("CENTERING"),     QStringLiteral("AIMING"),
            QStringLiteral("COMPLETE"),       QStringLiteral("ABORT"),
        };
        static const QStringList reasonRegistry = {
            QStringLiteral("no_target_selected"),
            QStringLiteral("abort_latched_until_reselection"),
            QStringLiteral("target_binding_changed"),
            QStringLiteral("target_occluded"),
            QStringLiteral("target_reacquiring"),
            QStringLiteral("target_recovered"),
            QStringLiteral("target_lost"),
            QStringLiteral("target_not_stably_tracking"),
            QStringLiteral("target_evidence_stale"),
            QStringLiteral("slide_confirmation_required"),
            QStringLiteral("slide_confirmation_expired"),
            QStringLiteral("avoidance_unavailable"),
            QStringLiteral("avoidance_stale"),
            QStringLiteral("avoidance_avoid"),
            QStringLiteral("avoidance_invalid"),
            QStringLiteral("range_unavailable"),
            QStringLiteral("range_target_or_frame_mismatch"),
            QStringLiteral("range_invalid"),
            QStringLiteral("range_freshness_or_consistency_invalid"),
            QStringLiteral("range_outside_approach_domain"),
            QStringLiteral("navigation_or_link_unhealthy"),
            QStringLiteral("required_telemetry_unavailable"),
            QStringLiteral("required_telemetry_stale_or_from_future"),
            QStringLiteral("altitude_outside_approach_domain"),
            QStringLiteral("airspeed_below_approach_minimum"),
            QStringLiteral("roll_outside_approach_domain"),
            QStringLiteral("pitch_outside_approach_domain"),
            QStringLiteral("target_outside_approach_corridor"),
            QStringLiteral("approach_completion_gate_reached"),
            QStringLiteral("approach_corridor_centered"),
            QStringLiteral("centering_advice_only"),
            QStringLiteral("fixed_wing_aim_active"),
        };
        constexpr quint32 kReasonMask = 0xFFFFFFFFU;
        const bool targetPresent = (statusFlags & 0x01U) != 0;
        const bool flightControlEnabled = (statusFlags & 0x02U) != 0;
        const bool aimControlActive = (statusFlags & 0x04U) != 0;
        const bool pilotInputCancelled = (statusFlags & 0x08U) != 0;
        if ((statusFlags & ~0x0FU) || phaseValue < 1 || phaseValue > phases.size() || reasonMask == 0 ||
            (reasonMask & ~kReasonMask) || targetPresent != (targetId != 0) ||
            (aimControlActive && !flightControlEnabled) ||
            (pilotInputCancelled && (!flightControlEnabled || aimControlActive))) {
            return fail(error, QStringLiteral("approach status content is invalid"));
        }
        fields.insert(QStringLiteral("targetPresent"), targetPresent);
        fields.insert(QStringLiteral("targetId"), targetPresent ? hashedIdentifier(targetId) : QString());
        fields.insert(QStringLiteral("targetRevision"), targetPresent ? targetRevision : 0);
        fields.insert(QStringLiteral("phase"), phases.at(phaseValue - 1));
        fields.insert(QStringLiteral("reasons"), decodeRegistryMask(reasonMask, reasonRegistry));
        fields.insert(QStringLiteral("yawErrorDeg"), decodeBearing(yawError));
        fields.insert(QStringLiteral("pitchErrorDeg"), decodeBearing(pitchError));
        fields.insert(QStringLiteral("yawAdviceDeg"), decodeBearing(yawAdvice));
        fields.insert(QStringLiteral("pitchAdviceDeg"), decodeBearing(pitchAdvice));
        fields.insert(QStringLiteral("bankAdviceDeg"), decodeBearing(bankAdvice));
        fields.insert(QStringLiteral("climbPitchAdviceDeg"), decodeBearing(climbAdvice));
        fields.insert(QStringLiteral("groundRangeM"), decodeDistance(groundRange));
        fields.insert(QStringLiteral("confirmationExpiresInS"), decodeDistance(confirmationTtl));
        fields.insert(QStringLiteral("advisoryOnly"), !flightControlEnabled);
        fields.insert(QStringLiteral("sitlHilOnly"), !flightControlEnabled);
        fields.insert(QStringLiteral("flightControlEnabled"), flightControlEnabled);
        fields.insert(QStringLiteral("aimControlActive"), aimControlActive);
        fields.insert(QStringLiteral("pilotInputCancelled"), pilotInputCancelled);
        fields.insert(QStringLiteral("physicalReleaseEnabled"), false);
    } else if (type == MessageType::PayloadTargetChallenge) {
        quint64 challengeToken = 0, selectedTargetToken = 0, aimpointTargetToken = 0;
        quint64 issuedAtMs = 0, expiresAtMs = 0;
        quint32 selectedTargetRevision = 0, aimpointTargetRevision = 0;
        QByteArray selectionBytes;
        quint8 pending = 0;
        if (!reader.readU64(&challengeToken) || !reader.readU64(&selectedTargetToken) ||
            !reader.readU32(&selectedTargetRevision) || !reader.readU64(&aimpointTargetToken) ||
            !reader.readU32(&aimpointTargetRevision) || !reader.readBytes(16, &selectionBytes) ||
            !reader.readU64(&issuedAtMs) || !reader.readU64(&expiresAtMs) || !reader.readU8(&pending) ||
            !reader.atEnd() || challengeToken == 0 || selectedTargetToken == 0 || aimpointTargetToken == 0 ||
            pending != 1 || expiresAtMs <= issuedAtMs) {
            return fail(error, QStringLiteral("payload target challenge is invalid"));
        }
        const QString selectionId = uuidString(selectionBytes);
        if (QUuid(selectionId).isNull()) {
            return fail(error, QStringLiteral("payload target challenge selection binding is invalid"));
        }
        fields.insert(QStringLiteral("challengeToken"), QString::number(challengeToken));
        fields.insert(QStringLiteral("selectedTargetToken"), QString::number(selectedTargetToken));
        fields.insert(QStringLiteral("selectedTargetRevision"), selectedTargetRevision);
        fields.insert(QStringLiteral("aimpointTargetToken"), QString::number(aimpointTargetToken));
        fields.insert(QStringLiteral("aimpointTargetRevision"), aimpointTargetRevision);
        fields.insert(QStringLiteral("selectionCommandId"), selectionId);
        fields.insert(QStringLiteral("issuedAtMs"), QVariant::fromValue(issuedAtMs));
        fields.insert(QStringLiteral("expiresAtMs"), QVariant::fromValue(expiresAtMs));
        fields.insert(QStringLiteral("pending"), true);
        fields.insert(QStringLiteral("hilOnly"), true);
        fields.insert(QStringLiteral("flightControlEnabled"), false);
        fields.insert(QStringLiteral("physicalReleaseEnabled"), false);
    } else if (type == MessageType::PayloadTargetConfirmation) {
        quint64 commandToken = 0, sessionToken = 0, challengeToken = 0;
        quint64 selectedTargetToken = 0, aimpointTargetToken = 0;
        quint32 selectedTargetRevision = 0, aimpointTargetRevision = 0;
        QByteArray selectionBytes;
        quint16 ttlMs = 0, durationMs = 0;
        quint8 completion = 0, continuous = 0;
        if (!reader.readU64(&commandToken) || !reader.readU64(&sessionToken) || !reader.readU64(&challengeToken) ||
            !reader.readU64(&selectedTargetToken) || !reader.readU32(&selectedTargetRevision) ||
            !reader.readU64(&aimpointTargetToken) || !reader.readU32(&aimpointTargetRevision) ||
            !reader.readBytes(16, &selectionBytes) || !reader.readU16(&ttlMs) || !reader.readU16(&durationMs) ||
            !reader.readU8(&completion) || !reader.readU8(&continuous) || !reader.atEnd() || commandToken == 0 ||
            sessionToken == 0 || challengeToken == 0 || selectedTargetToken == 0 || aimpointTargetToken == 0 ||
            ttlMs == 0 || durationMs == 0 || completion == 255 || continuous > 1) {
            return fail(error, QStringLiteral("payload target confirmation is invalid"));
        }
        const QString selectionId = uuidString(selectionBytes);
        if (QUuid(selectionId).isNull()) {
            return fail(error, QStringLiteral("payload target confirmation selection binding is invalid"));
        }
        fields.insert(QStringLiteral("commandToken"), QString::number(commandToken));
        fields.insert(QStringLiteral("sessionToken"), QString::number(sessionToken));
        fields.insert(QStringLiteral("challengeToken"), QString::number(challengeToken));
        fields.insert(QStringLiteral("selectedTargetToken"), QString::number(selectedTargetToken));
        fields.insert(QStringLiteral("selectedTargetRevision"), selectedTargetRevision);
        fields.insert(QStringLiteral("aimpointTargetToken"), QString::number(aimpointTargetToken));
        fields.insert(QStringLiteral("aimpointTargetRevision"), aimpointTargetRevision);
        fields.insert(QStringLiteral("selectionCommandId"), selectionId);
        fields.insert(QStringLiteral("ttlMs"), ttlMs);
        fields.insert(QStringLiteral("slideDurationMs"), durationMs);
        fields.insert(QStringLiteral("completionFraction"), decodeRatio(completion));
        fields.insert(QStringLiteral("continuous"), continuous == 1);
        fields.insert(QStringLiteral("hilOnly"), true);
        fields.insert(QStringLiteral("flightControlEnabled"), false);
        fields.insert(QStringLiteral("physicalReleaseEnabled"), false);
    } else if (type == MessageType::PayloadTargetAck) {
        quint64 commandToken = 0;
        quint8 accepted = 0, reason = 0;
        quint32 acknowledgedSequence = 0;
        if (!reader.readU64(&commandToken) || !reader.readU8(&accepted) || !reader.readU8(&reason) ||
            !reader.readU32(&acknowledgedSequence) || !reader.atEnd() || commandToken == 0 || accepted > 1 ||
            (accepted == 1 && reason != 0) || (accepted == 0 && reason == 0)) {
            return fail(error, QStringLiteral("payload target acknowledgement is invalid"));
        }
        fields.insert(QStringLiteral("commandToken"), QString::number(commandToken));
        fields.insert(QStringLiteral("accepted"), accepted == 1);
        fields.insert(QStringLiteral("reason"), reason);
        fields.insert(QStringLiteral("acknowledgedSequence"), acknowledgedSequence);
    } else if (type == MessageType::PayloadTargetStatus) {
        QByteArray selectionBytes;
        quint64 selectedTargetToken = 0, aimpointTargetToken = 0;
        quint32 selectedTargetRevision = 0, aimpointTargetRevision = 0;
        quint8 eligibilityValue = 0, flags = 0;
        quint16 confirmationTtl = 0;
        if (!reader.readBytes(16, &selectionBytes) || !reader.readU64(&selectedTargetToken) ||
            !reader.readU32(&selectedTargetRevision) || !reader.readU64(&aimpointTargetToken) ||
            !reader.readU32(&aimpointTargetRevision) || !reader.readU8(&eligibilityValue) ||
            !reader.readU16(&confirmationTtl) || !reader.readU8(&flags) || !reader.atEnd() ||
            selectedTargetToken == 0 || eligibilityValue < 1 || eligibilityValue > 6 || (flags & ~0x07U)) {
            return fail(error, QStringLiteral("payload target status is invalid"));
        }
        const QString selectionId = uuidString(selectionBytes);
        const bool aimpointPresent = (flags & 0x01U) != 0;
        const bool confirmationPending = (flags & 0x02U) != 0;
        const bool confirmationAccepted = (flags & 0x04U) != 0;
        const bool eligible = eligibilityValue == 1 || eligibilityValue == 2;
        if (QUuid(selectionId).isNull() || aimpointPresent != (aimpointTargetToken != 0) ||
            eligible != aimpointPresent || (confirmationPending && confirmationAccepted) ||
            ((confirmationPending || confirmationAccepted) != (confirmationTtl != 0xFFFFU)) ||
            ((confirmationPending || confirmationAccepted) && !eligible)) {
            return fail(error, QStringLiteral("payload target status binding is inconsistent"));
        }
        static const QStringList eligibility = {
            QStringLiteral("ELIGIBLE_FIRE"),
            QStringLiteral("ELIGIBLE_BURNING_CONTEXT"),
            QStringLiteral("TARGET_NOT_PAYLOAD_ELIGIBLE"),
            QStringLiteral("TARGET_NOT_STABLY_TRACKED"),
            QStringLiteral("FIRE_EVIDENCE_UNAVAILABLE"),
            QStringLiteral("FIRE_ASSOCIATION_AMBIGUOUS"),
        };
        fields.insert(QStringLiteral("selectionCommandId"), selectionId);
        fields.insert(QStringLiteral("selectedTargetToken"), QString::number(selectedTargetToken));
        fields.insert(QStringLiteral("selectedTargetRevision"), selectedTargetRevision);
        fields.insert(QStringLiteral("eligibility"), eligibility.at(eligibilityValue - 1));
        fields.insert(QStringLiteral("aimpointPresent"), aimpointPresent);
        fields.insert(QStringLiteral("aimpointTargetToken"),
                      aimpointPresent ? QString::number(aimpointTargetToken) : QString());
        fields.insert(QStringLiteral("aimpointTargetRevision"), aimpointPresent ? aimpointTargetRevision : 0);
        fields.insert(QStringLiteral("confirmationPending"), confirmationPending);
        fields.insert(QStringLiteral("confirmationAccepted"), confirmationAccepted);
        fields.insert(QStringLiteral("confirmationExpiresInS"), decodeDistance(confirmationTtl));
        fields.insert(QStringLiteral("advisoryOnly"), true);
        fields.insert(QStringLiteral("hilOnly"), true);
        fields.insert(QStringLiteral("flightControlEnabled"), false);
        fields.insert(QStringLiteral("physicalReleaseEnabled"), false);
    } else if (type == MessageType::TargetPoolStatus) {
        quint32 poolRevision = 0;
        quint8 pageIndex = 0, pageCount = 0, totalCount = 0, entryCount = 0;
        if (!reader.readU32(&poolRevision) || !reader.readU8(&pageIndex) || !reader.readU8(&pageCount) ||
            !reader.readU8(&totalCount) || !reader.readU8(&entryCount) || pageCount == 0 || pageIndex >= pageCount ||
            entryCount > 2) {
            return fail(error, QStringLiteral("target-pool status header is invalid"));
        }
        if ((totalCount == 0 && (pageCount != 1 || pageIndex != 0 || entryCount != 0)) ||
            (totalCount != 0 && (pageCount != static_cast<quint8>((totalCount + 1) / 2) ||
                                 entryCount != std::min<quint8>(2, totalCount - pageIndex * 2)))) {
            return fail(error, QStringLiteral("target-pool page coordinates are inconsistent"));
        }

        QVariantList entries;
        QSet<quint64> targetIds;
        for (quint8 index = 0; index < entryCount; ++index) {
            quint64 targetId = 0;
            quint8 stateValue = 0, flags = 0, confidence = 0, quality = 0;
            quint16 x1 = 0, y1 = 0, x2 = 0, y2 = 0;
            qint16 relativeBearing = 0;
            quint16 estimatedRange = 0, targetSpeed = 0;
            QByteArray labelBytes;
            if (!reader.readU64(&targetId) || !reader.readU8(&stateValue) || !reader.readU8(&flags) ||
                !reader.readBytes(16, &labelBytes) || !reader.readU8(&confidence) || !reader.readU8(&quality) ||
                !reader.readU16(&x1) || !reader.readU16(&y1) || !reader.readU16(&x2) || !reader.readU16(&y2) ||
                !reader.readI16(&relativeBearing) || !reader.readU16(&estimatedRange) ||
                !reader.readU16(&targetSpeed) || targetId == 0 || stateValue < 1 || stateValue > 7 ||
                (flags & ~0x3FU) || ((flags & 0x02U) && !(flags & 0x01U)) || targetIds.contains(targetId)) {
                return fail(error, QStringLiteral("target-pool entry is invalid"));
            }
            const bool bboxValid = (flags & 0x10U) != 0;
            if ((bboxValid && (x2 <= x1 || y2 <= y1)) || (!bboxValid && (x1 != 0 || y1 != 0 || x2 != 0 || y2 != 0))) {
                return fail(error, QStringLiteral("target-pool entry bounding box is invalid"));
            }
            targetIds.insert(targetId);
            const int terminator = labelBytes.indexOf('\0');
            const int labelLength = terminator < 0 ? labelBytes.size() : terminator;
            if (terminator >= 0 && std::any_of(labelBytes.cbegin() + terminator, labelBytes.cend(),
                                               [](char value) { return value != '\0'; })) {
                return fail(error, QStringLiteral("target-pool label padding is invalid"));
            }
            const QByteArray encodedLabel = labelBytes.first(labelLength);
            const QString label = QString::fromUtf8(encodedLabel);
            if (label.trimmed().isEmpty() || label.toUtf8() != encodedLabel) {
                return fail(error, QStringLiteral("target-pool label is invalid UTF-8"));
            }
            QVariantMap entry;
            entry.insert(QStringLiteral("targetId"), hashedIdentifier(targetId));
            entry.insert(QStringLiteral("state"), unifiedTrackState(stateValue));
            entry.insert(QStringLiteral("label"), label);
            entry.insert(QStringLiteral("confidence"), decodeRatio(confidence));
            entry.insert(QStringLiteral("trackingQuality"), decodeRatio(quality));
            entry.insert(QStringLiteral("locked"), (flags & 0x01U) != 0);
            entry.insert(QStringLiteral("primary"), (flags & 0x02U) != 0);
            entry.insert(QStringLiteral("actionable"), (flags & 0x04U) != 0);
            entry.insert(QStringLiteral("reidConfirmed"), (flags & 0x08U) != 0);
            entry.insert(QStringLiteral("operatorTracked"), (flags & 0x20U) != 0);
            entry.insert(QStringLiteral("bboxValid"), bboxValid);
            entry.insert(QStringLiteral("x1"), bboxValid ? static_cast<double>(x1) / 65535.0 : 0.0);
            entry.insert(QStringLiteral("y1"), bboxValid ? static_cast<double>(y1) / 65535.0 : 0.0);
            entry.insert(QStringLiteral("x2"), bboxValid ? static_cast<double>(x2) / 65535.0 : 0.0);
            entry.insert(QStringLiteral("y2"), bboxValid ? static_cast<double>(y2) / 65535.0 : 0.0);
            entry.insert(QStringLiteral("relativeBearingDeg"), decodeBearing(relativeBearing));
            entry.insert(QStringLiteral("estimatedRangeM"), decodeDistance(estimatedRange));
            entry.insert(QStringLiteral("targetSpeedMps"), decodeDistance(targetSpeed));
            entries.append(entry);
        }
        if (!reader.atEnd()) {
            return fail(error, QStringLiteral("target-pool status body has an invalid size"));
        }
        fields.insert(QStringLiteral("poolRevision"), poolRevision);
        fields.insert(QStringLiteral("pageIndex"), pageIndex);
        fields.insert(QStringLiteral("pageCount"), pageCount);
        fields.insert(QStringLiteral("totalTrackCount"), totalCount);
        fields.insert(QStringLiteral("entries"), entries);
        fields.insert(QStringLiteral("advisoryOnly"), true);
        fields.insert(QStringLiteral("flightControlEnabled"), false);
        fields.insert(QStringLiteral("physicalReleaseEnabled"), false);
    } else if (type == MessageType::SceneContextStatus) {
        quint32 contextRevision = 0;
        quint64 sourceFrameId = 0, sourceCapturedAtMs = 0;
        quint8 stateValue = 0, pageIndex = 0, pageCount = 0, totalCount = 0;
        if (!reader.readU32(&contextRevision) || !reader.readU64(&sourceFrameId) ||
            !reader.readU64(&sourceCapturedAtMs) || !reader.readU8(&stateValue) || !reader.readU8(&pageIndex) ||
            !reader.readU8(&pageCount) || !reader.readU8(&totalCount) || sourceFrameId == 0 || stateValue < 1 ||
            stateValue > 3 || pageCount == 0 || pageIndex >= pageCount) {
            return fail(error, QStringLiteral("scene-context status header is invalid"));
        }
        const int entryBytes = reader.remaining();
        constexpr int kSceneContextEntryBytes = 13;
        if (entryBytes % kSceneContextEntryBytes != 0) {
            return fail(error, QStringLiteral("scene-context entry bytes are malformed"));
        }
        const int entryCount = entryBytes / kSceneContextEntryBytes;
        if (entryCount > 2 ||
            (stateValue != 1 && (pageIndex != 0 || pageCount != 1 || totalCount != 0 || entryCount != 0)) ||
            (stateValue == 1 && totalCount == 0 && (pageIndex != 0 || pageCount != 1 || entryCount != 0)) ||
            (stateValue == 1 && totalCount != 0 &&
             (pageCount != static_cast<quint8>((totalCount + 1) / 2) ||
              entryCount != std::min<int>(2, totalCount - pageIndex * 2)))) {
            return fail(error, QStringLiteral("scene-context page coordinates are inconsistent"));
        }

        QVariantList entries;
        for (int index = 0; index < entryCount; ++index) {
            quint8 labelCode = 0;
            quint16 x1 = 0, y1 = 0, x2 = 0, y2 = 0, frameArea = 0, bboxFill = 0;
            if (!reader.readU8(&labelCode) || !reader.readU16(&x1) || !reader.readU16(&y1) || !reader.readU16(&x2) ||
                !reader.readU16(&y2) || !reader.readU16(&frameArea) || !reader.readU16(&bboxFill) || labelCode < 1 ||
                labelCode > 2 || x2 <= x1 || y2 <= y1 || frameArea == 0 || bboxFill == 0) {
                return fail(error, QStringLiteral("scene-context entry is invalid"));
            }
            QVariantMap entry;
            entry.insert(QStringLiteral("label"), labelCode == 1 ? QStringLiteral("road") : QStringLiteral("building"));
            entry.insert(QStringLiteral("x1"), static_cast<double>(x1) / 65535.0);
            entry.insert(QStringLiteral("y1"), static_cast<double>(y1) / 65535.0);
            entry.insert(QStringLiteral("x2"), static_cast<double>(x2) / 65535.0);
            entry.insert(QStringLiteral("y2"), static_cast<double>(y2) / 65535.0);
            entry.insert(QStringLiteral("frameAreaFraction"), static_cast<double>(frameArea) / 65535.0);
            entry.insert(QStringLiteral("bboxFillFraction"), static_cast<double>(bboxFill) / 65535.0);
            entry.insert(QStringLiteral("categoricalMaskOnly"), true);
            entries.append(entry);
        }
        if (!reader.atEnd()) {
            return fail(error, QStringLiteral("scene-context status body has an invalid size"));
        }
        fields.insert(QStringLiteral("contextRevision"), contextRevision);
        fields.insert(QStringLiteral("sourceFrameId"), hashedIdentifier(sourceFrameId));
        fields.insert(QStringLiteral("sourceCapturedAtMs"), QVariant::fromValue(sourceCapturedAtMs));
        fields.insert(QStringLiteral("state"), sceneContextState(stateValue));
        fields.insert(QStringLiteral("pageIndex"), pageIndex);
        fields.insert(QStringLiteral("pageCount"), pageCount);
        fields.insert(QStringLiteral("totalRegionCount"), totalCount);
        fields.insert(QStringLiteral("entries"), entries);
        fields.insert(QStringLiteral("confidenceAvailable"), false);
        fields.insert(QStringLiteral("targetIdentityAuthority"), false);
        fields.insert(QStringLiteral("advisoryOnly"), true);
        fields.insert(QStringLiteral("flightControlEnabled"), false);
        fields.insert(QStringLiteral("physicalReleaseEnabled"), false);
    } else if (type == MessageType::AuthorizationChallenge) {
        quint64 challengeToken = 0, missionToken = 0, targetToken = 0, sceneToken = 0;
        quint64 rulesetToken = 0, slotToken = 0, createdAtMs = 0, expiresAtMs = 0;
        quint32 targetRevision = 0;
        quint8 pending = 0;
        if (!reader.readU64(&challengeToken) || !reader.readU64(&missionToken) || !reader.readU64(&targetToken) ||
            !reader.readU64(&sceneToken) || !reader.readU64(&rulesetToken) || !reader.readU64(&slotToken) ||
            !reader.readU32(&targetRevision) || !reader.readU64(&createdAtMs) || !reader.readU64(&expiresAtMs) ||
            !reader.readU8(&pending) || !reader.atEnd() || pending > 1 || challengeToken == 0 || missionToken == 0 ||
            targetToken == 0 || sceneToken == 0 || rulesetToken == 0 || slotToken == 0 || expiresAtMs <= createdAtMs) {
            return fail(error, QStringLiteral("authorization challenge is invalid"));
        }
        fields.insert(QStringLiteral("challengeToken"), QString::number(challengeToken));
        fields.insert(QStringLiteral("missionToken"), QString::number(missionToken));
        fields.insert(QStringLiteral("targetToken"), QString::number(targetToken));
        fields.insert(QStringLiteral("sceneToken"), QString::number(sceneToken));
        fields.insert(QStringLiteral("rulesetToken"), QString::number(rulesetToken));
        fields.insert(QStringLiteral("payloadSlotToken"), QString::number(slotToken));
        fields.insert(QStringLiteral("targetRevision"), targetRevision);
        fields.insert(QStringLiteral("createdAtMs"), QVariant::fromValue(createdAtMs));
        fields.insert(QStringLiteral("expiresAtMs"), QVariant::fromValue(expiresAtMs));
        fields.insert(QStringLiteral("pending"), pending == 1);
    } else if (type == MessageType::AuthorizationDecision) {
        quint64 values[9]{};
        quint32 targetRevision = 0;
        quint8 decision = 0;
        quint16 ttlMs = 0;
        for (int index = 0; index < 8; ++index) {
            if (!reader.readU64(&values[index])) {
                return fail(error, QStringLiteral("authorization decision is truncated"));
            }
        }
        if (!reader.readU32(&targetRevision) || !reader.readU8(&decision) || !reader.readU64(&values[8]) ||
            !reader.readU16(&ttlMs) || !reader.atEnd() || decision < 1 || decision > 2 || ttlMs == 0) {
            return fail(error, QStringLiteral("authorization decision is invalid"));
        }
        for (quint64 value : values) {
            if (value == 0) {
                return fail(error, QStringLiteral("authorization decision token is zero"));
            }
        }
        fields.insert(QStringLiteral("commandToken"), QString::number(values[0]));
        fields.insert(QStringLiteral("sessionToken"), QString::number(values[1]));
        fields.insert(QStringLiteral("challengeToken"), QString::number(values[2]));
        fields.insert(QStringLiteral("missionToken"), QString::number(values[3]));
        fields.insert(QStringLiteral("targetToken"), QString::number(values[4]));
        fields.insert(QStringLiteral("sceneToken"), QString::number(values[5]));
        fields.insert(QStringLiteral("rulesetToken"), QString::number(values[6]));
        fields.insert(QStringLiteral("payloadSlotToken"), QString::number(values[7]));
        fields.insert(QStringLiteral("targetRevision"), targetRevision);
        fields.insert(QStringLiteral("decision"), decision == 1 ? QStringLiteral("APPROVE") : QStringLiteral("DENY"));
        fields.insert(QStringLiteral("operatorToken"), QString::number(values[8]));
        fields.insert(QStringLiteral("ttlMs"), ttlMs);
    } else {
        quint64 commandToken = 0;
        quint8 accepted = 0, reason = 0;
        quint32 acknowledgedSequence = 0;
        if (!reader.readU64(&commandToken) || !reader.readU8(&accepted) || !reader.readU8(&reason) ||
            !reader.readU32(&acknowledgedSequence) || !reader.atEnd() || accepted > 1 || commandToken == 0) {
            return fail(error, QStringLiteral("authorization-ack body is invalid"));
        }
        fields.insert(QStringLiteral("commandToken"), QString::number(commandToken));
        fields.insert(QStringLiteral("accepted"), accepted == 1);
        fields.insert(QStringLiteral("reason"), reason);
        fields.insert(QStringLiteral("acknowledgedSequence"), acknowledgedSequence);
    }

    packet->type = type;
    packet->sequence = sequence;
    packet->sentAtMs = sentAtMs;
    packet->fields = std::move(fields);
    if (error) {
        error->clear();
    }
    return true;
}

QByteArray MultiDetectOperatorProtocol::encodeSelection(const QString& commandId, const QString& sessionId,
                                                        quint32 sequence, const QString& action, int width, int height,
                                                        int rotationDegrees, double x1, double y1, double x2, double y2,
                                                        quint64 issuedAtMs, quint16 ttlMs, QString* error) const
{
    if (!configured()) {
        fail(error, QStringLiteral("operator-link key or stream is not configured"));
        return {};
    }
    const QUuid commandUuid(commandId);
    const QUuid sessionUuid(sessionId);
    const QByteArray commandBytes = commandUuid.toRfc4122();
    const QByteArray sessionBytes = sessionUuid.toRfc4122();
    if (commandUuid.isNull() || sessionUuid.isNull() || commandBytes.size() != 16 || sessionBytes.size() != 16) {
        fail(error, QStringLiteral("selection command and session IDs must be UUIDs"));
        return {};
    }
    quint8 actionValue = 0;
    if (action.compare(QStringLiteral("SELECT"), Qt::CaseInsensitive) == 0) {
        actionValue = 1;
    } else if (action.compare(QStringLiteral("SWITCH"), Qt::CaseInsensitive) == 0) {
        actionValue = 2;
    } else if (action.compare(QStringLiteral("CANCEL"), Qt::CaseInsensitive) == 0) {
        actionValue = 3;
    } else if (action.compare(QStringLiteral("SELECT_TRK"), Qt::CaseInsensitive) == 0) {
        actionValue = 4;
    } else if (action.compare(QStringLiteral("PROMOTE_LCK"), Qt::CaseInsensitive) == 0) {
        actionValue = 5;
    } else if (action.compare(QStringLiteral("DEMOTE_TRK"), Qt::CaseInsensitive) == 0) {
        actionValue = 6;
    } else if (action.compare(QStringLiteral("CANCEL_TRK"), Qt::CaseInsensitive) == 0) {
        actionValue = 7;
    } else {
        fail(error, QStringLiteral("selection action is invalid"));
        return {};
    }
    const quint8 rotation = rotationCode(rotationDegrees);
    if (width <= 0 || width > 65535 || height <= 0 || height > 65535 || rotation == 255 || ttlMs == 0 || ttlMs > 5000) {
        fail(error, QStringLiteral("selection geometry or TTL is invalid"));
        return {};
    }
    if (actionValue != 3 && (!std::isfinite(x1) || !std::isfinite(y1) || !std::isfinite(x2) || !std::isfinite(y2) ||
                             x1 < 0.0 || y1 < 0.0 || x2 > 1.0 || y2 > 1.0 || x2 <= x1 || y2 <= y1)) {
        fail(error, QStringLiteral("selection bounding box is invalid"));
        return {};
    }

    QByteArray body;
    body.reserve(62);
    body.append(commandBytes);
    body.append(sessionBytes);
    appendU8(body, actionValue);
    appendU32(body, _streamHash);
    appendU16(body, static_cast<quint16>(width));
    appendU16(body, static_cast<quint16>(height));
    appendU8(body, rotation);
    appendU16(body, ttlMs);
    const bool bboxPresent = actionValue != 3;
    appendU8(body, bboxPresent ? 1 : 0);
    appendU16(body, bboxPresent ? encodeCoordinate(x1) : 0);
    appendU16(body, bboxPresent ? encodeCoordinate(y1) : 0);
    appendU16(body, bboxPresent ? encodeCoordinate(x2) : 0);
    appendU16(body, bboxPresent ? encodeCoordinate(y2) : 0);
    appendU8(body, 0);
    appendU64(body, 0);
    return _encodeFrame(MessageType::TargetSelection, sequence, issuedAtMs, body, error);
}

QByteArray MultiDetectOperatorProtocol::encodeAuthorizationDecision(
    quint64 commandToken, quint64 sessionToken, quint64 challengeToken, quint64 missionToken, quint64 targetToken,
    quint64 sceneToken, quint64 rulesetToken, quint64 payloadSlotToken, quint32 targetRevision, bool approve,
    quint64 operatorToken, quint32 sequence, quint64 issuedAtMs, quint16 ttlMs, QString* error) const
{
    const quint64 tokens[] = {
        commandToken, sessionToken, challengeToken,   missionToken,  targetToken,
        sceneToken,   rulesetToken, payloadSlotToken, operatorToken,
    };
    if (!configured() || ttlMs == 0 || ttlMs > 5000 ||
        std::any_of(std::begin(tokens), std::end(tokens), [](quint64 value) { return value == 0; })) {
        fail(error, QStringLiteral("authorization decision tokens, key, or TTL are invalid"));
        return {};
    }
    QByteArray body;
    body.reserve(79);
    for (int index = 0; index < 8; ++index) {
        appendU64(body, tokens[index]);
    }
    appendU32(body, targetRevision);
    appendU8(body, approve ? 1 : 2);
    appendU64(body, operatorToken);
    appendU16(body, ttlMs);
    return _encodeFrame(MessageType::AuthorizationDecision, sequence, issuedAtMs, body, error);
}

QByteArray MultiDetectOperatorProtocol::encodeApproachConfirmation(
    quint64 commandToken, quint64 sessionToken, quint64 challengeToken, quint64 targetToken, quint32 targetRevision,
    const QString& selectionCommandId, quint32 sequence, quint64 issuedAtMs, quint16 ttlMs, quint16 slideDurationMs,
    double completionFraction, bool continuous, QString* error) const
{
    const QUuid selectionUuid(selectionCommandId);
    const QByteArray selectionBytes = selectionUuid.toRfc4122();
    if (!configured() || commandToken == 0 || sessionToken == 0 || challengeToken == 0 || targetToken == 0 ||
        selectionUuid.isNull() || selectionBytes.size() != 16 || ttlMs == 0 || ttlMs > 5000 || slideDurationMs == 0 ||
        slideDurationMs > 10000 || !std::isfinite(completionFraction) || completionFraction < 0.0 ||
        completionFraction > 1.0) {
        fail(error, QStringLiteral("approach confirmation binding or slide evidence is invalid"));
        return {};
    }
    QByteArray body;
    body.reserve(58);
    appendU64(body, commandToken);
    appendU64(body, sessionToken);
    appendU64(body, challengeToken);
    appendU64(body, targetToken);
    appendU32(body, targetRevision);
    body.append(selectionBytes);
    appendU16(body, ttlMs);
    appendU16(body, slideDurationMs);
    appendU8(body, static_cast<quint8>(std::llround(completionFraction * 254.0)));
    appendU8(body, continuous ? 1 : 0);
    return _encodeFrame(MessageType::ApproachConfirmation, sequence, issuedAtMs, body, error);
}

QByteArray MultiDetectOperatorProtocol::encodePayloadTargetConfirmation(
    quint64 commandToken, quint64 sessionToken, quint64 challengeToken, quint64 selectedTargetToken,
    quint32 selectedTargetRevision, quint64 aimpointTargetToken, quint32 aimpointTargetRevision,
    const QString& selectionCommandId, quint32 sequence, quint64 issuedAtMs, quint16 ttlMs, quint16 slideDurationMs,
    double completionFraction, bool continuous, QString* error) const
{
    const QUuid selectionUuid(selectionCommandId);
    const QByteArray selectionBytes = selectionUuid.toRfc4122();
    if (!configured() || commandToken == 0 || sessionToken == 0 || challengeToken == 0 || selectedTargetToken == 0 ||
        aimpointTargetToken == 0 || selectionUuid.isNull() || selectionBytes.size() != 16 || ttlMs == 0 ||
        ttlMs > 5000 || slideDurationMs == 0 || slideDurationMs > 10000 || !std::isfinite(completionFraction) ||
        completionFraction < 0.0 || completionFraction > 1.0) {
        fail(error, QStringLiteral("payload target confirmation binding or slide evidence is invalid"));
        return {};
    }
    QByteArray body;
    body.reserve(70);
    appendU64(body, commandToken);
    appendU64(body, sessionToken);
    appendU64(body, challengeToken);
    appendU64(body, selectedTargetToken);
    appendU32(body, selectedTargetRevision);
    appendU64(body, aimpointTargetToken);
    appendU32(body, aimpointTargetRevision);
    body.append(selectionBytes);
    appendU16(body, ttlMs);
    appendU16(body, slideDurationMs);
    appendU8(body, static_cast<quint8>(std::llround(completionFraction * 254.0)));
    appendU8(body, continuous ? 1 : 0);
    return _encodeFrame(MessageType::PayloadTargetConfirmation, sequence, issuedAtMs, body, error);
}

quint32 MultiDetectOperatorProtocol::hash32(const QString& value)
{
    const QByteArray digest = QCryptographicHash::hash(value.toUtf8(), QCryptographicHash::Sha256);
    const QByteArray head = digest.first(4);
    Reader reader(head);
    quint32 result = 0;
    (void) reader.readU32(&result);
    return result;
}

quint64 MultiDetectOperatorProtocol::hash64(const QString& value)
{
    const QByteArray digest = QCryptographicHash::hash(value.toUtf8(), QCryptographicHash::Sha256);
    const QByteArray head = digest.first(8);
    Reader reader(head);
    quint64 result = 0;
    (void) reader.readU64(&result);
    return result;
}

QString MultiDetectOperatorProtocol::hashedIdentifier(quint64 value)
{
    return QStringLiteral("hash64:%1").arg(value, 16, 16, QLatin1Char('0'));
}

QByteArray MultiDetectOperatorProtocol::_encodeFrame(MessageType type, quint32 sequence, quint64 sentAtMs,
                                                     const QByteArray& body, QString* error) const
{
    if (!configured() || body.size() > 65535) {
        fail(error, QStringLiteral("operator-link frame is not encodable"));
        return {};
    }
    QByteArray frame;
    frame.reserve(kHeaderBytes + body.size() + kAuthenticationTagBytes);
    frame.append(kMagic, 2);
    appendU8(frame, kProtocolVersion);
    appendU8(frame, static_cast<quint8>(type));
    appendU8(frame, 0);
    appendU8(frame, 0);
    appendU32(frame, sequence);
    appendU64(frame, sentAtMs);
    appendU16(frame, static_cast<quint16>(body.size()));
    frame.append(body);
    frame.append(
        QMessageAuthenticationCode::hash(frame, _hmacKey, QCryptographicHash::Sha256).first(kAuthenticationTagBytes));
    if (frame.size() > kMaximumPayloadBytes) {
        fail(error, QStringLiteral("operator-link frame exceeds TUNNEL capacity"));
        return {};
    }
    if (error) {
        error->clear();
    }
    return frame;
}
