#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QString>
#include <QtCore/QVariantMap>

class MultiDetectOperatorProtocol final
{
public:
    static constexpr quint16 kTunnelPayloadType = 42000;
    static constexpr int kMaximumPayloadBytes = 128;
    // Target coordinates are derived from a camera frame and a current GPS
    // fix.  Keep this bound in lockstep with AdaptiveRangingConfig's 1.5 s
    // GPS freshness gate.  The QGC side must reject an otherwise valid,
    // authenticated packet once its source frame has aged past this limit;
    // accepting a fresh transport envelope around an old coordinate would
    // let stale map evidence live forever.
    static constexpr quint16 kMaximumTargetGeolocationSourceAgeMs = 1500;
    // RangeStatus carries the actual metric distance shown in the native
    // Fly View card.  VALID/DEGRADED packets must carry both source age and
    // data freshness, and both must remain inside the same bound used by the
    // Python operator-link encoder and QML hold timer.  Missing freshness is
    // fail-closed; a retransmitted packet cannot make old data look current.
    static constexpr quint16 kMaximumRangeStatusSourceAgeMs = 1500;
    // A source frame id is expected to advance with the camera stream.  This
    // bounded hold time catches a producer that repeatedly resets sourceAgeMs
    // while replaying the same old frame.  It is intentionally longer than
    // the source-age limit to tolerate one heartbeat/retransmit burst.
    static constexpr quint64 kMaximumTargetGeolocationFrameHoldMs = 3000;

    enum class MessageType : quint8
    {
        Invalid = 0,
        TargetSelection = 1,
        SelectionAck = 2,
        TrackStatus = 3,
        MissionStatus = 4,
        SafetyStatus = 5,
        AuthorizationChallenge = 6,
        AuthorizationDecision = 7,
        AuthorizationAck = 8,
        PatrolStatus = 9,
        RangeStatus = 10,
        ReleaseStatus = 11,
        ApproachChallenge = 12,
        ApproachConfirmation = 13,
        ApproachAck = 14,
        ApproachStatus = 15,
        TargetPoolStatus = 16,
        SceneContextStatus = 17,
        PayloadTargetChallenge = 18,
        PayloadTargetConfirmation = 19,
        PayloadTargetAck = 20,
        PayloadTargetStatus = 21,
        TargetGeolocationStatus = 22,
        RuntimeStatus = 23,
        RuntimeStatusAck = 24,
        GnssStatus = 25,
        TargetObservationStatus = 26,
    };

    struct DecodedPacket
    {
        MessageType type = MessageType::Invalid;
        quint32 sequence = 0;
        quint64 sentAtMs = 0;
        QVariantMap fields;
    };

    MultiDetectOperatorProtocol(QByteArray hmacKey, QString streamId);

    bool configured() const { return _hmacKey.size() >= 32 && !_streamId.trimmed().isEmpty(); }

    QString streamId() const { return _streamId; }

    bool decode(const QByteArray& payload, DecodedPacket* packet, QString* error) const;

    QByteArray encodeSelection(const QString& commandId, const QString& sessionId, quint32 sequence,
                               const QString& action, int width, int height, int rotationDegrees, double x1, double y1,
                               double x2, double y2, quint64 issuedAtMs, quint16 ttlMs, QString* error) const;

    QByteArray encodeAuthorizationDecision(quint64 commandToken, quint64 sessionToken, quint64 challengeToken,
                                           quint64 missionToken, quint64 targetToken, quint64 sceneToken,
                                           quint64 rulesetToken, quint64 payloadSlotToken, quint32 targetRevision,
                                           bool approve, quint64 operatorToken, quint32 sequence, quint64 issuedAtMs,
                                           quint16 ttlMs, QString* error) const;

    QByteArray encodeApproachConfirmation(quint64 commandToken, quint64 sessionToken, quint64 challengeToken,
                                          quint64 targetToken, quint32 targetRevision,
                                          const QString& selectionCommandId, quint32 sequence, quint64 issuedAtMs,
                                          quint16 ttlMs, quint16 slideDurationMs, double completionFraction,
                                          bool continuous, QString* error) const;

    QByteArray encodePayloadTargetConfirmation(quint64 commandToken, quint64 sessionToken, quint64 challengeToken,
                                               quint64 selectedTargetToken, quint32 selectedTargetRevision,
                                               quint64 aimpointTargetToken, quint32 aimpointTargetRevision,
                                               const QString& selectionCommandId, quint32 sequence, quint64 issuedAtMs,
                                               quint16 ttlMs, quint16 slideDurationMs, double completionFraction,
                                               bool continuous, QString* error) const;

    QByteArray encodeRuntimeStatusAck(quint32 acknowledgedRuntimeSequence, quint64 runHash64, quint32 sequence,
                                      quint64 sentAtMs, QString* error) const;

    static quint32 hash32(const QString& value);
    static quint64 hash64(const QString& value);
    static QString hashedIdentifier(quint64 value);

private:
    QByteArray _encodeFrame(MessageType type, quint32 sequence, quint64 sentAtMs, const QByteArray& body,
                            QString* error) const;

    QByteArray _hmacKey;
    QString _streamId;
    quint32 _streamHash = 0;
};
