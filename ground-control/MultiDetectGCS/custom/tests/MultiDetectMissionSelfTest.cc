#include <QtCore/QCoreApplication>
#include <QtCore/QDataStream>
#include <QtCore/QDir>
#include <QtCore/QEventLoop>
#include <QtCore/QFile>
#include <QtCore/QMessageAuthenticationCode>
#include <QtCore/QTimer>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QSettings>
#include <cstdio>

#include "MultiDetectMissionController.h"
#include "MultiDetectMissionPackage.h"
#include "MultiDetectMissionSecurity.h"
#include "MultiDetectMissionWire.h"
#include "MultiDetectOperatorProtocol.h"

#ifndef MISSION_SUMMARY_FIXTURE
#define MISSION_SUMMARY_FIXTURE "tests/fixtures/mission_summary/run11-summary-pair.json"
#endif

namespace {
int failures = 0;

void check(bool condition, const char* description)
{
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", description);
        ++failures;
    }
}

QByteArray ack(quint64 token, int type, int index, bool accepted, const QString& detail = {})
{
    QByteArray result;
    QDataStream out(&result, QIODevice::WriteOnly);
    out.setByteOrder(QDataStream::BigEndian);
    out << token << quint8(type) << quint16(index) << quint8(accepted);
    return result + detail.toUtf8();
}

using Sent = QList<QPair<int, QByteArray>>;

void spin(int milliseconds)
{
    QEventLoop loop;
    QTimer::singleShot(milliseconds, &loop, &QEventLoop::quit);
    loop.exec();
}

// Onboard downlink: one type-35 transfer per document.
void deliver(MultiDetectMissionController& controller, quint64 token, const QByteArray& document)
{
    for (int i = 0; i < (document.size() + 43) / 44; ++i)
        controller.receive(35, MultiDetectMissionWire::chunk(token, i, document));
}

int countType(const Sent& sent, int type)
{
    int n = 0;
    for (const auto& item : sent)
        n += item.first == type ? 1 : 0;
    return n;
}

// Upload the controller's package and ACK it chunk by chunk; the last chunk
// gets ``lastDetail`` (the onboard "compiling" or verdict text).
quint64 uploadUntilLast(MultiDetectMissionController& controller, Sent& sent, const QString& lastDetail)
{
    if (!controller.upload())
        return 0;
    QVariantMap fields;
    QString error;
    quint64 token = 0;
    for (int guard = 0; controller.busy() && guard < 2000; ++guard) {
        if (!MultiDetectMissionWire::decode(28, sent.last().second, &fields, &error))
            return 0;
        token = fields.value("token").toULongLong();
        const int index = fields.value("index").toInt();
        const bool last = index + 1 == fields.value("count").toInt();
        controller.receive(29, ack(token, 28, index, true, last ? lastDetail : QStringLiteral("分块已接收")));
        if (last)
            break;
    }
    return token;
}

// Run one type-37 ground command transfer; returns the reassembled JSON.
QByteArray runCommand(MultiDetectMissionController& controller, Sent& sent, bool acceptLast, quint64* tokenOut,
                      const QString& lastDetail = QStringLiteral("ground command accepted"))
{
    QVariantMap fields;
    QString error;
    QByteArray document;
    for (int guard = 0; controller.busy() && guard < 512; ++guard) {
        if (sent.isEmpty() || sent.last().first != 37 ||
            !MultiDetectMissionWire::decode(37, sent.last().second, &fields, &error))
            return {};
        const quint64 token = fields.value("token").toULongLong();
        const int index = fields.value("index").toInt();
        const bool last = index + 1 == fields.value("count").toInt();
        if (tokenOut)
            *tokenOut = token;
        if (document.size() == index * 44)
            document += fields.value("data").toByteArray();
        controller.receive(29, ack(token, 37, index, last ? acceptLast : true,
                                   last ? lastDetail : QStringLiteral("ground command chunk received")));
    }
    return document;
}

const QByteArray package =
    R"({"mission_id":"test-001","task_text":"检查大门后返航","risk_level":"medium","link_loss_policy":{"action":"continue_then_return","timeout_s":60},"heartbeat_timeout_s":5,"home":[0,0],"places":{"poi:gate":[40,0]},"zones":{"zone:people":{"centre_ne":[20,20],"radius_m":5,"risk_tag":"people","oversight_required":true}},"routes":{},"operating_area":{"north_min_m":-50,"north_max_m":100,"east_min_m":-50,"east_max_m":100,"min_altitude_m":5,"max_altitude_m":40},"budget":{"time_s":900,"reserve_time_s":180,"energy_wh":200,"reserve_energy_wh":40},"oversight_steps":[]})";
}  // namespace

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    QCoreApplication::setOrganizationName(QStringLiteral("MultiDetect"));
    QCoreApplication::setApplicationName(QStringLiteral("MissionSelfTest"));
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope,
                       QDir::tempPath() + QStringLiteral("/multidetect-security-selftest"));
    if (argc == 3 && QByteArray(argv[1]) == "--validate") {
        QFile file(QString::fromLocal8Bit(argv[2]));
        if (!file.open(QIODevice::ReadOnly))
            return 2;
        QString error;
        const bool ok = MultiDetectMissionPackage::parse(file.readAll(), nullptr, &error);
        std::puts(ok ? "accepted" : qPrintable(error));
        return ok ? 0 : 1;
    }
    QString error;
    QJsonObject document;
    check(MultiDetectMissionPackage::parse(package, &document, &error), "canonical package accepted");
    check(MultiDetectMissionController::geoProjectionSelfTest(), "WGS-84 N/E projection including high latitude");
    check(!MultiDetectMissionPackage::parse(package.left(package.size() - 1) + ",\"mission_id\":\"duplicate\"}",
                                            nullptr, &error),
          "duplicate keys rejected");
    auto invalid = document;
    invalid["budget"] =
        QJsonObject{{"time_s", 10}, {"reserve_time_s", 10}, {"energy_wh", 20}, {"reserve_energy_wh", 1}};
    check(!MultiDetectMissionPackage::validate(invalid, &error), "reserve equality rejected");
    invalid = document;
    invalid["home"] = QJsonArray{101, 0};
    check(!MultiDetectMissionPackage::validate(invalid, &error), "home outside area rejected");
    invalid = document;
    invalid["link_loss_policy"] = QJsonObject{{"action", "hold_then_return"}};
    check(!MultiDetectMissionPackage::validate(invalid, &error), "timed policy needs timeout");
    invalid = document;
    invalid["unknown"] = 1;
    check(!MultiDetectMissionPackage::validate(invalid, &error), "unknown package field rejected");

    // Read-only onboard sample: keep the fixture as the source for the new
    // final-summary shape without modifying it during the self-test.
    QFile fixture(QStringLiteral(MISSION_SUMMARY_FIXTURE));
    check(fixture.open(QIODevice::ReadOnly), "run11 summary fixture is available read-only");
    if (fixture.isOpen()) {
        const QJsonObject sample = QJsonDocument::fromJson(fixture.readAll()).object().value("final_summary").toObject();
        check(sample.value("count_lower_bounds").toObject().value("s1").toBool() &&
                  sample.value("counts").toObject().value("s1").toInt() == 3,
              "run11 fixture supplies lower-bound final summary fields");
    }

    MultiDetectOperatorProtocol protocol(QByteArray(32, 'x'), QStringLiteral("camera-main"));
    const auto command = MultiDetectMissionWire::command(0x0102030405060708ULL, 0x1112131415161718ULL);
    const auto encoded =
        protocol.encodeMission(MultiDetectOperatorProtocol::MessageType::MissionAbort, command, 7, 1000, &error);
    check(
        encoded.toHex() ==
            "4d44021e00000000000700000000000003e8001001020304050607081112131415161718d3491dd7c5ac843f059f9b22c1ac5c03",
        "Python/C++ v2 golden bytes match");
    MultiDetectOperatorProtocol::DecodedPacket packet;
    check(protocol.decode(encoded, &packet, &error) && packet.fields.value("body").toByteArray() == command,
          "v2 authenticated round trip");
    auto tampered = encoded;
    tampered[25] ^= 1;
    check(!protocol.decode(tampered, &packet, &error), "tampered payload rejected");
    check(
        protocol.encodeMission(MultiDetectOperatorProtocol::MessageType::MissionPolicyTighten, command, 1, 1000, &error)
            .isEmpty(),
        "wrong body size rejected");
    const auto chunk = MultiDetectMissionWire::chunk(99, 0, package);
    check(protocol.encodeMission(MultiDetectOperatorProtocol::MessageType::MissionPackageChunk, chunk, 1, 1000, &error)
                  .size() == 128,
          "full fragment fits 128 bytes");

    const QByteArray preloadKey(32, 'p');
    const QString preloadPath = QDir::temp().filePath(QStringLiteral("multidetect-preselected-package.json"));
    QJsonObject signedPackage = document;
    check(MultiDetectMissionSecurity::signDocument(&signedPackage,
          MultiDetectMissionSecurity::SigningKey{QStringLiteral("hmac-sha256"), preloadKey},
          QStringLiteral("default"), &error), "preselected fixture signed");
    QFile preloadFile(preloadPath);
    check(preloadFile.open(QIODevice::WriteOnly) && preloadFile.write(QJsonDocument(signedPackage).toJson()) > 0,
          "preselected fixture written");
    preloadFile.close();
    qunsetenv("MULTIDETECT_MISSION_PACKAGE_PATH");
    { MultiDetectMissionController absent; check(!absent.valid(), "path absent leaves draft unchanged"); }
    qputenv("MULTIDETECT_MISSION_PACKAGE_PATH", preloadPath.toUtf8());
    qputenv("MULTIDETECT_OPERATOR_SIGNING_KEY", QByteArray("hmac:") + preloadKey.toHex());
    { MultiDetectMissionController loaded; check(loaded.valid() && loaded.uploadState().contains(QStringLiteral("预选")), "signed package preselected"); }
    qputenv("MULTIDETECT_MISSION_PACKAGE_PATH", (preloadPath + QStringLiteral(".missing")).toUtf8());
    qputenv("MULTIDETECT_OPERATOR_SIGNING_KEY", QByteArray("hmac:") + preloadKey.toHex());
    { MultiDetectMissionController missing; check(missing.uploadState().contains(QStringLiteral("不存在")), "missing package reported"); }
    signedPackage[QStringLiteral("signature")] = QStringLiteral("bad");
    check(preloadFile.open(QIODevice::WriteOnly) && preloadFile.write(QJsonDocument(signedPackage).toJson()) > 0,
          "invalid signature fixture written");
    preloadFile.close();
    qputenv("MULTIDETECT_MISSION_PACKAGE_PATH", preloadPath.toUtf8());
    qputenv("MULTIDETECT_OPERATOR_SIGNING_KEY", QByteArray("hmac:") + preloadKey.toHex());
    { MultiDetectMissionController bad; check(bad.uploadState().contains(QStringLiteral("签名校验失败")), "bad signature reported"); }
    qunsetenv("MULTIDETECT_MISSION_PACKAGE_PATH");
    qunsetenv("MULTIDETECT_OPERATOR_SIGNING_KEY");

    MultiDetectMissionController controller;
    check(!controller.valid(), "blank draft cannot upload");
    controller.setPackageJson(QString::fromUtf8(package));
    check(controller.valid(), "valid package updates model");
    Sent sent;
    controller.setSender([&](int type, const QByteArray& body) {
        sent.append(qMakePair(type, body));
        return true;
    });
    controller.setUploadGuard([] { return false; });
    check(!controller.upload() && sent.isEmpty(), "armed upload guard blocks transport");
    controller.setUploadGuard([] { return true; });
    check(controller.upload() && controller.busy(), "upload begins");
    QVariantMap fields;
    while (controller.busy()) {
        check(MultiDetectMissionWire::decode(28, sent.last().second, &fields, &error), "outgoing upload parses");
        const auto t = fields.value("token").toULongLong();
        const auto i = fields.value("index").toInt();
        controller.receive(29, ack(t + 1, 28, i, true));
        check(controller.busy(), "uncorrelated ACK cannot complete upload");
        controller.receive(29, ack(t, 28, i, true));
    }
    check(!controller.busy(), "signed command transfer completes");
    check(controller.activeMissionId() == "test-001" && controller.uploadProgress() == 1,
          "last ACK admits exact uploaded mission");
    check(!controller.tightenPolicy("continue", 0), "weaker policy rejected locally");
    check(!controller.tightenPolicy("continue_then_return", 61), "same policy cannot extend timeout");
    check(controller.tightenPolicy("hold_then_return", 30), "stronger policy sent");
    check(MultiDetectMissionWire::decode(37, sent.last().second, &fields, &error), "signed command chunk parses");
    const QJsonObject policyCommand = QJsonDocument::fromJson(runCommand(controller, sent, true, nullptr)).object();
    check(policyCommand.value("type").toString() == "policy_update" &&
              policyCommand.value("policy").toObject().value("action").toString() == "hold_then_return" &&
              policyCommand.value("policy").toObject().value("timeout_s").toDouble() == 30,
          "policy update carries the link_loss_policy object the supervisor parses");
    check(!policyCommand.contains("timestamp") && !policyCommand.contains("sequence"),
          "unsigned command leaves timestamping to the aircraft clock");
    check(!controller.busy(), "policy command transfer completes");
    check(controller.currentPolicy() == "continue_then_return", "effective policy waits for telemetry");

    const QByteArray status =
        R"({"kind":"status","mission_id":"test-001","sequence":1,"action":"return_now","reason":"时间储备不足","policy":"hold_then_return","timeout_s":30,"remaining_time_s":180,"remaining_energy_wh":null,"flight_active":true})";
    const int count = (status.size() + 43) / 44;
    for (int i = 0; i < count; ++i)
        controller.receive(35, MultiDetectMissionWire::chunk(101, i, status));
    check(controller.telemetryFresh() && controller.currentPolicy() == "hold_then_return", "validated status adopted");
    check(!controller.resumeMission(), "terminal onboard return cannot be resumed");
    check(!controller.upload(), "fresh airborne status blocks replacing package");
    const QByteArray log = R"({"kind":"decision","mission_id":"test-001","sequence":5,"action":"return_now"})";
    for (int i = 0; i < (log.size() + 43) / 44; ++i)
        controller.receive(35, MultiDetectMissionWire::chunk(102, i, log));
    controller.receive(35, MultiDetectMissionWire::chunk(102, (log.size() - 1) / 44, log));
    check(controller.logEntries().size() == 1, "log retry deduplicated");
    const QByteArray v2 = R"({"kind":"status","mission_id":"test-001","sequence":6,"v":2,"state":"running","supervisor":{"action":"continue","policy":"return_home","timeout_s":60,"link":"up"},"budget":{"time_left_s":90,"energy_left_wh":12},"step":{"skill":"count","query":"消防栓","index":0,"total":2,"progress":0.5,"primitives":4,"budget":10},"findings_total":2,"alerts_total":1})";
    for (int i = 0; i < (v2.size() + 43) / 44; ++i)
        controller.receive(35, MultiDetectMissionWire::chunk(103, i, v2));
    check(controller.currentPolicy() == "return_home" && controller.telemetry().value("findings_total").toInt() == 2,
          "v2 nested status normalized");
    const QByteArray plan = R"({"kind":"plan","mission_id":"test-001","sequence":7,"requires_approval":true,"steps":[{"skill":"count"}],"zones":[{"n":0,"e":0,"r":5}],"origin":{"lat":47.0,"lon":8.0}})";
    for (int i = 0; i < (plan.size() + 43) / 44; ++i)
        controller.receive(35, MultiDetectMissionWire::chunk(104, i, plan));
    check(!controller.plan().isEmpty(), "plan record exposed");
    check(!controller.planAwaitingDecision() && !controller.approvePlan(),
          "a plan without the onboard plan_sha256 cannot be approved");

    // Final summary pages exactly as mission_operator sends them: no log
    // sequence, one transfer (own token, chunk indexes from 0) per page,
    // pages may arrive out of order and are shown only once all are here.
    const QByteArray page1 =
        R"({"kind":"final_summary_page","mission_id":"test-001","page":1,"pages":3,"body":"{\"status\":\"completed\",\"counts\":{\"s1\":2},\"summary_zh\":\"任务完成；s1 至少 2 个（原因：覆盖未完全证实）\",\"summary_en\":\"completed\",\"operator_summary\":{\"status\":\"completed\",\"terminal_reason\":\"done\"},"})";
    const QByteArray page2 =
        R"({"kind":"final_summary_page","mission_id":"test-001","page":2,"pages":3,"body":"\"count_lower_bounds\":{\"s1\":true},\"findings\":[{\"id\":\"f1\"}],"})";
    const QByteArray page3 =
        R"({"kind":"final_summary_page","mission_id":"test-001","page":3,"pages":3,"body":"\"alerts\":[],\"history\":[{\"step\":\"s1\",\"state\":\"done\"}]}"})";
    const QByteArray otherMissionPage =
        R"({"kind":"final_summary_page","mission_id":"someone-else","page":1,"pages":1,"body":"{\"status\":\"forged\"}"})";
    sent.clear();
    deliver(controller, 0x3000, otherMissionPage);
    check(!controller.finalSummaryComplete() && controller.summaryPagesTotal() == 0,
          "summary page of another mission is not adopted");
    deliver(controller, 0x3002, page2);
    deliver(controller, 0x3003, page3);
    check(!controller.finalSummaryComplete() && controller.finalSummary().isEmpty() &&
              controller.summaryPagesReceived() == 2 && controller.summaryPagesTotal() == 3,
          "incomplete summary is never shown");
    deliver(controller, 0x3001, page1);
    check(controller.finalSummaryComplete() && controller.finalSummary().value("status").toString() == "completed" &&
              controller.finalSummary().value("count_lower_bounds").toMap().value("s1").toBool() &&
              controller.finalSummary().value("history").toList().size() == 1,
          "all pages joined in page order into one final_summary");
    check(controller.finalSummary().value("summary_zh").toString().contains(QStringLiteral("至少")) &&
              controller.finalSummary().value("operator_summary").toMap().value("terminal_reason").toString() == "done" &&
              controller.finalSummaryState() == QStringLiteral("摘要已完整接收"),
          "new operator summary fields are retained");
    bool pageAcksRestart = false;
    for (const auto& item : sent) {
        QVariantMap ackFields;
        if (item.first == 36 && MultiDetectMissionWire::decode(36, item.second, &ackFields, &error) &&
            ackFields.value("token").toULongLong() == 0x3001 && ackFields.value("index").toInt() == 0)
            pageAcksRestart = true;
    }
    check(pageAcksRestart, "each page is acknowledged by its own token from chunk 0");
    const int acksBefore = countType(sent, 36);
    deliver(controller, 0x3001, page1);
    check(countType(sent, 36) > acksBefore && controller.finalSummaryComplete(),
          "a retransmitted page is re-acknowledged without disturbing the summary");

    // A legacy one-transfer final_summary remains valid and exposes the old fields.
    const QByteArray legacy =
        R"({"kind":"final_summary","mission_id":"test-001","sequence":9,"summary":{"status":"completed","counts":{"s1":2},"findings":[{"id":"legacy"}]}})";
    deliver(controller, 0x3004, legacy);
    check(controller.finalSummaryComplete() && controller.finalSummary().value("findings").toList().size() == 1,
          "legacy final_summary remains compatible");

    // Invalid JSON is held back as an explicit incomplete state.
    MultiDetectMissionController malformedSummary;
    malformedSummary.setPackageJson(QString::fromUtf8(package));
    deliver(malformedSummary, 0x3010,
            R"({"kind":"final_summary_page","mission_id":"test-001","page":1,"pages":1,"body":"{bad"})");
    check(!malformedSummary.finalSummaryComplete() && malformedSummary.finalSummary().isEmpty() &&
              malformedSummary.finalSummaryState().contains(QStringLiteral("摘要未完整接收")),
          "invalid summary JSON never exposes a partial result");
    malformedSummary.shutdown();

    MultiDetectMissionController missingPage;
    missingPage.setPackageJson(QString::fromUtf8(package));
    deliver(missingPage, 0x3011,
            R"({"kind":"final_summary_page","mission_id":"test-001","page":1,"pages":2,"body":"{\"status\":\"completed\","})");
    spin(5300);
    check(!missingPage.finalSummaryComplete() && missingPage.finalSummary().isEmpty() &&
              missingPage.finalSummaryState().contains(QStringLiteral("摘要未完整接收")),
          "missing summary page times out without showing a partial result");
    missingPage.shutdown();
    controller.shutdown();

    // ---- Two-stage upload verdict, heartbeats only after admission --------
    {
        MultiDetectMissionController compiling;
        QByteArray second = package;
        second.replace("test-001", "test-002");
        compiling.setPackageJson(QString::fromUtf8(second));
        Sent out;
        compiling.setSender([&](int type, const QByteArray& body) {
            out.append(qMakePair(type, body));
            return true;
        });
        compiling.setUploadGuard([] { return true; });
        const QString compilingText = QStringLiteral("任务包已接收，机载编译中");
        quint64 uploadToken = uploadUntilLast(compiling, out, compilingText);
        check(uploadToken != 0 && compiling.busy() && compiling.onboardCompiling() &&
                  compiling.activeMissionId().isEmpty(),
              "first ACK of the last chunk only says the aircraft is compiling");
        spin(700);
        check(countType(out, 34) == 0, "no mission heartbeat before the package is admitted");
        QVariantMap last;
        MultiDetectMissionWire::decode(28, out.last().second, &last, &error);
        const int lastIndex = last.value("index").toInt();
        compiling.receive(29, ack(uploadToken, 28, lastIndex, false, QStringLiteral("拒绝: 计划超出作业区")));
        check(!compiling.busy() && compiling.activeMissionId().isEmpty() &&
                  compiling.uploadState().contains(QStringLiteral("计划超出作业区")),
              "second ACK carries the refusal verdict");
        spin(400);
        check(countType(out, 34) == 0, "a refused package never starts heartbeats");

        out.clear();
        uploadToken = uploadUntilLast(compiling, out, compilingText);
        MultiDetectMissionWire::decode(28, out.last().second, &last, &error);
        compiling.receive(29, ack(uploadToken, 28, last.value("index").toInt(), true, compilingText));
        check(compiling.onboardCompiling(), "a repeated compiling ACK keeps waiting for the verdict");
        const QByteArray strayPlan =
            R"({"kind":"plan","mission_id":"other-mission","sequence":1,"plan_sha256":"cd","requires_approval":true,"steps":[]})";
        deliver(compiling, 0x4000, strayPlan);
        check(compiling.plan().isEmpty(), "plan of a mission other than the uploaded one is ignored");
        compiling.receive(29, ack(uploadToken, 28, last.value("index").toInt(), true,
                                  QStringLiteral("任务包已通过机载验证并接纳")));
        check(!compiling.busy() && compiling.activeMissionId() == "test-002",
              "second ACK of the same token/index admits the mission");
        spin(700);
        int heartbeats = 0;
        for (const auto& item : out) {
            QVariantMap beat;
            if (item.first == 34 && MultiDetectMissionWire::decode(34, item.second, &beat, &error) &&
                beat.value("missionToken").toULongLong() == MultiDetectOperatorProtocol::hash64(QStringLiteral("test-002")))
                ++heartbeats;
        }
        check(heartbeats >= 1, "heartbeats start once the mission is admitted");

        // Plan approval: exact onboard digest, mission check, new token per command.
        const QString digest = QString(64, QLatin1Char('a'));
        const QByteArray onboardPlan =
            QByteArray(R"({"kind":"plan","mission_id":"test-002","sequence":3,"requires_approval":true,"plan_sha256":")") +
            digest.toLatin1() + R"(","task_text":"数一下消防栓","steps":[{"id":"s1","skill":"count"}]})";
        deliver(compiling, 0x4001, onboardPlan);
        check(compiling.planAwaitingDecision(), "approval-required plan awaits a decision");
        out.clear();
        check(compiling.approvePlan(), "approve_plan sent");
        check(!compiling.planAwaitingDecision(), "no second decision while one is in flight");
        quint64 firstToken = 0;
        const QJsonObject refused =
            QJsonDocument::fromJson(runCommand(compiling, out, false, &firstToken,
                                               QStringLiteral("ground supervisor rejected command")))
                .object();
        check(refused.value("type").toString() == "approve_plan" &&
                  refused.value("mission_id").toString() == "test-002" &&
                  refused.value("plan_sha256").toString() == digest && !refused.contains("signature"),
              "unsigned approve_plan carries mission_id and the onboard plan_sha256");
        check(compiling.planAwaitingDecision() && compiling.planDecisionState().contains(QStringLiteral("拒绝")),
              "a refused approval can be decided again");
        out.clear();
        check(compiling.approvePlan(), "approve_plan resent");
        quint64 secondToken = 0;
        runCommand(compiling, out, true, &secondToken);
        check(firstToken != 0 && secondToken != 0 && firstToken != secondToken,
              "every plan command uses a new transfer token");
        check(!compiling.planAwaitingDecision() && !compiling.approvePlan() && !compiling.rejectPlan(),
              "an accepted decision is final");
        compiling.shutdown();
    }

    // ---- Signed approval with a configured ground-command key ------------
    {
        QByteArray keyBytes;
        for (int i = 0; i < 32; ++i)
            keyBytes.append(static_cast<char>(i));
        qputenv("MULTIDETECT_GROUND_COMMAND_KEY", "hmac:" + keyBytes.toHex());
        qputenv("MULTIDETECT_OPERATOR_KEY_ID", "gcs-test");
        MultiDetectMissionController signer;
        qunsetenv("MULTIDETECT_OPERATOR_KEY_ID");
        check(qgetenv("MULTIDETECT_GROUND_COMMAND_KEY").isEmpty(), "ground command key is removed from the environment");
        QByteArray third = package;
        third.replace("test-001", "test-003");
        signer.setPackageJson(QString::fromUtf8(third));
        Sent out;
        signer.setSender([&](int type, const QByteArray& body) {
            out.append(qMakePair(type, body));
            return true;
        });
        signer.setUploadGuard([] { return true; });
        uploadUntilLast(signer, out, QStringLiteral("任务包已通过机载验证并接纳"));
        check(signer.activeMissionId() == "test-003", "signed-command controller admits its mission");
        const QString digest = QString(64, QLatin1Char('b'));
        deliver(signer, 0x5001,
                QByteArray(R"({"kind":"plan","mission_id":"test-003","sequence":2,"requires_approval":true,"plan_sha256":")") +
                    digest.toLatin1() + R"(","steps":[]})");
        out.clear();
        check(signer.rejectPlan(), "signed reject_plan sent");
        const QJsonObject command = QJsonDocument::fromJson(runCommand(signer, out, true, nullptr)).object();
        const QByteArray canonical = MultiDetectMissionSecurity::canonicalJson(
            command, {QStringLiteral("signature"), QStringLiteral("signature_alg")});
        const QByteArray expected =
            QMessageAuthenticationCode::hash(canonical, keyBytes, QCryptographicHash::Sha256).toBase64();
        check(command.value("type").toString() == "reject_plan" && command.value("plan_sha256").toString() == digest &&
                  command.value("sequence").toDouble() >= 1 && command.value("timestamp").toDouble() > 1.6e9 &&
                  command.value("timestamp_version").toInt() == 1 &&
                  command.value("timestamp_domain").toString() == "unix" &&
                  command.value("key_id").toString() == "gcs-test" &&
                  command.value("signature_alg").toString() == "hmac-sha256" &&
                  command.value("signature").toString().toLatin1() == expected,
              "configured key signs the plan command in the ground_event format");
        signer.shutdown();
    }
    {
        qputenv("MULTIDETECT_GROUND_COMMAND_KEY", "hmac:00");
        MultiDetectMissionController misconfigured;
        misconfigured.setPackageJson(QString::fromUtf8(package));
        misconfigured.setSender([](int, const QByteArray&) { return true; });
        check(!misconfigured.upload() && misconfigured.uploadState().contains("MULTIDETECT_GROUND_COMMAND_KEY"),
              "an unusable configured key fails closed instead of sending unsigned");
        misconfigured.shutdown();
    }
    std::printf("MultiDetectMissionSelfTest: %d failures\n", failures);
    return failures == 0 ? 0 : 1;
}
