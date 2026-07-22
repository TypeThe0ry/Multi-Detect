#pragma once

#include <QtCore/QHash>
#include <QtCore/QList>
#include <QtCore/QObject>
#include <QtCore/QTimer>
#include <QtCore/QVariantList>
#include <optional>

#include "LinkConfiguration.h"
#include "MultiDetectDepthGridProtocol.h"
#include "MultiDetectOperatorProtocol.h"
#include "QGCMAVLink.h"

class LinkInterface;
class MultiDetectDepthGridReceiver;

class MultiDetectOperatorController final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(bool configured READ configured CONSTANT)
    Q_PROPERTY(bool directPixhawkWritesEnabled READ directPixhawkWritesEnabled CONSTANT)
    Q_PROPERTY(bool physicalReleaseEnabled READ physicalReleaseEnabled CONSTANT)
    Q_PROPERTY(QString linkState READ linkState NOTIFY linkStateChanged)
    Q_PROPERTY(QString lastError READ lastError NOTIFY lastErrorChanged)
    Q_PROPERTY(QString streamId READ streamId CONSTANT)
    Q_PROPERTY(int streamWidth READ streamWidth CONSTANT)
    Q_PROPERTY(int streamHeight READ streamHeight CONSTANT)
    Q_PROPERTY(int streamRotation READ streamRotation CONSTANT)
    Q_PROPERTY(QString lastSelectionCommandId READ lastSelectionCommandId NOTIFY lastSelectionChanged)
    Q_PROPERTY(int pendingTrackSelectionCount READ pendingTrackSelectionCount NOTIFY pendingTrackSelectionCountChanged)
    Q_PROPERTY(qulonglong authenticatedPacketCount READ authenticatedPacketCount NOTIFY countersChanged)
    Q_PROPERTY(qulonglong rejectedPacketCount READ rejectedPacketCount NOTIFY countersChanged)
    Q_PROPERTY(QVariantMap patrolStatus READ patrolStatus NOTIFY patrolStatusChanged)
    Q_PROPERTY(QVariantMap rangeStatus READ rangeStatus NOTIFY rangeStatusChanged)
    Q_PROPERTY(QVariantMap targetGeolocationStatus READ targetGeolocationStatus NOTIFY targetGeolocationStatusChanged)
    Q_PROPERTY(QVariantMap releaseStatus READ releaseStatus NOTIFY releaseStatusChanged)
    Q_PROPERTY(QVariantMap approachChallenge READ approachChallenge NOTIFY approachChallengeChanged)
    Q_PROPERTY(QVariantMap approachStatus READ approachStatus NOTIFY approachStatusChanged)
    Q_PROPERTY(QVariantMap payloadTargetChallenge READ payloadTargetChallenge NOTIFY payloadTargetChallengeChanged)
    Q_PROPERTY(QVariantMap payloadTargetStatus READ payloadTargetStatus NOTIFY payloadTargetStatusChanged)
    Q_PROPERTY(QVariantList targetPool READ targetPool NOTIFY targetPoolChanged)
    Q_PROPERTY(QVariantList sceneContextRegions READ sceneContextRegions NOTIFY sceneContextChanged)
    Q_PROPERTY(QString sceneContextState READ sceneContextState NOTIFY sceneContextChanged)
    Q_PROPERTY(double trackingMetadataRateHz READ trackingMetadataRateHz NOTIFY trackingMetadataRateChanged)
    Q_PROPERTY(double targetPoolMetadataRateHz READ targetPoolMetadataRateHz NOTIFY targetPoolMetadataRateChanged)
    Q_PROPERTY(bool depthMapAvailable READ depthMapAvailable NOTIFY depthMapChanged)
    Q_PROPERTY(QString depthMapDataUrl READ depthMapDataUrl NOTIFY depthMapChanged)
    Q_PROPERTY(int depthGridWidth READ depthGridWidth NOTIFY depthMapChanged)
    Q_PROPERTY(int depthGridHeight READ depthGridHeight NOTIFY depthMapChanged)
    Q_PROPERTY(double depthMinimumM READ depthMinimumM NOTIFY depthMapChanged)
    Q_PROPERTY(double depthMaximumM READ depthMaximumM NOTIFY depthMapChanged)
    Q_PROPERTY(qint64 depthMapAgeMs READ depthMapAgeMs NOTIFY depthMapChanged)

public:
    explicit MultiDetectOperatorController(QObject* parent = nullptr);
    ~MultiDetectOperatorController() override;

    void shutdown();

    bool configured() const;

    bool directPixhawkWritesEnabled() const { return false; }

    bool physicalReleaseEnabled() const { return false; }

    QString linkState() const { return _linkState; }

    QString lastError() const { return _lastError; }

    QString streamId() const { return _streamId; }

    int streamWidth() const { return _streamWidth; }

    int streamHeight() const { return _streamHeight; }

    int streamRotation() const { return _streamRotation; }

    QString lastSelectionCommandId() const { return _lastSelectionCommandId; }

    int pendingTrackSelectionCount() const { return _pendingTrackSelections.size(); }

    qulonglong authenticatedPacketCount() const { return _authenticatedPackets; }

    qulonglong rejectedPacketCount() const { return _rejectedPackets; }

    QVariantMap patrolStatus() const { return _patrolStatus; }

    QVariantMap rangeStatus() const { return _rangeStatus; }

    QVariantMap targetGeolocationStatus() const { return _targetGeolocationStatus; }

    QVariantMap releaseStatus() const { return _releaseStatus; }

    QVariantMap approachChallenge() const { return _approachChallenge; }

    QVariantMap approachStatus() const { return _approachStatus; }

    QVariantMap payloadTargetChallenge() const { return _payloadTargetChallenge; }

    QVariantMap payloadTargetStatus() const { return _payloadTargetStatus; }

    QVariantList targetPool() const { return _targetPool; }

    QVariantList sceneContextRegions() const { return _sceneContextRegions; }

    QString sceneContextState() const { return _sceneContextState; }

    double trackingMetadataRateHz() const { return _trackingMetadataRateHz; }

    double targetPoolMetadataRateHz() const { return _targetPoolMetadataRateHz; }

    bool depthMapAvailable() const { return !_depthMapDataUrl.isEmpty(); }

    QString depthMapDataUrl() const { return _depthMapDataUrl; }

    int depthGridWidth() const { return _depthGridWidth; }

    int depthGridHeight() const { return _depthGridHeight; }

    double depthMinimumM() const { return _depthMinimumM; }

    double depthMaximumM() const { return _depthMaximumM; }

    qint64 depthMapAgeMs() const;

    Q_INVOKABLE bool sendTargetSelection(const QString& action, double x1 = 0.0, double y1 = 0.0, double x2 = 0.0,
                                         double y2 = 0.0);
    Q_INVOKABLE bool sendAuthorizationDecision(bool approve);
    Q_INVOKABLE bool sendApproachSlideConfirmation(int slideDurationMs, double completionFraction, bool continuous);
    Q_INVOKABLE bool sendPayloadTargetSlideConfirmation(int slideDurationMs, double completionFraction,
                                                        bool continuous);
    Q_INVOKABLE double depthAtNormalized(double x, double y) const;

signals:
    void linkStateChanged();
    void lastErrorChanged();
    void lastSelectionChanged();
    void pendingTrackSelectionCountChanged();
    void countersChanged();
    void patrolStatusChanged();
    void rangeStatusChanged();
    void targetGeolocationStatusChanged();
    void releaseStatusChanged();
    void approachChallengeChanged();
    void approachStatusChanged();
    void payloadTargetChallengeChanged();
    void payloadTargetStatusChanged();
    void targetPoolChanged();
    void sceneContextChanged();
    void trackingMetadataRateChanged();
    void targetPoolMetadataRateChanged();
    void depthMapChanged();
    void trackStatusReceived(const QVariantMap& status);
    void missionStatusReceived(const QVariantMap& status);
    void patrolStatusReceived(const QVariantMap& status);
    void rangeStatusReceived(const QVariantMap& status);
    void targetGeolocationStatusReceived(const QVariantMap& status);
    void releaseStatusReceived(const QVariantMap& status);
    void safetyStatusReceived(const QVariantMap& status);
    void authorizationChallengeReceived(const QVariantMap& challenge);
    void selectionAcknowledged(const QVariantMap& acknowledgement);
    void authorizationAcknowledged(const QVariantMap& acknowledgement);
    void approachAcknowledged(const QVariantMap& acknowledgement);
    void payloadTargetAcknowledged(const QVariantMap& acknowledgement);

private slots:
    void _mavlinkMessageReceived(LinkInterface* link, const mavlink_message_t& message);
    void _poll();
    void _startDirectOperatorLink();

private:
    struct ChallengeBinding
    {
        quint64 challengeToken = 0;
        quint64 missionToken = 0;
        quint64 targetToken = 0;
        quint64 sceneToken = 0;
        quint64 rulesetToken = 0;
        quint64 payloadSlotToken = 0;
        quint32 targetRevision = 0;
        quint64 expiresAtMs = 0;
        bool pending = false;
    };

    struct ApproachChallengeBinding
    {
        quint64 challengeToken = 0;
        quint64 targetToken = 0;
        quint32 targetRevision = 0;
        QString selectionCommandId;
        quint64 expiresAtMs = 0;
        bool pending = false;
    };

    struct PayloadTargetChallengeBinding
    {
        quint64 challengeToken = 0;
        quint64 selectedTargetToken = 0;
        quint32 selectedTargetRevision = 0;
        quint64 aimpointTargetToken = 0;
        quint32 aimpointTargetRevision = 0;
        QString selectionCommandId;
        quint64 expiresAtMs = 0;
        bool pending = false;
    };

    struct PendingDelivery
    {
        QByteArray payload;
        QString correlationId;
        quint64 commandToken = 0;
        quint32 sequence = 0;
        quint64 expiresAtMs = 0;
        quint64 nextAttemptAtMs = 0;
        int attempts = 0;
        bool authorizationDecision = false;
        bool approachConfirmation = false;
        bool payloadTargetConfirmation = false;
        bool approve = false;

        bool active() const { return !payload.isEmpty(); }

        void clear() { *this = {}; }
    };

    bool _sendPayload(const QByteArray& payload);
    bool _sendDirectHeartbeat();
    void _recordTargetPoolMetadataSnapshot(quint64 receivedAtMs);
    void _resetTargetPoolMetadataRate();
    void _adoptDepthGridFrame(const MultiDetectDepthGridFrame& frame, quint64 receivedAtMs);
    void _renderPendingDepthGrid();
    void _clearDepthGrid();
    bool _hasPendingTargetSelection() const;
    void _clearPendingTrackSelections();
    bool _sendPending(PendingDelivery* delivery);
    void _reject(const QString& reason);
    void _setLastError(const QString& error);
    void _setLinkState(const QString& state);
    bool _isNewStatus(MultiDetectOperatorProtocol::MessageType type, quint32 sequence, quint64 receivedAtMs);
    quint64 _operatorWireNowMs(quint64 localNowMs) const;
    void _observeRemoteWireTime(quint64 remoteSentAtMs, quint64 localReceivedAtMs);
    static quint64 _unsignedValue(const QVariantMap& fields, const QString& name, bool* ok);

    QByteArray _hmacKey;
    QString _streamId;
    int _streamWidth = 1280;
    int _streamHeight = 720;
    int _streamRotation = 0;
    int _localSystemId = 255;
    int _localComponentId = MAV_COMP_ID_MISSIONPLANNER;
    int _remoteSystemId = 1;
    int _remoteComponentId = 191;
    QString _operatorId;
    QString _operatorUdpHost;
    int _operatorUdpPort = 14580;
    int _operatorUdpLocalPort = 14581;
    int _depthGridUdpPort = 14582;
    int _depthGridJetsonPort = 14583;
    QByteArray _mavlinkSigningKey;
    bool _directOperatorRequested = false;

    MultiDetectOperatorProtocol _protocol;
    QString _sessionId;
    quint32 _selectionSequence = 0;
    quint32 _authorizationSequence = 0;
    quint32 _approachSequence = 0;
    quint32 _payloadTargetSequence = 0;
    QString _lastSelectionCommandId;
    QString _linkState;
    QString _lastError;
    quint64 _authenticatedPackets = 0;
    quint64 _rejectedPackets = 0;
    quint64 _lastAuthenticatedAtMs = 0;
    quint64 _remoteWireTimestampMs = 0;
    quint64 _remoteWireObservedAtLocalMs = 0;
    bool _remoteWireClockValid = false;
    quint64 _lastTrackingStatusAtMs = 0;
    quint64 _lastPatrolStatusAtMs = 0;
    quint64 _lastRangeStatusAtMs = 0;
    quint64 _lastTargetGeolocationStatusAtMs = 0;
    quint64 _lastReleaseStatusAtMs = 0;
    quint64 _lastApproachStatusAtMs = 0;
    quint64 _lastPayloadTargetStatusAtMs = 0;
    quint64 _lastTargetPoolStatusAtMs = 0;
    quint64 _lastSceneContextStatusAtMs = 0;
    quint64 _nextDirectHeartbeatAtMs = 0;
    QHash<int, quint32> _lastStatusSequence;
    QHash<int, quint64> _lastStatusAcceptedAtMs;
    ChallengeBinding _challenge;
    ApproachChallengeBinding _approachChallengeBinding;
    PayloadTargetChallengeBinding _payloadTargetChallengeBinding;
    bool _missionSafetyAllowed = false;
    bool _ruleSafetyAllowed = false;
    QVariantMap _patrolStatus;
    QVariantMap _rangeStatus;
    QVariantMap _targetGeolocationStatus;
    QVariantMap _releaseStatus;
    QVariantMap _approachChallenge;
    QVariantMap _approachStatus;
    QVariantMap _payloadTargetChallenge;
    QVariantMap _payloadTargetStatus;
    QVariantList _targetPool;
    QHash<int, QVariantList> _targetPoolPages;
    QHash<int, quint32> _targetPoolPageSequences;
    quint32 _targetPoolRevision = 0;
    int _targetPoolPageCount = 0;
    int _targetPoolTotalCount = 0;
    bool _targetPoolRevisionValid = false;
    QVariantList _sceneContextRegions;
    QString _sceneContextState;
    QHash<int, QVariantList> _sceneContextPages;
    QHash<int, quint32> _sceneContextPageSequences;
    quint32 _sceneContextRevision = 0;
    int _sceneContextPageCount = 0;
    int _sceneContextTotalCount = 0;
    QString _sceneContextFrameId;
    QString _sceneContextPendingState;
    bool _sceneContextRevisionValid = false;
    QList<quint64> _trackingMetadataSentAtMs;
    double _trackingMetadataRateHz = 0.0;
    bool _trackingMetadataRateLogged = false;
    QList<quint64> _targetPoolMetadataReceivedAtMs;
    double _targetPoolMetadataRateHz = 0.0;
    MultiDetectDepthGridReceiver* _depthGridReceiver = nullptr;
    QByteArray _depthGridRaw;
    QString _depthMapDataUrl;
    int _depthGridWidth = 0;
    int _depthGridHeight = 0;
    double _depthMinimumM = 0.0;
    double _depthMaximumM = 0.0;
    bool _depthGridLogarithmic = false;
    quint64 _lastDepthGridAtMs = 0;
    std::optional<MultiDetectDepthGridFrame> _pendingDepthGridFrame;
    QTimer _depthGridRenderTimer;
    PendingDelivery _pendingSelection;
    QHash<QString, PendingDelivery> _pendingTrackSelections;
    PendingDelivery _pendingAuthorization;
    PendingDelivery _pendingApproach;
    PendingDelivery _pendingPayloadTarget;
    SharedLinkConfigurationPtr _operatorConfiguration;
    QTimer _pollTimer;
    bool _shutdown = false;
};
