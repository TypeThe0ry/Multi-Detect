#include <QtCore/QCoreApplication>
#include <QtCore/QDataStream>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QSettings>
#include <cstdio>

#include "MultiDetectMissionController.h"
#include "MultiDetectMissionPackage.h"
#include "MultiDetectMissionWire.h"
#include "MultiDetectOperatorProtocol.h"

namespace {
int failures = 0;

void check(bool condition, const char* description)
{
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", description);
        ++failures;
    }
}

QByteArray ack(quint64 token, int type, int index, bool accepted)
{
    QByteArray result;
    QDataStream out(&result, QIODevice::WriteOnly);
    out.setByteOrder(QDataStream::BigEndian);
    out << token << quint8(type) << quint16(index) << quint8(accepted);
    return result;
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

    MultiDetectMissionController controller;
    check(!controller.valid(), "blank draft cannot upload");
    controller.setPackageJson(QString::fromUtf8(package));
    check(controller.valid(), "valid package updates model");
    QList<QPair<int, QByteArray>> sent;
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
    int commandIterations = 0;
    while (controller.busy() && commandIterations++ < 256) {
        const auto t = fields.value("token").toULongLong();
        const auto i = fields.value("index").toInt();
        controller.receive(29, ack(t, 37, i, true));
        if (controller.busy())
            check(MultiDetectMissionWire::decode(37, sent.last().second, &fields, &error), "next command chunk parses");
    }
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
    controller.shutdown();
    std::printf("MultiDetectMissionSelfTest: %d failures\n", failures);
    return failures == 0 ? 0 : 1;
}
