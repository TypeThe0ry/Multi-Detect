#include "MultiDetectMissionController.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QDateTime>
#include <QtCore/QFile>
#include <QtCore/QJsonDocument>
#include <QtCore/QRandomGenerator>
#include <QtCore/QSaveFile>
#include <algorithm>
#include <cmath>

#include "MultiDetectMissionPackage.h"
#include "MultiDetectMissionSecurity.h"
#include "MultiDetectMissionWire.h"
#include "MultiDetectOperatorProtocol.h"

namespace {
const QStringList policies = {"continue", "continue_then_return", "hold_then_return", "return_home", "return_now", "land_now"};
constexpr qint64 kTelemetryAssemblyDeadlineMs = 30'000;
constexpr qint64 kTelemetryAssemblyMemoryCap = 128 * 1024;

quint64 token()
{
    const quint64 value = QRandomGenerator::global()->generate64();
    return value == 0 ? 1 : value;
}

QByteArray configuredPrivateSeed()
{
    const QByteArray configured = qEnvironmentVariable("MULTIDETECT_OPERATOR_ED25519_PRIVATE_KEY").trimmed().toLatin1();
    QByteArray encoded = configured;
    const QByteArray prefix("ed25519-private:");
    if (encoded.startsWith(prefix))
        encoded = encoded.mid(prefix.size());
    qunsetenv("MULTIDETECT_OPERATOR_ED25519_PRIVATE_KEY");
    QByteArray seed = QByteArray::fromHex(encoded);
    if (seed.size() == 32 && seed.toHex().compare(encoded, Qt::CaseInsensitive) == 0)
        return seed;
    seed = QByteArray::fromBase64(encoded, QByteArray::Base64Encoding);
    return seed.size() == 32 ? seed : QByteArray();
}
}  // namespace

MultiDetectMissionController::MultiDetectMissionController(QObject* parent) : QObject(parent)
{
    // Deliberately invalid until the operator supplies the actual flight limits.
    setPackageJson(QStringLiteral(
        R"({"mission_id":"","task_text":"","places":{},"zones":{},"routes":{},"ref_labels":{},"operating_area":{"north_min_m":0,"north_max_m":0,"east_min_m":0,"east_max_m":0,"min_altitude_m":0,"max_altitude_m":0},"budget":{"time_s":0,"reserve_time_s":0,"energy_wh":0,"reserve_energy_wh":0},"home":[0,0],"risk_level":"medium","link_loss_policy":{"action":"continue_then_return","timeout_s":0},"heartbeat_timeout_s":5,"oversight_steps":[]})"));
    _clock.start();
    _missionPrivateSeed = configuredPrivateSeed();
    _missionKeyId = qEnvironmentVariable("MULTIDETECT_OPERATOR_KEY_ID", "default").trimmed();
    _requireMissionSignature = qEnvironmentVariable("MULTIDETECT_EXPECTED_RUNTIME_MODE").trimmed().toUpper() ==
                               QStringLiteral("REAL");
    _timer.setInterval(250);
    connect(&_timer, &QTimer::timeout, this, &MultiDetectMissionController::_poll);
    _timer.start();
}

MultiDetectMissionController::~MultiDetectMissionController()
{
    shutdown();
    std::fill(_missionPrivateSeed.begin(), _missionPrivateSeed.end(), '\0');
    _missionPrivateSeed.clear();
}

void MultiDetectMissionController::shutdown()
{
    _timer.stop();
    _sender = {};
    _pending = {};
}

void MultiDetectMissionController::setPackageJson(const QString& json)
{
    if (json == _json)
        return;
    _json = json;
    validate();
}

QString MultiDetectMissionController::defaultPolicy(const QString& risk) const
{
    return risk == "low" ? "continue" : risk == "high" ? "return_now" : "continue_then_return";
}

bool MultiDetectMissionController::validate()
{
    const QByteArray encoded = _json.toUtf8();
    _valid = encoded.size() <= MultiDetectMissionWire::kMaximumDocumentBytes &&
             MultiDetectMissionPackage::parse(encoded, &_document, &_validation);
    if (encoded.size() > MultiDetectMissionWire::kMaximumDocumentBytes)
        _validation = QStringLiteral("任务包超过 64 KiB 传输上限");
    if (_valid)
        _validation = QStringLiteral("本地校验通过；上传后仍需机载端准入确认");
    emit packageChanged();
    return _valid;
}

QString MultiDetectMissionController::_localPath(const QUrl& url)
{
    if (url.isLocalFile())
        return url.toLocalFile();
    if (url.scheme().isEmpty())
        return url.toString();
#ifdef Q_OS_WIN
    if (url.scheme().size() == 1 && url.toString().at(1) == ':')
        return url.toString();
#endif
    return {};
}

bool MultiDetectMissionController::loadFile(const QUrl& url)
{
    const QString path = _localPath(url);
    QFile file(path);
    if (path.isEmpty() || !file.open(QIODevice::ReadOnly))
        return _fail(QStringLiteral("无法读取本地任务包文件"));
    if (file.size() > MultiDetectMissionWire::kMaximumDocumentBytes)
        return _fail(QStringLiteral("任务包超过 64 KiB 传输上限"));
    const QByteArray data = file.readAll();
    // Validate the bytes before QString conversion can replace malformed UTF-8.
    QJsonObject document;
    QString error;
    if (!MultiDetectMissionPackage::parse(data, &document, &error))
        return _fail(error);
    setPackageJson(QString::fromUtf8(data));
    _state = QStringLiteral("任务包已载入；尚未上传");
    emit stateChanged();
    return _valid;
}

bool MultiDetectMissionController::saveFile(const QUrl& url)
{
    if (!validate())
        return _fail(_validation);
    const QString path = _localPath(url);
    QSaveFile file(path);
    const QByteArray data = _json.toUtf8();
    if (path.isEmpty() || !file.open(QIODevice::WriteOnly) || file.write(data) != data.size() || !file.commit())
        return _fail(QStringLiteral("无法保存本地任务包文件"));
    _state = QStringLiteral("已保存任务包");
    emit stateChanged();
    return true;
}

bool MultiDetectMissionController::upload()
{
    if (busy())
        return _fail(QStringLiteral("请等待当前请求确认"));
    if (!validate())
        return _fail(_validation);
    if ((_uploadGuard && !_uploadGuard()) || (_telemetry.value("flight_active").toBool() && telemetryFresh()))
        return _fail(QStringLiteral("飞行中不能替换任务包"));
    QJsonObject signedDocument = _document;
    QString signingError;
    if (_missionPrivateSeed.isEmpty()) {
        if (_requireMissionSignature)
            return _fail(QStringLiteral("REAL 模式必须配置 Ed25519 mission operator key"));
    } else if (!MultiDetectMissionSecurity::signDocument(&signedDocument, _missionPrivateSeed, _missionKeyId,
                                                         &signingError)) {
        return _fail(signingError);
    }
    _document = signedDocument;
    _uploadDocument = MultiDetectMissionSecurity::canonicalJson(signedDocument);
    _uploadToken = token();
    _progress = 0;
    _pending = {};
    _pending.type = 28;
    _pending.token = _uploadToken;
    _beginChunk();
    return _sendPending();
}

void MultiDetectMissionController::_beginChunk()
{
    const QByteArray& document = _pending.type == 37 ? _commandDocument : _uploadDocument;
    _pending.body = MultiDetectMissionWire::chunk(_pending.token, _pending.index, document);
    _pending.attempts = 0;
    _state = QStringLiteral("上传分片 %1 / %2；等待机载确认")
                 .arg(_pending.index + 1)
                 .arg((document.size() + 43) / 44);
    emit stateChanged();
}

bool MultiDetectMissionController::_sendPending()
{
    if (!_sender || !_sender(_pending.type, _pending.body)) {
        _pending = {};
        return _fail(QStringLiteral("签名的 Jetson 链路不可用；请求未完成"));
    }
    _pending.sentAt = _clock.elapsed();
    ++_pending.attempts;
    emit stateChanged();
    return true;
}

bool MultiDetectMissionController::_fail(const QString& message)
{
    _state = message;
    emit stateChanged();
    return false;
}

bool MultiDetectMissionController::_control(int type, const QString& policy, double timeout)
{
    if (_activeMissionId.isEmpty())
        return _fail(QStringLiteral("等待机载任务编号；不能控制未知任务"));
    if (busy() && type != 30)
        return _fail(QStringLiteral("请等待当前请求确认"));
    if (type == 32 && QStringList({"return_now", "land_now"}).contains(_telemetry.value("action").toString()))
        return _fail(QStringLiteral("机载返航或降落已锁定，不能恢复原任务"));
    if (type == 33) {
        const int oldRank = policies.indexOf(_currentPolicy), newRank = policies.indexOf(policy);
        const bool timed = newRank == 1 || newRank == 2;
        if (oldRank < 0 || newRank < oldRank || !std::isfinite(timeout) || timeout < 0 ||
            (newRank == oldRank && timed && timeout > _currentPolicyTimeout))
            return _fail(QStringLiteral("飞行中只允许收紧失联策略或缩短超时"));
    }
    QString sequenceError;
    const quint64 groundSequence = MultiDetectMissionSecurity::nextGroundSequence(_activeMissionId, &sequenceError);
    if (groundSequence == 0)
        return _fail(sequenceError);
    QJsonObject groundCommand{{QStringLiteral("mission_id"), _activeMissionId},
                              {QStringLiteral("type"), type == 30 ? QStringLiteral("abort")
                                   : type == 31 ? QStringLiteral("pause")
                                   : type == 32 ? QStringLiteral("resume")
                                   : type == 33 ? QStringLiteral("policy_update")
                                                : QStringLiteral("heartbeat")},
                              {QStringLiteral("sequence"),
                               static_cast<qint64>(groundSequence)},
                              {QStringLiteral("timestamp"), QDateTime::currentMSecsSinceEpoch() / 1000.0},
                              {QStringLiteral("timestamp_version"), 1},
                              {QStringLiteral("timestamp_domain"), QStringLiteral("unix")}};
    if (type == 33) {
        groundCommand.insert(QStringLiteral("policy"), policy);
        groundCommand.insert(QStringLiteral("timeout_s"), timeout);
    }
    QString signingError;
    if (_missionPrivateSeed.isEmpty()) {
        if (_requireMissionSignature)
            return _fail(QStringLiteral("REAL mode requires an Ed25519 ground-command key"));
    } else if (!MultiDetectMissionSecurity::signGroundCommand(&groundCommand, _missionPrivateSeed, _missionKeyId,
                                                              &signingError)) {
        return _fail(signingError);
    }
    _commandDocument = MultiDetectMissionSecurity::canonicalJson(groundCommand);
    _pending = {};
    _pending.type = 37;
    _pending.commandType = type;
    _pending.token = token();
    _pending.requestedPolicy = policy;
    _pending.requestedTimeout = timeout;
    _beginChunk();
    _state = QStringLiteral("指令已发送；等待机载确认");
    return _sendPending();
}

bool MultiDetectMissionController::abortMission()
{
    return _control(30);
}

bool MultiDetectMissionController::pauseMission()
{
    return _control(31);
}

bool MultiDetectMissionController::resumeMission()
{
    return _control(32);
}

bool MultiDetectMissionController::tightenPolicy(const QString& policy, double timeout)
{
    return _control(33, policy, timeout);
}

bool MultiDetectMissionController::_planDecision(bool approve)
{
    if (_activeMissionId.isEmpty() || _plan.isEmpty())
        return _fail(QStringLiteral("尚未收到可审批的任务计划"));
    if (busy())
        return _fail(QStringLiteral("请等待当前请求确认"));
    QString sequenceError;
    const quint64 sequence = MultiDetectMissionSecurity::nextGroundSequence(_activeMissionId, &sequenceError);
    if (!sequence)
        return _fail(sequenceError);
    const QByteArray planBytes = QJsonDocument::fromVariant(_plan).toJson(QJsonDocument::Compact);
    const QString sha = QString::fromLatin1(QCryptographicHash::hash(planBytes, QCryptographicHash::Sha256).toHex());
    QJsonObject command{{QStringLiteral("mission_id"), _activeMissionId},
                        {QStringLiteral("type"), approve ? QStringLiteral("approve_plan") : QStringLiteral("reject_plan")},
                        {QStringLiteral("plan_sha256"), sha},
                        {QStringLiteral("sequence"), static_cast<qint64>(sequence)},
                        {QStringLiteral("timestamp"), QDateTime::currentMSecsSinceEpoch() / 1000.0},
                        {QStringLiteral("timestamp_version"), 1},
                        {QStringLiteral("timestamp_domain"), QStringLiteral("unix")}};
    QString signingError;
    if (!_missionPrivateSeed.isEmpty() && !MultiDetectMissionSecurity::signGroundCommand(&command, _missionPrivateSeed, _missionKeyId, &signingError))
        return _fail(signingError);
    if (_missionPrivateSeed.isEmpty() && _requireMissionSignature)
        return _fail(QStringLiteral("REAL mode requires an Ed25519 ground-command key"));
    _commandDocument = MultiDetectMissionSecurity::canonicalJson(command);
    _pending = {};
    _pending.type = 37;
    _pending.commandType = approve ? 38 : 39;
    _pending.token = token();
    _beginChunk();
    _state = approve ? QStringLiteral("已发送计划批准") : QStringLiteral("已发送计划拒绝");
    return _sendPending();
}

bool MultiDetectMissionController::approvePlan() { return _planDecision(true); }
bool MultiDetectMissionController::rejectPlan() { return _planDecision(false); }

bool MultiDetectMissionController::telemetryFresh() const
{
    return _lastStatusAt >= 0 && _clock.elapsed() - _lastStatusAt <= 5000;
}

void MultiDetectMissionController::_poll()
{
    const auto now = _clock.elapsed();
    if (busy() && now - _pending.sentAt >= 1000) {
        if (_pending.attempts >= 5) {
            _pending = {};
            _fail(QStringLiteral("机载确认超时；任务或指令状态未知，请查看机载监控"));
        } else
            _sendPending();
    }
    if (!_activeMissionId.isEmpty() && _sender && now - _lastHeartbeatAt >= 1000) {
        _lastHeartbeatAt = now;
        // A fresh random request token per heartbeat: the aircraft caches
        // receipts by (type, token), so a counter that restarts with the GCS
        // or the mission would be answered from that cache, not applied.
        (void) _sender(34,
                       MultiDetectMissionWire::command(MultiDetectOperatorProtocol::hash64(_activeMissionId), token()));
    }
    const bool fresh = telemetryFresh();
    if (fresh != _fresh) {
        _fresh = fresh;
        emit telemetryChanged();
    }
    for (auto it = _assemblies.begin(); it != _assemblies.end();) {
        if (now >= it->deadline) {
            _telemetryAssemblyBytes -= it->reservedBytes;
            it = _assemblies.erase(it);
        } else
            ++it;
    }
}

void MultiDetectMissionController::receive(int type, const QByteArray& body)
{
    QVariantMap fields;
    QString error;
    if (!MultiDetectMissionWire::decode(type, body, &fields, &error))
        return;
    const quint64 request = fields.value("token").toULongLong();
    const int index = fields.value("index").toInt();
    if (type == 29) {
        if (!busy() || request != _pending.token || fields.value("requestType").toInt() != _pending.type ||
            index != _pending.index)
            return;
        if (!fields.value("accepted").toBool()) {
            _pending = {};
            _fail(QStringLiteral("机载拒绝：%1").arg(fields.value("detail").toString()));
            return;
        }
        if (_pending.type == 28 || _pending.type == 37) {
            const QByteArray& document = _pending.type == 37 ? _commandDocument : _uploadDocument;
            const int count = (document.size() + 43) / 44;
            _progress = double(index + 1) / count;
            if (index + 1 < count) {
                ++_pending.index;
                _beginChunk();
                _sendPending();
                return;
            }
            if (_pending.type == 37) {
                _state = QStringLiteral("ground command accepted by onboard verifier");
            } else {
            const auto uploaded = QJsonDocument::fromJson(_uploadDocument).object();
            const QString missionId = uploaded["mission_id"].toString();
            if (_activeMissionId != missionId) {
                _logs.clear();
                _telemetry.clear();
                _plan.clear(); _findings.clear(); _alerts.clear(); _finalSummary.clear();
                _summaryPages.clear(); _summaryPageCount = 0; _finalSummaryComplete = false;
                _lastStatusSequence = _lastLogSequence = 0;
                _lastStatusAt = -1;
            }
            _activeMissionId = missionId;
            _currentPolicy = uploaded["link_loss_policy"].toObject()["action"].toString();
            _currentPolicyTimeout = uploaded["link_loss_policy"].toObject()["timeout_s"].toDouble();
            }
            _state = QStringLiteral("任务包已由机载端校验并载入；是否起飞由机载端决定");
        } else {
            // Only telemetry describes the effective policy; the supervisor may
            // clamp the deadline more conservatively than the requested timeout.
            _state = QStringLiteral("机载端已接受指令；执行结果见监控");
        }
        _pending = {};
        emit stateChanged();
        emit telemetryChanged();
        return;
    }
    if (type != 35)
        return;
    const QByteArray digest = fields.value("digest").toByteArray();
    const auto sendTelemetryAck = [this, request, index]() {
        if (_sender)
            (void) _sender(36, MultiDetectMissionWire::telemetryAck(request, index));
    };
    if (_completed.contains(request)) {
        if (_completed.value(request) == digest)
            sendTelemetryAck();
        return;
    }
    if (!_assemblies.contains(request)) {
        const qint64 required = fields.value("total").toInt();
        const auto evictOldest = [this]() {
            if (_assemblies.isEmpty())
                return false;
            auto victim = _assemblies.begin();
            for (auto it = _assemblies.begin(); it != _assemblies.end(); ++it) {
                if (it->started < victim->started)
                    victim = it;
            }
            _telemetryAssemblyBytes -= victim->reservedBytes;
            _assemblies.erase(victim);
            return true;
        };
        while ((_assemblies.size() >= 4 || _telemetryAssemblyBytes + required > kTelemetryAssemblyMemoryCap) &&
               evictOldest()) {
        }
        if (_assemblies.size() >= 4 || _telemetryAssemblyBytes + required > kTelemetryAssemblyMemoryCap)
            return;
        Assembly a;
        a.digest = digest;
        a.total = static_cast<int>(required);
        a.started = _clock.elapsed();
        a.deadline = a.started + kTelemetryAssemblyDeadlineMs;
        a.reservedBytes = required;
        a.chunks.resize(fields.value("count").toInt());
        _assemblies.insert(request, a);
        _telemetryAssemblyBytes += required;
    }
    auto& a = _assemblies[request];
    if (a.digest != digest || a.total != fields.value("total").toInt() ||
        a.chunks.size() != fields.value("count").toInt())
        return;
    const QByteArray data = fields.value("data").toByteArray();
    if (!a.chunks[index].isEmpty() && a.chunks[index] != data)
        return;
    a.chunks[index] = data;
    QByteArray document;
    bool complete = true;
    for (const auto& c : a.chunks) {
        if (c.isEmpty())
            complete = false;
        document.append(c);
    }
    if (complete) {
        if (QCryptographicHash::hash(document, QCryptographicHash::Sha256) != digest || !_adopt(document)) {
            _telemetryAssemblyBytes -= a.reservedBytes;
            _assemblies.remove(request);
            sendTelemetryAck();
            return;
        }
        if (_completed.size() >= 128)
            _completed.erase(_completed.begin());
        _completed.insert(request, digest);
        _telemetryAssemblyBytes -= a.reservedBytes;
        _assemblies.remove(request);
    }
    sendTelemetryAck();
}

bool MultiDetectMissionController::_adopt(const QByteArray& json)
{
    QJsonDocument doc;
    QString error;
    if (!MultiDetectMissionPackage::parseStrictJson(json, &doc, &error) || !doc.isObject())
        return false;
    const auto obj = doc.object();
    const QString missionId = obj["mission_id"].toString();
    const double seq = obj["sequence"].toDouble(-1);
    if (missionId.trimmed().isEmpty() || !std::isfinite(seq) || seq < 1 || seq > 9007199254740991.0 ||
        std::floor(seq) != seq || (!_activeMissionId.isEmpty() && missionId != _activeMissionId))
        return false;
    const quint64 sequence = static_cast<quint64>(seq);
    if (obj["kind"] == "status") {
        // v2 uses nested supervisor/budget/step objects; retain legacy flat aliases for existing QML.
        QJsonObject normalized = obj;
        const auto supervisor = obj.value("supervisor").toObject();
        const auto budget = obj.value("budget").toObject();
        normalized.insert("action", supervisor.value("action").toString(obj.value("action").toString()));
        normalized.insert("reason", supervisor.value("reason").toString(obj.value("reason").toString()));
        normalized.insert("policy", supervisor.value("policy").toString(obj.value("policy").toString()));
        normalized.insert("timeout_s", supervisor.value("timeout_s").toDouble(obj.value("timeout_s").toDouble()));
        normalized.insert("link", supervisor.value("link").toString());
        normalized.insert("link_silent_s", supervisor.value("link_silent_s").toDouble());
        normalized.insert("remaining_time_s", budget.value("time_left_s").toDouble(obj.value("remaining_time_s").toDouble()));
        normalized.insert("remaining_energy_wh", budget.value("energy_left_wh").toDouble(obj.value("remaining_energy_wh").toDouble()));
        const auto step = obj.value("step").toObject();
        normalized.insert("progress", step.value("progress").toDouble(obj.value("progress").toDouble()));
        normalized.insert("primitives_used", step.value("primitives").toInt(obj.value("primitives_used").toInt()));
        normalized.insert("usage_budget", step.value("budget").toInt(obj.value("usage_budget").toInt()));
        normalized.insert("query", step.value("query").toString(obj.value("query").toString()));
        normalized.insert("findings_total", obj.value("findings_total").toInt());
        normalized.insert("alerts_total", obj.value("alerts_total").toInt());
        if (!policies.contains(normalized["policy"].toString()) ||
            !QStringList({"continue", "hold", "return_now", "land_now"}).contains(normalized["action"].toString()))
            return false;
        for (const QString& key : {QString("remaining_time_s"), QString("remaining_energy_wh"), QString("timeout_s")})
            if (!normalized[key].isNull() &&
                (!normalized[key].isDouble() || !std::isfinite(normalized[key].toDouble()) || normalized[key].toDouble() < 0))
                return false;
        if (sequence <= _lastStatusSequence)
            return true;  // ACK duplicates without refreshing stale state.
        _lastStatusSequence = sequence;
        _telemetry = normalized.toVariantMap();
        _lastStatusAt = _clock.elapsed();
        _currentPolicy = normalized["policy"].toString();
        _currentPolicyTimeout = normalized["timeout_s"].toDouble();
    } else {
        if (obj["kind"].toString().trimmed().isEmpty())
            return false;
        if (sequence <= _lastLogSequence)
            return true;
        _lastLogSequence = sequence;
        _logs.append(obj.toVariantMap());
        const QString kind = obj.value("kind").toString();
        if (kind == QStringLiteral("plan")) {
            _plan = obj.toVariantMap();
        } else if (kind == QStringLiteral("finding") || kind == QStringLiteral("alert")) {
            auto& list = kind == QStringLiteral("finding") ? _findings : _alerts;
            list.append(obj.toVariantMap());
        } else if (kind == QStringLiteral("final_summary_page")) {
            const int page = obj.value("page").toInt();
            _summaryPageCount = qMax(_summaryPageCount, obj.value("pages").toInt());
            if (page > 0 && page <= 1000)
                _summaryPages.insert(page, obj.value("body").toString().toUtf8());
            _finalSummaryComplete = _summaryPageCount > 0 && _summaryPages.size() == _summaryPageCount;
            if (_finalSummaryComplete) {
                QByteArray body;
                for (int i = 1; i <= _summaryPageCount; ++i) body += _summaryPages.value(i);
                QJsonParseError parseError;
                const auto summaryDoc = QJsonDocument::fromJson(body, &parseError);
                if (parseError.error == QJsonParseError::NoError && summaryDoc.isObject())
                    _finalSummary = summaryDoc.object().toVariantMap();
            }
        }
        while (_logs.size() > 50)
            _logs.removeFirst();
    }
    _activeMissionId = missionId;
    emit stateChanged();
    emit telemetryChanged();
    return true;
}
