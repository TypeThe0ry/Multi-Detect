pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtPositioning

import QGroundControl
import QGroundControl.Controls

// One native QGC dialog is shared by Fly and Plan. Geographic origin is a
// deliberate UI input, never inferred from the vehicle and never uploaded.
QGCPopupDialog {
    id: root

    property var mapControl: null
    property string originLatitude: ""
    property string originLongitude: ""
    readonly property var controller: typeof multiDetectMission !== "undefined" ? multiDetectMission : null
    property var _document: ({})
    property string _feedback: ""
    property bool _rawDirty: false
    property bool _syncingRaw: false
    property var _pendingNumbers: ({})
    readonly property var _policies: ["continue", "continue_then_return", "hold_then_return", "return_now", "land_now"]
    readonly property var _policyLabels: [qsTr("继续任务"), qsTr("继续一段时间后返航"), qsTr("悬停等待后返航"), qsTr("立即返航"), qsTr("立即降落")]
    readonly property var _risks: ["low", "medium", "high"]
    readonly property var _status: controller ? controller.telemetry : ({})
    readonly property bool _hasLiveMission: controller && controller.activeMissionId.length > 0
    readonly property bool _hasFreshStatus: _hasLiveMission && controller.telemetryFresh
    readonly property var _mapTargets: ["home"].concat(referenceNames("places"), referenceNames("zones"))
    readonly property var _tightenPolicies: controller && _policies.indexOf(controller.currentPolicy) >= 0
                                           ? _policies.slice(_policies.indexOf(controller.currentPolicy)) : []

    title: qsTr("任务包 · 飞前配置与机载监控")
    buttons: Dialog.Close

    function value(path, fallback) {
        let result = _document;
        for (let i = 0; i < path.length; ++i) {
            if (result === null || result === undefined || result[path[i]] === undefined)
                return fallback;
            result = result[path[i]];
        }
        return result;
    }

    function referenceNames(kind) {
        const entries = _document[kind];
        return entries && typeof entries === "object" && !Array.isArray(entries) ? Object.keys(entries) : [];
    }

    function syncDocument() {
        if (!controller)
            return;
        try {
            const document = JSON.parse(controller.packageJson);
            if (!document || typeof document !== "object" || Array.isArray(document))
                throw new Error(qsTr("任务包顶层必须是对象"));
            _document = document;
            if (!_rawDirty && !rawJson.activeFocus) {
                _syncingRaw = true;
                rawJson.text = controller.packageJson;
                _syncingRaw = false;
            }
        } catch (error) {
            _feedback = qsTr("JSON 无法解析：%1").arg(String(error));
        }
    }

    function commitDocument(document) {
        if (!controller)
            return;
        controller.packageJson = JSON.stringify(document, null, 2);
        syncDocument();
        _feedback = qsTr("草稿已更新，请重新校验；机载接受以上传确认结果为准。");
    }

    function setValue(path, nextValue) {
        if (_rawDirty && !applyRawDraft())
            return false;
        const document = JSON.parse(JSON.stringify(_document));
        let target = document;
        for (let i = 0; i < path.length - 1; ++i) {
            const array = typeof path[i + 1] === "number";
            if (target[path[i]] === null || typeof target[path[i]] !== "object" ||
                    Array.isArray(target[path[i]]) !== array)
                target[path[i]] = array ? [] : {};
            target = target[path[i]];
        }
        target[path[path.length - 1]] = nextValue;
        commitDocument(document);
        return true;
    }

    function queueNumber(path, text) {
        const pending = Object.assign({}, _pendingNumbers);
        pending[JSON.stringify(path)] = { "path": path, "text": text };
        _pendingNumbers = pending;
    }

    function numberText(path) {
        const pending = _pendingNumbers[JSON.stringify(path)];
        return pending ? pending.text : String(value(path, 0));
    }

    function numberValue(path, text) {
        const number = Number(text);
        if (text.trim().length === 0 || !Number.isFinite(number)) {
            _feedback = qsTr("数值必须为有限数字。");
            queueNumber(path, text);
            return false;
        }
        if (!setValue(path, number))
            return false;
        const pending = Object.assign({}, _pendingNumbers);
        delete pending[JSON.stringify(path)];
        _pendingNumbers = pending;
        return true;
    }

    function applyRawDraft() {
        if (!_rawDirty)
            return true;
        try {
            const document = JSON.parse(rawJson.text);
            if (!document || typeof document !== "object" || Array.isArray(document))
                throw new Error(qsTr("任务包顶层必须是对象"));
            _rawDirty = false;
            _pendingNumbers = ({});
            commitDocument(document);
            return true;
        } catch (error) {
            _feedback = qsTr("JSON 无法应用：%1；上传已阻止。").arg(String(error));
            return false;
        }
    }

    function prepareDraft() {
        // Move focus before validation so the last visible field is committed.
        root.forceActiveFocus();
        if (!applyRawDraft())
            return false;
        const pending = Object.keys(_pendingNumbers).map(key => _pendingNumbers[key]);
        for (let i = 0; i < pending.length; ++i) {
            if (!numberValue(pending[i].path, pending[i].text))
                return false;
        }
        return true;
    }

    function removeReference(kind, name) {
        if (_rawDirty && !applyRawDraft())
            return;
        const document = JSON.parse(JSON.stringify(_document));
        if (!document[kind] || typeof document[kind] !== "object")
            return;
        delete document[kind][name];
        if (document.ref_labels)
            delete document.ref_labels[name];
        const pending = Object.assign({}, _pendingNumbers);
        Object.keys(pending).forEach(key => {
            if (pending[key].path[0] === kind && pending[key].path[1] === name)
                delete pending[key];
        });
        _pendingNumbers = pending;
        // Keep uploaded routes/plan intact: validation reports dangling refs.
        commitDocument(document);
    }

    function addReference(kind, text) {
        const suffix = text.trim();
        const prefix = kind === "places" ? "poi:" : "zone:";
        const name = suffix.indexOf(prefix) === 0 ? suffix : prefix + suffix;
        if (!suffix || name === "zone:all" || value([kind, name], null) !== null) {
            _feedback = qsTr("名称不能为空、重复或使用保留名称 zone:all。");
            return;
        }
        const north = Number(value(["home", 0], 0));
        const east = Number(value(["home", 1], 0));
        setValue([kind, name], kind === "places" ? [north, east] :
                 { "centre_ne": [north, east], "radius_m": 5, "risk_tag": "", "oversight_required": false });
    }

    function addPlaceRiskZone(place) {
        if (_rawDirty && !applyRawDraft())
            return false;
        const point = value(["places", place], null);
        if (!Array.isArray(point) || point.length !== 2 || !Number.isFinite(point[0]) || !Number.isFinite(point[1])) {
            _feedback = qsTr("地点坐标无效，请先填写有限的北／东坐标。");
            return false;
        }
        const zone = "zone:" + place.slice(4) + "_risk";
        if (referenceNames("zones").indexOf(zone) >= 0) {
            _feedback = qsTr("风险区域 %1 已存在；请在区域栏编辑，不会覆盖。").arg(zone);
            return false;
        }
        const document = JSON.parse(JSON.stringify(_document));
        if (!document.zones || typeof document.zones !== "object" || Array.isArray(document.zones))
            document.zones = ({});
        document.zones[zone] = { "centre_ne": point.slice(), "radius_m": 5, "risk_tag": "", "oversight_required": false };
        if (!document.ref_labels || typeof document.ref_labels !== "object" || Array.isArray(document.ref_labels))
            document.ref_labels = ({});
        document.ref_labels[zone] = String(value(["ref_labels", place], place)) + qsTr(" · 风险区域");
        commitDocument(document);
        _feedback = qsTr("已创建 %1；请在区域栏填写风险标签、调整半径，并明确选择是否需要监督。地点本身不添加未知字段。").arg(zone);
        return true;
    }

    function policyText(policy) {
        const index = _policies.indexOf(policy);
        return index >= 0 ? _policyLabels[index] : qsTr("尚未收到");
    }

    function translatedAction(action) {
        const labels = { "continue": qsTr("继续任务"), "execute": qsTr("执行任务"),
            "hold": qsTr("等待"), "pause": qsTr("暂停"), "paused": qsTr("已暂停"),
            "return": qsTr("返航"), "return_home": qsTr("返航"), "land": qsTr("降落"),
            "return_now": qsTr("立即返航"), "land_now": qsTr("立即降落"),
            "abort": qsTr("中止"), "complete": qsTr("完成"), "completed": qsTr("完成") };
        return labels[action] || (action ? qsTr("其他机载动作") : qsTr("尚未收到"));
    }

    function reasonText(reason) {
        const text = String(reason || "");
        const labels = {
            "mission admitted; onboard operation enabled": qsTr("任务已准入，机载自主执行已启用"),
            "operator policy permits autonomous execution": qsTr("当前操作策略允许自主执行"),
            "operator link-loss policy": qsTr("已按飞前失联策略处理"),
            "pause bounded by operator policy": qsTr("暂停受飞前操作策略约束"),
            "operator policy silence/pause timeout": qsTr("失联或暂停计时已到，执行保守返航"),
            "waiting for link or operator resume": qsTr("等待链路恢复或操作员恢复任务"),
            "operator pause; awaiting resume": qsTr("操作员已暂停，等待恢复"),
            "remaining time at or below reserve": qsTr("剩余时间已达到返航储备阈值"),
            "remaining energy at or below reserve": qsTr("剩余能量已达到返航储备阈值"),
            "repeated low-confidence perception verdicts": qsTr("连续感知判断置信度过低，执行保守返航"),
            "explicit operator abort": qsTr("操作员明确中止任务，执行返航"),
            "mission id mismatch": qsTr("地面指令任务编号不匹配"),
            "unknown event type": qsTr("地面指令类型未知"),
            "event timestamp outside this flight": qsTr("地面事件时间戳不属于本次飞行"),
            "policy update would relax operator policy": qsTr("已拒绝会放宽策略的更新"),
            "policy update would extend the existing return deadline": qsTr("已拒绝会延长返航期限的更新"),
            "stale heartbeat": qsTr("已拒绝过期心跳")
        };
        if (labels[text])
            return labels[text];
        const step = /^step (.+) requires operator oversight without link$/.exec(text);
        if (step)
            return qsTr("步骤 %1 需要人工监督，但链路已断开").arg(step[1]);
        const zone = /^high-risk zone (.+) \((.*)\) requires oversight without link$/.exec(text);
        if (zone)
            return qsTr("高风险区域 %1（标签：%2）需要监督，但链路已断开").arg(zone[1]).arg(zone[2]);
        return text ? qsTr("机载报告了其他原因，详见原始审计字段") : qsTr("尚未收到");
    }

    function kindText(kind) {
        const labels = {
            "status": qsTr("监督器状态"), "mission_started": qsTr("任务已开始"),
            "ground_event": qsTr("地面事件已接受"), "ground_event_rejected": qsTr("地面事件已拒绝"),
            "estimates": qsTr("剩余预算估计"), "escalation": qsTr("安全策略已收紧"),
            "decision": qsTr("监督器决定"), "perception_verdict": qsTr("感知判断"),
            "execution": qsTr("机载执行记录"), "final_summary": qsTr("任务最终汇总")
        };
        return labels[kind] || qsTr("其他机载记录");
    }

    function logText(record) {
        const entry = record || ({});
        const state = entry.state || ({});
        const summary = entry.summary || ({});
        const decision = entry.decision && typeof entry.decision === "object" ? entry.decision : ({});
        let text = qsTr("序号 %1 · %2 · %3").arg(entry.sequence || "--")
            .arg(String(entry.recorded_at || entry.timestamp || "--")).arg(kindText(entry.kind));
        const events = { "heartbeat": qsTr("心跳"), "abort": qsTr("中止"), "pause": qsTr("暂停"),
            "resume": qsTr("恢复"), "policy_update": qsTr("收紧策略") };
        if (events[entry.event_type])
            text += qsTr(" · 指令：%1").arg(events[entry.event_type]);
        const action = typeof entry.action === "string" ? entry.action : state.supervisor_action || summary.action;
        if (action)
            text += qsTr(" · 决定：%1").arg(translatedAction(action));
        const reason = entry.reason || state.supervisor_reason || summary.reason;
        if (reason)
            text += qsTr("\n原因：%1").arg(reasonText(reason));
        if (decision.reason)
            text += qsTr("\n规划决定（%1）：%2").arg(decision.reason_code || "").arg(decision.reason);
        if (decision.question)
            text += qsTr("\n需要澄清：%1").arg(decision.question);
        const statuses = { "completed": qsTr("任务完成"), "recovery_finished": qsTr("安全返航／降落完成"), "stopped": qsTr("任务已停止") };
        if (summary.status)
            text += qsTr(" · 结果：%1").arg(statuses[summary.status] || qsTr("其他机载结果"));
        return text;
    }

    function finalSummaryText(summary) {
        if (!summary || typeof summary !== "object")
            return "";
        if (typeof summary.summary_zh === "string" && summary.summary_zh.length > 0)
            return summary.summary_zh;
        if (typeof summary.summary_en === "string" && summary.summary_en.length > 0)
            return summary.summary_en;
        const counts = summary.counts && typeof summary.counts === "object" ? summary.counts : {};
        const countText = Object.keys(counts).map(key => key + "：" + String(counts[key]) + " 个").join("；");
        const findings = Array.isArray(summary.findings) ? summary.findings.length : 0;
        const status = typeof summary.status === "string" ? summary.status : "";
        const decision = summary.decision && typeof summary.decision === "object" ? summary.decision : summary;
        const reason = typeof decision.planner_reason === "string" ? decision.planner_reason :
            (typeof decision.reason === "string" ? decision.reason : "");
        const question = typeof decision.question === "string" ? decision.question : "";
        return [reason ? qsTr("规划决定：%1").arg(reason) : "",
                question ? qsTr("需要澄清：%1").arg(question) : "",
                status ? qsTr("状态：%1").arg(status) : "",
                countText ? qsTr("计数：%1").arg(countText) : "",
                qsTr("发现：%1 条").arg(findings)].filter(part => part.length > 0).join("\n");
    }

    function planDecision(plan) {
        const value = plan && typeof plan === "object" ? plan : ({});
        const validation = validationSource(value);
        return typeof validation.decision === "string" ? validation.decision :
            (typeof value.validation_decision === "string" ? value.validation_decision : "");
    }

    function planReasons(plan) {
        const value = plan && typeof plan === "object" ? plan : ({});
        const validation = validationSource(value);
        return Array.isArray(validation.reasons_zh) ? validation.reasons_zh.filter(item => typeof item === "string") : [];
    }

    function planQuestions(plan) {
        const value = plan && typeof plan === "object" ? plan : ({});
        const validation = validationSource(value);
        return Array.isArray(validation.questions_zh) ? validation.questions_zh.filter(item => typeof item === "string").slice(0, 3) : [];
    }

    function planRepairs(plan) {
        const value = plan && typeof plan === "object" ? plan : ({});
        const validation = validationSource(value);
        return Array.isArray(validation.repairs) ? validation.repairs.filter(item => typeof item === "string") : [];
    }

    function validationSource(plan) {
        if (plan.validation && typeof plan.validation === "object")
            return plan.validation;
        const entries = root.controller && Array.isArray(root.controller.logEntries) ? root.controller.logEntries : [];
        for (let i = entries.length - 1; i >= 0; --i) {
            const entry = entries[i];
            if (!entry || entry.kind !== "mission_refused")
                continue;
            if (entry.validation && typeof entry.validation === "object")
                return entry.validation;
            const violations = Array.isArray(entry.violations) ? entry.violations : [];
            return { "decision": entry.decision, "reasons_zh": violations.map(item => item && item.message_zh).filter(item => typeof item === "string"),
                "questions_zh": violations.reduce((all, item) => all.concat(Array.isArray(item && item.questions_zh) ? item.questions_zh : []), []) };
        }
        return plan;
    }

    function retaskStatusText(plan) {
        const value = plan && typeof plan === "object" ? plan : ({});
        const status = typeof value.retask_status === "string" ? value.retask_status :
            (typeof value.status === "string" ? value.status : "");
        const labels = {
            "retask_received": qsTr("已受理"), "retask_planning": qsTr("规划中"),
            "retask_validated": qsTr("校验结论"), "retask_approval": qsTr("待审批"),
            "retask_switched": qsTr("已切换"), "retask_rejected": qsTr("已拒绝")
        };
        return labels[status] || status;
    }

    function currentRetaskStatus() {
        const fromPlan = retaskStatusText(root.controller ? root.controller.plan : ({}));
        if (fromPlan.length > 0)
            return fromPlan;
        const entries = root.controller && Array.isArray(root.controller.logEntries) ? root.controller.logEntries : [];
        for (let i = entries.length - 1; i >= 0; --i) {
            const kind = entries[i] && typeof entries[i].kind === "string" ? entries[i].kind : "";
            if (kind.indexOf("retask_") === 0)
                return retaskStatusText({ "retask_status": kind });
        }
        return "";
    }

    function planHistoryText(summary) {
        const history = summary && Array.isArray(summary.plan_history) ? summary.plan_history : [];
        return history.filter(item => item && typeof item === "object").map(item => {
            const version = item.plan_version !== undefined ? String(item.plan_version) : "?";
            const digest = typeof item.plan_sha256 === "string" ? item.plan_sha256 : "";
            const mode = typeof item.mode === "string" ? item.mode : "";
            return qsTr("版本 %1%2%3").arg(version)
                .arg(mode ? qsTr(" · 模式 %1").arg(mode) : "")
                .arg(digest ? qsTr(" · plan_sha256 %1").arg(digest) : "");
        }).join("\n");
    }

    function summaryOperatorLine(summary) {
        const operator = summary && summary.operator_summary && typeof summary.operator_summary === "object"
            ? summary.operator_summary : ({});
        const terminal = operator.terminal && typeof operator.terminal === "object" ? operator.terminal : ({});
        const status = typeof terminal.status === "string" ? terminal.status :
            (typeof operator.status === "string" ? operator.status :
            (typeof summary.status === "string" ? summary.status : ""));
        const action = typeof terminal.action === "string" ? terminal.action :
            (typeof operator.terminal_action === "string" ? operator.terminal_action :
            (typeof summary.terminal_action === "string" ? summary.terminal_action : ""));
        const reason = typeof terminal.reason === "string" ? terminal.reason :
            (typeof operator.terminal_reason === "string" ? operator.terminal_reason :
            (typeof summary.terminal_reason === "string" ? summary.terminal_reason : ""));
        return [status ? qsTr("状态：%1").arg(status) : "",
                action ? qsTr("终止动作：%1").arg(action) : "",
                reason ? qsTr("终止原因：%1").arg(reason) : ""].filter(part => part.length > 0).join("\n");
    }

    function summaryLowerBoundText(summary) {
        if (!summary || typeof summary !== "object" || !summary.count_lower_bounds ||
                typeof summary.count_lower_bounds !== "object")
            return "";
        const keys = Object.keys(summary.count_lower_bounds).filter(key => summary.count_lower_bounds[key] === true);
        return keys.length > 0 ? qsTr("下限计数：%1").arg(keys.join("、")) : "";
    }

    function summaryTruncationText(summary) {
        if (!summary || summary.detail_truncated !== true)
            return "";
        const omitted = summary.omitted && typeof summary.omitted === "object" ? summary.omitted : ({});
        const fields = Object.keys(omitted).filter(key => typeof omitted[key] === "number")
            .map(key => key + " " + omitted[key] + " 条");
        return qsTr("细节已截断%1").arg(fields.length ? qsTr("（省略：%1）").arg(fields.join("、")) : "");
    }

    function remainingText(number, unit) {
        return typeof number === "number" && Number.isFinite(number) ? number.toFixed(1) + " " + unit : qsTr("尚未收到");
    }

    function tightenAllowed() {
        if (!_hasFreshStatus || tightenPolicy.currentIndex < 0 || !tightenTimeout.text.trim())
            return false;
        const timeout = Number(tightenTimeout.text);
        const policy = _tightenPolicies[tightenPolicy.currentIndex];
        if (!Number.isFinite(timeout) || timeout < 0 || !policy)
            return false;
        return policy !== controller.currentPolicy ||
               (policy !== "continue_then_return" && policy !== "hold_then_return") ||
               timeout <= controller.currentPolicyTimeout;
    }

    function pickMapCentre() {
        const latitude = Number(originLatitude);
        const longitude = Number(originLongitude);
        if (!originLatitude.trim() || !originLongitude.trim() || !Number.isFinite(latitude) ||
                !Number.isFinite(longitude) || latitude <= -90 || latitude >= 90 || longitude < -180 || longitude > 180) {
            _feedback = qsTr("先显式填写任务本地坐标原点的 WGS84 纬度、经度；不会使用飞机当前位置。");
            return;
        }
        if (!mapControl || !mapControl.center || !mapControl.center.isValid) {
            _feedback = qsTr("地图中心不可用。请先在原生地图中定位，再打开任务包。");
            return;
        }
        const origin = QtPositioning.coordinate(latitude, longitude);
        const distance = origin.distanceTo(mapControl.center);
        const bearing = origin.azimuthTo(mapControl.center) * Math.PI / 180;
        const point = [Math.round(distance * Math.cos(bearing) * 100) / 100,
                       Math.round(distance * Math.sin(bearing) * 100) / 100];
        const name = _mapTargets[mapTarget.currentIndex];
        if (!name || !Number.isFinite(point[0]) || !Number.isFinite(point[1])) {
            _feedback = qsTr("取点失败，请检查原点和地图中心。");
            return;
        }
        if (name === "home")
            setValue(["home"], point);
        else if (name.indexOf("zone:") === 0)
            setValue(["zones", name, "centre_ne"], point);
        else
            setValue(["places", name], point);
        _feedback = qsTr("已取地图中心：北 %1 m、东 %2 m。原点只用于此界面，须与机载任务坐标系一致。").arg(point[0]).arg(point[1]);
    }

    component NumberField: RowLayout {
        id: numberRow
        required property string label
        required property var path
        Layout.fillWidth: true
        QGCLabel { Layout.fillWidth: true; text: numberRow.label }
        QGCTextField {
            Layout.preferredWidth: ScreenTools.defaultFontPixelWidth * 13
            text: root.numberText(numberRow.path)
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            onTextEdited: root.queueNumber(numberRow.path, text)
            onEditingFinished: root.numberValue(numberRow.path, text)
        }
    }

    component SectionLabel: QGCLabel {
        Layout.fillWidth: true
        Layout.topMargin: ScreenTools.defaultFontPixelHeight * 0.6
        font.bold: true
        font.pointSize: ScreenTools.mediumFontPointSize
        wrapMode: Text.WordWrap
    }

    QGCPalette { id: qgcPal }

    QGCFlickable {
        width: Math.min(root.maxContentAvailableWidth, ScreenTools.defaultFontPixelWidth * 94)
        height: Math.min(root.maxContentAvailableHeight, ScreenTools.defaultFontPixelHeight * 35)
        contentHeight: content.implicitHeight
        clip: true

        ColumnLayout {
            id: content
            width: parent.width
            spacing: ScreenTools.defaultFontPixelHeight * 0.45

            QGCLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("无人机自主完成任务。飞前上传任务包；地面站可选，用于监控、中止、暂停／恢复和收紧策略。飞行中不得放宽策略。")
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: !root.controller
                color: qgcPal.warningText
                text: qsTr("任务包控制器不可用。")
            }

            SectionLabel { text: qsTr("一、任务与风险") }
            RowLayout {
                Layout.fillWidth: true
                QGCLabel { text: qsTr("任务编号") }
                QGCTextField {
                    Layout.fillWidth: true
                    text: String(root.value(["mission_id"], ""))
                    onEditingFinished: root.setValue(["mission_id"], text)
                }
            }
            QGCLabel { text: qsTr("任务描述（机载编译任务，或通过下方 JSON 保留／编辑已有计划）") }
            TextArea {
                Layout.fillWidth: true
                Layout.preferredHeight: ScreenTools.defaultFontPixelHeight * 4
                color: qgcPal.text
                selectionColor: qgcPal.buttonHighlight
                wrapMode: TextEdit.Wrap
                text: String(root.value(["task_text"], ""))
                background: Rectangle { color: qgcPal.textField }
                onActiveFocusChanged: {
                    if (!activeFocus && root.controller && text !== root.value(["task_text"], ""))
                        root.setValue(["task_text"], text);
                }
            }
            RowLayout {
                Layout.fillWidth: true
                QGCLabel { text: qsTr("风险等级") }
                QGCComboBox {
                    model: [qsTr("低风险"), qsTr("中风险"), qsTr("高风险")]
                    currentIndex: root._risks.indexOf(root.value(["risk_level"], "medium"))
                    onActivated: index => root.setValue(["risk_level"], root._risks[index])
                }
                QGCLabel {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("建议默认：%1（可在飞前覆盖）").arg(root.policyText(root.controller ? root.controller.defaultPolicy(root.value(["risk_level"], "medium")) : ""))
                }
                QGCButton {
                    text: qsTr("采用建议")
                    enabled: !!root.controller
                    onClicked: root.setValue(["link_loss_policy", "action"], root.controller.defaultPolicy(root.value(["risk_level"], "medium")))
                }
            }
            RowLayout {
                Layout.fillWidth: true
                QGCLabel { text: qsTr("失联策略") }
                QGCComboBox {
                    Layout.fillWidth: true
                    model: root._policyLabels
                    currentIndex: root._policies.indexOf(root.value(["link_loss_policy", "action"], ""))
                    onActivated: index => root.setValue(["link_loss_policy", "action"], root._policies[index])
                }
            }
            NumberField { label: qsTr("继续／等待后返航计时（秒）"); path: ["link_loss_policy", "timeout_s"] }
            NumberField { label: qsTr("心跳超时（秒）"); path: ["heartbeat_timeout_s"] }

            SectionLabel { text: qsTr("二、作业范围与预算") }
            QGCLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("所有位置均为任务本地北／东坐标（米），不是经纬度。高度限制与机载任务使用相同基准。返航点须在范围内，圆形区域须完整落在范围内。")
            }
            NumberField { label: qsTr("北向下界（米）"); path: ["operating_area", "north_min_m"] }
            NumberField { label: qsTr("北向上界（米）"); path: ["operating_area", "north_max_m"] }
            NumberField { label: qsTr("东向下界（米）"); path: ["operating_area", "east_min_m"] }
            NumberField { label: qsTr("东向上界（米）"); path: ["operating_area", "east_max_m"] }
            NumberField { label: qsTr("最低高度（米）"); path: ["operating_area", "min_altitude_m"] }
            NumberField { label: qsTr("最高高度（米）"); path: ["operating_area", "max_altitude_m"] }
            NumberField { label: qsTr("任务时间预算（秒）"); path: ["budget", "time_s"] }
            NumberField { label: qsTr("返航预留时间（秒）"); path: ["budget", "reserve_time_s"] }
            NumberField { label: qsTr("任务能量预算（瓦时）"); path: ["budget", "energy_wh"] }
            NumberField { label: qsTr("返航预留能量（瓦时）"); path: ["budget", "reserve_energy_wh"] }
            NumberField { label: qsTr("返航点北向（米）"); path: ["home", 0] }
            NumberField { label: qsTr("返航点东向（米）"); path: ["home", 1] }

            SectionLabel { text: qsTr("三、命名地点与风险区域") }
            QGCLabel {
                Layout.fillWidth: true
                text: qsTr("地点使用 poi: 前缀，区域使用 zone: 前缀。地点风险标签通过伴随圆形区域约束，可用下方按钮创建；标签、半径和监督要求在区域栏填写。显示名可单独编辑；计划引用使用编号。删除后须重新校验引用。路线与已有计划在表单编辑中保留。")
                wrapMode: Text.WordWrap
            }
            Repeater {
                model: root.referenceNames("places")
                delegate: ColumnLayout {
                    id: placeRow
                    required property string modelData
                    Layout.fillWidth: true
                    RowLayout {
                        QGCLabel { Layout.fillWidth: true; font.bold: true; text: placeRow.modelData }
                        QGCButton { text: qsTr("删除地点"); onClicked: root.removeReference("places", placeRow.modelData) }
                    }
                    QGCTextField {
                        Layout.fillWidth: true
                        placeholderText: qsTr("地点显示名")
                        text: String(root.value(["ref_labels", placeRow.modelData], ""))
                        onEditingFinished: root.setValue(["ref_labels", placeRow.modelData], text)
                    }
                    NumberField { label: qsTr("北向（米）"); path: ["places", placeRow.modelData, 0] }
                    NumberField { label: qsTr("东向（米）"); path: ["places", placeRow.modelData, 1] }
                    QGCButton {
                        text: qsTr("为地点添加风险区域")
                        onClicked: {
                            const place = placeRow.modelData;
                            if (root.prepareDraft())
                                root.addPlaceRiskZone(place);
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                QGCTextField { id: newPlace; Layout.fillWidth: true; placeholderText: qsTr("新地点编号，例如 gate") }
                QGCButton { text: qsTr("添加地点"); onClicked: root.addReference("places", newPlace.text) }
            }
            Repeater {
                model: root.referenceNames("zones")
                delegate: ColumnLayout {
                    id: zoneRow
                    required property string modelData
                    Layout.fillWidth: true
                    RowLayout {
                        QGCLabel { Layout.fillWidth: true; font.bold: true; text: zoneRow.modelData }
                        QGCButton { text: qsTr("删除区域"); onClicked: root.removeReference("zones", zoneRow.modelData) }
                    }
                    QGCTextField {
                        Layout.fillWidth: true
                        placeholderText: qsTr("区域显示名")
                        text: String(root.value(["ref_labels", zoneRow.modelData], ""))
                        onEditingFinished: root.setValue(["ref_labels", zoneRow.modelData], text)
                    }
                    NumberField { label: qsTr("圆心北向（米）"); path: ["zones", zoneRow.modelData, "centre_ne", 0] }
                    NumberField { label: qsTr("圆心东向（米）"); path: ["zones", zoneRow.modelData, "centre_ne", 1] }
                    NumberField { label: qsTr("半径（米）"); path: ["zones", zoneRow.modelData, "radius_m"] }
                    QGCTextField {
                        Layout.fillWidth: true
                        placeholderText: qsTr("风险标签，例如：人员、道路、障碍物")
                        text: String(root.value(["zones", zoneRow.modelData, "risk_tag"], ""))
                        onEditingFinished: root.setValue(["zones", zoneRow.modelData, "risk_tag"], text)
                    }
                    QGCCheckBox {
                        text: qsTr("该区域要求人工监督（失联时机载保守处理）")
                        checked: root.value(["zones", zoneRow.modelData, "oversight_required"], false)
                        onClicked: root.setValue(["zones", zoneRow.modelData, "oversight_required"], checked)
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                QGCTextField { id: newZone; Layout.fillWidth: true; placeholderText: qsTr("新圆形区域编号，例如 people") }
                QGCButton { text: qsTr("添加区域"); onClicked: root.addReference("zones", newZone.text) }
            }

            SectionLabel { text: qsTr("地图中心取点（可选）") }
            QGCLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("先在原生地图移动到目标位置，再打开面板。显式填写与机载任务一致的 WGS84 本地坐标原点。此原点仅用于界面换算，不写入任务包；不会自动使用飞机 GPS。")
            }
            RowLayout {
                Layout.fillWidth: true
                QGCLabel { text: qsTr("原点纬度") }
                QGCTextField { Layout.fillWidth: true; text: root.originLatitude; onTextEdited: root.originLatitude = text }
                QGCLabel { text: qsTr("原点经度") }
                QGCTextField { Layout.fillWidth: true; text: root.originLongitude; onTextEdited: root.originLongitude = text }
            }
            RowLayout {
                Layout.fillWidth: true
                QGCComboBox { id: mapTarget; Layout.fillWidth: true; model: root._mapTargets.map(name => name === "home" ? qsTr("返航点") : name) }
                QGCButton { text: qsTr("地图中心取点"); enabled: !!root.mapControl; onClicked: root.pickMapCentre() }
            }

            SectionLabel { text: qsTr("四、需要监督的步骤") }
            QGCLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("填写机载计划中的步骤编号，以逗号分隔。正常自主步骤不需要地面站批准；监督要求只约束指定步骤。已有计划可在 JSON 中查看步骤编号。")
            }
            QGCTextField {
                Layout.fillWidth: true
                text: Array.isArray(root.value(["oversight_steps"], [])) ? root.value(["oversight_steps"], []).join(", ") : ""
                placeholderText: qsTr("例如：inspect_gate, cross_road")
                onEditingFinished: root.setValue(["oversight_steps"], text.split(/[,，]/).map(entry => entry.trim()).filter(entry => entry.length > 0))
            }

            SectionLabel { text: qsTr("五、JSON 导入、校验与上传") }
            RowLayout {
                Layout.fillWidth: true
                QGCButton { text: qsTr("导入任务包"); enabled: !!root.controller && !root.controller.busy; onClicked: packageFile.openForLoad() }
                QGCButton { text: qsTr("导出任务包"); enabled: !!root.controller; onClicked: { if (root.prepareDraft()) packageFile.openForSave(); } }
                QGCButton {
                    text: qsTr("飞前本地校验")
                    enabled: !!root.controller
                    onClicked: { if (root.prepareDraft()) root.controller.validate(); }
                }
                QGCButton {
                    text: qsTr("上传至机载")
                    primary: true
                    enabled: !!root.controller && !root.controller.busy
                    onClicked: { if (root.prepareDraft() && root.controller.validate()) root.controller.upload(); }
                }
            }
            QGCLabel {
                Layout.fillWidth: true
                color: root.controller && root.controller.valid ? qgcPal.colorGreen : qgcPal.warningText
                wrapMode: Text.WordWrap
                text: root.controller ? root.controller.validationMessage : qsTr("等待控制器")
            }
            QGCLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: root.controller ? qsTr("上传状态：%1 · %2%").arg(root.controller.uploadState).arg(Math.round(root.controller.uploadProgress * 100)) : ""
            }
            QGCLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("本地校验仅检查飞前草稿。收到机载接受确认才表示任务包装载成功；上传不等于解锁、起飞或开始执行。")
            }

            SectionLabel { text: qsTr("计划校验与操作员审批") }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && Object.keys(root.controller.plan || {}).length > 0
                wrapMode: Text.WordWrap
                text: {
                    const decision = root.planDecision(root.controller ? root.controller.plan : ({}));
                    const digest = root.controller && root.controller.plan ? root.controller.plan.plan_sha256 : "";
                    return qsTr("机载结论：%1%2").arg(decision || qsTr("未提供"))
                        .arg(typeof digest === "string" && digest.length > 0 ? qsTr("\nplan_sha256：%1").arg(digest) : "");
                }
            }
            Repeater {
                model: root.planReasons(root.controller ? root.controller.plan : ({}))
                delegate: QGCLabel {
                    required property string modelData
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("原因：%1").arg(modelData)
                }
            }
            Repeater {
                model: root.planRepairs(root.controller ? root.controller.plan : ({}))
                delegate: QGCLabel {
                    required property string modelData
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("自动修复：%1").arg(modelData)
                }
            }
            Repeater {
                model: root.planQuestions(root.controller ? root.controller.plan : ({}))
                delegate: QGCLabel {
                    required property string modelData
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("待澄清问题：%1").arg(modelData)
                }
            }
            RowLayout {
                Layout.fillWidth: true
                visible: !!root.controller && root.controller.planAwaitingDecision
                QGCButton {
                    text: qsTr("批准计划")
                    primary: true
                    enabled: root.planDecision(root.controller ? root.controller.plan : ({})) !== "clarify" &&
                             root.planDecision(root.controller ? root.controller.plan : ({})) !== "reject"
                    onClicked: root.controller.approvePlan()
                }
                QGCButton {
                    text: qsTr("拒绝计划")
                    enabled: !!root.controller
                    onClicked: root.controller.rejectPlan()
                }
            }
            QGCButton {
                Layout.fillWidth: true
                visible: root.planDecision(root.controller ? root.controller.plan : ({})) === "clarify"
                text: qsTr("修改任务并重新提交")
                onClicked: {
                    showJson.checked = true;
                    root._feedback = qsTr("请在上方任务描述或 JSON 编辑器中补足澄清问题，再点击上传至机载。旧计划不会被批准。");
                }
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && root.controller.planDecisionState.length > 0
                wrapMode: Text.WordWrap
                text: root.controller ? root.controller.planDecisionState : ""
            }

            SectionLabel { text: qsTr("飞行中改派") }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && root.currentRetaskStatus().length > 0
                wrapMode: Text.WordWrap
                text: qsTr("改派状态：%1").arg(root.currentRetaskStatus())
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && root.controller.plan && typeof root.controller.plan.retask_reason_zh === "string"
                wrapMode: Text.WordWrap
                text: qsTr("原因：%1").arg(root.controller ? root.controller.plan.retask_reason_zh : "")
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && root.controller.plan && typeof root.controller.plan.plan_sha256 === "string" &&
                         root.currentRetaskStatus() === qsTr("待审批")
                wrapMode: Text.WordWrap
                text: qsTr("审批绑定候选 plan_sha256：%1").arg(root.controller.plan.plan_sha256)
            }
            QGCButton {
                Layout.fillWidth: true
                visible: !!root.controller && root._hasLiveMission
                text: qsTr("上传签名任务包并替换剩余步骤")
                onClicked: {
                    if (root.prepareDraft()) {
                        if (typeof root.controller.uploadRetask === "function") root.controller.uploadRetask();
                        else root.controller.upload();
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                visible: !!root.controller && root.currentRetaskStatus() === qsTr("待审批")
                QGCButton { text: qsTr("批准改派"); primary: true; onClicked: root.controller.approveRetask ? root.controller.approveRetask() : root.controller.approvePlan() }
                QGCButton { text: qsTr("拒绝改派"); onClicked: root.controller.rejectRetask ? root.controller.rejectRetask() : root.controller.rejectPlan() }
            }
            QGCCheckBox { id: showJson; text: qsTr("展开完整 JSON 编辑器（含路线与步骤计划）") }
            QGCLabel {
                Layout.fillWidth: true
                visible: root._rawDirty || Object.keys(root._pendingNumbers).length > 0
                color: qgcPal.warningText
                wrapMode: Text.WordWrap
                text: qsTr("存在尚未应用的编辑；校验、导出和上传会先应用当前输入。无效输入会阻止操作。")
            }
            TextArea {
                id: rawJson
                Layout.fillWidth: true
                Layout.preferredHeight: ScreenTools.defaultFontPixelHeight * 13
                visible: showJson.checked
                color: qgcPal.text
                selectionColor: qgcPal.buttonHighlight
                wrapMode: TextEdit.Wrap
                selectByMouse: true
                background: Rectangle { color: qgcPal.textField }
                onTextChanged: { if (!root._syncingRaw) root._rawDirty = true; }
            }
            QGCButton {
                visible: showJson.checked
                enabled: !!root.controller
                text: qsTr("应用 JSON 至草稿")
                onClicked: root.applyRawDraft()
            }

            SectionLabel { text: qsTr("六、机载监控与保守干预") }
            QGCLabel {
                Layout.fillWidth: true
                color: root._hasFreshStatus ? qgcPal.colorGreen : qgcPal.warningText
                wrapMode: Text.WordWrap
                text: root._hasFreshStatus ? qsTr("机载下行状态新鲜 · 任务 %1").arg(root.controller.activeMissionId) : qsTr("下行状态缺失或过期；显示值不能作为当前机载状态。")
            }
            QGCLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("监督器决定：%1\n原因：%2\n当前失联策略：%3 · 计时 %4 秒\n剩余时间：%5 · 剩余能量：%6")
                    .arg(root.translatedAction(root._status.action))
                    .arg(root.reasonText(root._status.reason))
                    .arg(root.policyText(root.controller ? root.controller.currentPolicy : ""))
                    .arg(root.controller ? root.controller.currentPolicyTimeout : 0)
                    .arg(root.remainingText(root._status.remaining_time_s, qsTr("秒")))
                    .arg(root.remainingText(root._status.remaining_energy_wh, qsTr("瓦时")))
            }
            RowLayout {
                Layout.fillWidth: true
                QGCButton { text: qsTr("中止任务"); enabled: root._hasLiveMission; onClicked: root.controller.abortMission() }
                QGCButton { text: qsTr("暂停任务"); enabled: root._hasFreshStatus; onClicked: root.controller.pauseMission() }
                QGCButton { text: qsTr("恢复任务"); enabled: root._hasFreshStatus; onClicked: root.controller.resumeMission() }
            }
            QGCLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("恢复仅恢复暂停执行，不放宽机载安全决定。策略只允许同级缩短计时或提高保守程度；由机载再次核验。")
            }
            RowLayout {
                Layout.fillWidth: true
                QGCComboBox {
                    id: tightenPolicy
                    Layout.fillWidth: true
                    model: root._tightenPolicies.map(policy => root.policyText(policy))
                }
                QGCLabel { text: qsTr("计时（秒）") }
                QGCTextField {
                    id: tightenTimeout
                    Layout.preferredWidth: ScreenTools.defaultFontPixelWidth * 9
                    text: root.controller ? String(root.controller.currentPolicyTimeout) : "0"
                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                }
                QGCButton {
                    text: qsTr("收紧策略")
                    enabled: root.tightenAllowed()
                    onClicked: root.controller.tightenPolicy(root._tightenPolicies[tightenPolicy.currentIndex], Number(tightenTimeout.text))
                }
            }
            QGCLabel { font.bold: true; text: qsTr("最近机载日志（仅显示下行已收到记录）") }
            SectionLabel { text: qsTr("任务最终摘要") }
            QGCLabel {
                Layout.fillWidth: true
                color: root.controller && root.controller.finalSummaryComplete ? qgcPal.colorGreen : qgcPal.warningText
                wrapMode: Text.WordWrap
                text: root.controller ? (root.controller.finalSummaryState || qsTr("尚未收到最终摘要")) : qsTr("摘要未完整接收")
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && root.controller.finalSummaryComplete
                wrapMode: Text.WordWrap
                text: root.finalSummaryText(root.controller ? root.controller.finalSummary : ({}))
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && root.controller.finalSummaryComplete && root.summaryOperatorLine(root.controller.finalSummary).length > 0
                wrapMode: Text.WordWrap
                text: root.summaryOperatorLine(root.controller.finalSummary)
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && root.controller.finalSummaryComplete && root.summaryLowerBoundText(root.controller.finalSummary).length > 0
                color: qgcPal.warningText
                text: root.summaryLowerBoundText(root.controller.finalSummary)
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && root.controller.finalSummaryComplete && root.summaryTruncationText(root.controller.finalSummary).length > 0
                color: qgcPal.warningText
                wrapMode: Text.WordWrap
                text: root.summaryTruncationText(root.controller.finalSummary)
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: !!root.controller && root.controller.finalSummaryComplete && root.planHistoryText(root.controller.finalSummary).length > 0
                wrapMode: Text.WordWrap
                text: qsTr("计划版本历史：\n%1").arg(root.planHistoryText(root.controller.finalSummary))
            }
            QGCCheckBox { id: showRawAudit; text: qsTr("显示机载原始审计字段（保留机器原文）") }
            QGCLabel {
                Layout.fillWidth: true
                visible: showRawAudit.checked && !!root._status.reason
                wrapMode: Text.WordWrap
                text: qsTr("当前决定的原始原因：%1").arg(String(root._status.reason || ""))
            }
            Repeater {
                model: root.controller ? root.controller.logEntries : []
                delegate: QGCLabel {
                    required property var modelData
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: root.logText(modelData) + (showRawAudit.checked ? qsTr("\n原始审计：%1").arg(JSON.stringify(modelData)) : "")
                }
            }
            QGCLabel {
                Layout.fillWidth: true
                text: root.controller && root.controller.logEntries.length > 0 ? "" : qsTr("尚未收到机载日志。")
            }
            QGCLabel {
                Layout.fillWidth: true
                color: qgcPal.warningText
                wrapMode: Text.WordWrap
                text: root._feedback
                visible: text.length > 0
            }
            QGCButton { Layout.alignment: Qt.AlignRight; text: qsTr("关闭任务包面板"); onClicked: root.close() }
        }
    }

    QGCFileDialog {
        id: packageFile
        folder: QGroundControl.settingsManager.appSettings.telemetrySavePath
        nameFilters: [qsTr("任务包 JSON (*.json)")]
        defaultSuffix: "json"
        title: qsTr("任务包导入／导出")
        onAcceptedForLoad: function(file) {
            if (root.controller.loadFile(file)) {
                root._rawDirty = false;
                root._pendingNumbers = ({});
            }
            root.syncDocument();
            close();
        }
        onAcceptedForSave: function(file) {
            if (root.prepareDraft())
                root.controller.saveFile(file);
            close();
        }
    }

    Connections {
        target: root.controller
        function onPackageChanged() { root.syncDocument(); }
    }

    Component.onCompleted: syncDocument()
}
