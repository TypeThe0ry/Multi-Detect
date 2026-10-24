#include "MultiDetectMissionController.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QDateTime>
#include <QtCore/QFile>
#include <QtCore/QJsonDocument>
#include <QtCore/QRandomGenerator>
#include <QtCore/QSaveFile>
#include <QtCore/QtMath>
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

// Read once, then remove from the process environment like the legacy seed.
MultiDetectMissionSecurity::SigningKey takeSigningKey(const char* name, QString* error)
{
    QByteArray text = qgetenv(name).trimmed();
    qunsetenv(name);
    MultiDetectMissionSecurity::SigningKey key;
    if (!text.isEmpty() && !MultiDetectMissionSecurity::parseSigningKey(text, &key, error))
        *error = QString::fromLatin1(name) + QStringLiteral(": ") + *error;
    std::fill(text.begin(), text.end(), '\0');
    return key;
}

// Planning a prompt-only package onboard can take minutes; the operator is
// told the result is unknown only after this long without a verdict.
constexpr qint64 kCompileVerdictTimeoutMs = 15 * 60 * 1000;
// While compiling, re-query the verdict by resending the last chunk.  The
// aircraft answers it idempotently ("编译中" or the cached verdict), which
// also recovers a lost verdict ACK.
constexpr qint64 kCompileVerdictQueryMs = 5000;
const QString kCompilingMarker = QStringLiteral("编译中");
}  // namespace

MultiDetectMissionController::MultiDetectMissionController(QObject* parent) : QObject(parent)
{
    // Deliberately invalid until the operator supplies the actual flight limits.
    setPackageJson(QStringLiteral(
        R"({"mission_id":"","task_text":"","places":{},"zones":{},"routes":{},"ref_labels":{},"operating_area":{"north_min_m":0,"north_max_m":0,"east_min_m":0,"east_max_m":0,"min_altitude_m":0,"max_altitude_m":0},"budget":{"time_s":0,"reserve_time_s":0,"energy_wh":0,"reserve_energy_wh":0},"home":[0,0],"risk_level":"medium","link_loss_policy":{"action":"continue_then_return","timeout_s":0},"heartbeat_timeout_s":5,"oversight_steps":[]})"));
    _clock.start();
    // Package and ground-command keys mirror the aircraft: its --operator-key
    // verifies both by default; a separate ground-command key may be set.
    const QByteArray seed = configuredPrivateSeed();
    if (!seed.isEmpty())
        _packageKey = MultiDetectMissionSecurity::SigningKey{QStringLiteral("ed25519"), seed};
    QString keyError;
    MultiDetectMissionSecurity::SigningKey operatorKey = takeSigningKey("MULTIDETECT_OPERATOR_SIGNING_KEY", &keyError);
    if (operatorKey.isValid()) {
        _packageKey.wipe();
        _packageKey = operatorKey;
    }
    QString commandKeyError;
    MultiDetectMissionSecurity::SigningKey commandKey =
        takeSigningKey("MULTIDETECT_GROUND_COMMAND_KEY", &commandKeyError);
    _commandKey = commandKey.isValid() ? commandKey : _packageKey;
    operatorKey.wipe();
    commandKey.wipe();
    // A configured but unusable key fails closed instead of sending unsigned.
    _keyError = !keyError.isEmpty() ? keyError : commandKeyError;
    _missionKeyId = qEnvironmentVariable("MULTIDETECT_OPERATOR_KEY_ID", "default").trimmed();
    _requireMissionSignature = qEnvironmentVariable("MULTIDETECT_EXPECTED_RUNTIME_MODE").trimmed().toUpper() ==
                               QStringLiteral("REAL");
    const QString configuredPackage = qEnvironmentVariable("MULTIDETECT_MISSION_PACKAGE_PATH").trimmed();
    if (!configuredPackage.isEmpty())
        loadEnvironmentPackage();
    _timer.setInterval(250);
    connect(&_timer, &QTimer::timeout, this, &MultiDetectMissionController::_poll);
    _timer.start();
}

MultiDetectMissionController::~MultiDetectMissionController()
{
    shutdown();
    _packageKey.wipe();
    _commandKey.wipe();
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

bool MultiDetectMissionController::loadEnvironmentPackage()
{
    const QString path = qEnvironmentVariable("MULTIDETECT_MISSION_PACKAGE_PATH").trimmed();
    if (path.isEmpty())
        return false;
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly))
        return _fail(QStringLiteral("预选任务包不存在或无法读取：%1").arg(path));
    if (file.size() > MultiDetectMissionWire::kMaximumDocumentBytes)
        return _fail(QStringLiteral("预选任务包超过 64 KiB 传输上限"));
    const QByteArray data = file.readAll();
    QJsonObject document;
    QString error;
    if (!MultiDetectMissionPackage::parse(data, &document, &error))
        return _fail(QStringLiteral("预选任务包校验失败：%1").arg(error));
    if (!MultiDetectMissionSecurity::verifyDocument(document, _packageKey, &error))
        return _fail(QStringLiteral("预选任务包签名校验失败：%1").arg(error));
    setPackageJson(QString::fromUtf8(data));
    _state = QStringLiteral("已预选任务包；尚未上传");
    emit stateChanged();
    return true;
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
    if (!_keyError.isEmpty())
        return _fail(_keyError);
    QJsonObject signedDocument = _document;
    QString signingError;
    if (!_packageKey.isValid()) {
        if (_requireMissionSignature)
            return _fail(QStringLiteral("REAL 模式必须配置任务包签名密钥"));
    } else if (!MultiDetectMissionSecurity::signDocument(&signedDocument, _packageKey, _missionKeyId,
                                                         &signingError)) {
        return _fail(signingError);
    }
    _document = signedDocument;
    _uploadDocument = MultiDetectMissionSecurity::canonicalJson(signedDocument);
    _uploadMissionId = signedDocument.value(QStringLiteral("mission_id")).toString();
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
    QJsonObject groundCommand{{QStringLiteral("mission_id"), _activeMissionId},
                              {QStringLiteral("type"), type == 30 ? QStringLiteral("abort")
                                   : type == 31 ? QStringLiteral("pause")
                                   : type == 32 ? QStringLiteral("resume")
                                   : type == 33 ? QStringLiteral("policy_update")
                                                : QStringLiteral("heartbeat")}};
    if (type == 33) {
        // The supervisor parses this with mission_package.parse_policy.
        groundCommand.insert(QStringLiteral("policy"),
                             QJsonObject{{QStringLiteral("action"), policy}, {QStringLiteral("timeout_s"), timeout}});
    }
    QString signingError;
    if (!_prepareCommand(&groundCommand, &signingError))
        return _fail(signingError);
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

bool MultiDetectMissionController::_prepareCommand(QJsonObject* command, QString* error)
{
    if (!_keyError.isEmpty()) {
        *error = _keyError;
        return false;
    }
    if (!_commandKey.isValid()) {
        if (_requireMissionSignature) {
            *error = QStringLiteral("REAL 模式必须配置地面命令签名密钥");
            return false;
        }
        // Unsigned SIM/test commands carry no sequence or timestamp: the
        // aircraft stamps them with its own clock, and a GCS wall-clock stamp
        // would fall outside its flight window (mission_supervisor.ground_event).
        return true;
    }
    // Signed commands follow the ground_event contract: per-mission sequence
    // high-water, versioned Unix timestamp and the configured key's signature.
    QString sequenceError;
    const QString missionId = command->value(QStringLiteral("mission_id")).toString();
    const quint64 sequence = MultiDetectMissionSecurity::nextGroundSequence(missionId, &sequenceError);
    if (!sequence) {
        *error = sequenceError;
        return false;
    }
    command->insert(QStringLiteral("sequence"), static_cast<qint64>(sequence));
    command->insert(QStringLiteral("timestamp"), QDateTime::currentMSecsSinceEpoch() / 1000.0);
    command->insert(QStringLiteral("timestamp_version"), 1);
    command->insert(QStringLiteral("timestamp_domain"), QStringLiteral("unix"));
    return MultiDetectMissionSecurity::signGroundCommand(command, _commandKey, _missionKeyId, error);
}

bool MultiDetectMissionController::planAwaitingDecision() const
{
    const QString digest = _plan.value(QStringLiteral("plan_sha256")).toString();
    return _plan.value(QStringLiteral("requires_approval")).toBool() && !digest.isEmpty() &&
           !_decidedPlans.contains(digest) && !(_pending.type == 37 && _pending.planDigest == digest);
}

bool MultiDetectMissionController::_planDecision(bool approve)
{
    // Approve exactly the plan the aircraft announced: its own plan_sha256
    // covers mission id, package and plan, so it is never recomputed here.
    const QString digest = _plan.value(QStringLiteral("plan_sha256")).toString();
    const QString missionId = _plan.value(QStringLiteral("mission_id")).toString();
    if (_plan.isEmpty() || digest.isEmpty())
        return _fail(QStringLiteral("尚未收到可审批的任务计划"));
    if (missionId.isEmpty() || missionId != _activeMissionId)
        return _fail(QStringLiteral("计划所属任务与当前任务不一致，不能审批"));
    if (_decidedPlans.contains(digest))
        return _fail(QStringLiteral("该计划已作出决定"));
    if (busy())
        return _fail(QStringLiteral("请等待当前请求确认"));
    QJsonObject command{{QStringLiteral("mission_id"), missionId},
                        {QStringLiteral("type"), approve ? QStringLiteral("approve_plan") : QStringLiteral("reject_plan")},
                        {QStringLiteral("plan_sha256"), digest}};
    QString signingError;
    if (!_prepareCommand(&command, &signingError))
        return _fail(signingError);
    _commandDocument = MultiDetectMissionSecurity::canonicalJson(command);
    _pending = {};
    _pending.type = 37;
    _pending.commandType = approve ? 38 : 39;
    _pending.token = token();  // every command is a new transfer
    _pending.planDigest = digest;
    _beginChunk();
    _planDecisionState = approve ? QStringLiteral("批准已发送，等待机载确认") : QStringLiteral("拒绝已发送，等待机载确认");
    _state = approve ? QStringLiteral("已发送计划批准") : QStringLiteral("已发送计划拒绝");
    return _sendPending();
}
bool MultiDetectMissionController::approvePlan() { return _planDecision(true); }
bool MultiDetectMissionController::rejectPlan() { return _planDecision(false); }

QVariantMap MultiDetectMissionController::coordinateForOffset(double northM, double eastM, double originLatitude,
                                                               double originLongitude) const
{
    if (!std::isfinite(northM) || !std::isfinite(eastM) || !std::isfinite(originLatitude) ||
        !std::isfinite(originLongitude) || std::abs(originLatitude) >= 90.0 || std::abs(originLongitude) > 180.0)
        return {};
    constexpr double a = 6378137.0;
    constexpr double e2 = 0.0066943799901413165;
    const double latitude = qDegreesToRadians(originLatitude);
    const double sinLat = std::sin(latitude);
    const double denominator = std::sqrt(1.0 - e2 * sinLat * sinLat);
    const double primeVertical = a / denominator;
    const double meridional = a * (1.0 - e2) / (denominator * denominator * denominator);
    const double cosine = std::cos(latitude);
    if (std::abs(cosine) < 1e-10)
        return {};
    const double resultLatitude = originLatitude + qRadiansToDegrees(northM / meridional);
    const double resultLongitude = originLongitude + qRadiansToDegrees(eastM / (primeVertical * cosine));
    if (std::abs(resultLatitude) > 90.0 || std::abs(resultLongitude) > 180.0)
        return {};
    return {{QStringLiteral("latitude"), resultLatitude}, {QStringLiteral("longitude"), resultLongitude}};
}

bool MultiDetectMissionController::geoProjectionSelfTest()
{
    MultiDetectMissionController controller;
    const auto equator = controller.coordinateForOffset(100.0, 100.0, 0.0, 0.0);
    const auto highLatitude = controller.coordinateForOffset(100.0, 100.0, 80.0, 0.0);
    const double latitude = qDegreesToRadians(80.0);
    const double primeVertical = 6378137.0 / std::sqrt(1.0 - 0.0066943799901413165 * std::sin(latitude) *
                                                                  std::sin(latitude));
    const double highEastM = qDegreesToRadians(highLatitude.value(QStringLiteral("longitude")).toDouble()) *
                             primeVertical * std::cos(latitude);
    return std::abs(equator.value(QStringLiteral("latitude")).toDouble() - 0.00090437) < 2e-7 &&
           std::abs(equator.value(QStringLiteral("longitude")).toDouble() - 0.00089832) < 2e-7 &&
           std::abs(highEastM - 100.0) < 0.02 &&
           controller.coordinateForOffset(0.0, 0.0, 91.0, 0.0).isEmpty();
}

bool MultiDetectMissionController::telemetryFresh() const
{
    return _lastStatusAt >= 0 && _clock.elapsed() - _lastStatusAt <= 5000;
}

void MultiDetectMissionController::_poll()
{
    const auto now = _clock.elapsed();
    if (_summaryDeadline >= 0 && !_finalSummaryComplete && now >= _summaryDeadline) {
        _summaryPages.clear();
        _summaryDeadline = -1;
        _summaryAssemblyInvalid = true;
        _finalSummaryState = QStringLiteral("摘要未完整接收（分页超时）");
        emit telemetryChanged();
    }
    if (busy() && _pending.awaitingVerdict) {
        if (now >= _pending.verdictDeadline) {
            _pending = {};
            _fail(QStringLiteral("机载编译结论超时；任务包是否被接纳未知，请查看机载监控"));
        } else if (now - _pending.sentAt >= kCompileVerdictQueryMs) {
            _sendPending();
        }
    } else if (busy() && now - _pending.sentAt >= 1000) {
        if (_pending.attempts >= 5) {
            if (_pending.type == 37 && !_pending.planDigest.isEmpty())
                _planDecisionState = QStringLiteral("审批命令确认超时；结果未知，可重新发送");
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
        const QString detail = fields.value("detail").toString();
        if (!fields.value("accepted").toBool()) {
            if (_pending.type == 28)
                _uploadMissionId.clear();  // refused: its telemetry cannot follow
            if (_pending.type == 37 && !_pending.planDigest.isEmpty())
                _planDecisionState = QStringLiteral("机载拒绝审批命令：%1").arg(detail);
            _pending = {};
            _fail(QStringLiteral("机载拒绝：%1").arg(detail));
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
            if (_pending.type == 28 && detail.contains(kCompilingMarker)) {
                // First ACK of the last chunk: received, the aircraft is
                // planning.  The verdict is a second ACK for this token/index;
                // until then the mission is not admitted (no heartbeats).
                if (!_pending.awaitingVerdict) {
                    _pending.awaitingVerdict = true;
                    _pending.verdictDeadline = _clock.elapsed() + kCompileVerdictTimeoutMs;
                }
                _pending.attempts = 0;
                _state = QStringLiteral("任务包已送达，机载编译中；等待机载最终结论");
                emit stateChanged();
                return;
            }
            if (_pending.type == 37) {
                if (!_pending.planDigest.isEmpty()) {
                    _decidedPlans.insert(_pending.planDigest);
                    _planDecisionState = _pending.commandType == 38 ? QStringLiteral("机载已接受计划批准")
                                                                    : QStringLiteral("机载已接受计划拒绝");
                }
                _state = QStringLiteral("机载端已接受地面命令");
            } else {
                _admitUpload(detail);
                _pending = {};
                emit stateChanged();
                emit telemetryChanged();
                return;
            }
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
        if (_completed.value(request) == digest) {
            sendTelemetryAck();
            return;
        }
        // Same token, different document: a new transfer (e.g. after an
        // onboard restart), not a conflicting retry of the completed one.
        _completed.remove(request);
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

void MultiDetectMissionController::_beginMission(const QString& missionId)
{
    if (_activeMissionId == missionId)
        return;
    _logs.clear();
    _telemetry.clear();
    _plan.clear();
    _findings.clear();
    _alerts.clear();
    _finalSummary.clear();
    _finalSummaryState.clear();
    _summaryPages.clear();
    _summaryPageCount = 0;
    _summaryDeadline = -1;
    _summaryAssemblyInvalid = false;
    _finalSummaryComplete = false;
    _planDecisionState.clear();
    _lastStatusSequence = _lastLogSequence = 0;
    _lastStatusAt = -1;
    _activeMissionId = missionId;
}

void MultiDetectMissionController::_admitUpload(const QString& detail)
{
    const auto uploaded = QJsonDocument::fromJson(_uploadDocument).object();
    _beginMission(uploaded["mission_id"].toString());
    _currentPolicy = uploaded["link_loss_policy"].toObject()["action"].toString();
    _currentPolicyTimeout = uploaded["link_loss_policy"].toObject()["timeout_s"].toDouble();
    _progress = 1;
    _state = detail.isEmpty() ? QStringLiteral("任务包已由机载端校验并载入；是否起飞由机载端决定")
                              : QStringLiteral("机载结论：%1；是否起飞由机载端决定").arg(detail);
}

void MultiDetectMissionController::_switchMission(const QString& missionId)
{
    if (missionId == _activeMissionId)
        return;
    _beginMission(missionId);
    // Telemetry of the uploaded mission settles an upload whose verdict ACK
    // has not (yet) arrived: the aircraft only reports an admitted mission.
    if (_pending.type == 28 && missionId == _uploadMissionId &&
        _pending.index + 1 == (_uploadDocument.size() + 43) / 44) {
        _admitUpload({});
        _pending = {};
    }
}

bool MultiDetectMissionController::_adoptSummaryPage(const QJsonObject& page)
{
    // Every page is its own transfer and carries no log sequence; pages are
    // shown only once all of them are here and the joined text parses.
    const QJsonValue pageValue = page.value(QStringLiteral("page"));
    const QJsonValue pagesValue = page.value(QStringLiteral("pages"));
    const QJsonValue bodyValue = page.value(QStringLiteral("body"));
    const double pageNumber = pageValue.toDouble(-1), pageCount = pagesValue.toDouble(-1);
    if (!pageValue.isDouble() || !pagesValue.isDouble() || !bodyValue.isString() ||
        std::floor(pageNumber) != pageNumber || std::floor(pageCount) != pageCount || pageCount < 1 ||
        pageCount > 1000 || pageNumber < 1 || pageNumber > pageCount)
        return false;
    _switchMission(page.value(QStringLiteral("mission_id")).toString());
    if (_finalSummaryComplete)
        return true;  // a late duplicate of an already assembled summary
    if (_summaryAssemblyInvalid) {
        // A retransmitted first page may recover a malformed or conflicting
        // assembly; never expose the previous partial bytes while retrying.
        _summaryPages.clear();
        _summaryPageCount = 0;
        _summaryAssemblyInvalid = false;
        _summaryDeadline = -1;
    }
    const int pages = static_cast<int>(pageCount);
    if (_summaryPageCount != 0 && _summaryPageCount != pages) {
        _summaryPages.clear();
        _summaryDeadline = -1;
        _summaryAssemblyInvalid = true;
        _finalSummaryState = QStringLiteral("摘要未完整接收（分页总数冲突）");
        return true;
    }
    if (_summaryPageCount == 0) {
        _summaryPageCount = pages;
        _summaryDeadline = _clock.elapsed() + 5000;
        _finalSummary.clear();
        _finalSummaryState = QStringLiteral("摘要接收中（%1 / %2 页）").arg(_summaryPages.size()).arg(pages);
    }
    const int number = static_cast<int>(pageNumber);
    const QByteArray body = bodyValue.toString().toUtf8();
    if (_summaryPages.contains(number) && _summaryPages.value(number) != body) {
        _summaryPages.clear();
        _summaryDeadline = -1;
        _summaryAssemblyInvalid = true;
        _finalSummaryState = QStringLiteral("摘要未完整接收（重复页内容冲突）");
        return true;
    }
    _summaryPages.insert(number, body);
    _finalSummaryState = QStringLiteral("摘要接收中（%1 / %2 页）").arg(_summaryPages.size()).arg(pages);
    if (_summaryPages.size() < _summaryPageCount)
        return true;
    QByteArray joined;
    for (int i = 1; i <= _summaryPageCount; ++i)
        joined += _summaryPages.value(i);
    QJsonDocument summary;
    QString error;
    if (!MultiDetectMissionPackage::parseStrictJson(joined, &summary, &error) || !summary.isObject()) {
        _summaryPages.clear();
        _summaryPageCount = 0;
        _summaryDeadline = -1;
        _summaryAssemblyInvalid = true;
        _finalSummaryState = QStringLiteral("摘要未完整接收（JSON 非法）");
        return true;
    }
    _finalSummary = summary.object().toVariantMap();
    _finalSummaryComplete = true;
    _summaryDeadline = -1;
    _finalSummaryState = QStringLiteral("摘要已完整接收");
    _logs.append(QVariantMap{{QStringLiteral("kind"), QStringLiteral("final_summary")},
                             {QStringLiteral("mission_id"), page.value(QStringLiteral("mission_id")).toString()},
                             {QStringLiteral("assembled_pages"), _summaryPageCount},
                             {QStringLiteral("summary"), _finalSummary}});
    while (_logs.size() > 50)
        _logs.removeFirst();
    return true;
}

bool MultiDetectMissionController::_adopt(const QByteArray& json)
{
    QJsonDocument doc;
    QString error;
    if (!MultiDetectMissionPackage::parseStrictJson(json, &doc, &error) || !doc.isObject())
        return false;
    const auto obj = doc.object();
    const QString missionId = obj["mission_id"].toString();
    if (missionId.trimmed().isEmpty())
        return false;
    // Only the current mission, or the one just uploaded: its telemetry proves
    // admission even when the verdict ACK was lost.  With neither known (a
    // GCS started after the aircraft loaded its package) the first is adopted.
    if (missionId != _activeMissionId && !(_activeMissionId.isEmpty() && _uploadMissionId.isEmpty()) &&
        missionId != _uploadMissionId)
        return false;
    const QString kind = obj.value(QStringLiteral("kind")).toString();
    if (kind == QStringLiteral("final_summary_page")) {
        if (!_adoptSummaryPage(obj))
            return false;
        emit stateChanged();
        emit telemetryChanged();
        return true;
    }
    const double seq = obj["sequence"].toDouble(-1);
    if (!std::isfinite(seq) || seq < 1 || seq > 9007199254740991.0 || std::floor(seq) != seq)
        return false;
    const quint64 sequence = static_cast<quint64>(seq);
    QJsonObject normalized;
    if (kind == QStringLiteral("status")) {
        // v2 uses nested supervisor/budget/step objects; retain legacy flat aliases for existing QML.
        normalized = obj;
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
    } else if (kind.trimmed().isEmpty()) {
        return false;
    }
    _switchMission(missionId);
    if (kind == QStringLiteral("status")) {
        if (sequence <= _lastStatusSequence)
            return true;  // ACK duplicates without refreshing stale state.
        _lastStatusSequence = sequence;
        _telemetry = normalized.toVariantMap();
        _lastStatusAt = _clock.elapsed();
        _currentPolicy = normalized["policy"].toString();
        _currentPolicyTimeout = normalized["timeout_s"].toDouble();
    } else {
        if (sequence <= _lastLogSequence)
            return true;
        _lastLogSequence = sequence;
        _logs.append(obj.toVariantMap());
        if (kind == QStringLiteral("plan")) {
            _plan = obj.toVariantMap();
            const QString digest = obj.value(QStringLiteral("plan_sha256")).toString();
            _planDecisionState = _decidedPlans.contains(digest)  ? QStringLiteral("该计划已作出决定")
                                 : obj.value(QStringLiteral("requires_approval")).toBool()
                                     ? QStringLiteral("机载计划等待操作员批准")
                                     : QStringLiteral("机载计划无需批准");
        } else if (kind == QStringLiteral("finding") || kind == QStringLiteral("alert")) {
            auto& list = kind == QStringLiteral("finding") ? _findings : _alerts;
            list.append(obj.toVariantMap());
        } else if (kind == QStringLiteral("plan_approved") || kind == QStringLiteral("plan_rejected")) {
            const QString digest = obj.value(QStringLiteral("plan_sha256")).toString();
            if (!digest.isEmpty())
                _decidedPlans.insert(digest);
            _planDecisionState = kind == QStringLiteral("plan_approved") ? QStringLiteral("机载记录：计划已批准")
                                                                         : QStringLiteral("机载记录：计划已拒绝");
        } else if (kind == QStringLiteral("final_summary")) {
            // A summary that fits one transfer: the log record wraps it.
            const QJsonValue inner = obj.value(QStringLiteral("summary"));
            _finalSummary = inner.isObject() ? inner.toObject().toVariantMap() : obj.toVariantMap();
            _finalSummaryComplete = true;
            _finalSummaryState = QStringLiteral("摘要已完整接收");
            _summaryPages.clear();
            _summaryPageCount = 0;
            _summaryDeadline = -1;
            _summaryAssemblyInvalid = false;
        }
        while (_logs.size() > 50)
            _logs.removeFirst();
    }
    emit stateChanged();
    emit telemetryChanged();
    return true;
}
