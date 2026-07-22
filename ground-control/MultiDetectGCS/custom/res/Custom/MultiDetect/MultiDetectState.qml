pragma Singleton

import QGroundControl
import QtCore

import QtQuick

QtObject {
    id: root

    readonly property var _activeVehicle: QGroundControl.multiVehicleManager.activeVehicle
    readonly property bool vehicleArmed: _activeVehicle !== null && _activeVehicle.armed === true
    readonly property bool missionConfigurationLocked: vehicleArmed
    property bool _missionSafetyAllowed: false

    // Production metadata only. No API in this singleton can write flight
    // controls, upload a mission, drive an actuator, or release a payload.
    readonly property var _operator: (typeof multiDetectOperator !== "undefined") ? multiDetectOperator : null
    property bool _ruleSafetyAllowed: false
    property string activePayloadSlot: ""
    property string authorizationBinding: ""
    property bool authorizationChallengeActive: false
    property int authorizationExpiresInS: 0
    property string authorizationState: "NONE"
    property Timer authorizationTimer: Timer {
        interval: 1000
        repeat: true

        onTriggered: {
            root.authorizationExpiresInS -= 1;
            if (root.authorizationExpiresInS <= 0) {
                stop();
                root.authorizationChallengeActive = false;
                root.authorizationState = "NONE";
                root.missionPhase = "SEARCHING";
                root.statusMessage = qsTr("授权挑战已过期，必须重新评估安全条件");
            }
        }
    }
    property real estimatedRangeM: 0.0
    property double targetRangeUpdatedAtMs: 0
    // The target-pool wire contract carries this SLAM/visual-motion estimate
    // independently of the single-target TrackStatus packet. Keep it in the
    // state singleton so both tracked and fallback boxes render it consistently.
    property real targetSpeedMps: NaN
    property double targetSpeedUpdatedAtMs: 0
    property Timer targetMetricExpiryTimer: Timer {
        interval: 250
        repeat: true
        running: true

        onTriggered: {
            const nowMs = Date.now();
            if (root.targetRangeUpdatedAtMs > 0 && nowMs - root.targetRangeUpdatedAtMs > 1500) {
                root.targetRangeUpdatedAtMs = 0;
                root.estimatedRangeM = 0.0;
            }
            if (root.targetSpeedUpdatedAtMs > 0 && nowMs - root.targetSpeedUpdatedAtMs > 1500) {
                root.targetSpeedUpdatedAtMs = 0;
                root.targetSpeedMps = NaN;
            }
        }
    }
    property bool approachChallengeActive: false
    property int approachChallengeExpiresInS: 0
    property Timer approachChallengeTimer: Timer {
        interval: 1000
        repeat: true

        onTriggered: {
            if (!root.approachChallengeActive) {
                stop();
                return;
            }
            root.approachChallengeExpiresInS -= 1;
            if (root.approachChallengeExpiresInS <= 0) {
                stop();
                root.approachChallengeActive = false;
                root.approachChallengeExpiresInS = 0;
                root.statusMessage = qsTr("模式 3 执行挑战已过期");
            }
        }
    }
    property string approachPhase: "SEARCH"
    property var approachReasons: []
    property real approachYawErrorDeg: 0.0
    property real approachPitchErrorDeg: 0.0
    property real approachYawAdviceDeg: 0.0
    property real approachPitchAdviceDeg: 0.0
    property real approachBankAdviceDeg: 0.0
    property real approachClimbAdviceDeg: 0.0
    property real approachGroundRangeM: -1.0
    property bool approachStatusFresh: false
    property bool approachFlightControlEnabled: false
    property bool approachAimControlActive: false
    property bool approachPilotInputCancelled: false
    property bool mode3ExecutionLatched: false
    // Keep the execution banner static and use one audio cue per session.
    property bool _mode3ExecutionCuePlayed: false
    property bool payloadTargetChallengeActive: false
    property int payloadTargetChallengeExpiresInS: 0
    property string payloadTargetEligibility: "UNAVAILABLE"
    property bool payloadTargetAimpointPresent: false
    property bool payloadTargetConfirmationAccepted: false
    property bool fireAlert: false
    readonly property bool fixedWingAimControlEnabled: approachStatusFresh &&
                                                         approachFlightControlEnabled
    readonly property bool fixedWingAimActive: missionMode === "OBSERVE" &&
                                                 approachStatusFresh &&
                                                 approachAimControlActive &&
                                                 !approachPilotInputCancelled
    readonly property bool mode3AimUiActive: missionMode === "OBSERVE" &&
                                              !approachPilotInputCancelled &&
                                              (mode3ExecutionLatched || fixedWingAimActive)
    property real jetsonFps: 0.0
    property real jetsonTemperatureC: 0.0
    readonly property string metadataLinkState: _operator !== null ? _operator.linkState : "UNAVAILABLE"
    readonly property real trackingMetadataRateHz: _operator !== null ? _operator.trackingMetadataRateHz : 0.0
    // Candidate/TRK/LCK boxes are received through paged target-pool snapshots.
    // Keep their measured cadence separate from the single-target TrackStatus
    // cadence so the video overlay can choose a stable visual filter in DET
    // as well as in TRK and LCK.
    readonly property real targetPoolMetadataRateHz: _operator !== null ? _operator.targetPoolMetadataRateHz : 0.0
    readonly property bool depthMapAvailable: _operator !== null && _operator.depthMapAvailable
    readonly property string depthMapDataUrl: depthMapAvailable ? _operator.depthMapDataUrl : ""
    readonly property int depthGridWidth: depthMapAvailable ? _operator.depthGridWidth : 0
    readonly property int depthGridHeight: depthMapAvailable ? _operator.depthGridHeight : 0
    readonly property real depthMinimumM: depthMapAvailable ? _operator.depthMinimumM : 0.0
    readonly property real depthMaximumM: depthMapAvailable ? _operator.depthMaximumM : 0.0
    readonly property real depthMapAgeMs: depthMapAvailable ? _operator.depthMapAgeMs : -1
    property string missionMode: "PATROL"
    property string missionPhase: "SEARCHING"
    property bool manualReleaseRequestLatched: false
    property string interactionState: "DET"
    property string confirmedPrimaryLockTargetId: ""
    // A paged target-pool can contain one last LCK snapshot after DEMOTE_TRK.
    // Do not resurrect that stale lock in the UI while its TRK update is due.
    property string demotedPrimaryLockTargetId: ""
    // Announce each authoritative primary lock once. The target-pool can emit
    // the same confirmed lock on every metadata refresh, so this guard stays
    // local to the confirmed target identity instead of the optimistic UI state.
    property string _lastLockedCueTargetId: ""
    readonly property real lockMinimumConfidence: 0.35
    readonly property real lockMinimumTrackingQuality: 0.45
    readonly property bool hasConfirmedPrimaryLock: targetId !== "" && targetBoxValid &&
                                                     confirmedPrimaryLockTargetId === targetId
    readonly property bool lckActionable: interactionState === "LCK" && targetBoxValid &&
                                           hasConfirmedPrimaryLock &&
                                           !(missionMode === "OBSERVE" && approachPilotInputCancelled)
    readonly property bool executionActionable: vehicleArmed && lckActionable
    readonly property bool operatorConfigured: _operator !== null && _operator.configured

    signal lckCueRequested()
    signal mode3ExecutionCueRequested()

    function depthAtNormalized(x, y) {
        if (_operator === null || !depthMapAvailable)
            return NaN;
        return _operator.depthAtNormalized(x, y);
    }

    function _requestLckCue(targetIdToAnnounce) {
        const normalizedTargetId = String(targetIdToAnnounce || "");
        if (normalizedTargetId === "" || normalizedTargetId === _lastLockedCueTargetId)
            return;
        _lastLockedCueTargetId = normalizedTargetId;
        lckCueRequested();
    }

    function _requestMode3ExecutionCue() {
        if (_mode3ExecutionCuePlayed)
            return;
        _mode3ExecutionCuePlayed = true;
        mode3ExecutionCueRequested();
    }

    function _refreshApproachChallenge() {
        if (_operator === null) {
            approachChallengeActive = false;
            approachChallengeExpiresInS = 0;
            approachChallengeTimer.stop();
            return;
        }
        const challenge = _operator.approachChallenge;
        if (!vehicleArmed || challenge.pending === undefined || missionMode !== "OBSERVE" ||
                !challenge.boundToCurrentSelection) {
            approachChallengeActive = false;
            approachChallengeExpiresInS = 0;
            approachChallengeTimer.stop();
            return;
        }
        approachChallengeActive = challenge.pending;
        approachChallengeExpiresInS = challenge.expiresInS;
        if (challenge.pending) {
            approachChallengeTimer.restart();
            statusMessage = qsTr("模式 3 待确认");
        } else {
            approachChallengeTimer.stop();
        }
    }

    function _refreshPayloadTargetChallenge() {
        if (_operator === null) {
            payloadTargetChallengeActive = false;
            payloadTargetChallengeExpiresInS = 0;
            return;
        }
        const challenge = _operator.payloadTargetChallenge;
        if (!vehicleArmed || challenge.pending === undefined || missionMode !== "PAYLOAD" ||
                !challenge.boundToCurrentSelection) {
            payloadTargetChallengeActive = false;
            payloadTargetChallengeExpiresInS = 0;
            return;
        }
        payloadTargetChallengeActive = challenge.pending;
        payloadTargetChallengeExpiresInS = challenge.expiresInS;
        if (challenge.pending)
            statusMessage = qsTr("模式 2 待确认");
    }

    onMode3AimUiActiveChanged: {
        if (!mode3AimUiActive)
            _mode3ExecutionCuePlayed = false;
    }

    onConfirmedPrimaryLockTargetIdChanged: {
        if (confirmedPrimaryLockTargetId === "")
            _lastLockedCueTargetId = "";
    }

    property Connections operatorConnections: Connections {
        function onApproachAcknowledged(acknowledgement) {
            if (!acknowledgement.correlated)
                return;
            root.approachChallengeActive = false;
            root.approachChallengeTimer.stop();
            if (acknowledgement.accepted) {
                root.mode3ExecutionLatched = true;
                root.interactionState = "TGT";
                root.approachPilotInputCancelled = false;
                root.statusMessage = qsTr("模式 3 已确认");
                root._requestMode3ExecutionCue();
            } else {
                root.mode3ExecutionLatched = false;
                root.interactionState = "LCK";
                root.statusMessage = qsTr("Jetson 拒绝模式 3 确认，原因码：") + acknowledgement.reason;
            }
        }

        function onPayloadTargetAcknowledged(acknowledgement) {
            if (!acknowledgement.correlated)
                return;
            root.payloadTargetChallengeActive = false;
            if (acknowledgement.accepted) {
                root.interactionState = "TGT";
                root.statusMessage = qsTr("Jetson 已确认模式 2 连续滑动；等待目标安全规则与授权挑战");
            } else {
                root.statusMessage = qsTr("Jetson 拒绝模式 2 目标确认，原因码：") + acknowledgement.reason;
            }
        }

        function onAuthorizationAcknowledged(acknowledgement) {
            if (!acknowledgement.correlated)
                return;
            root.authorizationChallengeActive = false;
            root.authorizationTimer.stop();
            if (!acknowledgement.accepted) {
                root.authorizationState = "NONE";
                root.statusMessage = qsTr("Jetson 拒绝授权决定，原因码：") + acknowledgement.reason;
            } else if (acknowledgement.decision === "DENY") {
                root.authorizationState = "NONE";
                root.missionPhase = "SEARCHING";
                root.statusMessage = qsTr("Jetson 已确认操作员拒绝决定");
            } else {
                root.authorizationState = "PENDING";
                root.statusMessage = qsTr("Jetson 已确认批准决定；等待任务状态，物理输出仍锁定");
            }
        }

        function onAuthorizationChallengeReceived(challenge) {
            if (root.missionMode !== "PAYLOAD") {
                root.authorizationChallengeActive = false;
                root.authorizationState = "NONE";
                root.statusMessage = qsTr("巡检模式收到载荷授权挑战，已保持禁止");
                return;
            }
            root.authorizationChallengeActive = challenge.pending;
            root.authorizationExpiresInS = challenge.expiresInS;
            root.authorizationBinding = challenge.targetToken + " / " + challenge.payloadSlotToken + " / rev-" + challenge.targetRevision;
            root.authorizationState = challenge.pending ? "PENDING" : "NONE";
            if (challenge.pending) {
                root.missionPhase = "AWAITING_AUTHORIZATION";
                root.authorizationTimer.restart();
                root.statusMessage = root.safetyAllowed ? qsTr("安全规则满足，等待人工授权") : qsTr("授权挑战已收到，但安全规则尚未全部满足");
            } else {
                root.authorizationTimer.stop();
            }
        }

        function onApproachChallengeChanged() {
            root._refreshApproachChallenge();
        }

        function onApproachStatusChanged() {
            const status = root._operator.approachStatus;
            if (status.phase === undefined) {
                root._resetApproachStatus();
                return;
            }
            root.approachPhase = status.phase;
            root.approachReasons = status.reasons;
            root.approachYawErrorDeg = Number.isFinite(status.yawErrorDeg) ? status.yawErrorDeg : 0.0;
            root.approachPitchErrorDeg = Number.isFinite(status.pitchErrorDeg) ? status.pitchErrorDeg : 0.0;
            root.approachYawAdviceDeg = Number.isFinite(status.yawAdviceDeg) ? status.yawAdviceDeg : 0.0;
            root.approachPitchAdviceDeg = Number.isFinite(status.pitchAdviceDeg) ? status.pitchAdviceDeg : 0.0;
            root.approachBankAdviceDeg = Number.isFinite(status.bankAdviceDeg) ? status.bankAdviceDeg : 0.0;
            root.approachClimbAdviceDeg = Number.isFinite(status.climbPitchAdviceDeg) ? status.climbPitchAdviceDeg : 0.0;
            root.approachGroundRangeM = Number.isFinite(status.groundRangeM) ? status.groundRangeM : -1.0;
            root.approachStatusFresh = true;
            root.approachFlightControlEnabled = status.flightControlEnabled === true;
            root.approachAimControlActive = status.aimControlActive === true;
            root.approachPilotInputCancelled = status.pilotInputCancelled === true;
            if (root.missionMode === "OBSERVE") {
                root.missionPhase = status.phase;
                if (root.approachPilotInputCancelled) {
                    root.mode3ExecutionLatched = false;
                    root.interactionState = "LCK";
                    root.statusMessage = qsTr("遥控输入已接管 · 模式 3 瞄准已取消");
                } else if (root.approachAimControlActive) {
                    root.mode3ExecutionLatched = true;
                    root.interactionState = "TGT";
                    root.statusMessage = qsTr("模式 3 正在瞄准");
                    root._requestMode3ExecutionCue();
                } else if (status.phase === "ABORT") {
                    // A status packet can describe an abort before the accepted
                    // confirmation acknowledgement or arrive after a short link
                    // gap.  Only an explicit cancel, target change, mode change,
                    // rejection, or pilot override may dismiss the execution UI.
                    root.interactionState = root.mode3ExecutionLatched ? "TGT" : "LCK";
                    root.statusMessage = root.mode3ExecutionLatched ?
                                             qsTr("模式 3 瞄准状态异常 · 请明确取消") :
                                             qsTr("模式 3 已中止");
                } else if (status.phase === "AIMING" || status.phase === "CENTERING")
                    root.statusMessage = status.flightControlEnabled ? qsTr("模式 3 瞄准") : qsTr("模式 3 等待控制");
            }
        }

        function onPayloadTargetChallengeChanged() {
            root._refreshPayloadTargetChallenge();
        }

        function onPayloadTargetStatusChanged() {
            const status = root._operator.payloadTargetStatus;
            if (status.eligibility === undefined) {
                root._resetPayloadTargetStatus();
                return;
            }
            root.payloadTargetEligibility = status.eligibility;
            root.payloadTargetAimpointPresent = status.aimpointPresent;
            root.payloadTargetConfirmationAccepted = status.confirmationAccepted;
            if (root.missionMode !== "PAYLOAD")
                return;
            if (status.confirmationAccepted) {
                root.statusMessage = qsTr("模式 2 目标已人工确认；等待目标安全规则和独立授权");
            } else if (status.confirmationPending) {
                root.statusMessage = qsTr("模式 2 候选目标有效；请连续滑动确认当前选择与实时目标绑定");
            } else if (status.eligibility === "TARGET_NOT_PAYLOAD_ELIGIBLE") {
                root.statusMessage = qsTr("该目标可跟踪，但当前 Jetson 目标解析器尚未允许进入投放流程");
            } else if (status.eligibility === "FIRE_EVIDENCE_UNAVAILABLE") {
                root.statusMessage = qsTr("未找到可与当前选择唯一关联的视觉证据，保持禁止");
            } else if (status.eligibility === "FIRE_ASSOCIATION_AMBIGUOUS") {
                root.statusMessage = qsTr("存在多个相似候选，无法唯一绑定，保持禁止");
            } else if (status.eligibility === "TARGET_NOT_STABLY_TRACKED") {
                root.statusMessage = qsTr("目标跟踪不稳定或受遮挡，等待重捕获");
            }
        }

        function onTargetPoolChanged() {
            root.targetPoolEntries = root._operator.targetPool;
            root._reconcilePendingTargetActions();
            root._reconcileDemotedPrimaryLock(root.targetPoolEntries);
            root._observeConfirmedPrimaryLock(root.targetPoolEntries);
            root._reconcileCurrentTargetFromPool();
            root._updateInteractionState();
            root._tryReconnectPromoteLock();
        }

        function onSceneContextChanged() {
            root.sceneContextRegions = root._operator.sceneContextRegions;
            root.sceneContextState = root._operator.sceneContextState === "" ? "UNAVAILABLE" : root._operator.sceneContextState;
        }

        function onLastErrorChanged() {
            if (root._operator.lastError !== "") {
                root.statusMessage = qsTr("目标元数据链路：") + root._operator.lastError;
            }
        }

        function onLinkStateChanged() {
            if (root._operator.linkState === "STALE") {
                root.reconnectSelectionPending = root.targetBoxValid &&
                        root.hasLocalSelectionSession && !root.hasPendingTargetSelection;
                root.reconnectPromoteLock = root.reconnectSelectionPending &&
                        (root.interactionState === "LCK" || root.interactionState === "TGT" ||
                         root.confirmedPrimaryLockTargetId !== "");
                root.reconnectSelectionTimer.stop();
                root._resetSafetyAndAuthorization();
                root._resetRangeStatus();
                root._resetReleaseStatus();
                root._resetApproachStatus();
                root._resetPayloadTargetStatus();
                root.confirmedPrimaryLockTargetId = "";
                root.targetPoolEntries = [];
                root._resetSceneContext();
                root.statusMessage = qsTr("Jetson 目标元数据超时；安全与授权状态已失效");
            } else if (root._operator.linkState === "AUTHENTICATED" &&
                       root.reconnectSelectionPending) {
                root.reconnectSelectionTimer.restart();
                root.statusMessage = qsTr("Jetson 链路已恢复；正在重建目标绑定");
            } else if (root._operator.linkState === "REJECTED") {
                root.statusMessage = qsTr("目标元数据被拒绝：") + root._operator.lastError;
            }
        }

        function onMissionStatusReceived(status) {
            root.missionPhase = status.phase;
            root.releaseWindow = status.releaseWindow;
            root.authorizationState = status.authorizationState;
            root.remainingPayloadCount = status.remainingPayloadCount;
            root.totalPayloadCount = status.totalPayloadCount;
            root.activePayloadSlot = status.activePayloadSlot;
            root._missionSafetyAllowed = status.safetyKnown && status.safetyAllowed;
            root.safetyAllowed = root._missionSafetyAllowed && root._ruleSafetyAllowed;
            // MissionStatus is mission-wide telemetry and can legitimately lag
            // behind a just-selected TRK/LCK target.  The target pool owns an
            // active local selection, so never let an old mission packet replace
            // its target id or target-bound measurements.
            const missionTargetId = String(status.targetId || "");
            const canAdoptMissionTarget = missionTargetId !== "" &&
                    root.targetId === "" && !root.targetBoxValid &&
                    !root.hasPendingTargetSelection &&
                    !root.hasLocalSelectionSession &&
                    !root.hasOperatorTrackedTargets;
            const missionTargetMatchesCurrent = missionTargetId !== "" &&
                    (missionTargetId === root.targetId || canAdoptMissionTarget);
            if (canAdoptMissionTarget) {
                root.targetId = missionTargetId;
                root.targetSpeedMps = NaN;
                root.targetSpeedUpdatedAtMs = 0;
                root.relativeBearingAvailable = false;
                root.relativeBearingDeg = 0.0;
                root.estimatedRangeM = 0.0;
                root.targetRangeUpdatedAtMs = 0;
            }
            if (missionTargetMatchesCurrent) {
                if (status.targetConfidence >= 0.0)
                    root.targetConfidence = status.targetConfidence;
                root.relativeBearingAvailable = Number.isFinite(status.relativeBearingDeg);
                root.relativeBearingDeg = root.relativeBearingAvailable ?
                                            status.relativeBearingDeg : 0.0;
                if (Number.isFinite(status.estimatedRangeM)) {
                    root.estimatedRangeM = status.estimatedRangeM;
                    root.targetRangeUpdatedAtMs = Date.now();
                }
            }
        }

        function onPatrolStatusReceived(status) {
            root.patrolPhase = status.phase;
            root.totalTrackCount = status.totalTrackCount;
            root.lockedTrackCount = status.lockedTrackCount;
            root.patrolSourceAgeMs = status.sourceAgeMs;
            root.returnObserveDirection = status.returnDirection;
            root.returnObserveValidity = status.returnValidity;
            root.returnEvidenceAgeS = status.returnEvidenceAgeS;
            root.estimatedMinimumTurnRadiusM = status.estimatedMinimumTurnRadiusM;
            if (root.missionMode !== "PATROL")
                return;

            // A PATROL snapshot can still describe the previous primary target
            // while a newly drawn rectangle is in flight to Jetson. Keep the
            // optimistic rectangle until a correlated TrackStatus confirms or
            // rejects the new command instead of erasing it on the next patrol
            // telemetry tick.
            if (root.hasPendingTargetSelection)
                return;

            root.missionPhase = status.phase;
            if (status.phase === "LOST" && status.returnDirection !== "") {
                root.statusMessage = qsTr("目标已丢失；返回观察仅为建议，需操作员确认并先完成 SITL 验证");
            } else if (status.phase === "REACQUIRING" || status.phase === "OCCLUDED") {
                root.statusMessage = qsTr("目标受遮挡，Jetson 正在扩大范围重捕获");
            } else if (status.phase === "PATROL") {
                root.statusMessage = qsTr("自动巡检中；持续检测环境与维护后台目标池");
            } else {
                root.statusMessage = qsTr("巡检目标池已更新：") + status.lockedTrackCount + qsTr(" 个锁定 / ") + status.totalTrackCount + qsTr(" 个轨迹");
            }
        }

        function onPatrolStatusChanged() {
            if (root._operator.patrolStatus.phase === undefined)
                root._resetPatrolStatus();
        }

        function onRangeStatusChanged() {
            if (root._operator.rangeStatus.validity === undefined)
                root._resetRangeStatus();
        }

        function onTargetGeolocationStatusChanged() {
            if (root._operator.targetGeolocationStatus.available === undefined)
                root._resetTargetGeolocationStatus();
        }

        function onRangeStatusReceived(status) {
            root.rangeTargetId = status.targetId;
            root.rangeCalibrationId = status.calibrationId;
            root.rangeValidity = status.validity;
            root.rangeSlantM = Number.isFinite(status.slantRangeM) ? status.slantRangeM : -1.0;
            root.rangeGroundM = Number.isFinite(status.groundRangeM) ? status.groundRangeM : -1.0;
            root.rangeSlantLowM = Number.isFinite(status.slantRangeLowM) ? status.slantRangeLowM : -1.0;
            root.rangeSlantHighM = Number.isFinite(status.slantRangeHighM) ? status.slantRangeHighM : -1.0;
            root.rangeRelativeBearingDeg = Number.isFinite(status.relativeBearingDeg) ? status.relativeBearingDeg : 0.0;
            root.rangeBearingAvailable = Number.isFinite(status.relativeBearingDeg);
            root.rangeSourceAgeMs = status.sourceAgeMs;
            root.rangeDataFreshnessS = Number.isFinite(status.dataFreshnessS) ? status.dataFreshnessS : -1.0;
            root.rangeSensorConsistency = status.sensorConsistency;
            root.rangeReasons = status.reasons;
            root.rangeSources = status.sources;
            root.rangeSourceContributions = status.sourceContributions || [];
            root.rangeFusionProfile = String(status.fusionProfile || "outdoor-multimodal-v1");
            root.rangeVehicleProfile = String(status.vehicleProfile || "auto");
            root.rangeNavigationState = String(status.navigationState || "unknown");
            root.rangeMotionRegime = String(status.motionRegime || "unknown");
            // Manual TrackStatus uses a short-lived manual command identity
            // before the unified target pool resolves the same rectangle to its
            // stable target id.  RangeStatus is emitted from that stable pool.
            // Adopt only the matching operator-owned pool entry, never a
            // merely nearby DET candidate, so an LCK keeps its metric value
            // through the association handoff.
            const matchingTrackedEntry = root._operatorTrackedEntry(root.rangeTargetId);
            const currentIsManual = root.targetId === "" ||
                    String(root.targetId).indexOf("manual-") === 0;
            const rangeBelongsToCurrentTarget = root.rangeTargetId === root.targetId ||
                    (matchingTrackedEntry !== undefined &&
                     (currentIsManual ||
                      root.confirmedPrimaryLockTargetId === root.rangeTargetId));
            if (rangeBelongsToCurrentTarget && matchingTrackedEntry !== undefined &&
                    root.targetId !== root.rangeTargetId) {
                root._adoptTargetPoolEntry(matchingTrackedEntry);
            }
            if (root.rangeSlantM >= 0.0 && rangeBelongsToCurrentTarget) {
                root.estimatedRangeM = root.rangeSlantM;
                root.targetRangeUpdatedAtMs = Date.now();
            }
        }

        function onTargetGeolocationStatusReceived(status) {
            root.targetGeolocationTargetId = String(status.targetId || "");
            root.targetGeolocationSourceFrameId = String(status.sourceFrameId || "");
            root.targetGeolocationAvailable = status.available === true;
            root.targetGeolocationReason = String(status.reason || "");
            root.targetGeolocationSourceAgeMs = Number(status.sourceAgeMs || 0);
            root.targetLatitudeDeg = Number.isFinite(status.latitudeDeg) ? status.latitudeDeg : NaN;
            root.targetLongitudeDeg = Number.isFinite(status.longitudeDeg) ? status.longitudeDeg : NaN;
            root.targetHorizontalSigmaM = Number.isFinite(status.horizontalSigmaM) ?
                    status.horizontalSigmaM : -1.0;
        }

        function onReleaseStatusChanged() {
            if (root._operator.releaseStatus.timingStatus === undefined)
                root._resetReleaseStatus();
        }

        function onReleaseStatusReceived(status) {
            root.releaseTimingStatus = status.timingStatus;
            root.releaseTargetId = status.targetId;
            root.releaseCalibrationId = status.calibrationId;
            root.releaseRangeBindingPresent = status.rangeBindingPresent;
            root.releaseImpactAvailable = status.impactAvailable;
            root.releaseEllipseAvailable = status.ellipseAvailable;
            root.releaseRangeIntervalAvailable = status.rangeIntervalAvailable;
            root.releaseReasons = status.reasons;
            root.releaseTargetNorthM = Number.isFinite(status.targetNorthOffsetM) ? status.targetNorthOffsetM : -1.0;
            root.releaseTargetEastM = Number.isFinite(status.targetEastOffsetM) ? status.targetEastOffsetM : -1.0;
            root.releaseImpactNorthM = Number.isFinite(status.impactNorthOffsetM) ? status.impactNorthOffsetM : -1.0;
            root.releaseImpactEastM = Number.isFinite(status.impactEastOffsetM) ? status.impactEastOffsetM : -1.0;
            root.releaseAlongErrorM = Number.isFinite(status.alongTrackErrorM) ? status.alongTrackErrorM : -1.0;
            root.releaseCrossErrorM = Number.isFinite(status.crossTrackErrorM) ? status.crossTrackErrorM : -1.0;
            root.releaseEllipseMajorM = Number.isFinite(status.errorEllipseMajorM) ? status.errorEllipseMajorM : -1.0;
            root.releaseEllipseMinorM = Number.isFinite(status.errorEllipseMinorM) ? status.errorEllipseMinorM : -1.0;
            root.releaseEllipseOrientationDeg = Number.isFinite(status.errorEllipseOrientationDeg) ? status.errorEllipseOrientationDeg : 0.0;
            root.releaseGroundRangeM = Number.isFinite(status.estimatedGroundRangeM) ? status.estimatedGroundRangeM : -1.0;
            root.releaseGroundRangeLowM = Number.isFinite(status.groundRangeLowM) ? status.groundRangeLowM : -1.0;
            root.releaseGroundRangeHighM = Number.isFinite(status.groundRangeHighM) ? status.groundRangeHighM : -1.0;
            root.releaseDescentTimeS = Number.isFinite(status.payloadDescentTimeS) ? status.payloadDescentTimeS : -1.0;
            root.releaseLeadDistanceM = Number.isFinite(status.releaseLeadDistanceM) ? status.releaseLeadDistanceM : -1.0;
            root.releaseRangeConsistency = Number.isFinite(status.rangeSensorConsistency) ? status.rangeSensorConsistency : 0.0;
            if (root.missionMode === "PAYLOAD")
                root.statusMessage = root.releaseTimingText();
        }

        function onSafetyStatusReceived(status) {
            root.safetyPassCount = status.passCount;
            root.safetyDenyCount = status.denyCount;
            root.safetyUnknownCount = status.unknownCount;
            root._ruleSafetyAllowed = status.allowed;
            root.safetyAllowed = root._missionSafetyAllowed && root._ruleSafetyAllowed;
            if (!root.safetyAllowed && root.authorizationChallengeActive) {
                root.authorizationChallengeActive = false;
                root.authorizationState = "NONE";
                root.authorizationTimer.stop();
                root.statusMessage = qsTr("安全状态变化，待处理授权挑战已撤销");
            }
        }

        function onSelectionAcknowledged(acknowledgement) {
            if (!acknowledgement.correlated)
                return;
            const acknowledgementCommandId = String(acknowledgement.commandId || "");
            const acknowledgementIsTrackedSelection =
                    root._isLocalTrackSelectionCommand(acknowledgementCommandId);
            const acknowledgementIsOlderTrk = acknowledgementIsTrackedSelection &&
                    acknowledgementCommandId !== "" &&
                    acknowledgementCommandId !== root.selectionCommandId;
            if (acknowledgement.accepted) {
                if (!acknowledgementIsOlderTrk) {
                    root.statusMessage = root.pendingSelectionAction === "CANCEL_TRK" ?
                                         qsTr("单目标 TRK 已取消") :
                                         root.pendingSelectionAction === "PROMOTE_LCK" ? "LCK" :
                                         qsTr("Jetson 已接受目标选择，等待跟踪状态");
                }
            } else if (acknowledgementIsOlderTrk) {
                // Another later manual box owns the fallback rectangle. A
                // rejection for this earlier independent TRK must not erase it.
                root._forgetLocalTrackSelectionCommand(acknowledgementCommandId);
                root.statusMessage = qsTr("Jetson 拒绝其中一个 TRK；其余目标保持");
            } else {
                const rejectedAction = root.pendingSelectionAction;
                root.selectionTrackStatusTimer.stop();
                root.selectionAwaitingTrackStatus = false;
                root.selectionCommandId = "";
                if (acknowledgementIsTrackedSelection)
                    root._forgetLocalTrackSelectionCommand(acknowledgementCommandId);
                if (rejectedAction === "CANCEL_TRK")
                    root._removePendingCancelledTarget(root.pendingCancelledTargetId);
                else if (rejectedAction === "PROMOTE_LCK")
                    root.exclusiveLockPendingTargetId = "";
                else
                    root.targetBoxValid = false;
                root.pendingSelectionAction = "";
                root.pendingCancelledTargetId = "";
                if (!root.hasOperatorTrackedTargets && !root.hasPendingTrackSelections)
                    root.hasLocalSelectionSession = false;
                root._reconcileCurrentTargetFromPool();
                root._updateInteractionState();
                root.statusMessage = qsTr("Jetson 拒绝目标选择，原因码：") + acknowledgement.reason;
            }
        }

        function onTrackStatusReceived(status) {
            const statusSelectionCommandId = String(status.selectionCommandId || "");
            const targetsCurrentSelection = root.selectionCommandId !== "" &&
                    statusSelectionCommandId === root.selectionCommandId;
            const correlatedTrackSelection =
                    root._isLocalTrackSelectionCommand(statusSelectionCommandId);
            const correlatedSelection = targetsCurrentSelection || correlatedTrackSelection;
            if (root.selectionAwaitingTrackStatus && !correlatedSelection)
                return;
            // Tracking packets from an earlier QGC process or direct client are
            // background telemetry. Only a command issued by this UI may enter TRK.
            if (!correlatedSelection)
                return;
            // A first manual box can report after a second box has become the
            // active fallback selection. It remains in the target pool and is
            // rendered there, but must never move the current focus back.
            if (!targetsCurrentSelection)
                return;
            if (root.selectionAwaitingTrackStatus && correlatedSelection &&
                    status.state === "INITIALIZING" && !status.bboxValid) {
                root.statusMessage = qsTr("Jetson 已接受目标，正在初始化跟踪器");
                return;
            }
            if (correlatedSelection) {
                root.selectionAwaitingTrackStatus = false;
                root.selectionTrackStatusTimer.stop();
            }
            const completedAction = root.pendingSelectionAction;
            if (completedAction === "CANCEL_TRK" && status.state === "CANCELLED") {
                root.selectionCommandId = "";
                root.pendingSelectionAction = "";
                root.pendingCancelledTargetId = "";
                root._reconcileCurrentTargetFromPool();
                root._updateInteractionState();
                root.statusMessage = qsTr("单目标 TRK 已取消");
                return;
            }
            root.trackingState = status.state;
            // A correlated SELECT_TRK/PROMOTE_LCK reply is the authoritative
            // association for the command just issued by this UI.  Its unified
            // target id can differ from the detector candidate that was clicked
            // (or from another already-tracked manual rectangle), so accept it
            // without allowing unrelated status packets to move the active box.
            const canAdoptCorrelatedTarget = correlatedSelection &&
                    completedAction !== "CANCEL_TRK";
            // A terminal LOST status can legitimately have targetPresent=false.
            // Keep its association separate from the visible-bbox association so
            // that a just-lost manual target clears only its own fallback box.
            const statusReferencesCurrent = status.targetId !== "" &&
                    (status.targetId === root.targetId ||
                     (canAdoptCorrelatedTarget && root.targetId === ""));
            const statusTargetsCurrent = status.targetPresent &&
                    (canAdoptCorrelatedTarget || root.targetId === "" ||
                     status.targetId === root.targetId);
            if (statusTargetsCurrent) {
                const targetChanged = root.targetId !== status.targetId;
                root.targetId = status.targetId;
                root.targetLabel = status.label;
                if (targetChanged) {
                    root.targetSpeedMps = NaN;
                    root.targetSpeedUpdatedAtMs = 0;
                    root.relativeBearingAvailable = false;
                    root.relativeBearingDeg = 0.0;
                    root.estimatedRangeM = 0.0;
                    root.targetRangeUpdatedAtMs = 0;
                }
            }
            if (statusTargetsCurrent) {
                root.targetConfidence = status.confidence >= 0.0 ? status.confidence : 0.0;
                root.trackingQuality = status.trackingQuality >= 0.0 ? status.trackingQuality : 0.0;
                if (status.bboxValid) {
                    root.targetBoxValid = true;
                    root.targetX1 = status.x1;
                    root.targetY1 = status.y1;
                    root.targetX2 = status.x2;
                    root.targetY2 = status.y2;
                }
                root.relativeBearingAvailable = Number.isFinite(status.relativeBearingDeg);
                root.relativeBearingDeg = root.relativeBearingAvailable ? status.relativeBearingDeg : 0.0;
                if (Number.isFinite(status.estimatedRangeM)) {
                    root.estimatedRangeM = status.estimatedRangeM;
                    root.targetRangeUpdatedAtMs = Date.now();
                }
                root.fireAlert = (status.state === "TRACKING" || status.state === "INITIALIZING") &&
                                 root._isFireLabel(status.label);
            }
            if (status.state === "TRACKING") {
                root.statusMessage = root.fireAlert ? qsTr("火情目标持续跟踪中") : qsTr("目标持续跟踪中");
            } else if (status.state === "INITIALIZING") {
                root.statusMessage = qsTr("Jetson 已接受目标，正在初始化跟踪器");
            } else if (status.state === "LOST") {
                root.statusMessage = qsTr("目标跟踪丢失；等待 Jetson 重捕获");
                if (statusReferencesCurrent)
                    root._clearCurrentTargetAfterLoss();
            } else if (status.state === "REJECTED") {
                root.statusMessage = qsTr("Jetson 拒绝了当前目标选择");
                root.targetBoxValid = false;
                if (completedAction === "PROMOTE_LCK")
                    root.exclusiveLockPendingTargetId = "";
            } else if (status.state === "CANCELLED") {
                root.targetBoxValid = false;
            }
            root.pendingSelectionAction = "";
            root.pendingCancelledTargetId = "";
            root._reconcileCurrentTargetFromPool();
            root._updateInteractionState();
        }

        enabled: target !== null
        target: root._operator
    }
    property Settings persistedSettings: Settings {
        property string selectedMissionMode: "PATROL"
        property int selectedRcReleaseChannel: 0
        property int depthDisplayMode: 1

        category: "MultiDetectMission"
    }
    readonly property bool physicalReleaseEnabled: false
    property Connections rcConnections: Connections {
        function onArmedChanged() {
            if (!root.vehicleArmed) {
                root.approachChallengeActive = false;
                root.approachChallengeExpiresInS = 0;
                root.approachChallengeTimer.stop();
                root.payloadTargetChallengeActive = false;
                root.payloadTargetChallengeExpiresInS = 0;
                root.mode3ExecutionLatched = false;
                root.approachAimControlActive = false;
                if (root.interactionState === "TGT")
                    root.interactionState = "LCK";
                if (root.lckActionable)
                    root.statusMessage = qsTr("LCK 已保持；ARM 后进入执行");
                return;
            }
            root._refreshApproachChallenge();
            root._refreshPayloadTargetChallenge();
            if (root.lckActionable && !root.approachChallengeActive &&
                    !root.payloadTargetChallengeActive)
                root.statusMessage = qsTr("已 ARM；等待 Jetson 生成当前 LCK 的执行挑战");
        }

        function onRcChannelsRawChanged(channelValues) {
            root.updateRcChannels(channelValues);
        }

        enabled: target !== null
        target: root._activeVehicle
    }

    // The selected RC channel is observed only. A high switch records a local
    // request state, but physical release remains compile-time disabled.
    property int rcReleaseChannel: 0
    property int rcReleasePwm: 0
    readonly property string rcReleaseState: rcReleaseChannel <= 0 ? qsTr("未配置") : !rcSignalAvailable ? qsTr("无信号") : manualReleaseRequestLatched ? qsTr("手动请求已收到（输出锁定）") : rcReleaseSwitchActive ? qsTr("开关高电平（等待回位）") : qsTr("安全")
    property bool rcReleaseSwitchActive: false
    property bool rcSignalAvailable: false
    property bool relativeBearingAvailable: false
    property real relativeBearingDeg: 0.0
    property string patrolPhase: "PATROL"
    property int patrolSourceAgeMs: 0
    property int totalTrackCount: 0
    property int lockedTrackCount: 0
    property string returnObserveDirection: ""
    property string returnObserveValidity: ""
    property real returnEvidenceAgeS: -1.0
    property real estimatedMinimumTurnRadiusM: -1.0
    property string releaseWindow: "UNAVAILABLE"
    property real releaseAlongErrorM: -1.0
    property string releaseCalibrationId: ""
    property real releaseCrossErrorM: -1.0
    property real releaseDescentTimeS: -1.0
    property real releaseEllipseMajorM: -1.0
    property real releaseEllipseMinorM: -1.0
    property real releaseEllipseOrientationDeg: 0.0
    property real releaseGroundRangeHighM: -1.0
    property real releaseGroundRangeLowM: -1.0
    property real releaseGroundRangeM: -1.0
    property real releaseImpactEastM: -1.0
    property real releaseImpactNorthM: -1.0
    property bool releaseImpactAvailable: false
    property real releaseLeadDistanceM: -1.0
    property bool releaseRangeBindingPresent: false
    property bool releaseRangeIntervalAvailable: false
    property real releaseRangeConsistency: 0.0
    property var releaseReasons: []
    property real releaseTargetEastM: -1.0
    property string releaseTargetId: ""
    property real releaseTargetNorthM: -1.0
    property string releaseTimingStatus: "UNAVAILABLE"
    property bool releaseEllipseAvailable: false
    property bool rangeBearingAvailable: false
    property string rangeCalibrationId: ""
    property real rangeDataFreshnessS: -1.0
    property real rangeGroundM: -1.0
    property string rangeFusionProfile: "outdoor-multimodal-v1"
    property string rangeVehicleProfile: "auto"
    property string rangeNavigationState: "unknown"
    property string rangeMotionRegime: "unknown"
    property var rangeReasons: []
    property real rangeRelativeBearingDeg: 0.0
    property real rangeSensorConsistency: 0.0
    property real rangeSlantHighM: -1.0
    property real rangeSlantLowM: -1.0
    property real rangeSlantM: -1.0
    property int rangeSourceAgeMs: 0
    property var rangeSourceContributions: []
    property var rangeSources: []
    property string rangeTargetId: ""
    property string rangeValidity: "UNAVAILABLE"
    property bool targetGeolocationAvailable: false
    property real targetHorizontalSigmaM: -1.0
    property real targetLatitudeDeg: NaN
    property real targetLongitudeDeg: NaN
    property string targetGeolocationReason: ""
    property int targetGeolocationSourceAgeMs: 0
    property string targetGeolocationSourceFrameId: ""
    property string targetGeolocationTargetId: ""
    property int remainingPayloadCount: 0
    property bool safetyAllowed: false
    property int safetyDenyCount: 0
    property int safetyPassCount: 0
    property int safetyUnknownCount: 19
    property string selectionCommandId: ""
    property bool selectionAwaitingTrackStatus: false
    property var localTrackSelectionCommandIds: []
    // The controller keeps a retry/correlation entry for every SELECT_TRK so
    // two quick manual rectangles cannot overwrite one another. The local
    // fallback still tracks only the latest box, while this count keeps LCK and
    // per-box cancellation disabled until all in-flight TRK commands settle.
    readonly property int pendingTrackSelectionCount: _operator !== null &&
                                                     Number.isFinite(_operator.pendingTrackSelectionCount) ?
                                                     _operator.pendingTrackSelectionCount : 0
    readonly property bool hasPendingTrackSelections: pendingTrackSelectionCount > 0
    readonly property bool hasPendingTargetSelection: selectionAwaitingTrackStatus ||
                                                      hasPendingTrackSelections
    property string pendingSelectionAction: ""
    property string pendingCancelledTargetId: ""
    property var pendingCancelledTargetIds: []
    property string exclusiveLockPendingTargetId: ""
    property bool hasLocalSelectionSession: false
    property bool reconnectSelectionPending: false
    property bool reconnectPromoteLock: false
    property Timer reconnectSelectionTimer: Timer {
        interval: 300
        repeat: false

        onTriggered: root._resumeSelectionAfterReconnect()
    }
    property bool selectionMode: false
    property int selectionSequence: 0
    property Timer selectionTrackStatusTimer: Timer {
        interval: 5000
        repeat: false

        onTriggered: {
            if (!root.selectionAwaitingTrackStatus)
                return;
            const timedOutAction = root.pendingSelectionAction;
            const timedOutCommandId = root.selectionCommandId;
            root.selectionAwaitingTrackStatus = false;
            root.selectionCommandId = "";
            if (timedOutAction === "SELECT_TRK")
                root._forgetLocalTrackSelectionCommand(timedOutCommandId);
            if (timedOutAction === "CANCEL_TRK")
                root._removePendingCancelledTarget(root.pendingCancelledTargetId);
            else if (timedOutAction === "PROMOTE_LCK")
                root.exclusiveLockPendingTargetId = "";
            else
                root.targetBoxValid = false;
            root.pendingSelectionAction = "";
            root.pendingCancelledTargetId = "";
            if (!root.hasOperatorTrackedTargets && !root.hasPendingTrackSelections)
                root.hasLocalSelectionSession = false;
            root._reconcileCurrentTargetFromPool();
            root._updateInteractionState();
            root.statusMessage = qsTr("Jetson 未返回当前框选的实时跟踪状态");
        }
    }
    property string statusMessage: operatorConfigured ? qsTr("等待 Jetson 签名目标元数据") : qsTr("Jetson 目标元数据链路未配置")
    property bool targetBoxValid: false
    property real targetConfidence: 0.0
    property string targetId: ""
    property string targetLabel: ""
    property var targetPoolEntries: []
    readonly property var selectableTargetPoolEntries: _selectableTargetPoolEntries(targetPoolEntries)
    readonly property var operatorTrackedTargetPoolEntries: _operatorTrackedTargetPoolEntries(targetPoolEntries)
    readonly property bool hasOperatorTrackedTargets: operatorTrackedTargetPoolEntries.length > 0
    property var sceneContextRegions: []
    property string sceneContextState: "UNAVAILABLE"
    property real targetX1: 0.0
    property real targetX2: 0.0
    property real targetY1: 0.0
    property real targetY2: 0.0
    property int totalPayloadCount: 0
    property real trackingQuality: 0.0
    property string trackingState: "IDLE"
    property Connections vehicleManagerConnections: Connections {
        function onActiveVehicleChanged() {
            root.rcReleasePwm = 0;
            root.rcSignalAvailable = false;
            root.rcReleaseSwitchActive = false;
            root.manualReleaseRequestLatched = false;
        }

        target: QGroundControl.multiVehicleManager
    }

    function _clamp01(value) {
        return Math.max(0.0, Math.min(1.0, value));
    }

    function _arrayContains(values, value) {
        if (values === undefined || values === null || value === "")
            return false;
        for (let index = 0; index < values.length; ++index) {
            if (values[index] === value)
                return true;
        }
        return false;
    }

    function _isLocalTrackSelectionCommand(commandId) {
        return _arrayContains(localTrackSelectionCommandIds, commandId);
    }

    function _rememberLocalTrackSelectionCommand(commandId) {
        if (commandId === "" || _isLocalTrackSelectionCommand(commandId))
            return;
        const commands = localTrackSelectionCommandIds.slice();
        // Keep enough recent command IDs to correlate delayed status/ack
        // packets for several simultaneous manual boxes without allowing this
        // UI-side history to grow across a long patrol.
        while (commands.length >= 24)
            commands.shift();
        commands.push(commandId);
        localTrackSelectionCommandIds = commands;
    }

    function _forgetLocalTrackSelectionCommand(commandId) {
        if (commandId === "")
            return;
        const remaining = [];
        for (let index = 0; index < localTrackSelectionCommandIds.length; ++index) {
            if (localTrackSelectionCommandIds[index] !== commandId)
                remaining.push(localTrackSelectionCommandIds[index]);
        }
        localTrackSelectionCommandIds = remaining;
    }

    function _hasConfirmedPrimaryLock(entries) {
        if (entries === undefined || entries === null || targetId === "")
            return false;
        for (let index = 0; index < entries.length; ++index) {
            const entry = entries[index];
            if (entry !== undefined && entry.targetId === targetId &&
                    entry.locked === true && entry.primary === true && entry.bboxValid)
                return true;
        }
        return false;
    }

    function _observeConfirmedPrimaryLock(entries) {
        if (!hasLocalSelectionSession || entries === undefined || entries === null)
            return;
        for (let index = 0; index < entries.length; ++index) {
            const entry = entries[index];
            if (entry !== undefined && entry.targetId !== "" && entry.bboxValid &&
                    entry.operatorTracked === true &&
                    !isDemotedPrimaryLockAwaitingPool(entry) &&
                    entry.locked === true && entry.primary === true) {
                // Jetson's unified target-pool identity is authoritative.  A
                // detector-backed selection may replace the optimistic UI ID
                // while keeping the same object and bounding box.
                confirmedPrimaryLockTargetId = entry.targetId;
                exclusiveLockPendingTargetId = "";
                _adoptTargetPoolEntry(entry);
                _requestLckCue(entry.targetId);
                return;
            }
        }
    }

    function isDemotedPrimaryLockAwaitingPool(entry) {
        return entry !== undefined && entry !== null &&
               demotedPrimaryLockTargetId !== "" &&
               entry.targetId === demotedPrimaryLockTargetId &&
               entry.locked === true && entry.primary === true;
    }

    function _reconcileDemotedPrimaryLock(entries) {
        if (demotedPrimaryLockTargetId === "")
            return;
        for (let index = 0; index < entries.length; ++index) {
            const entry = entries[index];
            if (entry === undefined || entry.targetId !== demotedPrimaryLockTargetId)
                continue;
            if (!(entry.locked === true && entry.primary === true))
                demotedPrimaryLockTargetId = "";
            return;
        }
        demotedPrimaryLockTargetId = "";
    }

    function _resumeSelectionAfterReconnect() {
        if (!reconnectSelectionPending || _operator === null || !operatorConfigured ||
                !targetBoxValid || hasPendingTargetSelection)
            return;
        if (!_operator.sendTargetSelection("SELECT_TRK", targetX1, targetY1, targetX2, targetY2)) {
            statusMessage = qsTr("目标重绑定发送失败：") + _operator.lastError;
            reconnectSelectionTimer.interval = 1000;
            reconnectSelectionTimer.restart();
            return;
        }
        reconnectSelectionTimer.interval = 300;
        reconnectSelectionPending = false;
        selectionCommandId = _operator.lastSelectionCommandId;
        _rememberLocalTrackSelectionCommand(selectionCommandId);
        selectionAwaitingTrackStatus = true;
        pendingSelectionAction = "SELECT_TRK";
        hasLocalSelectionSession = true;
        selectionTrackStatusTimer.restart();
        confirmedPrimaryLockTargetId = "";
        exclusiveLockPendingTargetId = "";
        mode3ExecutionLatched = false;
        interactionState = "TRK";
        statusMessage = qsTr("Jetson 会话已恢复；正在重建 TRK") +
                (reconnectPromoteLock ? qsTr("，随后恢复 LCK") : "");
    }

    function _tryReconnectPromoteLock() {
        if (!reconnectPromoteLock || hasPendingTargetSelection || missionMode === "PATROL")
            return;
        const entries = operatorTrackedTargetPoolEntries;
        if (entries.length === 0)
            return;
        for (let index = 0; index < entries.length; ++index) {
            if (entries[index].locked === true && entries[index].primary === true) {
                reconnectPromoteLock = false;
                return;
            }
        }
        _adoptTargetPoolEntry(entries[0]);
        interactionState = "TRK";
        if (canPromoteCurrentTarget() && promoteLock())
            reconnectPromoteLock = false;
    }

    function _removePendingCancelledTarget(candidateTargetId) {
        if (candidateTargetId === "")
            return;
        const remaining = [];
        for (let index = 0; index < pendingCancelledTargetIds.length; ++index) {
            if (pendingCancelledTargetIds[index] !== candidateTargetId)
                remaining.push(pendingCancelledTargetIds[index]);
        }
        pendingCancelledTargetIds = remaining;
    }

    function _reconcilePendingTargetActions() {
        const remainingCancelled = [];
        for (let pendingIndex = 0; pendingIndex < pendingCancelledTargetIds.length; ++pendingIndex) {
            const pendingTargetId = pendingCancelledTargetIds[pendingIndex];
            let stillTracked = false;
            for (let entryIndex = 0; entryIndex < targetPoolEntries.length; ++entryIndex) {
                const entry = targetPoolEntries[entryIndex];
                if (entry !== undefined && entry.targetId === pendingTargetId &&
                        entry.operatorTracked === true) {
                    stillTracked = true;
                    break;
                }
            }
            if (stillTracked)
                remainingCancelled.push(pendingTargetId);
        }
        pendingCancelledTargetIds = remainingCancelled;

        if (exclusiveLockPendingTargetId === "")
            return;
        let confirmedExclusiveEntry = undefined;
        let serverTrackedCount = 0;
        for (let index = 0; index < targetPoolEntries.length; ++index) {
            const entry = targetPoolEntries[index];
            if (entry === undefined || entry.operatorTracked !== true)
                continue;
            serverTrackedCount += 1;
            if (entry.locked && entry.primary && !isDemotedPrimaryLockAwaitingPool(entry))
                confirmedExclusiveEntry = entry;
        }
        if (confirmedExclusiveEntry !== undefined && serverTrackedCount === 1) {
            // PROMOTE_LCK is confirmed by the sole authoritative, locally
            // selected primary lock.  Do not require its unified ID to equal
            // the optimistic candidate ID captured before Jetson association.
            confirmedPrimaryLockTargetId = confirmedExclusiveEntry.targetId;
            exclusiveLockPendingTargetId = "";
            _adoptTargetPoolEntry(confirmedExclusiveEntry);
            _requestLckCue(confirmedExclusiveEntry.targetId);
        }
    }

    function _isSelectableTargetLabel(label) {
        const normalized = String(label || "").trim().toLowerCase();
        return [
            "flame", "fire", "hotspot", "burned_area",
            "smoke", "smoldering_area", "smolder_area",
            "person", "pedestrian", "people", "firefighter",
            "vehicle", "car", "van", "truck", "bus", "train",
            "motorcycle", "bicycle", "motor", "boat",
            "aircraft", "airplane", "aeroplane", "plane", "helicopter",
            "drone", "uav"
        ].indexOf(normalized) >= 0;
    }

    function _selectableTargetPoolEntries(entries) {
        if (entries === undefined || entries === null)
            return [];
        const visible = [];
        for (let index = 0; index < entries.length; ++index) {
            const entry = entries[index];
            // A cancellation is optimistic: Jetson can require one target-pool
            // cadence to remove the old TRK entry. Do not redraw that entry as a
            // DET candidate during the acknowledgement window, otherwise one
            // press cancels the tracker but leaves an apparently live box.
            if (entry === undefined || entry === null ||
                    _arrayContains(pendingCancelledTargetIds, entry.targetId))
                continue;
            const trackedByThisUi = isOperatorTrackedTarget(entry);
            const exclusiveTargetId = exclusiveLockPendingTargetId !== "" ?
                                       exclusiveLockPendingTargetId :
                                       ((interactionState === "LCK" || interactionState === "TGT") ?
                                        targetId : "");
            if (exclusiveTargetId !== "" &&
                    entry.targetId !== exclusiveTargetId)
                continue;
            if (entry.bboxValid &&
                    String(entry.state || "").toUpperCase() !== "LOST" &&
                    (_isSelectableTargetLabel(entry.label) || trackedByThisUi)) {
                visible.push(entry);
            }
        }
        return visible;
    }

    function _operatorTrackedTargetPoolEntries(entries) {
        if (!hasLocalSelectionSession || entries === undefined || entries === null)
            return [];
        const tracked = [];
        for (let index = 0; index < entries.length; ++index) {
            const entry = entries[index];
            if (isOperatorTrackedTarget(entry) && entry.bboxValid &&
                    String(entry.state || "").toUpperCase() !== "LOST") {
                tracked.push(entry);
            }
        }
        return tracked;
    }

    function isOperatorTrackedTarget(entry) {
        if (!hasLocalSelectionSession || entry === undefined || entry.operatorTracked !== true)
            return false;
        if (_arrayContains(pendingCancelledTargetIds, entry.targetId))
            return false;
        return exclusiveLockPendingTargetId === "" ||
               entry.targetId === exclusiveLockPendingTargetId;
    }

    function hasPoolTrackedTarget(candidateTargetId) {
        if (candidateTargetId === "")
            return false;
        const entries = operatorTrackedTargetPoolEntries;
        for (let index = 0; index < entries.length; ++index) {
            if (entries[index].targetId === candidateTargetId)
                return true;
        }
        return false;
    }

    function hasPoolTrackedBoxForFallback(x1, y1, x2, y2) {
        // A just-drawn manual rectangle can receive its legacy TrackStatus
        // identity one target-pool cadence before the unified pool reports the
        // same object under its stable ID.  Matching only targetId then renders
        // both the optimistic fallback and the authoritative pool box.  Treat a
        // strongly overlapping operator-owned pool box as the same visual
        // target while the IDs converge; independent manual TRK boxes remain
        // visible because they do not overlap this closely.
        if (!Number.isFinite(x1) || !Number.isFinite(y1) ||
                !Number.isFinite(x2) || !Number.isFinite(y2) ||
                x2 <= x1 || y2 <= y1)
            return false;
        const fallbackArea = (x2 - x1) * (y2 - y1);
        const entries = operatorTrackedTargetPoolEntries;
        for (let index = 0; index < entries.length; ++index) {
            const entry = entries[index];
            if (entry === undefined || !entry.bboxValid || isDemotedPrimaryLockAwaitingPool(entry))
                continue;
            if (entry.targetId === targetId)
                return true;
            const overlapLeft = Math.max(x1, entry.x1);
            const overlapTop = Math.max(y1, entry.y1);
            const overlapRight = Math.min(x2, entry.x2);
            const overlapBottom = Math.min(y2, entry.y2);
            const overlapWidth = Math.max(0.0, overlapRight - overlapLeft);
            const overlapHeight = Math.max(0.0, overlapBottom - overlapTop);
            const intersectionArea = overlapWidth * overlapHeight;
            const entryArea = Math.max(0.0, (entry.x2 - entry.x1) * (entry.y2 - entry.y1));
            const unionArea = fallbackArea + entryArea - intersectionArea;
            if (unionArea > 0.0 && intersectionArea / unionArea >= 0.55)
                return true;
        }
        return false;
    }

    // Unlike operatorTrackedTargetPoolEntries, this looks at the full latest
    // snapshot.  LOST entries are deliberately filtered from the rendered
    // pool, but the selected fallback box must see that terminal state instead
    // of remaining clickable above a different, still-tracked target.
    function _rawTargetPoolEntry(candidateTargetId) {
        if (candidateTargetId === "")
            return undefined;
        for (let index = 0; index < targetPoolEntries.length; ++index) {
            const entry = targetPoolEntries[index];
            if (entry !== undefined && entry.targetId === candidateTargetId)
                return entry;
        }
        return undefined;
    }

    function _currentTargetIsExplicitlyLost() {
        const entry = _rawTargetPoolEntry(targetId);
        return entry !== undefined && String(entry.state || "").toUpperCase() === "LOST";
    }

    function _clearCurrentTargetAfterLoss() {
        const lostTargetId = targetId;
        const invalidatesExclusiveLock = lostTargetId !== "" &&
                (confirmedPrimaryLockTargetId === lostTargetId ||
                 exclusiveLockPendingTargetId === lostTargetId ||
                 interactionState === "LCK" || interactionState === "TGT");

        // This is intentionally narrower than _clearTargetLocally(): a single
        // lost target must not discard any other locally owned TRK sessions.
        targetId = "";
        targetLabel = "";
        targetConfidence = 0.0;
        trackingQuality = 0.0;
        targetSpeedMps = NaN;
        targetSpeedUpdatedAtMs = 0;
        targetSpeedUpdatedAtMs = 0;
        relativeBearingAvailable = false;
        relativeBearingDeg = 0.0;
        estimatedRangeM = 0.0;
        targetRangeUpdatedAtMs = 0;
        targetRangeUpdatedAtMs = 0;
        _resetRangeStatus();
        targetBoxValid = false;
        trackingState = "LOST";
        fireAlert = false;

        if (invalidatesExclusiveLock) {
            confirmedPrimaryLockTargetId = "";
            exclusiveLockPendingTargetId = "";
            mode3ExecutionLatched = false;
            _resetSafetyAndAuthorization();
            _resetReleaseStatus();
            _resetApproachStatus();
            _resetPayloadTargetStatus();
        }
    }

    function _clearCurrentTargetAfterCancellation(cancelledTargetId) {
        // This is intentionally scoped to the target that was cancelled. Other
        // local TRK boxes remain visible and can still be selected or promoted.
        if (cancelledTargetId === "" || targetId !== cancelledTargetId)
            return false;

        targetId = "";
        targetLabel = "";
        targetConfidence = 0.0;
        trackingQuality = 0.0;
        targetX1 = 0.0;
        targetY1 = 0.0;
        targetX2 = 0.0;
        targetY2 = 0.0;
        targetSpeedMps = NaN;
        targetSpeedUpdatedAtMs = 0;
        relativeBearingAvailable = false;
        relativeBearingDeg = 0.0;
        estimatedRangeM = 0.0;
        targetRangeUpdatedAtMs = 0;
        _resetRangeStatus();
        targetBoxValid = false;
        trackingState = "CANCELLED";
        fireAlert = false;
        confirmedPrimaryLockTargetId = "";
        exclusiveLockPendingTargetId = "";
        mode3ExecutionLatched = false;
        _resetSafetyAndAuthorization();
        _resetApproachStatus();
        _resetPayloadTargetStatus();
        return true;
    }

    function _adoptTargetPoolEntry(entry) {
        if (entry === undefined || !entry.bboxValid)
            return;
        const priorTargetId = targetId;
        targetId = entry.targetId;
        targetLabel = entry.label;
        targetConfidence = entry.confidence;
        trackingQuality = entry.trackingQuality;
        targetX1 = entry.x1;
        targetY1 = entry.y1;
        targetX2 = entry.x2;
        targetY2 = entry.y2;
        // Target-pool snapshots are authoritative for the selected target.  Do
        // not keep an older visual measurement alive when the newest snapshot
        // explicitly has no range/bearing estimate for this target.
        relativeBearingAvailable = Number.isFinite(entry.relativeBearingDeg);
        relativeBearingDeg = relativeBearingAvailable ? entry.relativeBearingDeg : 0.0;
        if (Number.isFinite(entry.estimatedRangeM)) {
            estimatedRangeM = entry.estimatedRangeM;
            targetRangeUpdatedAtMs = Date.now();
        } else if (priorTargetId !== entry.targetId) {
            estimatedRangeM = 0.0;
            targetRangeUpdatedAtMs = 0;
        }
        if (Number.isFinite(entry.targetSpeedMps)) {
            targetSpeedMps = entry.targetSpeedMps;
            targetSpeedUpdatedAtMs = Date.now();
        } else if (priorTargetId !== entry.targetId) {
            targetSpeedMps = NaN;
            targetSpeedUpdatedAtMs = 0;
        }
        targetBoxValid = true;
        trackingState = String(entry.state || "TRACKING").toUpperCase();
    }

    function _reconcileCurrentTargetFromPool() {
        // Keep an optimistic manual rectangle visible while its SELECT_TRK
        // acknowledgement is still in flight.  A prior tracked target in the
        // pool must not steal focus from the new rectangle during that window.
        if (selectionAwaitingTrackStatus && pendingSelectionAction === "SELECT_TRK" &&
                targetBoxValid) {
            return;
        }
        if (_currentTargetIsExplicitlyLost())
            _clearCurrentTargetAfterLoss();
        const entries = operatorTrackedTargetPoolEntries;
        if (entries.length === 0)
            return;
        let best = null;
        for (let index = 0; index < entries.length; ++index) {
            if (entries[index].locked && entries[index].primary &&
                    !isDemotedPrimaryLockAwaitingPool(entries[index])) {
                best = entries[index];
                break;
            }
        }
        if (best === null && targetId !== "") {
            for (let index = 0; index < entries.length; ++index) {
                if (entries[index].targetId === targetId) {
                    best = entries[index];
                    break;
                }
            }
        }
        // Target-pool snapshots can arrive one cadence later than a correlated
        // TrackStatus.  Preserve that explicitly selected target until its own
        // entry arrives instead of falling back to a different tracked object.
        if (best === null && hasLocalSelectionSession && targetId !== "" &&
                targetBoxValid) {
            return;
        }
        if (best === null && targetBoxValid) {
            const centerX = (targetX1 + targetX2) * 0.5;
            const centerY = (targetY1 + targetY2) * 0.5;
            let bestDistance = Number.POSITIVE_INFINITY;
            for (let index = 0; index < entries.length; ++index) {
                const entryCenterX = (entries[index].x1 + entries[index].x2) * 0.5;
                const entryCenterY = (entries[index].y1 + entries[index].y2) * 0.5;
                const dx = entryCenterX - centerX;
                const dy = entryCenterY - centerY;
                const distance = dx * dx + dy * dy;
                if (distance < bestDistance) {
                    bestDistance = distance;
                    best = entries[index];
                }
            }
        }
        _adoptTargetPoolEntry(best === null ? entries[0] : best);
    }

    function _clearTargetLocally() {
        reconnectSelectionTimer.stop();
        reconnectSelectionPending = false;
        reconnectPromoteLock = false;
        selectionTrackStatusTimer.stop();
        selectionAwaitingTrackStatus = false;
        selectionMode = false;
        selectionCommandId = "";
        pendingSelectionAction = "";
        localTrackSelectionCommandIds = [];
        pendingCancelledTargetId = "";
        pendingCancelledTargetIds = [];
        exclusiveLockPendingTargetId = "";
        confirmedPrimaryLockTargetId = "";
        mode3ExecutionLatched = false;
        hasLocalSelectionSession = false;
        targetId = "";
        targetLabel = "";
        targetConfidence = 0.0;
        trackingQuality = 0.0;
        targetSpeedMps = NaN;
        targetSpeedUpdatedAtMs = 0;
        relativeBearingAvailable = false;
        relativeBearingDeg = 0.0;
        estimatedRangeM = 0.0;
        targetRangeUpdatedAtMs = 0;
        _resetRangeStatus();
        targetBoxValid = false;
        trackingState = "CANCELLED";
        interactionState = "DET";
        missionPhase = "SEARCHING";
        fireAlert = false;
        _resetSafetyAndAuthorization();
        _resetApproachStatus();
        _resetPayloadTargetStatus();
    }

    function _isFireLabel(label) {
        const normalized = label.toLowerCase();
        return normalized === "flame" || normalized === "smoke" || normalized === "hotspot" || normalized === "smoldering_area" || normalized === "smolder_area" || normalized === "burned_area";
    }

    function _operatorTrackedEntry(targetIdToMatch) {
        if (targetIdToMatch === "")
            return undefined;
        const entries = operatorTrackedTargetPoolEntries;
        for (let index = 0; index < entries.length; ++index) {
            if (entries[index].targetId === targetIdToMatch)
                return entries[index];
        }
        return undefined;
    }

    function isLockEligibleTarget(entry) {
        if (!isOperatorTrackedTarget(entry) || !entry.bboxValid || entry.actionable !== true)
            return false;
        const state = String(entry.state || "").toUpperCase();
        if (state !== "TRACKING" && state !== "RECOVERED")
            return false;
        const confidence = Number(entry.confidence);
        const quality = Number(entry.trackingQuality);
        return Number.isFinite(confidence) && confidence >= lockMinimumConfidence &&
               Number.isFinite(quality) && quality >= lockMinimumTrackingQuality;
    }

    function canPromoteCurrentTarget() {
        return isLockEligibleTarget(_operatorTrackedEntry(targetId));
    }

    function _updateInteractionState() {
        if (mode3AimUiActive) {
            interactionState = "TGT";
            return;
        }
        if (exclusiveLockPendingTargetId !== "") {
            interactionState = "LCK";
            return;
        }
        if (hasConfirmedPrimaryLock) {
            interactionState = "LCK";
            return;
        }
        for (let index = 0; index < operatorTrackedTargetPoolEntries.length; ++index) {
            const entry = operatorTrackedTargetPoolEntries[index];
            if (entry.locked && entry.primary && !isDemotedPrimaryLockAwaitingPool(entry)) {
                confirmedPrimaryLockTargetId = entry.targetId;
                _adoptTargetPoolEntry(entry);
                interactionState = "LCK";
                _requestLckCue(entry.targetId);
                return;
            }
        }
        const optimisticTrack = hasLocalSelectionSession && targetBoxValid &&
                                trackingState !== "CANCELLED" && trackingState !== "REJECTED";
        interactionState = hasPendingTargetSelection || hasOperatorTrackedTargets || optimisticTrack ?
                           "TRK" : "DET";
    }

    function _resetSafetyAndAuthorization() {
        safetyPassCount = 0;
        safetyDenyCount = 0;
        safetyUnknownCount = 19;
        safetyAllowed = false;
        _missionSafetyAllowed = false;
        _ruleSafetyAllowed = false;
        releaseWindow = "UNAVAILABLE";
        authorizationState = "NONE";
        authorizationChallengeActive = false;
        authorizationExpiresInS = 0;
        authorizationBinding = "";
        authorizationTimer.stop();
    }

    function _resetPatrolStatus() {
        patrolPhase = "PATROL";
        patrolSourceAgeMs = 0;
        totalTrackCount = 0;
        lockedTrackCount = 0;
        returnObserveDirection = "";
        returnObserveValidity = "";
        returnEvidenceAgeS = -1.0;
        estimatedMinimumTurnRadiusM = -1.0;
        if (missionMode === "PATROL")
            statusMessage = qsTr("巡检元数据超时；等待 Jetson 更新目标池");
    }

    function _resetSceneContext() {
        sceneContextRegions = [];
        sceneContextState = "UNAVAILABLE";
    }

    function _resetRangeStatus() {
        rangeTargetId = "";
        rangeCalibrationId = "";
        rangeValidity = "UNAVAILABLE";
        rangeSlantM = -1.0;
        rangeGroundM = -1.0;
        rangeSlantLowM = -1.0;
        rangeSlantHighM = -1.0;
        rangeRelativeBearingDeg = 0.0;
        rangeBearingAvailable = false;
        rangeSourceAgeMs = 0;
        rangeDataFreshnessS = -1.0;
        rangeSensorConsistency = 0.0;
        rangeReasons = [];
        rangeSources = [];
        rangeSourceContributions = [];
        rangeFusionProfile = "outdoor-multimodal-v1";
        rangeVehicleProfile = "auto";
        rangeNavigationState = "unknown";
        rangeMotionRegime = "unknown";
    }

    function _resetTargetGeolocationStatus() {
        targetGeolocationAvailable = false;
        targetHorizontalSigmaM = -1.0;
        targetLatitudeDeg = NaN;
        targetLongitudeDeg = NaN;
        targetGeolocationReason = "";
        targetGeolocationSourceAgeMs = 0;
        targetGeolocationSourceFrameId = "";
        targetGeolocationTargetId = "";
    }

    function _resetReleaseStatus() {
        releaseTimingStatus = "UNAVAILABLE";
        releaseTargetId = "";
        releaseCalibrationId = "";
        releaseRangeBindingPresent = false;
        releaseImpactAvailable = false;
        releaseEllipseAvailable = false;
        releaseRangeIntervalAvailable = false;
        releaseReasons = [];
        releaseTargetNorthM = -1.0;
        releaseTargetEastM = -1.0;
        releaseImpactNorthM = -1.0;
        releaseImpactEastM = -1.0;
        releaseAlongErrorM = -1.0;
        releaseCrossErrorM = -1.0;
        releaseEllipseMajorM = -1.0;
        releaseEllipseMinorM = -1.0;
        releaseEllipseOrientationDeg = 0.0;
        releaseGroundRangeM = -1.0;
        releaseGroundRangeLowM = -1.0;
        releaseGroundRangeHighM = -1.0;
        releaseDescentTimeS = -1.0;
        releaseLeadDistanceM = -1.0;
        releaseRangeConsistency = 0.0;
    }

    function _resetApproachStatus() {
        approachChallengeActive = false;
        approachChallengeExpiresInS = 0;
        approachChallengeTimer.stop();
        approachPhase = "SEARCH";
        approachReasons = [];
        approachYawErrorDeg = 0.0;
        approachPitchErrorDeg = 0.0;
        approachYawAdviceDeg = 0.0;
        approachPitchAdviceDeg = 0.0;
        approachBankAdviceDeg = 0.0;
        approachClimbAdviceDeg = 0.0;
        approachGroundRangeM = -1.0;
        approachStatusFresh = false;
        approachFlightControlEnabled = false;
        approachAimControlActive = false;
        approachPilotInputCancelled = false;
    }

    function _resetPayloadTargetStatus() {
        payloadTargetChallengeActive = false;
        payloadTargetChallengeExpiresInS = 0;
        payloadTargetEligibility = "UNAVAILABLE";
        payloadTargetAimpointPresent = false;
        payloadTargetConfirmationAccepted = false;
    }

    function approachAdviceText() {
        if (approachPhase === "SEARCH")
            return "--";
        if (approachPhase === "ABORT")
            return qsTr("中止");
        return approachYawErrorDeg.toFixed(1) + "° / " + approachPitchErrorDeg.toFixed(1) + "°";
    }

    function approachChallengePromptText(targetActionable, isMobile) {
        if (!vehicleArmed)
            return qsTr("请先 ARM");
        if (approachChallengeActive) {
            return (isMobile ? qsTr("滑动确认") : qsTr("确认执行")) +
                    " · " + approachChallengeExpiresInS + " s";
        }
        const recovering = approachPhase === "ABORT" &&
                (approachReasons.indexOf("target_occluded") >= 0 ||
                 approachReasons.indexOf("target_reacquiring") >= 0 ||
                 approachReasons.indexOf("target_lost") >= 0 ||
                 approachReasons.indexOf("target_evidence_stale") >= 0);
        if (recovering)
            return qsTr("目标重捕获中");
        return targetActionable ? qsTr("等待 Jetson 执行挑战") : qsTr("等待 LCK");
    }

    function confirmApproachSlide(slideDurationMs, completionFraction, continuous) {
        if (!vehicleArmed) {
            statusMessage = qsTr("请先 ARM，再确认模式 3 执行");
            return false;
        }
        if (missionMode !== "OBSERVE" || !approachChallengeActive) {
            statusMessage = qsTr("模式 3 确认被拒绝：没有与当前目标绑定的新鲜挑战");
            return false;
        }
        if (!operatorConfigured || !_operator.sendApproachSlideConfirmation(slideDurationMs, completionFraction, continuous)) {
            statusMessage = qsTr("模式 3 确认发送失败：") + (_operator !== null ? _operator.lastError : qsTr("控制器不可用"));
            return false;
        }
        approachChallengeActive = false;
        approachChallengeTimer.stop();
        mode3ExecutionLatched = true;
        interactionState = "TGT";
        statusMessage = qsTr("模式 3 正在启动瞄准");
        _requestMode3ExecutionCue();
        return true;
    }

    function confirmPayloadTargetSlide(slideDurationMs, completionFraction, continuous) {
        if (!vehicleArmed) {
            statusMessage = qsTr("请先 ARM，再确认模式 2 执行");
            return false;
        }
        if (missionMode !== "PAYLOAD" || !payloadTargetChallengeActive) {
            statusMessage = qsTr("模式 2 确认被拒绝：没有与当前选择绑定的新鲜目标挑战");
            return false;
        }
        if (!operatorConfigured ||
                !_operator.sendPayloadTargetSlideConfirmation(slideDurationMs, completionFraction, continuous)) {
            statusMessage = qsTr("模式 2 确认发送失败：") +
                (_operator !== null ? _operator.lastError : qsTr("控制器不可用"));
            return false;
        }
        payloadTargetChallengeActive = false;
        statusMessage = qsTr("模式 2 连续滑动证据已发送；等待 Jetson ACK，物理输出仍锁定");
        return true;
    }

    function releaseTimingText() {
        if (releaseTimingStatus === "UNAVAILABLE")
            return qsTr("等待 Jetson 投放窗口元数据");
        if (releaseTimingStatus === "WINDOW")
            return qsTr("进入建议投放窗口；仅显示解算结果，物理输出仍锁定");
        if (releaseTimingStatus === "TOO_EARLY")
            return qsTr("尚未到达建议投放窗口");
        if (releaseTimingStatus === "TOO_LATE")
            return qsTr("已错过建议投放窗口，保持中止");
        return qsTr("投放窗口无效，保持中止");
    }

    function releaseImpactText() {
        if (releaseTimingStatus === "UNAVAILABLE")
            return qsTr("无数据");
        const impact = releaseImpactAvailable ?
            qsTr("落点 N/E ") + releaseImpactNorthM.toFixed(1) + "/" + releaseImpactEastM.toFixed(1) + " m" : qsTr("落点不可用");
        const interval = releaseRangeIntervalAvailable ?
            " · 95% [" + releaseGroundRangeLowM.toFixed(1) + ", " + releaseGroundRangeHighM.toFixed(1) + "] m" : "";
        return impact + interval;
    }

    function releaseUncertaintyText() {
        if (!releaseEllipseAvailable)
            return qsTr("误差椭圆不可用");
        return qsTr("95% 椭圆 ") + releaseEllipseMajorM.toFixed(1) + " × " + releaseEllipseMinorM.toFixed(1) +
            " m @ " + releaseEllipseOrientationDeg.toFixed(1) + "° · " + qsTr("一致性 ") +
            Math.round(releaseRangeConsistency * 100) + "%";
    }

    function releaseReasonText() {
        if (releaseReasons === undefined || releaseReasons.length === 0)
            return qsTr("无原因数据");
        return releaseReasons.join(" · ");
    }

    function rangeSummaryText() {
        if (rangeValidity === "UNAVAILABLE")
            return qsTr("无数据");
        const distance = rangeSlantM >= 0.0 ? rangeSlantM.toFixed(1) + " m" : qsTr("无距离");
        const interval = rangeSlantLowM >= 0.0 && rangeSlantHighM >= 0.0 ?
            " · 95% [" + rangeSlantLowM.toFixed(1) + ", " + rangeSlantHighM.toFixed(1) + "]" : "";
        return rangeValidity + " · " + distance + interval;
    }

    function rangeQualityText() {
        if (rangeValidity === "UNAVAILABLE")
            return qsTr("等待 Jetson 测距元数据");
        const bearing = rangeBearingAvailable ? " · " + rangeRelativeBearingDeg.toFixed(1) + "°" : "";
        const freshness = rangeDataFreshnessS >= 0.0 ? " · " + rangeDataFreshnessS.toFixed(1) + " s" : "";
        return qsTr("一致性 ") + Math.round(rangeSensorConsistency * 100) + "%" + bearing + freshness;
    }

    function targetGeolocationText() {
        if (targetGeolocationTargetId === "")
            return qsTr("不可定位：等待 Jetson 目标坐标状态");
        if (targetId !== "" && targetGeolocationTargetId !== targetId)
            return qsTr("不可定位：等待当前目标匹配");
        if (targetGeolocationAvailable && Number.isFinite(targetLatitudeDeg) &&
                Number.isFinite(targetLongitudeDeg) && targetHorizontalSigmaM >= 0.0) {
            return "GPS " + targetLatitudeDeg.toFixed(7) + ", " + targetLongitudeDeg.toFixed(7) +
                    " ± " + targetHorizontalSigmaM.toFixed(1) + " m (1σ)";
        }
        const reasonLabels = {
            "gps_navigation_not_qualified": qsTr("GPS 未通过质量门禁"),
            "target_offset_unavailable": qsTr("目标 N/E 偏移不可用"),
            "geolocation_input_invalid": qsTr("GPS 输入无效"),
            "target_uncertainty_out_of_wire_range": qsTr("坐标不确定度超出显示范围"),
            "target_range_invalid": qsTr("目标测距无效")
        };
        return qsTr("不可定位：") +
                (reasonLabels[targetGeolocationReason] || qsTr("等待合格 GPS"));
    }

    function rangeFusionText() {
        const vehicle = rangeVehicleProfile === "fixed-wing" ? qsTr("固定翼") :
                        rangeVehicleProfile === "multirotor" ? qsTr("多旋翼") : qsTr("自动");
        const navigation = rangeNavigationState === "gps-aided" ? "GPS" :
                           rangeNavigationState === "local-ned" ? "NED" :
                           rangeNavigationState === "airspeed-dr" ? qsTr("空速推算") :
                           rangeNavigationState === "vision-only" ? qsTr("视觉") : "--";
        return qsTr("Outdoor Fusion") + " · " + vehicle + " · " + navigation;
    }

    function rangeContributionText() {
        if (rangeSourceContributions === undefined || rangeSourceContributions.length === 0) {
            if (depthMapAvailable)
                return "DEP " + depthMinimumM.toFixed(1) + "–" + depthMaximumM.toFixed(0) + " m";
            return qsTr("等待深度/SLAM测距来源");
        }
        const labels = {
            "monocular_metric": "DEP",
            "rgb_slam": "RGB",
            "vio": "VI",
            "camera_ground": "AGL",
            "monocular_size": "SIZE",
            "pixhawk_agl": "PX4"
        };
        const values = [];
        for (let index = 0; index < rangeSourceContributions.length; ++index) {
            const item = rangeSourceContributions[index];
            const name = labels[String(item.source || "")] || String(item.source || "SRC");
            const range = Number(item.rangeM);
            const weight = Number(item.weight);
            values.push(name + " " + (Number.isFinite(range) ? range.toFixed(1) + "m" : "--") +
                        " " + (Number.isFinite(weight) ? Math.round(weight * 100) + "%" : "--"));
        }
        return values.join(" · ");
    }

    function returnObserveText() {
        if (returnObserveDirection === "")
            return qsTr("无");
        const direction = returnObserveDirection === "LEFT" ? qsTr("左转复查") : returnObserveDirection === "RIGHT" ? qsTr("右转复查") : qsTr("按航线复查");
        const radius = estimatedMinimumTurnRadiusM > 0.0 ? " · R≥" + estimatedMinimumTurnRadiusM.toFixed(1) + " m" : "";
        return direction + " · " + returnObserveValidity + radius;
    }

    function applySelection(x1, y1, x2, y2) {
        if (!Number.isFinite(x1) || !Number.isFinite(y1) || !Number.isFinite(x2) || !Number.isFinite(y2)) {
            statusMessage = qsTr("框选失败：坐标无效");
            return false;
        }
        const left = _clamp01(Math.min(x1, x2));
        const top = _clamp01(Math.min(y1, y2));
        const right = _clamp01(Math.max(x1, x2));
        const bottom = _clamp01(Math.max(y1, y2));
        if ((right - left) < 0.02 || (bottom - top) < 0.02) {
            statusMessage = qsTr("框选失败：目标框过小");
            return false;
        }
        if (!operatorConfigured) {
            statusMessage = qsTr("框选失败：Jetson 目标元数据链路未配置");
            return false;
        }

        reconnectSelectionTimer.stop();
        reconnectSelectionPending = false;
        reconnectPromoteLock = false;
        _resetSafetyAndAuthorization();
        _resetPayloadTargetStatus();
        approachPilotInputCancelled = false;
        mode3ExecutionLatched = false;
        confirmedPrimaryLockTargetId = "";
        if (!_operator.sendTargetSelection("SELECT_TRK", left, top, right, bottom)) {
            statusMessage = qsTr("框选发送失败：") + _operator.lastError;
            return false;
        }
        selectionSequence += 1;
        selectionCommandId = _operator.lastSelectionCommandId;
        _rememberLocalTrackSelectionCommand(selectionCommandId);
        selectionAwaitingTrackStatus = true;
        pendingSelectionAction = "SELECT_TRK";
        hasLocalSelectionSession = true;
        selectionTrackStatusTimer.restart();
        targetId = "";
        targetLabel = "manual";
        targetConfidence = 0.0;
        trackingQuality = 0.0;
        targetSpeedMps = NaN;
        relativeBearingAvailable = false;
        relativeBearingDeg = 0.0;
        estimatedRangeM = 0.0;
        targetX1 = left;
        targetY1 = top;
        targetX2 = right;
        targetY2 = bottom;
        targetBoxValid = true;
        trackingState = "INITIALIZING";
        interactionState = "TRK";
        missionPhase = "TRACKING";
        selectionMode = false;
        statusMessage = qsTr("框选已发送，等待 Jetson ACK 与实时跟踪状态");
        return true;
    }

    function selectCandidate(entry) {
        if (!operatorConfigured || entry === undefined || !entry.bboxValid)
            return false;
        reconnectSelectionTimer.stop();
        reconnectSelectionPending = false;
        reconnectPromoteLock = false;
        _resetSafetyAndAuthorization();
        _resetPayloadTargetStatus();
        approachPilotInputCancelled = false;
        mode3ExecutionLatched = false;
        confirmedPrimaryLockTargetId = "";
        if (!_operator.sendTargetSelection("SELECT_TRK", entry.x1, entry.y1, entry.x2, entry.y2)) {
            statusMessage = qsTr("TRK 发送失败：") + _operator.lastError;
            return false;
        }
        selectionCommandId = _operator.lastSelectionCommandId;
        _rememberLocalTrackSelectionCommand(selectionCommandId);
        selectionAwaitingTrackStatus = true;
        pendingSelectionAction = "SELECT_TRK";
        hasLocalSelectionSession = true;
        selectionTrackStatusTimer.restart();
        targetId = entry.targetId;
        targetLabel = entry.label;
        targetConfidence = entry.confidence;
        trackingQuality = entry.trackingQuality;
        targetSpeedMps = Number.isFinite(entry.targetSpeedMps) ? entry.targetSpeedMps : NaN;
        targetSpeedUpdatedAtMs = Number.isFinite(entry.targetSpeedMps) ? Date.now() : 0;
        relativeBearingAvailable = Number.isFinite(entry.relativeBearingDeg);
        relativeBearingDeg = relativeBearingAvailable ? entry.relativeBearingDeg : 0.0;
        estimatedRangeM = Number.isFinite(entry.estimatedRangeM) ? entry.estimatedRangeM : 0.0;
        targetRangeUpdatedAtMs = Number.isFinite(entry.estimatedRangeM) ? Date.now() : 0;
        targetX1 = entry.x1;
        targetY1 = entry.y1;
        targetX2 = entry.x2;
        targetY2 = entry.y2;
        targetBoxValid = true;
        trackingState = "INITIALIZING";
        interactionState = "TRK";
        selectionMode = false;
        statusMessage = "TRK";
        return true;
    }

    function selectTrackedCandidate(entry) {
        if (!isOperatorTrackedTarget(entry) || !entry.bboxValid)
            return false;
        _adoptTargetPoolEntry(entry);
        if (entry.locked && entry.primary && !isDemotedPrimaryLockAwaitingPool(entry)) {
            confirmedPrimaryLockTargetId = entry.targetId;
            interactionState = "LCK";
            statusMessage = "LCK";
            _requestLckCue(entry.targetId);
            return true;
        }
        interactionState = "TRK";
        statusMessage = "TRK";
        return true;
    }

    function lockTrackedCandidate(entry) {
        if (missionMode === "PATROL" || !isOperatorTrackedTarget(entry) ||
                !entry.bboxValid || hasPendingTargetSelection)
            return false;
        if (!isLockEligibleTarget(entry)) {
            statusMessage = qsTr("LCK 等待稳定目标");
            return false;
        }
        _adoptTargetPoolEntry(entry);
        interactionState = "TRK";
        return promoteLock();
    }

    function promoteLock() {
        if (missionMode === "PATROL" || interactionState !== "TRK" || !targetBoxValid ||
                !operatorConfigured || hasPendingTargetSelection)
            return false;
        if (!canPromoteCurrentTarget()) {
            statusMessage = qsTr("LCK 等待稳定目标");
            return false;
        }
        _resetSafetyAndAuthorization();
        _resetPayloadTargetStatus();
        const pilotCancellationBeforePromotion = approachPilotInputCancelled;
        approachPilotInputCancelled = false;
        mode3ExecutionLatched = false;
        confirmedPrimaryLockTargetId = "";
        const optimisticTargetId = targetId;
        demotedPrimaryLockTargetId = "";
        exclusiveLockPendingTargetId = optimisticTargetId;
        interactionState = "LCK";
        statusMessage = "LCK";
        if (!_operator.sendTargetSelection("PROMOTE_LCK", targetX1, targetY1, targetX2, targetY2)) {
            exclusiveLockPendingTargetId = "";
            approachPilotInputCancelled = pilotCancellationBeforePromotion;
            interactionState = "TRK";
            statusMessage = qsTr("LCK 发送失败：") + _operator.lastError;
            return false;
        }
        selectionCommandId = _operator.lastSelectionCommandId;
        selectionAwaitingTrackStatus = true;
        pendingSelectionAction = "PROMOTE_LCK";
        hasLocalSelectionSession = true;
        selectionTrackStatusTimer.restart();
        return true;
    }

    // The execution challenge is bound to the most recent LCK command.  Keep a
    // confirmed LCK visually continuous while minting one fresh binding just
    // before opening an execution dialog; this prevents a prior TRK/cancel
    // command from leaving the dialog waiting on an obsolete challenge.
    function refreshLockForExecution() {
        if (!vehicleArmed || !operatorConfigured || !hasConfirmedPrimaryLock ||
                !targetBoxValid || hasPendingTargetSelection)
            return false;
        if (!_operator.sendTargetSelection("PROMOTE_LCK", targetX1, targetY1, targetX2, targetY2)) {
            statusMessage = qsTr("LCK 刷新失败：") + _operator.lastError;
            return false;
        }
        selectionCommandId = _operator.lastSelectionCommandId;
        selectionAwaitingTrackStatus = true;
        pendingSelectionAction = "PROMOTE_LCK";
        demotedPrimaryLockTargetId = "";
        exclusiveLockPendingTargetId = targetId;
        selectionTrackStatusTimer.restart();
        statusMessage = qsTr("LCK 已刷新，正在获取执行确认");
        return true;
    }

    function demoteTrack() {
        if ((interactionState !== "LCK" && interactionState !== "TGT") ||
                !targetBoxValid || !operatorConfigured || hasPendingTargetSelection)
            return false;
        reconnectPromoteLock = false;
        if (!_operator.sendTargetSelection("DEMOTE_TRK", targetX1, targetY1, targetX2, targetY2)) {
            statusMessage = qsTr("TRK 发送失败：") + _operator.lastError;
            return false;
        }
        selectionCommandId = _operator.lastSelectionCommandId;
        selectionAwaitingTrackStatus = true;
        pendingSelectionAction = "DEMOTE_TRK";
        demotedPrimaryLockTargetId = targetId;
        exclusiveLockPendingTargetId = "";
        selectionTrackStatusTimer.restart();
        mode3ExecutionLatched = false;
        confirmedPrimaryLockTargetId = "";
        interactionState = "TRK";
        statusMessage = "TRK";
        return true;
    }

    function cancelMode3Aim() {
        if (missionMode !== "OBSERVE" || !mode3AimUiActive)
            return false;
        const previousExecutionLatched = mode3ExecutionLatched;
        const previousAimControlActive = approachAimControlActive;
        mode3ExecutionLatched = false;
        approachAimControlActive = false;
        statusMessage = qsTr("正在取消模式 3 瞄准");
        if (demoteTrack())
            return true;
        mode3ExecutionLatched = previousExecutionLatched;
        approachAimControlActive = previousAimControlActive;
        return false;
    }

    function cancelTrackedCandidate(entry) {
        if (!operatorConfigured || !isOperatorTrackedTarget(entry) || !entry.bboxValid)
            return false;
        if (!_operator.sendTargetSelection("CANCEL_TRK", entry.x1, entry.y1, entry.x2, entry.y2)) {
            statusMessage = qsTr("TRK 取消发送失败：") + _operator.lastError;
            return false;
        }
        // The controller treats a cancellation as an exclusive command.  Drop
        // correlation tokens for an older SELECT_TRK so a late acknowledgement
        // cannot restore this box after the operator has cancelled it.
        localTrackSelectionCommandIds = [];
        const hidden = pendingCancelledTargetIds.slice();
        if (!_arrayContains(hidden, entry.targetId))
            hidden.push(entry.targetId);
        pendingCancelledTargetIds = hidden;
        pendingCancelledTargetId = entry.targetId;
        pendingSelectionAction = "CANCEL_TRK";
        selectionCommandId = _operator.lastSelectionCommandId;
        selectionAwaitingTrackStatus = true;
        selectionTrackStatusTimer.restart();
        _clearCurrentTargetAfterCancellation(entry.targetId);
        selectionMode = false;
        _reconcileCurrentTargetFromPool();
        _updateInteractionState();
        statusMessage = qsTr("单目标 TRK 取消中");
        return true;
    }

    function _cancelCurrentTrackByBox() {
        if (!operatorConfigured || !targetBoxValid)
            return false;
        const cancelledTargetId = targetId;
        if (!_operator.sendTargetSelection("CANCEL_TRK", targetX1, targetY1, targetX2, targetY2)) {
            statusMessage = qsTr("TRK 取消发送失败：") + _operator.lastError;
            return false;
        }

        // CANCEL_TRK is exclusive at the controller and supersedes an earlier
        // unacknowledged SELECT_TRK. Hide this exact fallback rectangle at once;
        // any other authoritative TRK entries remain in the pool and stay visible.
        reconnectSelectionTimer.stop();
        reconnectSelectionPending = false;
        reconnectPromoteLock = false;
        localTrackSelectionCommandIds = [];
        selectionCommandId = _operator.lastSelectionCommandId;
        pendingSelectionAction = "CANCEL_TRK";
        pendingCancelledTargetId = cancelledTargetId;
        selectionAwaitingTrackStatus = true;
        selectionTrackStatusTimer.restart();
        selectionMode = false;
        if (cancelledTargetId !== "" && _arrayContains(pendingCancelledTargetIds, cancelledTargetId))
            _removePendingCancelledTarget(cancelledTargetId);
        targetId = "";
        targetLabel = "";
        targetConfidence = 0.0;
        trackingQuality = 0.0;
        targetBoxValid = false;
        trackingState = "CANCELLED";
        targetSpeedMps = NaN;
        targetSpeedUpdatedAtMs = 0;
        relativeBearingAvailable = false;
        relativeBearingDeg = 0.0;
        estimatedRangeM = 0.0;
        targetRangeUpdatedAtMs = 0;
        _resetRangeStatus();
        confirmedPrimaryLockTargetId = "";
        exclusiveLockPendingTargetId = "";
        mode3ExecutionLatched = false;
        _resetSafetyAndAuthorization();
        _resetApproachStatus();
        _resetPayloadTargetStatus();
        hasLocalSelectionSession = hasOperatorTrackedTargets;
        _updateInteractionState();
        statusMessage = qsTr("当前 TRK 取消中");
        return true;
    }

    function approveAuthorization() {
        if (missionMode !== "PAYLOAD" || !authorizationChallengeActive || !safetyAllowed) {
            statusMessage = qsTr("授权被拒绝：挑战无效或安全条件不满足");
            return false;
        }
        if (!operatorConfigured || !_operator.sendAuthorizationDecision(true)) {
            statusMessage = qsTr("授权决定发送失败：") + (_operator !== null ? _operator.lastError : qsTr("控制器不可用"));
            return false;
        }
        statusMessage = qsTr("批准决定已发送；等待 Jetson ACK，物理输出仍锁定");
        return true;
    }

    function beginSelection() {
        if (!operatorConfigured) {
            statusMessage = qsTr("框选不可用：Jetson 目标元数据链路未配置");
            return false;
        }
        // LCK is exclusive: a new manual rectangle must not locally clear the
        // primary-lock identity while Jetson is still concentrating on it.
        // The existing "返回 TRK" action performs the explicit demotion first.
        if (interactionState === "LCK" || interactionState === "TGT" ||
                exclusiveLockPendingTargetId !== "") {
            statusMessage = qsTr("LCK 状态请先返回 TRK");
            return false;
        }
        selectionMode = true;
        statusMessage = qsTr("在视频上拖动框选目标；松开后发送到 Jetson");
        return true;
    }

    function cancelTarget() {
        if (selectionMode) {
            selectionMode = false;
            statusMessage = qsTr("框选已取消");
            return true;
        }
        const entries = operatorTrackedTargetPoolEntries;
        let selectedEntry = null;
        for (let index = 0; index < entries.length; ++index) {
            if (entries[index].targetId === targetId) {
                selectedEntry = entries[index];
                break;
            }
        }
        if (selectedEntry !== null)
            return cancelTrackedCandidate(selectedEntry);
        if (targetBoxValid)
            return _cancelCurrentTrackByBox();
        if (_currentTargetIsExplicitlyLost()) {
            _clearCurrentTargetAfterLoss();
            _reconcileCurrentTargetFromPool();
            _updateInteractionState();
            statusMessage = qsTr("当前目标已失效；其余 TRK 保持");
            return true;
        }
        // Target actions are per-box.  If the selected box is not yet present
        // in the authoritative pool, do not fall back to a sole unrelated TRK
        // or emit the legacy global CANCEL command.
        if (targetId !== "" || hasLocalSelectionSession ||
                selectionCommandId !== "") {
            statusMessage = qsTr("当前目标尚未与 Jetson 目标池关联；未取消其它 TRK");
            return false;
        }
        _clearTargetLocally();
        statusMessage = qsTr("TRK 已取消");
        return true;
    }

    function denyAuthorization() {
        if (!operatorConfigured || !_operator.sendAuthorizationDecision(false)) {
            statusMessage = qsTr("拒绝决定发送失败：") + (_operator !== null ? _operator.lastError : qsTr("控制器不可用"));
            return false;
        }
        statusMessage = qsTr("拒绝决定已发送；等待 Jetson ACK");
        return true;
    }

    function setMissionMode(mode) {
        if (mode !== "PATROL" && mode !== "PAYLOAD" && mode !== "OBSERVE") {
            return false;
        }
        if (missionConfigurationLocked) {
            statusMessage = qsTr("飞机已解锁，任务模块必须在起飞前配置");
            return false;
        }
        missionMode = mode;
        persistedSettings.selectedMissionMode = mode;
        mode3ExecutionLatched = false;
        _resetSafetyAndAuthorization();
        _resetApproachStatus();
        _resetPayloadTargetStatus();
        manualReleaseRequestLatched = false;
        statusMessage = mode === "PATROL" ? qsTr("模式 1") :
                        mode === "PAYLOAD" ? qsTr("模式 2") : qsTr("模式 3");
        return true;
    }

    function missionModeDisplayName() {
        if (missionMode === "PAYLOAD")
            return qsTr("投放模块（模式 2）");
        if (missionMode === "OBSERVE")
            return qsTr("载荷模块（模式 3）");
        return qsTr("无模块（模式 1）");
    }

    function setRcReleaseChannel(channel) {
        if (channel !== 0 && (channel < 5 || channel > 18)) {
            return false;
        }
        if (missionConfigurationLocked) {
            statusMessage = qsTr("飞机已解锁，手动投放通道必须在起飞前配置");
            return false;
        }
        rcReleaseChannel = channel;
        persistedSettings.selectedRcReleaseChannel = channel;
        rcReleasePwm = 0;
        rcSignalAvailable = false;
        rcReleaseSwitchActive = false;
        manualReleaseRequestLatched = false;
        return true;
    }

    function switchTarget(offset) {
        if (!operatorConfigured || !targetBoxValid || hasPendingTargetSelection) {
            statusMessage = qsTr("切换失败：没有可用的实时目标框");
            return false;
        }
        _resetSafetyAndAuthorization();
        _resetPayloadTargetStatus();
        if (!_operator.sendTargetSelection("SWITCH", targetX1, targetY1, targetX2, targetY2)) {
            statusMessage = qsTr("目标切换发送失败：") + _operator.lastError;
            return false;
        }
        selectionCommandId = _operator.lastSelectionCommandId;
        selectionAwaitingTrackStatus = true;
        selectionTrackStatusTimer.restart();
        trackingState = "INITIALIZING";
        statusMessage = offset < 0 ? qsTr("已请求切换到上一目标") : qsTr("已请求切换到下一目标");
        return true;
    }

    function updateRcChannels(channelValues) {
        if (rcReleaseChannel <= 0 || channelValues === undefined || channelValues.length < rcReleaseChannel) {
            rcReleasePwm = 0;
            rcSignalAvailable = false;
            rcReleaseSwitchActive = false;
            manualReleaseRequestLatched = false;
            return;
        }
        const value = Number(channelValues[rcReleaseChannel - 1]);
        const available = Number.isFinite(value) && value >= 800 && value <= 2200;
        const wasActive = rcReleaseSwitchActive;
        rcReleasePwm = available ? Math.round(value) : 0;
        rcSignalAvailable = available;
        rcReleaseSwitchActive = available && value >= 1800;
        if (available && !rcReleaseSwitchActive)
            manualReleaseRequestLatched = false;
        if (!wasActive && rcReleaseSwitchActive) {
            if (missionMode === "PAYLOAD") {
                manualReleaseRequestLatched = true;
                statusMessage = qsTr("收到遥控器手动投放请求；已记录请求，物理输出保持锁定");
            } else {
                statusMessage = qsTr("当前不是投放模块，已忽略遥控器手动投放请求");
            }
        }
    }

    Component.onCompleted: {
        const storedMode = persistedSettings.selectedMissionMode;
        missionMode = storedMode === "PAYLOAD" ? "PAYLOAD" :
                      storedMode === "OBSERVE" || storedMode === "APPROACH_HIL" ? "OBSERVE" : "PATROL";
        if (storedMode === "APPROACH_HIL")
            persistedSettings.selectedMissionMode = "OBSERVE";
        const storedChannel = persistedSettings.selectedRcReleaseChannel;
        rcReleaseChannel = storedChannel >= 5 && storedChannel <= 18 ? storedChannel : 0;
    }
}
