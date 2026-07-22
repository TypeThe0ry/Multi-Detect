#include "MultiDetectOperatorController.h"

#include <QtCore/QBuffer>
#include <QtCore/QDateTime>
#include <QtCore/QSet>
#include <QtCore/QSettings>
#include <QtCore/QUuid>
#include <QtGui/QColor>
#include <QtGui/QImage>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <limits>

#include "LinkInterface.h"
#include "LinkManager.h"
#include "MAVLinkProtocol.h"
#include "MAVLinkSigning.h"
#include "MultiDetectDepthGridReceiver.h"
#include "MultiDetectPagedStatusSequence.h"
#include "MultiVehicleManager.h"
#include "QGCLoggingCategory.h"
#include "SigningController.h"
#include "UDPLink.h"
#include "Vehicle.h"
#include "VehicleLinkManager.h"

#ifndef MULTIDETECT_QGC_DIRECT_PIXHAWK_WRITES
#error "MULTIDETECT_QGC_DIRECT_PIXHAWK_WRITES must be defined for the custom application"
#endif
#ifndef MULTIDETECT_PHYSICAL_RELEASE
#error "MULTIDETECT_PHYSICAL_RELEASE must be defined for the custom application"
#endif

static_assert(MULTIDETECT_QGC_DIRECT_PIXHAWK_WRITES == 0, "QGC must not duplicate Jetson Mode-3 Pixhawk setpoints");
static_assert(MULTIDETECT_PHYSICAL_RELEASE == 0, "Operator metadata transport must not enable physical release");

QGC_LOGGING_CATEGORY(MultiDetectOperatorLog, "MultiDetect.Operator")

namespace {

QString environmentValue(const char* name, const QString& fallback = {})
{
    if (qEnvironmentVariableIsSet(name)) {
        return qEnvironmentVariable(name);
    }
#ifdef Q_OS_WIN
    // HKCU\Environment is also the persistent provisioning store. Reading it
    // directly makes a manually launched app work immediately, even when the
    // already-running Explorer process has not refreshed its environment yet.
    QSettings userEnvironment(QStringLiteral("HKEY_CURRENT_USER\\Environment"), QSettings::NativeFormat);
    const QVariant persisted = userEnvironment.value(QString::fromLatin1(name));
    if (persisted.isValid()) {
        return persisted.toString();
    }
#endif
    return fallback;
}

int environmentInteger(const char* name, int fallback)
{
    bool ok = false;
    const int value = environmentValue(name).toInt(&ok);
    return ok ? value : fallback;
}

bool validSystemId(int value)
{
    return value >= 1 && value <= 255;
}

bool validComponentId(int value)
{
    return value >= 1 && value <= 255;
}

}  // namespace

MultiDetectOperatorController::MultiDetectOperatorController(QObject* parent)
    : QObject(parent),
      _hmacKey(environmentValue("MULTIDETECT_OPERATOR_KEY").toUtf8()),
      _streamId(environmentValue("MULTIDETECT_OPERATOR_STREAM_ID", QStringLiteral("camera-main"))),
      _streamWidth(environmentInteger("MULTIDETECT_OPERATOR_STREAM_WIDTH", 1280)),
      _streamHeight(environmentInteger("MULTIDETECT_OPERATOR_STREAM_HEIGHT", 720)),
      _streamRotation(environmentInteger("MULTIDETECT_OPERATOR_STREAM_ROTATION", 0)),
      _localSystemId(environmentInteger("MULTIDETECT_OPERATOR_GCS_SYSTEM_ID", 255)),
      _remoteSystemId(environmentInteger("MULTIDETECT_OPERATOR_JETSON_SYSTEM_ID", 1)),
      _remoteComponentId(environmentInteger("MULTIDETECT_OPERATOR_JETSON_COMPONENT_ID", 191)),
      _operatorId(environmentValue("MULTIDETECT_OPERATOR_ID")),
      _operatorUdpHost(environmentValue("MULTIDETECT_OPERATOR_UDP_HOST", QStringLiteral("192.168.144.20")).trimmed()),
      _operatorUdpPort(environmentInteger("MULTIDETECT_OPERATOR_UDP_PORT", 14580)),
      _operatorUdpLocalPort(environmentInteger("MULTIDETECT_OPERATOR_UDP_LOCAL_PORT", 14581)),
      _depthGridUdpPort(environmentInteger("MULTIDETECT_DEPTH_GRID_UDP_PORT", 14582)),
      _depthGridJetsonPort(environmentInteger("MULTIDETECT_DEPTH_GRID_JETSON_PORT", 14583)),
      _protocol(_hmacKey, _streamId),
      _sessionId(QUuid::createUuid().toString(QUuid::WithoutBraces))
{
    _directOperatorRequested = !_operatorUdpHost.isEmpty();
    const QByteArray signingKeyHex = environmentValue("MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX").toUtf8().trimmed();
    const QByteArray decodedSigningKey = QByteArray::fromHex(signingKeyHex);
    const bool signingKeyValid = signingKeyHex.size() == 64 && decodedSigningKey.size() == 32 &&
                                 decodedSigningKey.toHex().compare(signingKeyHex, Qt::CaseInsensitive) == 0;
    if (signingKeyValid) {
        _mavlinkSigningKey = decodedSigningKey;
    }

    if (!validSystemId(_localSystemId) || !validSystemId(_remoteSystemId) || !validComponentId(_localComponentId) ||
        !validComponentId(_remoteComponentId) || _streamWidth <= 0 || _streamWidth > 65535 || _streamHeight <= 0 ||
        _streamHeight > 65535 ||
        (_streamRotation != 0 && _streamRotation != 90 && _streamRotation != 180 && _streamRotation != 270)) {
        _hmacKey.clear();
        _protocol = MultiDetectOperatorProtocol({}, _streamId);
        _lastError = QStringLiteral("operator-link endpoint or video geometry is invalid");
    } else if (!_protocol.configured()) {
        _lastError = QStringLiteral("set MULTIDETECT_OPERATOR_KEY to at least 32 UTF-8 bytes");
    } else if (_directOperatorRequested &&
               (_operatorUdpPort < 1024 || _operatorUdpPort > 65535 || _operatorUdpLocalPort < 1024 ||
                _operatorUdpLocalPort > 65535 || _operatorUdpPort == _operatorUdpLocalPort || !signingKeyValid)) {
        _lastError = QStringLiteral(
            "direct operator UDP requires valid ports and MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX with 64 hex digits");
    }
    _linkState = configured() && (!_directOperatorRequested || !_mavlinkSigningKey.isEmpty())
                     ? QStringLiteral("WAITING_FOR_SIGNED_METADATA")
                     : QStringLiteral("NOT_CONFIGURED");

    connect(MAVLinkProtocol::instance(), &MAVLinkProtocol::messageReceived, this,
            &MultiDetectOperatorController::_mavlinkMessageReceived);
    _pollTimer.setInterval(100);
    connect(&_pollTimer, &QTimer::timeout, this, &MultiDetectOperatorController::_poll);
    _pollTimer.start();
    _depthGridRenderTimer.setInterval(200);
    _depthGridRenderTimer.setSingleShot(true);
    connect(&_depthGridRenderTimer, &QTimer::timeout, this, &MultiDetectOperatorController::_renderPendingDepthGrid);

    if (_depthGridUdpPort >= 1024 && _depthGridUdpPort <= 65535 && _depthGridJetsonPort >= 1024 &&
        _depthGridJetsonPort <= 65535 && _depthGridUdpPort != _depthGridJetsonPort) {
        _depthGridReceiver = new MultiDetectDepthGridReceiver(_hmacKey, this);
        connect(_depthGridReceiver, &MultiDetectDepthGridReceiver::frameReady, this,
                &MultiDetectOperatorController::_adoptDepthGridFrame);
        if (!_depthGridReceiver->start(static_cast<quint16>(_depthGridUdpPort), _operatorUdpHost,
                                       static_cast<quint16>(_depthGridJetsonPort))) {
            qCWarning(MultiDetectOperatorLog).nospace() << "depth-grid UDP bind failed port=" << _depthGridUdpPort
                                                        << " error=" << _depthGridReceiver->lastError();
            _depthGridReceiver->deleteLater();
            _depthGridReceiver = nullptr;
        }
    }

    if (_directOperatorRequested && !_mavlinkSigningKey.isEmpty()) {
        QTimer::singleShot(0, this, &MultiDetectOperatorController::_startDirectOperatorLink);
    }
}

MultiDetectOperatorController::~MultiDetectOperatorController()
{
    shutdown();
}

void MultiDetectOperatorController::shutdown()
{
    if (_shutdown) {
        return;
    }
    _shutdown = true;
    _pollTimer.stop();
    disconnect(MAVLinkProtocol::instance(), nullptr, this, nullptr);
    _pendingSelection.clear();
    _clearPendingTrackSelections();
    _pendingAuthorization.clear();
    _pendingApproach.clear();
    _pendingPayloadTarget.clear();
    _challenge = {};
    _approachChallengeBinding = {};
    _payloadTargetChallengeBinding = {};
    if (_depthGridReceiver != nullptr) {
        _depthGridReceiver->close();
        _depthGridReceiver = nullptr;
    }
    _clearDepthGrid();
    if (_operatorConfiguration) {
        LinkManager::instance()->removeConfiguration(_operatorConfiguration.get());
        _operatorConfiguration.reset();
    }
    _mavlinkSigningKey.fill('\0');
    _mavlinkSigningKey.clear();
}

bool MultiDetectOperatorController::configured() const
{
    return _protocol.configured();
}

qint64 MultiDetectOperatorController::depthMapAgeMs() const
{
    if (_lastDepthGridAtMs == 0) {
        return -1;
    }
    return QDateTime::currentMSecsSinceEpoch() - static_cast<qint64>(_lastDepthGridAtMs);
}

double MultiDetectOperatorController::depthAtNormalized(double x, double y) const
{
    if (_depthGridRaw.isEmpty() || _depthGridWidth <= 0 || _depthGridHeight <= 0 || !std::isfinite(x) ||
        !std::isfinite(y)) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    const int column = std::clamp(static_cast<int>(x * _depthGridWidth), 0, _depthGridWidth - 1);
    const int row = std::clamp(static_cast<int>(y * _depthGridHeight), 0, _depthGridHeight - 1);
    const qsizetype index = static_cast<qsizetype>(row) * _depthGridWidth + column;
    const quint8 value = static_cast<quint8>(_depthGridRaw.at(index));
    if (value == 0) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    const double fraction = static_cast<double>(value - 1) / 254.0;
    if (_depthGridLogarithmic) {
        return _depthMinimumM * std::pow(_depthMaximumM / _depthMinimumM, fraction);
    }
    return _depthMinimumM + fraction * (_depthMaximumM - _depthMinimumM);
}

void MultiDetectOperatorController::_adoptDepthGridFrame(const MultiDetectDepthGridFrame& frame, quint64 receivedAtMs)
{
    if (frame.quantizedDepth.size() != static_cast<qsizetype>(frame.width) * frame.height) {
        return;
    }
    // Preserve the newest decoded grid for measurements, but limit costly PNG/UI updates to 5 Hz.
    _depthGridRaw = frame.quantizedDepth;
    _depthGridWidth = frame.width;
    _depthGridHeight = frame.height;
    _depthMinimumM = frame.minimumDepthM;
    _depthMaximumM = frame.maximumDepthM;
    _depthGridLogarithmic = frame.logarithmicEncoding;
    _lastDepthGridAtMs = receivedAtMs;
    _pendingDepthGridFrame = frame;
    if (!_depthGridRenderTimer.isActive()) {
        _depthGridRenderTimer.start();
    }
}

void MultiDetectOperatorController::_renderPendingDepthGrid()
{
    if (!_pendingDepthGridFrame.has_value()) {
        return;
    }
    const MultiDetectDepthGridFrame frame = *_pendingDepthGridFrame;
    _pendingDepthGridFrame.reset();
    QImage heatmap(frame.width, frame.height, QImage::Format_RGBA8888);
    for (int row = 0; row < frame.height; ++row) {
        for (int column = 0; column < frame.width; ++column) {
            const qsizetype index = static_cast<qsizetype>(row) * frame.width + column;
            const quint8 value = static_cast<quint8>(frame.quantizedDepth.at(index));
            if (value == 0) {
                heatmap.setPixelColor(column, row, QColor(0, 0, 0, 0));
                continue;
            }
            const double fraction = static_cast<double>(value - 1) / 254.0;
            const QColor color =
                QColor::fromHsvF(static_cast<float>((1.0 - fraction) * (240.0 / 360.0)), 1.0F, 1.0F, 0.78F);
            heatmap.setPixelColor(column, row, color);
        }
    }
    QByteArray encodedPng;
    QBuffer buffer(&encodedPng);
    buffer.open(QIODevice::WriteOnly);
    if (!heatmap.save(&buffer, "PNG")) {
        return;
    }
    _depthMapDataUrl = QStringLiteral("data:image/png;base64,") + QString::fromLatin1(encodedPng.toBase64());
    emit depthMapChanged();
}

void MultiDetectOperatorController::_clearDepthGrid()
{
    const bool changed = !_depthMapDataUrl.isEmpty() || !_depthGridRaw.isEmpty();
    _depthGridRenderTimer.stop();
    _pendingDepthGridFrame.reset();
    if (_depthGridReceiver != nullptr) {
        _depthGridReceiver->reset();
    }
    _depthGridRaw.clear();
    _depthMapDataUrl.clear();
    _depthGridWidth = 0;
    _depthGridHeight = 0;
    _depthMinimumM = 0.0;
    _depthMaximumM = 0.0;
    _depthGridLogarithmic = false;
    _lastDepthGridAtMs = 0;
    if (changed) {
        emit depthMapChanged();
    }
}

void MultiDetectOperatorController::_recordTargetPoolMetadataSnapshot(quint64 receivedAtMs)
{
    if (!_targetPoolMetadataReceivedAtMs.isEmpty()) {
        const quint64 previousAtMs = _targetPoolMetadataReceivedAtMs.constLast();
        if (receivedAtMs < previousAtMs) {
            _targetPoolMetadataReceivedAtMs.clear();
        } else if (receivedAtMs == previousAtMs) {
            return;
        }
    }
    _targetPoolMetadataReceivedAtMs.append(receivedAtMs);
    while (_targetPoolMetadataReceivedAtMs.size() > 60) {
        _targetPoolMetadataReceivedAtMs.removeFirst();
    }
    if (_targetPoolMetadataReceivedAtMs.size() < 2) {
        return;
    }
    const quint64 spanMs = _targetPoolMetadataReceivedAtMs.constLast() - _targetPoolMetadataReceivedAtMs.constFirst();
    const double newRate =
        spanMs > 0
            ? (static_cast<double>(_targetPoolMetadataReceivedAtMs.size() - 1) * 1000.0 / static_cast<double>(spanMs))
            : 0.0;
    if (std::abs(newRate - _targetPoolMetadataRateHz) >= 0.01) {
        _targetPoolMetadataRateHz = newRate;
        emit targetPoolMetadataRateChanged();
    }
}

void MultiDetectOperatorController::_resetTargetPoolMetadataRate()
{
    _targetPoolMetadataReceivedAtMs.clear();
    if (_targetPoolMetadataRateHz != 0.0) {
        _targetPoolMetadataRateHz = 0.0;
        emit targetPoolMetadataRateChanged();
    }
}

bool MultiDetectOperatorController::_hasPendingTargetSelection() const
{
    return _pendingSelection.active() || !_pendingTrackSelections.isEmpty();
}

void MultiDetectOperatorController::_clearPendingTrackSelections()
{
    if (_pendingTrackSelections.isEmpty()) {
        return;
    }
    _pendingTrackSelections.clear();
    emit pendingTrackSelectionCountChanged();
}

bool MultiDetectOperatorController::sendTargetSelection(const QString& action, double x1, double y1, double x2,
                                                        double y2)
{
    if (!configured()) {
        _setLastError(QStringLiteral("operator-link is not configured"));
        return false;
    }
    const QString normalizedAction = action.trimmed().toUpper();
    if (normalizedAction != QStringLiteral("SELECT") && normalizedAction != QStringLiteral("SWITCH") &&
        normalizedAction != QStringLiteral("CANCEL") && normalizedAction != QStringLiteral("SELECT_TRK") &&
        normalizedAction != QStringLiteral("PROMOTE_LCK") && normalizedAction != QStringLiteral("DEMOTE_TRK") &&
        normalizedAction != QStringLiteral("CANCEL_TRK")) {
        _setLastError(QStringLiteral("target-selection action is invalid"));
        return false;
    }
    const bool isTrackSelection = normalizedAction == QStringLiteral("SELECT_TRK");
    if (isTrackSelection) {
        // Multiple manual/candidate TRK rectangles are independent commands.
        // Do not let a later box discard the retry/correlation state for an
        // earlier one; Jetson can materialize both in the same target-pool
        // revision.  Exclusive actions remain serialized below.
        if (_pendingSelection.active()) {
            _setLastError(QStringLiteral("an exclusive target-selection acknowledgement is still pending"));
            return false;
        }
    } else if (_hasPendingTargetSelection()) {
        // A non-TRK action is exclusive by definition.  It supersedes any
        // unacknowledged visual selections rather than allowing a later LCK or
        // cancel command to race an old manual rectangle.
        _pendingSelection.clear();
        _clearPendingTrackSelections();
    }
    if (normalizedAction != QStringLiteral("CANCEL") &&
        (!std::isfinite(x1) || !std::isfinite(y1) || !std::isfinite(x2) || !std::isfinite(y2) || x1 < 0.0 || y1 < 0.0 ||
         x2 > 1.0 || y2 > 1.0 || x2 - x1 < 0.02 || y2 - y1 < 0.02)) {
        _setLastError(QStringLiteral("target-selection rectangle is invalid or too small"));
        return false;
    }

    // Every new selection command invalidates all target-bound slide evidence
    // before a new command ID is minted.
    _pendingApproach.clear();
    _approachChallengeBinding = {};
    if (!_approachChallenge.isEmpty()) {
        _approachChallenge.clear();
        emit approachChallengeChanged();
    }
    _pendingPayloadTarget.clear();
    _payloadTargetChallengeBinding = {};
    if (!_payloadTargetChallenge.isEmpty()) {
        _payloadTargetChallenge.clear();
        emit payloadTargetChallengeChanged();
    }
    if (!_payloadTargetStatus.isEmpty()) {
        _payloadTargetStatus.clear();
        emit payloadTargetStatusChanged();
    }

    const quint64 localNowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
    const quint64 wireNowMs = _operatorWireNowMs(localNowMs);
    const quint16 ttlMs = 3000;
    const QString commandId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    const quint32 sequence = ++_selectionSequence;
    QString error;
    const QByteArray payload =
        _protocol.encodeSelection(commandId, _sessionId, sequence, normalizedAction, _streamWidth, _streamHeight,
                                  _streamRotation, x1, y1, x2, y2, wireNowMs, ttlMs, &error);
    if (payload.isEmpty()) {
        _setLastError(error);
        return false;
    }

    _lastSelectionCommandId = commandId;
    emit lastSelectionChanged();

    PendingDelivery delivery;
    delivery.payload = payload;
    delivery.correlationId = commandId;
    delivery.sequence = sequence;
    delivery.expiresAtMs = localNowMs + ttlMs;
    delivery.nextAttemptAtMs = localNowMs;
    if (isTrackSelection) {
        _pendingTrackSelections.insert(commandId, delivery);
        emit pendingTrackSelectionCountChanged();
        auto pendingIt = _pendingTrackSelections.find(commandId);
        const bool sent = _sendPending(&pendingIt.value());
        if (!sent) {
            _pendingTrackSelections.erase(pendingIt);
            emit pendingTrackSelectionCountChanged();
        }
        return sent;
    }

    _pendingSelection = delivery;
    const bool sent = _sendPending(&_pendingSelection);
    if (!sent) {
        _pendingSelection.clear();
    }
    return sent;
}

bool MultiDetectOperatorController::sendAuthorizationDecision(bool approve)
{
    const quint64 localNowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
    const quint64 wireNowMs = _operatorWireNowMs(localNowMs);
    if (!configured() || !_challenge.pending || wireNowMs >= _challenge.expiresAtMs) {
        _setLastError(QStringLiteral("no fresh authorization challenge is active"));
        return false;
    }
    if (!_missionSafetyAllowed || !_ruleSafetyAllowed) {
        _setLastError(QStringLiteral("authorization is blocked because safety is not fully allowed"));
        return false;
    }
    if (_operatorId.trimmed().isEmpty()) {
        _setLastError(QStringLiteral("set MULTIDETECT_OPERATOR_ID before sending an authorization decision"));
        return false;
    }
    if (_pendingAuthorization.active()) {
        _setLastError(QStringLiteral("an authorization acknowledgement is still pending"));
        return false;
    }

    const quint64 remainingMs = _challenge.expiresAtMs - wireNowMs;
    const quint16 ttlMs = static_cast<quint16>(std::min<quint64>(2000, remainingMs));
    if (ttlMs == 0) {
        _setLastError(QStringLiteral("authorization challenge expired"));
        return false;
    }
    quint64 commandToken = MultiDetectOperatorProtocol::hash64(QUuid::createUuid().toString(QUuid::WithoutBraces) +
                                                               QString::number(wireNowMs));
    if (commandToken == 0) {
        commandToken = 1;
    }
    quint64 sessionToken = MultiDetectOperatorProtocol::hash64(_sessionId);
    quint64 operatorToken = MultiDetectOperatorProtocol::hash64(_operatorId.trimmed());
    if (sessionToken == 0) {
        sessionToken = 1;
    }
    if (operatorToken == 0) {
        operatorToken = 1;
    }
    const quint32 sequence = ++_authorizationSequence;
    QString error;
    const QByteArray payload = _protocol.encodeAuthorizationDecision(
        commandToken, sessionToken, _challenge.challengeToken, _challenge.missionToken, _challenge.targetToken,
        _challenge.sceneToken, _challenge.rulesetToken, _challenge.payloadSlotToken, _challenge.targetRevision, approve,
        operatorToken, sequence, wireNowMs, ttlMs, &error);
    if (payload.isEmpty()) {
        _setLastError(error);
        return false;
    }

    _pendingAuthorization.payload = payload;
    _pendingAuthorization.commandToken = commandToken;
    _pendingAuthorization.sequence = sequence;
    _pendingAuthorization.expiresAtMs = localNowMs + ttlMs;
    _pendingAuthorization.nextAttemptAtMs = localNowMs;
    _pendingAuthorization.authorizationDecision = true;
    _pendingAuthorization.approve = approve;
    const bool sent = _sendPending(&_pendingAuthorization);
    if (!sent) {
        _pendingAuthorization.clear();
    }
    return sent;
}

bool MultiDetectOperatorController::sendApproachSlideConfirmation(int slideDurationMs, double completionFraction,
                                                                  bool continuous)
{
    const quint64 localNowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
    const quint64 wireNowMs = _operatorWireNowMs(localNowMs);
    if (!configured() || !_approachChallengeBinding.pending || wireNowMs >= _approachChallengeBinding.expiresAtMs) {
        _setLastError(QStringLiteral("no fresh Mode-3 approach challenge is active"));
        return false;
    }
    if (_approachChallengeBinding.selectionCommandId != _lastSelectionCommandId) {
        _setLastError(QStringLiteral("Mode-3 approach challenge is not bound to the current target selection"));
        return false;
    }
    if (_hasPendingTargetSelection()) {
        _setLastError(QStringLiteral("target selection must be acknowledged before Mode-3 confirmation"));
        return false;
    }
    if (_pendingApproach.active()) {
        _setLastError(QStringLiteral("a Mode-3 approach acknowledgement is still pending"));
        return false;
    }
    if (!continuous || slideDurationMs < 600 || slideDurationMs > 4000 || !std::isfinite(completionFraction) ||
        completionFraction < 0.98 || completionFraction > 1.0) {
        _setLastError(QStringLiteral("Mode-3 confirmation requires a complete continuous slide of at least 600 ms"));
        return false;
    }

    const quint64 remainingMs = _approachChallengeBinding.expiresAtMs - wireNowMs;
    const quint16 ttlMs = static_cast<quint16>(std::min<quint64>(2000, remainingMs));
    if (ttlMs == 0) {
        _setLastError(QStringLiteral("Mode-3 approach challenge expired"));
        return false;
    }
    quint64 commandToken = MultiDetectOperatorProtocol::hash64(QUuid::createUuid().toString(QUuid::WithoutBraces) +
                                                               QString::number(wireNowMs));
    quint64 sessionToken = MultiDetectOperatorProtocol::hash64(_sessionId);
    commandToken = commandToken == 0 ? 1 : commandToken;
    sessionToken = sessionToken == 0 ? 1 : sessionToken;
    const quint32 sequence = ++_approachSequence;
    QString error;
    const QByteArray payload = _protocol.encodeApproachConfirmation(
        commandToken, sessionToken, _approachChallengeBinding.challengeToken, _approachChallengeBinding.targetToken,
        _approachChallengeBinding.targetRevision, _approachChallengeBinding.selectionCommandId, sequence, wireNowMs,
        ttlMs, static_cast<quint16>(slideDurationMs), completionFraction, continuous, &error);
    if (payload.isEmpty()) {
        _setLastError(error);
        return false;
    }

    _pendingApproach.payload = payload;
    _pendingApproach.commandToken = commandToken;
    _pendingApproach.sequence = sequence;
    _pendingApproach.expiresAtMs = localNowMs + ttlMs;
    _pendingApproach.nextAttemptAtMs = localNowMs;
    _pendingApproach.approachConfirmation = true;
    const bool sent = _sendPending(&_pendingApproach);
    if (!sent) {
        _pendingApproach.clear();
    }
    return sent;
}

bool MultiDetectOperatorController::sendPayloadTargetSlideConfirmation(int slideDurationMs, double completionFraction,
                                                                       bool continuous)
{
    const quint64 localNowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
    const quint64 wireNowMs = _operatorWireNowMs(localNowMs);
    if (!configured() || !_payloadTargetChallengeBinding.pending ||
        wireNowMs >= _payloadTargetChallengeBinding.expiresAtMs) {
        _setLastError(QStringLiteral("no fresh Mode-2 payload target challenge is active"));
        return false;
    }
    if (_payloadTargetChallengeBinding.selectionCommandId != _lastSelectionCommandId) {
        _setLastError(QStringLiteral("Mode-2 payload target challenge is not bound to the current selection"));
        return false;
    }
    if (_hasPendingTargetSelection()) {
        _setLastError(QStringLiteral("target selection must be acknowledged before Mode-2 confirmation"));
        return false;
    }
    if (_pendingPayloadTarget.active()) {
        _setLastError(QStringLiteral("a Mode-2 payload target acknowledgement is still pending"));
        return false;
    }
    if (!continuous || slideDurationMs < 600 || slideDurationMs > 4000 || !std::isfinite(completionFraction) ||
        completionFraction < 0.98 || completionFraction > 1.0) {
        _setLastError(QStringLiteral("Mode-2 confirmation requires a complete continuous slide of at least 600 ms"));
        return false;
    }

    const quint64 remainingMs = _payloadTargetChallengeBinding.expiresAtMs - wireNowMs;
    const quint16 ttlMs = static_cast<quint16>(std::min<quint64>(2000, remainingMs));
    if (ttlMs == 0) {
        _setLastError(QStringLiteral("Mode-2 payload target challenge expired"));
        return false;
    }
    quint64 commandToken = MultiDetectOperatorProtocol::hash64(QUuid::createUuid().toString(QUuid::WithoutBraces) +
                                                               QString::number(wireNowMs));
    quint64 sessionToken = MultiDetectOperatorProtocol::hash64(_sessionId);
    commandToken = commandToken == 0 ? 1 : commandToken;
    sessionToken = sessionToken == 0 ? 1 : sessionToken;
    const quint32 sequence = ++_payloadTargetSequence;
    QString error;
    const QByteArray payload = _protocol.encodePayloadTargetConfirmation(
        commandToken, sessionToken, _payloadTargetChallengeBinding.challengeToken,
        _payloadTargetChallengeBinding.selectedTargetToken, _payloadTargetChallengeBinding.selectedTargetRevision,
        _payloadTargetChallengeBinding.aimpointTargetToken, _payloadTargetChallengeBinding.aimpointTargetRevision,
        _payloadTargetChallengeBinding.selectionCommandId, sequence, wireNowMs, ttlMs,
        static_cast<quint16>(slideDurationMs), completionFraction, continuous, &error);
    if (payload.isEmpty()) {
        _setLastError(error);
        return false;
    }

    _pendingPayloadTarget.payload = payload;
    _pendingPayloadTarget.commandToken = commandToken;
    _pendingPayloadTarget.sequence = sequence;
    _pendingPayloadTarget.expiresAtMs = localNowMs + ttlMs;
    _pendingPayloadTarget.nextAttemptAtMs = localNowMs;
    _pendingPayloadTarget.payloadTargetConfirmation = true;
    const bool sent = _sendPending(&_pendingPayloadTarget);
    if (!sent) {
        _pendingPayloadTarget.clear();
    }
    return sent;
}

void MultiDetectOperatorController::_mavlinkMessageReceived(LinkInterface* link, const mavlink_message_t& message)
{
    if (message.msgid != MAVLINK_MSG_ID_TUNNEL) {
        return;
    }
    if (_directOperatorRequested && (!_operatorConfiguration || link != _operatorConfiguration->link())) {
        return;
    }
    mavlink_tunnel_t tunnel{};
    mavlink_msg_tunnel_decode(&message, &tunnel);
    if (tunnel.payload_type != MultiDetectOperatorProtocol::kTunnelPayloadType) {
        return;
    }
    if (!configured()) {
        _reject(QStringLiteral("operator-link metadata arrived before key configuration"));
        return;
    }
    if (message.sysid != _remoteSystemId || message.compid != _remoteComponentId ||
        tunnel.target_system != _localSystemId || tunnel.target_component != _localComponentId) {
        _reject(QStringLiteral("operator-link MAVLink endpoint does not match"));
        return;
    }
    const bool signedFrame = message.magic == MAVLINK_STX && (message.incompat_flags & MAVLINK_IFLAG_SIGNED) != 0;
    if (!signedFrame) {
        _reject(QStringLiteral("unsigned operator-link MAVLink frame was rejected"));
        return;
    }
    if (tunnel.payload_length == 0 || tunnel.payload_length > MultiDetectOperatorProtocol::kMaximumPayloadBytes) {
        _reject(QStringLiteral("operator-link TUNNEL payload length is invalid"));
        return;
    }

    const QByteArray payload(reinterpret_cast<const char*>(tunnel.payload), tunnel.payload_length);
    MultiDetectOperatorProtocol::DecodedPacket packet;
    QString error;
    if (!_protocol.decode(payload, &packet, &error)) {
        _reject(error);
        return;
    }
    if (packet.type == MultiDetectOperatorProtocol::MessageType::TargetSelection ||
        packet.type == MultiDetectOperatorProtocol::MessageType::AuthorizationDecision ||
        packet.type == MultiDetectOperatorProtocol::MessageType::ApproachConfirmation ||
        packet.type == MultiDetectOperatorProtocol::MessageType::PayloadTargetConfirmation) {
        _reject(QStringLiteral("operator-link message direction is invalid for GCS"));
        return;
    }

    _authenticatedPackets++;
    _lastAuthenticatedAtMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
    _observeRemoteWireTime(packet.sentAtMs, _lastAuthenticatedAtMs);
    emit countersChanged();
    _setLastError({});
    _setLinkState(QStringLiteral("AUTHENTICATED"));
    qCInfo(MultiDetectOperatorLog).nospace() << "authenticated metadata type=" << static_cast<int>(packet.type)
                                             << " sequence=" << packet.sequence << " outer_signed=" << signedFrame;
    QVariantMap fields = packet.fields;
    fields.insert(QStringLiteral("sequence"), packet.sequence);
    fields.insert(QStringLiteral("sentAtMs"), QVariant::fromValue(packet.sentAtMs));

    if (packet.type == MultiDetectOperatorProtocol::MessageType::SelectionAck) {
        const QString commandId = fields.value(QStringLiteral("commandId")).toString();
        const quint32 acknowledgedSequence = fields.value(QStringLiteral("acknowledgedSequence")).toUInt();
        bool correlated = _pendingSelection.active() && commandId == _pendingSelection.correlationId &&
                          acknowledgedSequence == _pendingSelection.sequence;
        if (!correlated) {
            const auto pendingTrackIt = _pendingTrackSelections.constFind(commandId);
            correlated =
                pendingTrackIt != _pendingTrackSelections.cend() && acknowledgedSequence == pendingTrackIt->sequence;
        }
        fields.insert(QStringLiteral("correlated"), correlated);
        if (correlated) {
            if (_pendingSelection.active() && commandId == _pendingSelection.correlationId) {
                _pendingSelection.clear();
            } else {
                _pendingTrackSelections.remove(commandId);
                emit pendingTrackSelectionCountChanged();
            }
            const int trackStatusKey = static_cast<int>(MultiDetectOperatorProtocol::MessageType::TrackStatus);
            _lastStatusSequence.remove(trackStatusKey);
            _lastStatusAcceptedAtMs.remove(trackStatusKey);
        }
        emit selectionAcknowledged(fields);
        return;
    }
    if (packet.type == MultiDetectOperatorProtocol::MessageType::AuthorizationAck) {
        bool tokenOk = false;
        const quint64 token = _unsignedValue(fields, QStringLiteral("commandToken"), &tokenOk);
        const bool correlated =
            tokenOk && _pendingAuthorization.active() && token == _pendingAuthorization.commandToken &&
            fields.value(QStringLiteral("acknowledgedSequence")).toUInt() == _pendingAuthorization.sequence;
        fields.insert(QStringLiteral("correlated"), correlated);
        fields.insert(QStringLiteral("decision"),
                      _pendingAuthorization.approve ? QStringLiteral("APPROVE") : QStringLiteral("DENY"));
        if (correlated) {
            _pendingAuthorization.clear();
            _challenge.pending = false;
        }
        emit authorizationAcknowledged(fields);
        return;
    }
    if (packet.type == MultiDetectOperatorProtocol::MessageType::ApproachAck) {
        bool tokenOk = false;
        const quint64 token = _unsignedValue(fields, QStringLiteral("commandToken"), &tokenOk);
        const bool correlated =
            tokenOk && _pendingApproach.active() && token == _pendingApproach.commandToken &&
            fields.value(QStringLiteral("acknowledgedSequence")).toUInt() == _pendingApproach.sequence;
        fields.insert(QStringLiteral("correlated"), correlated);
        if (correlated) {
            _pendingApproach.clear();
            _approachChallengeBinding.pending = false;
            if (!_approachChallenge.isEmpty()) {
                _approachChallenge.clear();
                emit approachChallengeChanged();
            }
        }
        emit approachAcknowledged(fields);
        return;
    }
    if (packet.type == MultiDetectOperatorProtocol::MessageType::PayloadTargetAck) {
        bool tokenOk = false;
        const quint64 token = _unsignedValue(fields, QStringLiteral("commandToken"), &tokenOk);
        const bool correlated =
            tokenOk && _pendingPayloadTarget.active() && token == _pendingPayloadTarget.commandToken &&
            fields.value(QStringLiteral("acknowledgedSequence")).toUInt() == _pendingPayloadTarget.sequence;
        fields.insert(QStringLiteral("correlated"), correlated);
        if (correlated) {
            _pendingPayloadTarget.clear();
            _payloadTargetChallengeBinding.pending = false;
            if (!_payloadTargetChallenge.isEmpty()) {
                _payloadTargetChallenge.clear();
                emit payloadTargetChallengeChanged();
            }
        }
        emit payloadTargetAcknowledged(fields);
        return;
    }
    if (packet.type == MultiDetectOperatorProtocol::MessageType::TrackStatus) {
        // A correlated tracking update proves that Jetson accepted and applied
        // the selection, even if the one-shot SelectionAck datagram was lost.
        // Treat it as an implicit acknowledgement so the reliable-delivery
        // timer cannot report a false retry-budget failure while tracking is
        // visibly active.
        const QString selectionCommandId = fields.value(QStringLiteral("selectionCommandId")).toString();
        const bool singleSelectionAcknowledged =
            _pendingSelection.active() && selectionCommandId == _pendingSelection.correlationId;
        const auto pendingTrackIt = _pendingTrackSelections.constFind(selectionCommandId);
        const bool trackSelectionAcknowledged = pendingTrackIt != _pendingTrackSelections.cend();
        const bool implicitlyAcknowledged = singleSelectionAcknowledged || trackSelectionAcknowledged;
        const quint32 acknowledgedSequence = singleSelectionAcknowledged
                                                 ? _pendingSelection.sequence
                                                 : (trackSelectionAcknowledged ? pendingTrackIt->sequence : 0U);
        if (implicitlyAcknowledged) {
            const int trackStatusKey = static_cast<int>(MultiDetectOperatorProtocol::MessageType::TrackStatus);
            _lastStatusSequence.remove(trackStatusKey);
            _lastStatusAcceptedAtMs.remove(trackStatusKey);
        }
        if (!_isNewStatus(packet.type, packet.sequence, _lastAuthenticatedAtMs)) {
            return;
        }
        if (implicitlyAcknowledged) {
            QVariantMap acknowledgement;
            acknowledgement.insert(QStringLiteral("commandId"), selectionCommandId);
            acknowledgement.insert(QStringLiteral("acknowledgedSequence"), acknowledgedSequence);
            acknowledgement.insert(QStringLiteral("accepted"), true);
            acknowledgement.insert(QStringLiteral("reason"), 0);
            acknowledgement.insert(QStringLiteral("correlated"), true);
            if (singleSelectionAcknowledged) {
                _pendingSelection.clear();
            } else {
                _pendingTrackSelections.remove(selectionCommandId);
                emit pendingTrackSelectionCountChanged();
            }
            emit selectionAcknowledged(acknowledgement);
        }
        if (!_trackingMetadataSentAtMs.isEmpty() && packet.sentAtMs <= _trackingMetadataSentAtMs.constLast()) {
            _trackingMetadataSentAtMs.clear();
            _trackingMetadataRateHz = 0.0;
            _trackingMetadataRateLogged = false;
            emit trackingMetadataRateChanged();
        }
        _trackingMetadataSentAtMs.append(packet.sentAtMs);
        _lastTrackingStatusAtMs = _lastAuthenticatedAtMs;
        while (_trackingMetadataSentAtMs.size() > 60) {
            _trackingMetadataSentAtMs.removeFirst();
        }
        if (_trackingMetadataSentAtMs.size() >= 2) {
            const quint64 spanMs = _trackingMetadataSentAtMs.constLast() - _trackingMetadataSentAtMs.constFirst();
            const double newRate =
                spanMs > 0
                    ? (static_cast<double>(_trackingMetadataSentAtMs.size() - 1) * 1000.0 / static_cast<double>(spanMs))
                    : 0.0;
            if (std::abs(newRate - _trackingMetadataRateHz) >= 0.01) {
                _trackingMetadataRateHz = newRate;
                emit trackingMetadataRateChanged();
            }
            if (!_trackingMetadataRateLogged && _trackingMetadataSentAtMs.size() >= 30) {
                _trackingMetadataRateLogged = true;
                qCInfo(MultiDetectOperatorLog).nospace()
                    << "tracking metadata rate samples=" << _trackingMetadataSentAtMs.size()
                    << " hz=" << QString::number(_trackingMetadataRateHz, 'f', 3);
            }
        }
        emit trackStatusReceived(fields);
    } else if (packet.type != MultiDetectOperatorProtocol::MessageType::TargetPoolStatus &&
               packet.type != MultiDetectOperatorProtocol::MessageType::SceneContextStatus &&
               !_isNewStatus(packet.type, packet.sequence, _lastAuthenticatedAtMs)) {
        return;
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::PatrolStatus) {
        _patrolStatus = fields;
        _lastPatrolStatusAtMs = _lastAuthenticatedAtMs;
        emit patrolStatusChanged();
        emit patrolStatusReceived(fields);
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::RangeStatus) {
        _rangeStatus = fields;
        _lastRangeStatusAtMs = _lastAuthenticatedAtMs;
        emit rangeStatusChanged();
        emit rangeStatusReceived(fields);
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::TargetGeolocationStatus) {
        _targetGeolocationStatus = fields;
        _lastTargetGeolocationStatusAtMs = _lastAuthenticatedAtMs;
        emit targetGeolocationStatusChanged();
        emit targetGeolocationStatusReceived(fields);
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::ReleaseStatus) {
        _releaseStatus = fields;
        _lastReleaseStatusAtMs = _lastAuthenticatedAtMs;
        emit releaseStatusChanged();
        emit releaseStatusReceived(fields);
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::ApproachStatus) {
        _approachStatus = fields;
        _lastApproachStatusAtMs = _lastAuthenticatedAtMs;
        emit approachStatusChanged();
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::PayloadTargetStatus) {
        _payloadTargetStatus = fields;
        _lastPayloadTargetStatusAtMs = _lastAuthenticatedAtMs;
        emit payloadTargetStatusChanged();
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::TargetPoolStatus) {
        const quint32 revision = fields.value(QStringLiteral("poolRevision")).toUInt();
        const int pageIndex = fields.value(QStringLiteral("pageIndex")).toInt();
        const int pageCount = fields.value(QStringLiteral("pageCount")).toInt();
        const int totalCount = fields.value(QStringLiteral("totalTrackCount")).toInt();
        if (_targetPoolRevisionValid && revision != _targetPoolRevision &&
            static_cast<qint32>(revision - _targetPoolRevision) <= 0) {
            return;
        }
        if (!_targetPoolRevisionValid || revision != _targetPoolRevision) {
            _targetPoolRevision = revision;
            _targetPoolRevisionValid = true;
            _targetPoolPageCount = pageCount;
            _targetPoolTotalCount = totalCount;
            _targetPoolPages.clear();
            _targetPoolPageSequences.clear();
        } else if (pageCount != _targetPoolPageCount || totalCount != _targetPoolTotalCount) {
            _reject(QStringLiteral("target-pool pages disagree within one revision"));
            _targetPoolPages.clear();
            _targetPoolPageSequences.clear();
            _targetPoolRevisionValid = false;
            return;
        }
        if (!MultiDetectPagedStatusSequence::acceptPageSequence(&_targetPoolPageSequences, pageIndex,
                                                                packet.sequence)) {
            return;
        }
        _targetPoolPages.insert(pageIndex, fields.value(QStringLiteral("entries")).toList());
        _lastTargetPoolStatusAtMs = _lastAuthenticatedAtMs;
        if (_targetPoolPages.size() == _targetPoolPageCount) {
            QVariantList assembled;
            QSet<QString> targetIds;
            for (int index = 0; index < _targetPoolPageCount; ++index) {
                if (!_targetPoolPages.contains(index)) {
                    return;
                }
                for (const QVariant& value : _targetPoolPages.value(index)) {
                    const QVariantMap entry = value.toMap();
                    const QString targetId = entry.value(QStringLiteral("targetId")).toString();
                    if (targetId.isEmpty() || targetIds.contains(targetId)) {
                        _reject(QStringLiteral("target-pool snapshot contains duplicate targets"));
                        _targetPoolPages.clear();
                        _targetPoolPageSequences.clear();
                        _targetPoolRevisionValid = false;
                        return;
                    }
                    targetIds.insert(targetId);
                    assembled.append(entry);
                }
            }
            if (assembled.size() != _targetPoolTotalCount) {
                _reject(QStringLiteral("target-pool snapshot size does not match its declaration"));
                _targetPoolPages.clear();
                _targetPoolPageSequences.clear();
                _targetPoolRevisionValid = false;
                return;
            }
            _recordTargetPoolMetadataSnapshot(_lastAuthenticatedAtMs);
            if (_targetPool != assembled) {
                _targetPool = assembled;
                emit targetPoolChanged();
            }
            qCInfo(MultiDetectOperatorLog).nospace()
                << "target-pool snapshot complete revision=" << _targetPoolRevision << " tracks=" << assembled.size()
                << " pages=" << _targetPoolPageCount;
        }
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::SceneContextStatus) {
        const quint32 revision = fields.value(QStringLiteral("contextRevision")).toUInt();
        const int pageIndex = fields.value(QStringLiteral("pageIndex")).toInt();
        const int pageCount = fields.value(QStringLiteral("pageCount")).toInt();
        const int totalCount = fields.value(QStringLiteral("totalRegionCount")).toInt();
        const QString frameId = fields.value(QStringLiteral("sourceFrameId")).toString();
        const QString state = fields.value(QStringLiteral("state")).toString();
        if (_sceneContextRevisionValid && revision != _sceneContextRevision &&
            static_cast<qint32>(revision - _sceneContextRevision) <= 0) {
            return;
        }
        if (!_sceneContextRevisionValid || revision != _sceneContextRevision) {
            _sceneContextRevision = revision;
            _sceneContextRevisionValid = true;
            _sceneContextPageCount = pageCount;
            _sceneContextTotalCount = totalCount;
            _sceneContextFrameId = frameId;
            _sceneContextPendingState = state;
            _sceneContextPages.clear();
            _sceneContextPageSequences.clear();
        } else if (pageCount != _sceneContextPageCount || totalCount != _sceneContextTotalCount ||
                   frameId != _sceneContextFrameId || state != _sceneContextPendingState) {
            _reject(QStringLiteral("scene-context pages disagree within one revision"));
            _sceneContextPages.clear();
            _sceneContextPageSequences.clear();
            _sceneContextRevisionValid = false;
            return;
        }
        if (!MultiDetectPagedStatusSequence::acceptPageSequence(&_sceneContextPageSequences, pageIndex,
                                                                packet.sequence)) {
            return;
        }
        _sceneContextPages.insert(pageIndex, fields.value(QStringLiteral("entries")).toList());
        _lastSceneContextStatusAtMs = _lastAuthenticatedAtMs;
        if (_sceneContextPages.size() == _sceneContextPageCount) {
            QVariantList assembled;
            for (int index = 0; index < _sceneContextPageCount; ++index) {
                if (!_sceneContextPages.contains(index)) {
                    return;
                }
                assembled.append(_sceneContextPages.value(index));
            }
            if (assembled.size() != _sceneContextTotalCount) {
                _reject(QStringLiteral("scene-context snapshot size does not match its declaration"));
                _sceneContextPages.clear();
                _sceneContextPageSequences.clear();
                _sceneContextRevisionValid = false;
                return;
            }
            if (_sceneContextRegions != assembled || _sceneContextState != state) {
                _sceneContextRegions = assembled;
                _sceneContextState = state;
                emit sceneContextChanged();
            }
            qCDebug(MultiDetectOperatorLog).nospace()
                << "scene-context snapshot complete revision=" << _sceneContextRevision << " state=" << state
                << " regions=" << assembled.size() << " pages=" << _sceneContextPageCount;
        }
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::ApproachChallenge) {
        bool valuesOk = false;
        _approachChallengeBinding.challengeToken = _unsignedValue(fields, QStringLiteral("challengeToken"), &valuesOk);
        bool ok = false;
        _approachChallengeBinding.targetToken = _unsignedValue(fields, QStringLiteral("targetToken"), &ok);
        valuesOk = valuesOk && ok;
        _approachChallengeBinding.targetRevision = fields.value(QStringLiteral("targetRevision")).toUInt(&ok);
        valuesOk = valuesOk && ok;
        _approachChallengeBinding.selectionCommandId = fields.value(QStringLiteral("selectionCommandId")).toString();
        valuesOk = valuesOk && !_approachChallengeBinding.selectionCommandId.isEmpty();
        _approachChallengeBinding.expiresAtMs = fields.value(QStringLiteral("expiresAtMs")).toULongLong(&ok);
        valuesOk = valuesOk && ok;
        _approachChallengeBinding.pending = valuesOk && fields.value(QStringLiteral("pending")).toBool() &&
                                            _approachChallengeBinding.selectionCommandId == _lastSelectionCommandId;
        const quint64 localNowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
        const qint64 remainingMs = static_cast<qint64>(_approachChallengeBinding.expiresAtMs) -
                                   static_cast<qint64>(_operatorWireNowMs(localNowMs));
        fields.insert(QStringLiteral("expiresInS"), std::max<qint64>(0, (remainingMs + 999) / 1000));
        fields.insert(QStringLiteral("boundToCurrentSelection"), _approachChallengeBinding.pending);
        if (!valuesOk) {
            _approachChallengeBinding = {};
            _reject(QStringLiteral("Mode-3 approach challenge token conversion failed"));
            return;
        }
        _approachChallenge = fields;
        emit approachChallengeChanged();
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::PayloadTargetChallenge) {
        bool valuesOk = false;
        _payloadTargetChallengeBinding.challengeToken =
            _unsignedValue(fields, QStringLiteral("challengeToken"), &valuesOk);
        bool ok = false;
        _payloadTargetChallengeBinding.selectedTargetToken =
            _unsignedValue(fields, QStringLiteral("selectedTargetToken"), &ok);
        valuesOk = valuesOk && ok;
        _payloadTargetChallengeBinding.selectedTargetRevision =
            fields.value(QStringLiteral("selectedTargetRevision")).toUInt(&ok);
        valuesOk = valuesOk && ok;
        _payloadTargetChallengeBinding.aimpointTargetToken =
            _unsignedValue(fields, QStringLiteral("aimpointTargetToken"), &ok);
        valuesOk = valuesOk && ok;
        _payloadTargetChallengeBinding.aimpointTargetRevision =
            fields.value(QStringLiteral("aimpointTargetRevision")).toUInt(&ok);
        valuesOk = valuesOk && ok;
        _payloadTargetChallengeBinding.selectionCommandId =
            fields.value(QStringLiteral("selectionCommandId")).toString();
        valuesOk = valuesOk && !_payloadTargetChallengeBinding.selectionCommandId.isEmpty();
        _payloadTargetChallengeBinding.expiresAtMs = fields.value(QStringLiteral("expiresAtMs")).toULongLong(&ok);
        valuesOk = valuesOk && ok;
        _payloadTargetChallengeBinding.pending =
            valuesOk && fields.value(QStringLiteral("pending")).toBool() &&
            _payloadTargetChallengeBinding.selectionCommandId == _lastSelectionCommandId;
        const quint64 localNowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
        const qint64 remainingMs = static_cast<qint64>(_payloadTargetChallengeBinding.expiresAtMs) -
                                   static_cast<qint64>(_operatorWireNowMs(localNowMs));
        fields.insert(QStringLiteral("expiresInS"), std::max<qint64>(0, (remainingMs + 999) / 1000));
        fields.insert(QStringLiteral("boundToCurrentSelection"), _payloadTargetChallengeBinding.pending);
        if (!valuesOk) {
            _payloadTargetChallengeBinding = {};
            _reject(QStringLiteral("Mode-2 payload target challenge token conversion failed"));
            return;
        }
        _payloadTargetChallenge = fields;
        emit payloadTargetChallengeChanged();
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::MissionStatus) {
        _missionSafetyAllowed = fields.value(QStringLiteral("safetyKnown")).toBool() &&
                                fields.value(QStringLiteral("safetyAllowed")).toBool();
        emit missionStatusReceived(fields);
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::SafetyStatus) {
        _ruleSafetyAllowed = fields.value(QStringLiteral("allowed")).toBool();
        emit safetyStatusReceived(fields);
    } else if (packet.type == MultiDetectOperatorProtocol::MessageType::AuthorizationChallenge) {
        bool valuesOk = false;
        _challenge.challengeToken = _unsignedValue(fields, QStringLiteral("challengeToken"), &valuesOk);
        bool ok = false;
        _challenge.missionToken = _unsignedValue(fields, QStringLiteral("missionToken"), &ok);
        valuesOk = valuesOk && ok;
        _challenge.targetToken = _unsignedValue(fields, QStringLiteral("targetToken"), &ok);
        valuesOk = valuesOk && ok;
        _challenge.sceneToken = _unsignedValue(fields, QStringLiteral("sceneToken"), &ok);
        valuesOk = valuesOk && ok;
        _challenge.rulesetToken = _unsignedValue(fields, QStringLiteral("rulesetToken"), &ok);
        valuesOk = valuesOk && ok;
        _challenge.payloadSlotToken = _unsignedValue(fields, QStringLiteral("payloadSlotToken"), &ok);
        valuesOk = valuesOk && ok;
        _challenge.targetRevision = fields.value(QStringLiteral("targetRevision")).toUInt(&ok);
        valuesOk = valuesOk && ok;
        _challenge.expiresAtMs = fields.value(QStringLiteral("expiresAtMs")).toULongLong(&ok);
        valuesOk = valuesOk && ok;
        _challenge.pending = valuesOk && fields.value(QStringLiteral("pending")).toBool();
        const quint64 localNowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
        const qint64 remainingMs =
            static_cast<qint64>(_challenge.expiresAtMs) - static_cast<qint64>(_operatorWireNowMs(localNowMs));
        fields.insert(QStringLiteral("expiresInS"), std::max<qint64>(0, (remainingMs + 999) / 1000));
        if (!valuesOk) {
            _challenge = {};
            _reject(QStringLiteral("authorization challenge token conversion failed"));
            return;
        }
        emit authorizationChallengeReceived(fields);
    }
}

void MultiDetectOperatorController::_poll()
{
    const quint64 nowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
    const quint64 wireNowMs = _operatorWireNowMs(nowMs);
    if (_directOperatorRequested && nowMs >= _nextDirectHeartbeatAtMs) {
        (void) _sendDirectHeartbeat();
        _nextDirectHeartbeatAtMs = nowMs + 1000;
    }
    // Target pools and depth grids can briefly burst over Wi-Fi while the
    // continuous video/UI path remains usable.  Keep a bounded grace period
    // before invalidating signed state; actual safety state is still cleared
    // immediately when this expiry is reached.
    if (_lastAuthenticatedAtMs != 0 && nowMs - _lastAuthenticatedAtMs > 5000 &&
        _linkState == QStringLiteral("AUTHENTICATED")) {
        _setLinkState(QStringLiteral("STALE"));
        _missionSafetyAllowed = false;
        _ruleSafetyAllowed = false;
        _challenge.pending = false;
        _approachChallengeBinding.pending = false;
        _payloadTargetChallengeBinding.pending = false;
        if (!_patrolStatus.isEmpty()) {
            _patrolStatus.clear();
            emit patrolStatusChanged();
        }
        if (!_rangeStatus.isEmpty()) {
            _rangeStatus.clear();
            emit rangeStatusChanged();
        }
        if (!_targetGeolocationStatus.isEmpty()) {
            _targetGeolocationStatus.clear();
            emit targetGeolocationStatusChanged();
        }
        if (!_releaseStatus.isEmpty()) {
            _releaseStatus.clear();
            emit releaseStatusChanged();
        }
        if (!_approachChallenge.isEmpty()) {
            _approachChallenge.clear();
            emit approachChallengeChanged();
        }
        if (!_approachStatus.isEmpty()) {
            _approachStatus.clear();
            emit approachStatusChanged();
        }
        if (!_payloadTargetChallenge.isEmpty()) {
            _payloadTargetChallenge.clear();
            emit payloadTargetChallengeChanged();
        }
        if (!_payloadTargetStatus.isEmpty()) {
            _payloadTargetStatus.clear();
            emit payloadTargetStatusChanged();
        }
        _targetPoolPages.clear();
        _targetPoolPageSequences.clear();
        _targetPoolRevisionValid = false;
        _resetTargetPoolMetadataRate();
        if (!_targetPool.isEmpty()) {
            _targetPool.clear();
            emit targetPoolChanged();
        }
        _sceneContextPages.clear();
        _sceneContextPageSequences.clear();
        _sceneContextRevisionValid = false;
        if (!_sceneContextRegions.isEmpty() || !_sceneContextState.isEmpty()) {
            _sceneContextRegions.clear();
            _sceneContextState.clear();
            emit sceneContextChanged();
        }
        _trackingMetadataSentAtMs.clear();
        _trackingMetadataRateLogged = false;
        if (_trackingMetadataRateHz != 0.0) {
            _trackingMetadataRateHz = 0.0;
            emit trackingMetadataRateChanged();
        }
    }
    constexpr quint64 kDepthGridStaleMs = 1500;
    if (_lastDepthGridAtMs != 0 && nowMs - _lastDepthGridAtMs > kDepthGridStaleMs) {
        _clearDepthGrid();
    }
    constexpr quint64 kPatrolStatusStaleMs = 2000;
    constexpr quint64 kTrackingMetadataStaleMs = 1000;
    if (_lastTrackingStatusAtMs != 0 && nowMs - _lastTrackingStatusAtMs > kTrackingMetadataStaleMs) {
        _lastTrackingStatusAtMs = 0;
        _trackingMetadataSentAtMs.clear();
        _trackingMetadataRateLogged = false;
        if (_trackingMetadataRateHz != 0.0) {
            _trackingMetadataRateHz = 0.0;
            emit trackingMetadataRateChanged();
        }
    }
    if (_lastPatrolStatusAtMs != 0 && nowMs - _lastPatrolStatusAtMs > kPatrolStatusStaleMs) {
        _lastPatrolStatusAtMs = 0;
        if (!_patrolStatus.isEmpty()) {
            _patrolStatus.clear();
            emit patrolStatusChanged();
        }
    }
    constexpr quint64 kRangeStatusStaleMs = 1000;
    if (_lastRangeStatusAtMs != 0 && nowMs - _lastRangeStatusAtMs > kRangeStatusStaleMs) {
        _lastRangeStatusAtMs = 0;
        if (!_rangeStatus.isEmpty()) {
            _rangeStatus.clear();
            emit rangeStatusChanged();
        }
    }
    constexpr quint64 kTargetGeolocationStatusStaleMs = 1000;
    if (_lastTargetGeolocationStatusAtMs != 0 &&
        nowMs - _lastTargetGeolocationStatusAtMs > kTargetGeolocationStatusStaleMs) {
        _lastTargetGeolocationStatusAtMs = 0;
        if (!_targetGeolocationStatus.isEmpty()) {
            _targetGeolocationStatus.clear();
            emit targetGeolocationStatusChanged();
        }
    }
    constexpr quint64 kReleaseStatusStaleMs = 1000;
    if (_lastReleaseStatusAtMs != 0 && nowMs - _lastReleaseStatusAtMs > kReleaseStatusStaleMs) {
        _lastReleaseStatusAtMs = 0;
        if (!_releaseStatus.isEmpty()) {
            _releaseStatus.clear();
            emit releaseStatusChanged();
        }
    }

    constexpr quint64 kApproachStatusStaleMs = 1000;
    if (_lastApproachStatusAtMs != 0 && nowMs - _lastApproachStatusAtMs > kApproachStatusStaleMs) {
        _lastApproachStatusAtMs = 0;
        if (!_approachStatus.isEmpty()) {
            _approachStatus.clear();
            emit approachStatusChanged();
        }
    }
    constexpr quint64 kPayloadTargetStatusStaleMs = 1000;
    if (_lastPayloadTargetStatusAtMs != 0 && nowMs - _lastPayloadTargetStatusAtMs > kPayloadTargetStatusStaleMs) {
        _lastPayloadTargetStatusAtMs = 0;
        if (!_payloadTargetStatus.isEmpty()) {
            _payloadTargetStatus.clear();
            emit payloadTargetStatusChanged();
        }
    }
    // Target-pool snapshots are paged and can briefly pause while Jetson shifts
    // detector budget to a selected target. Keep the last complete atomic
    // revision through one bounded transport/detector gap so boxes do not flash
    // away during an otherwise authenticated tracking session.
    constexpr quint64 kTargetPoolStatusStaleMs = 2500;
    if (_lastTargetPoolStatusAtMs != 0 && nowMs - _lastTargetPoolStatusAtMs > kTargetPoolStatusStaleMs) {
        _lastTargetPoolStatusAtMs = 0;
        _targetPoolPages.clear();
        _targetPoolPageSequences.clear();
        _targetPoolRevisionValid = false;
        _resetTargetPoolMetadataRate();
        if (!_targetPool.isEmpty()) {
            _targetPool.clear();
            emit targetPoolChanged();
        }
    }
    constexpr quint64 kSceneContextStatusStaleMs = 2000;
    if (_lastSceneContextStatusAtMs != 0 && nowMs - _lastSceneContextStatusAtMs > kSceneContextStatusStaleMs) {
        _lastSceneContextStatusAtMs = 0;
        _sceneContextPages.clear();
        _sceneContextPageSequences.clear();
        _sceneContextRevisionValid = false;
        if (!_sceneContextRegions.isEmpty() || _sceneContextState != QStringLiteral("STALE")) {
            _sceneContextRegions.clear();
            _sceneContextState = QStringLiteral("STALE");
            emit sceneContextChanged();
        }
    }
    if (_challenge.pending && wireNowMs >= _challenge.expiresAtMs) {
        _challenge = {};
    }
    if (_approachChallengeBinding.pending && wireNowMs >= _approachChallengeBinding.expiresAtMs) {
        _approachChallengeBinding = {};
        if (!_approachChallenge.isEmpty()) {
            _approachChallenge.clear();
            emit approachChallengeChanged();
        }
    }
    if (_payloadTargetChallengeBinding.pending && wireNowMs >= _payloadTargetChallengeBinding.expiresAtMs) {
        _payloadTargetChallengeBinding = {};
        if (!_payloadTargetChallenge.isEmpty()) {
            _payloadTargetChallenge.clear();
            emit payloadTargetChallengeChanged();
        }
    }

    bool trackSelectionCountChanged = false;
    for (auto pendingTrackIt = _pendingTrackSelections.begin(); pendingTrackIt != _pendingTrackSelections.end();) {
        PendingDelivery& delivery = pendingTrackIt.value();
        if (nowMs > delivery.expiresAtMs) {
            pendingTrackIt = _pendingTrackSelections.erase(pendingTrackIt);
            trackSelectionCountChanged = true;
            _setLastError(QStringLiteral("target-selection acknowledgement timed out"));
            continue;
        }
        // A manual selection may need a few video frames before Jetson returns
        // its TrackStatus. Keep its own retry budget independent from every
        // other manual rectangle that is in flight.
        if (delivery.attempts < 3 && nowMs >= delivery.nextAttemptAtMs) {
            (void) _sendPending(&delivery);
        }
        ++pendingTrackIt;
    }
    if (trackSelectionCountChanged) {
        emit pendingTrackSelectionCountChanged();
    }

    for (PendingDelivery* delivery :
         {&_pendingSelection, &_pendingAuthorization, &_pendingApproach, &_pendingPayloadTarget}) {
        if (!delivery->active()) {
            continue;
        }
        if (nowMs > delivery->expiresAtMs) {
            const bool authorization = delivery->authorizationDecision;
            const bool approach = delivery->approachConfirmation;
            const bool payloadTarget = delivery->payloadTargetConfirmation;
            delivery->clear();
            _setLastError(authorization   ? QStringLiteral("authorization acknowledgement timed out")
                          : approach      ? QStringLiteral("Mode-3 approach acknowledgement timed out")
                          : payloadTarget ? QStringLiteral("Mode-2 payload target acknowledgement timed out")
                                          : QStringLiteral("target-selection acknowledgement timed out"));
            continue;
        }
        // Stop retransmitting after the bounded attempt count, but keep the
        // correlation alive until TTL expiry. Jetson may need several frames
        // to initialize the tracker; that correlated TrackStatus is a valid
        // implicit acknowledgement and commonly arrives after 300 ms.
        if (delivery->attempts >= 3) {
            continue;
        }
        if (nowMs >= delivery->nextAttemptAtMs) {
            (void) _sendPending(delivery);
        }
    }
}

void MultiDetectOperatorController::_startDirectOperatorLink()
{
    if (!_directOperatorRequested || !configured() || _mavlinkSigningKey.size() != 32 || _operatorConfiguration) {
        return;
    }
    auto* configuration = new UDPConfiguration(QStringLiteral("MultiDetect Jetson metadata (ephemeral)"));
    configuration->setDynamic(true);
    configuration->setAutoConnect(false);
    configuration->setLocalPort(static_cast<quint16>(_operatorUdpLocalPort));
    configuration->addHost(_operatorUdpHost, static_cast<quint16>(_operatorUdpPort));
    _operatorConfiguration = LinkManager::instance()->addConfiguration(configuration);
    if (!_operatorConfiguration || !LinkManager::instance()->createConnectedLink(_operatorConfiguration)) {
        if (_operatorConfiguration) {
            LinkManager::instance()->removeConfiguration(_operatorConfiguration.get());
            _operatorConfiguration.reset();
        }
        _setLastError(QStringLiteral("failed to create the direct Jetson operator metadata link"));
        _setLinkState(QStringLiteral("NOT_CONFIGURED"));
        return;
    }

    LinkInterface* const link = _operatorConfiguration->link();
    SigningController* const signing = link ? link->signing() : nullptr;
    if (!link || !signing ||
        !signing->initSigningImmediate(QByteArrayView(_mavlinkSigningKey),
                                       MAVLinkSigning::UnsignedAcceptancePolicy::Strict,
                                       QStringLiteral("multidetect-direct-operator"))) {
        LinkManager::instance()->removeConfiguration(_operatorConfiguration.get());
        _operatorConfiguration.reset();
        _setLastError(QStringLiteral("failed to enable MAVLink signing on the direct operator metadata link"));
        _setLinkState(QStringLiteral("NOT_CONFIGURED"));
        return;
    }

    _setLastError({});
    _setLinkState(QStringLiteral("DIRECT_SIGNED_METADATA_READY"));
    qCInfo(MultiDetectOperatorLog).nospace() << "direct signed operator metadata ready host=" << _operatorUdpHost
                                             << " port=" << _operatorUdpPort << " local_port=" << _operatorUdpLocalPort;
}

bool MultiDetectOperatorController::_sendPayload(const QByteArray& payload)
{
    SharedLinkInterfacePtr link;
    Vehicle* vehicle = nullptr;
    if (_directOperatorRequested) {
        if (!_operatorConfiguration || !_operatorConfiguration->link()) {
            _setLastError(QStringLiteral("the direct Jetson operator metadata link is unavailable"));
            return false;
        }
        link = LinkManager::instance()->sharedLinkInterfacePointerForLink(_operatorConfiguration->link());
    } else {
        vehicle = MultiVehicleManager::instance()->activeVehicle();
        if (!vehicle) {
            _setLastError(QStringLiteral("no active vehicle link is available for operator metadata"));
            return false;
        }
        link = vehicle->vehicleLinkManager()->primaryLink().lock();
    }
    if (!link || !link->isConnected() || !link->mavlinkChannelIsSet()) {
        _setLastError(_directOperatorRequested
                          ? QStringLiteral("the direct Jetson operator metadata link is not connected")
                          : QStringLiteral("the active vehicle primary link is unavailable"));
        return false;
    }
    if (MAVLinkProtocol::instance()->getSystemId() != _localSystemId) {
        _setLastError(QStringLiteral("QGC MAVLink system ID does not match operator-link GCS endpoint"));
        return false;
    }
    if (!link->signing() || !link->signing()->isEnabled()) {
        _setLastError(QStringLiteral("MAVLink signing must be enabled before sending operator metadata"));
        return false;
    }
    if (payload.isEmpty() || payload.size() > MultiDetectOperatorProtocol::kMaximumPayloadBytes) {
        _setLastError(QStringLiteral("operator metadata payload is invalid"));
        return false;
    }

    std::array<uint8_t, MultiDetectOperatorProtocol::kMaximumPayloadBytes> tunnelPayload{};
    std::memcpy(tunnelPayload.data(), payload.constData(), static_cast<size_t>(payload.size()));
    mavlink_message_t message{};
    mavlink_msg_tunnel_pack_chan(
        static_cast<uint8_t>(_localSystemId), static_cast<uint8_t>(_localComponentId), link->mavlinkChannel(), &message,
        static_cast<uint8_t>(_remoteSystemId), static_cast<uint8_t>(_remoteComponentId),
        MultiDetectOperatorProtocol::kTunnelPayloadType, static_cast<uint8_t>(payload.size()), tunnelPayload.data());
    if (_directOperatorRequested) {
        link->sendMessageThreadSafe(message);
        return true;
    }
    return vehicle->sendMessageOnLinkThreadSafe(link.get(), message);
}

bool MultiDetectOperatorController::_sendDirectHeartbeat()
{
    if (!_operatorConfiguration || !_operatorConfiguration->link()) {
        return false;
    }
    const SharedLinkInterfacePtr link =
        LinkManager::instance()->sharedLinkInterfacePointerForLink(_operatorConfiguration->link());
    if (!link || !link->isConnected() || !link->mavlinkChannelIsSet() || !link->signing() ||
        !link->signing()->isEnabled()) {
        return false;
    }

    // Metadata-path keepalive only. Jetson authenticates the signed MAVLink
    // frame, learns the current UDP return endpoint, and deliberately ignores
    // the unrelated HEARTBEAT payload.
    mavlink_message_t message{};
    mavlink_msg_heartbeat_pack_chan(static_cast<uint8_t>(_localSystemId), static_cast<uint8_t>(_localComponentId),
                                    link->mavlinkChannel(), &message, MAV_TYPE_GCS, MAV_AUTOPILOT_INVALID, 0, 0,
                                    MAV_STATE_ACTIVE);
    link->sendMessageThreadSafe(message);
    return true;
}

bool MultiDetectOperatorController::_sendPending(PendingDelivery* delivery)
{
    if (!delivery || !delivery->active()) {
        return false;
    }
    const quint64 nowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
    if (nowMs > delivery->expiresAtMs || delivery->attempts >= 3) {
        return false;
    }
    const bool sent = _sendPayload(delivery->payload);
    if (sent) {
        ++delivery->attempts;
        delivery->nextAttemptAtMs = nowMs + 250;
        _setLastError({});
    } else {
        delivery->nextAttemptAtMs = nowMs + 250;
    }
    return sent;
}

void MultiDetectOperatorController::_reject(const QString& reason)
{
    ++_rejectedPackets;
    emit countersChanged();
    _setLastError(reason);
    qCWarning(MultiDetectOperatorLog) << "rejected metadata:" << reason;
    if (_linkState != QStringLiteral("AUTHENTICATED")) {
        _setLinkState(QStringLiteral("REJECTED"));
    }
}

void MultiDetectOperatorController::_setLastError(const QString& error)
{
    if (_lastError == error) {
        return;
    }
    _lastError = error;
    emit lastErrorChanged();
}

void MultiDetectOperatorController::_setLinkState(const QString& state)
{
    if (_linkState == state) {
        return;
    }
    _linkState = state;
    emit linkStateChanged();
}

bool MultiDetectOperatorController::_isNewStatus(MultiDetectOperatorProtocol::MessageType type, quint32 sequence,
                                                 quint64 receivedAtMs)
{
    constexpr quint64 kStatusEpochResetAfterMs = 1500;
    const int key = static_cast<int>(type);
    const auto existing = _lastStatusSequence.constFind(key);
    if (existing != _lastStatusSequence.constEnd() && static_cast<qint32>(sequence - existing.value()) <= 0) {
        const quint64 lastAcceptedAtMs = _lastStatusAcceptedAtMs.value(key, receivedAtMs);
        if (receivedAtMs < lastAcceptedAtMs || receivedAtMs - lastAcceptedAtMs < kStatusEpochResetAfterMs) {
            return false;
        }
        qCInfo(MultiDetectOperatorLog).nospace()
            << "resetting status sequence epoch type=" << key << " previous=" << existing.value()
            << " incoming=" << sequence << " inactive_ms=" << (receivedAtMs - lastAcceptedAtMs);
    }
    _lastStatusSequence.insert(key, sequence);
    _lastStatusAcceptedAtMs.insert(key, receivedAtMs);
    return true;
}

quint64 MultiDetectOperatorController::_operatorWireNowMs(quint64 localNowMs) const
{
    if (!_remoteWireClockValid) {
        return localNowMs;
    }
    if (localNowMs <= _remoteWireObservedAtLocalMs) {
        return _remoteWireTimestampMs;
    }
    const quint64 elapsedMs = localNowMs - _remoteWireObservedAtLocalMs;
    if (_remoteWireTimestampMs > std::numeric_limits<quint64>::max() - elapsedMs) {
        return std::numeric_limits<quint64>::max();
    }
    return _remoteWireTimestampMs + elapsedMs;
}

void MultiDetectOperatorController::_observeRemoteWireTime(quint64 remoteSentAtMs, quint64 localReceivedAtMs)
{
    if (remoteSentAtMs == 0) {
        return;
    }
    const bool observationStale = _remoteWireClockValid && localReceivedAtMs >= _remoteWireObservedAtLocalMs &&
                                  localReceivedAtMs - _remoteWireObservedAtLocalMs > 3000;
    if (!_remoteWireClockValid || observationStale || remoteSentAtMs >= _remoteWireTimestampMs) {
        _remoteWireTimestampMs = remoteSentAtMs;
        _remoteWireObservedAtLocalMs = localReceivedAtMs;
        _remoteWireClockValid = true;
    }
}

quint64 MultiDetectOperatorController::_unsignedValue(const QVariantMap& fields, const QString& name, bool* ok)
{
    return fields.value(name).toString().toULongLong(ok);
}
