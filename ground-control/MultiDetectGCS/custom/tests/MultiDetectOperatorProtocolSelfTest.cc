#include <QtCore/QCoreApplication>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDebug>
#include <QtCore/QHash>
#include <QtCore/QMessageAuthenticationCode>
#include <QtCore/QStringList>
#include <cmath>
#include <cstdio>

#include "MultiDetectOperatorProtocol.h"
#include "MultiDetectObservationEpoch.h"
#include "MultiDetectPagedStatusSequence.h"

namespace {

int check(bool condition, const char* message)
{
    if (!condition) {
        qCritical().noquote() << "FAIL:" << message;
        std::fprintf(stderr, "FAIL: %s\n", message);
        std::fflush(stderr);
        return 1;
    }
    return 0;
}

QByteArray vector(const char* hex)
{
    return QByteArray::fromHex(QByteArray(hex));
}

QByteArray resign(QByteArray frame, const QByteArray& key)
{
    constexpr int kAuthenticationTagBytes = 16;
    frame.chop(kAuthenticationTagBytes);
    frame.append(
        QMessageAuthenticationCode::hash(frame, key, QCryptographicHash::Sha256).first(kAuthenticationTagBytes));
    return frame;
}

}  // namespace

int main(int argc, char* argv[])
{
    QCoreApplication application(argc, argv);
    Q_UNUSED(application)

    const QByteArray key("operator-link-unit-test-key-32-bytes-minimum");
    MultiDetectOperatorProtocol protocol(key, QStringLiteral("camera-main"));
    int failures = 0;
    failures += check(protocol.configured(), "protocol must accept the shared 32-byte test key");

    MultiDetectObservationEpoch observationEpoch;
    failures += check(observationEpoch.observe(0) == MultiDetectObservationEpoch::Result::Invalid,
                      "zero observation epoch must be invalid");
    failures += check(observationEpoch.observe(0x11111111U) == MultiDetectObservationEpoch::Result::Advanced &&
                          observationEpoch.activeEpoch() == 0x11111111U,
                      "first observation epoch must become active");
    failures += check(observationEpoch.observe(0x11111111U) == MultiDetectObservationEpoch::Result::Current,
                      "current observation epoch must remain current");
    failures += check(observationEpoch.observe(0x22222222U) == MultiDetectObservationEpoch::Result::Advanced &&
                          observationEpoch.activeEpoch() == 0x22222222U,
                      "new observation epoch must atomically advance");
    failures += check(observationEpoch.observe(0x11111111U) == MultiDetectObservationEpoch::Result::Retired &&
                          observationEpoch.activeEpoch() == 0x22222222U,
                      "delayed old observation epoch must never reactivate");

    QHash<int, quint32> pagedStatusSequence;
    failures += check(MultiDetectPagedStatusSequence::acceptPageSequence(&pagedStatusSequence, 1, 20),
                      "first target-pool page must be accepted");
    failures += check(MultiDetectPagedStatusSequence::acceptPageSequence(&pagedStatusSequence, 0, 19),
                      "an earlier-sequence sibling page must be accepted out of transport order");
    failures += check(!MultiDetectPagedStatusSequence::acceptPageSequence(&pagedStatusSequence, 1, 20),
                      "duplicate target-pool page must be ignored");
    failures += check(!MultiDetectPagedStatusSequence::acceptPageSequence(&pagedStatusSequence, 1, 18),
                      "stale target-pool page must be ignored per page");
    failures += check(MultiDetectPagedStatusSequence::acceptPageSequence(&pagedStatusSequence, 1, 21),
                      "newer target-pool page must replace its own earlier page");

    QString error;
    const QByteArray selection = protocol.encodeSelection(
        QStringLiteral("11111111-1111-4111-8111-111111111111"), QStringLiteral("22222222-2222-4222-8222-222222222222"),
        7, QStringLiteral("SELECT"), 1280, 720, 0, 0.32, 0.21, 0.61, 0.72, 1000125, 3000, &error);
    const QByteArray expectedSelection = vector(
        "4d44010100000000000700000000000f42bd003e11111111111141118111111111111111"
        "22222222222242228222222222222222011f875890050002d0000bb80151eb35c29c28"
        "b8510000000000000000004b1f00cb66264ea196e0684b4a7400e9");
    failures += check(error.isEmpty(), "selection encoder returned an error");
    failures +=
        check(selection == expectedSelection, "selection encoding must match the Python golden vector byte-for-byte");

    const QByteArray expectedDecision = vector(
        "4d44010700000000001600000000000f4e5c004f00000000000000650000000000000066"
        "000000000000000b000000000000000c000000000000000d000000000000000e00000000"
        "0000000f00000000000000100000000701000000000000006707d04338bd590d988726c8"
        "c7e69b10bd81d3");
    const QByteArray decision =
        protocol.encodeAuthorizationDecision(101, 102, 11, 12, 13, 14, 15, 16, 7, true, 103, 22, 1003100, 2000, &error);
    failures += check(error.isEmpty(), "authorization encoder returned an error");
    failures +=
        check(decision == expectedDecision, "authorization encoding must match the Python golden vector byte-for-byte");

    const QByteArray expectedApproachConfirmation = vector(
        "4d44010d00000000006f0000000000018a88003a000000000000012f0000000000000194"
        "000000000000006500000000000000ca0000000711111111111141118111111111111111"
        "07d00320fe015f4f66849c249244750690cf3f44711e");
    const QByteArray approachConfirmation = protocol.encodeApproachConfirmation(
        303, 404, 101, 202, 7, QStringLiteral("11111111-1111-4111-8111-111111111111"), 111, 101000, 2000, 800, 1.0,
        true, &error);
    failures += check(error.isEmpty(), "approach confirmation encoder returned an error");
    failures += check(approachConfirmation == expectedApproachConfirmation,
                      "approach confirmation encoding must match the Python golden vector byte-for-byte");

    const QByteArray expectedPayloadTargetConfirmation = vector(
        "4d4401130000000000710000000000018a88004600000000000003210000000000000322"
        "00000000000002bd00000000000002be0000000700000000000002bf0000000811111111"
        "11114111811111111111111107d00320fe011f60df56d7979ec17bbd4e39a25c918f");
    const QByteArray payloadTargetConfirmation = protocol.encodePayloadTargetConfirmation(
        801, 802, 701, 702, 7, 703, 8, QStringLiteral("11111111-1111-4111-8111-111111111111"), 113, 101000, 2000, 800,
        1.0, true, &error);
    failures += check(error.isEmpty(), "payload target confirmation encoder returned an error");
    failures += check(payloadTargetConfirmation == expectedPayloadTargetConfirmation,
                      "payload target confirmation must match the Python golden vector byte-for-byte");

    const QByteArray track = vector(
        "4d44010300000000006300000000000f433a0055f6222a1106eefe4f1111111111114111"
        "8111111111111111021f875890050002d0000144bd7e7ea668a6d601547b38529eb8bae"
        "10c736d6f6c6465725f6172656100000000e7ddca2f504ad6614dc40032fe5c033449cdf"
        "376d7757fbe0accb48e2eebf2ef");
    MultiDetectOperatorProtocol::DecodedPacket packet;
    error.clear();
    const QByteArray cancelTrack = protocol.encodeSelection(
        QStringLiteral("33333333-3333-4333-8333-333333333333"), QStringLiteral("22222222-2222-4222-8222-222222222222"),
        8, QStringLiteral("CANCEL_TRK"), 1280, 720, 0, 0.32, 0.21, 0.61, 0.72, 1000225, 3000, &error);
    failures += check(error.isEmpty(), "single-track cancel encoder returned an error");
    failures += check(protocol.decode(cancelTrack, &packet, &error), "single-track cancel must decode");
    failures += check(packet.fields.value(QStringLiteral("action")).toString() == QStringLiteral("CANCEL_TRK"),
                      "single-track cancel action mismatch");
    failures += check(packet.fields.value(QStringLiteral("bboxValid")).toBool(),
                      "single-track cancel must keep its target box");

    failures += check(protocol.decode(track, &packet, &error), "track golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::TrackStatus,
                      "track vector message type mismatch");
    failures += check(packet.sequence == 99, "track vector sequence mismatch");
    failures += check(packet.fields.value(QStringLiteral("state")).toString() == QStringLiteral("TRACKING"),
                      "track state mismatch");
    failures += check(packet.fields.value(QStringLiteral("label")).toString() == QStringLiteral("smolder_area"),
                      "track label mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("estimatedRangeM")).toDouble() - 82.0) < 0.001,
                      "track range mismatch");

    const QByteArray runtimeStatus = vector(
        "4d4401170000000000740000000000018a9c002402010b00000000000001020311223344"
        "5566778899aabbccddeeff000123456789abcdef71d117da3f779776df5cff2d9cfd8a3d");
    failures += check(runtimeStatus.size() == 72, "runtime status must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(runtimeStatus, &packet, &error), "runtime status golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::RuntimeStatus,
                      "runtime status message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("mode")).toString() == QStringLiteral("SIM") &&
                          packet.fields.value(QStringLiteral("backendHealth")).toString() ==
                              QStringLiteral("HEALTHY"),
                      "runtime mode or backend health mismatch");
    failures += check(packet.fields.value(QStringLiteral("scenarioPresent")).toBool() &&
                          packet.fields.value(QStringLiteral("groundTruthIsolated")).toBool() &&
                          !packet.fields.value(QStringLiteral("hardwareOutputsPresent")).toBool() &&
                          packet.fields.value(QStringLiteral("profileValid")).toBool(),
                      "runtime status flags mismatch");
    failures += check(packet.fields.value(QStringLiteral("scenarioSeed")).toString() == QStringLiteral("66051") &&
                          packet.fields.value(QStringLiteral("scenarioHash64")).toString() ==
                              QStringLiteral("1122334455667788") &&
                          packet.fields.value(QStringLiteral("airframeProfileHash64")).toString() ==
                              QStringLiteral("99aabbccddeeff00") &&
                          packet.fields.value(QStringLiteral("runHash64")).toString() ==
                              QStringLiteral("0123456789abcdef"),
                      "runtime status signed identity mismatch");
    failures += check(packet.fields.value(QStringLiteral("metadataOnly")).toBool() &&
                          !packet.fields.value(QStringLiteral("directPixhawkWrites")).toBool() &&
                          !packet.fields.value(QStringLiteral("physicalReleaseEnabled")).toBool(),
                      "runtime status must remain display-only");

    const QByteArray expectedRuntimeStatusAck = vector(
        "4d4401180000000000750000000000018aa1000c000000740123456789abcdef"
        "4cd881cb1e13f077518cd60dd837383e");
    const QByteArray runtimeStatusAck = protocol.encodeRuntimeStatusAck(
        116, static_cast<quint64>(0x0123456789abcdefULL), 117, 101025, &error);
    failures += check(error.isEmpty(), "runtime-status ACK encoder returned an error");
    failures += check(runtimeStatusAck == expectedRuntimeStatusAck,
                      "runtime-status ACK must match the Python golden vector byte-for-byte");
    failures += check(protocol.decode(runtimeStatusAck, &packet, &error),
                      "runtime-status ACK golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::RuntimeStatusAck &&
                          packet.fields.value(QStringLiteral("acknowledgedRuntimeSequence")).toUInt() == 116 &&
                          packet.fields.value(QStringLiteral("runHash64")).toString() ==
                              QStringLiteral("0123456789abcdef"),
                      "runtime-status ACK correlation mismatch");

    QByteArray tamperedRuntimeStatusAck = runtimeStatusAck;
    tamperedRuntimeStatusAck[25] = static_cast<char>(tamperedRuntimeStatusAck.at(25) ^ 0x01);
    failures += check(!protocol.decode(tamperedRuntimeStatusAck, &packet, &error),
                      "runtime-status ACK mutation must fail authentication");
    failures += check(protocol.encodeRuntimeStatusAck(116, 0, 117, 101025, &error).isEmpty(),
                      "runtime-status ACK with a zero run hash must not encode");

    QByteArray runtimeUnknownFlags = runtimeStatus;
    runtimeUnknownFlags[22] = static_cast<char>(0x1b);
    runtimeUnknownFlags = resign(runtimeUnknownFlags, key);
    failures += check(!protocol.decode(runtimeUnknownFlags, &packet, &error),
                      "authenticated runtime status with unknown flags must fail");

    QByteArray runtimeAbsentScenario = runtimeStatus;
    runtimeAbsentScenario[22] = static_cast<char>(0x0a);
    runtimeAbsentScenario = resign(runtimeAbsentScenario, key);
    failures += check(!protocol.decode(runtimeAbsentScenario, &packet, &error),
                      "runtime status cannot hide non-zero scenario identity fields");

    QByteArray runtimeMissingIsolation = runtimeStatus;
    runtimeMissingIsolation[22] = static_cast<char>(0x09);
    runtimeMissingIsolation = resign(runtimeMissingIsolation, key);
    failures += check(!protocol.decode(runtimeMissingIsolation, &packet, &error),
                      "non-REAL runtime status must isolate ground truth");

    QByteArray runtimeHardwareOutputs = runtimeStatus;
    runtimeHardwareOutputs[22] = static_cast<char>(0x0f);
    runtimeHardwareOutputs = resign(runtimeHardwareOutputs, key);
    failures += check(!protocol.decode(runtimeHardwareOutputs, &packet, &error),
                      "non-REAL runtime status cannot expose hardware outputs");

    QByteArray runtimeRealScenario = runtimeStatus;
    runtimeRealScenario[20] = static_cast<char>(4);
    runtimeRealScenario = resign(runtimeRealScenario, key);
    failures += check(!protocol.decode(runtimeRealScenario, &packet, &error),
                      "REAL runtime status cannot bind a scenario");

    const QByteArray mission = vector(
        "4d44010400000000006400000000000f433a0035f6222a1106eefe4f558674ce263fcabd"
        "050302010003000401ab4656dd28bdab3e01bddd3a6e5dab59faeafebb0274fff2000102"
        "7339e8df4111840a04415d7ab75ebdcb8d");
    failures += check(protocol.decode(mission, &packet, &error), "mission vector must decode");
    failures +=
        check(packet.fields.value(QStringLiteral("phase")).toString() == QStringLiteral("AWAITING_AUTHORIZATION"),
              "mission phase mismatch");
    failures += check(packet.fields.value(QStringLiteral("safetyAllowed")).toBool(), "mission safety flag mismatch");
    failures += check(packet.fields.value(QStringLiteral("remainingPayloadCount")).toInt() == 3,
                      "mission payload count mismatch");

    const QByteArray safety = vector(
        "4d44010500000000006500000000000f433a0032f6222a1106eefe4f558674ce263fcabd"
        "40b04de7833162a001ab4656dd28bdab3e0002002100000001000200000000002001ad92"
        "7fccb188533069a5a126e524e909");
    failures += check(protocol.decode(safety, &packet, &error), "safety vector must decode");
    failures += check(packet.fields.value(QStringLiteral("passCount")).toInt() == 1, "safety pass count mismatch");
    failures += check(packet.fields.value(QStringLiteral("denyCount")).toInt() == 1, "safety deny count mismatch");
    failures +=
        check(packet.fields.value(QStringLiteral("unknownCount")).toInt() == 1, "safety unknown count mismatch");
    failures += check(!packet.fields.value(QStringLiteral("allowed")).toBool(), "safety vector must remain denied");

    const QByteArray patrol = vector(
        "4d4401090000000000cf00000000000f433a004af6222a1106eefe4feb0e2ef3bb393c6c"
        "07070701020855bcbb15f86fdc51eb35c29c28b85105666c616d65000000000000000000"
        "0000ece2000a00031c7eaca291a9617c003200040369a047a4d446cdfe99f4ab9ac0f343"
        "49b7");
    failures += check(patrol.size() == 110, "patrol golden vector must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(patrol, &packet, &error), "patrol golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::PatrolStatus,
                      "patrol vector message type mismatch");
    failures += check(packet.sequence == 207, "patrol vector sequence mismatch");
    failures += check(packet.fields.value(QStringLiteral("phase")).toString() == QStringLiteral("LOST"),
                      "patrol phase mismatch");
    failures += check(packet.fields.value(QStringLiteral("targetState")).toString() == QStringLiteral("LOST"),
                      "patrol target state mismatch");
    failures += check(packet.fields.value(QStringLiteral("label")).toString() == QStringLiteral("flame"),
                      "patrol target label mismatch");
    failures += check(packet.fields.value(QStringLiteral("totalTrackCount")).toInt() == 10,
                      "patrol total track count mismatch");
    failures += check(packet.fields.value(QStringLiteral("lockedTrackCount")).toInt() == 3,
                      "patrol locked track count mismatch");
    failures += check(packet.fields.value(QStringLiteral("returnDirection")).toString() == QStringLiteral("LEFT"),
                      "patrol return direction mismatch");
    failures += check(packet.fields.value(QStringLiteral("returnValidity")).toString() == QStringLiteral("DEGRADED"),
                      "patrol return validity mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("returnEvidenceAgeS")).toDouble() - 0.4) < 0.001,
                      "patrol evidence age mismatch");
    failures +=
        check(std::abs(packet.fields.value(QStringLiteral("estimatedMinimumTurnRadiusM")).toDouble() - 87.3) < 0.001,
              "patrol minimum turn radius mismatch");
    failures += check(packet.fields.value(QStringLiteral("operatorConfirmationRequired")).toBool(),
                      "patrol return advice must require operator confirmation");
    failures += check(packet.fields.value(QStringLiteral("sitlValidationRequired")).toBool(),
                      "patrol return advice must require SITL validation");
    failures += check(!packet.fields.value(QStringLiteral("flightControlEnabled")).toBool(),
                      "patrol status must not enable flight control");

    const QByteArray range = vector(
        "4d44010a00000000006700000000000f433a005bf6222a1106eefe4fe65c7f305ba849ec5"
        "a736508010f59749b383ba6daf97c120269001000000009000004d2041c04ae04f703fd043b"
        "fb2e87ce007d00000400ffffff1d003200017f04a004cc0029015e04da00460000000000009d"
        "ad2029d901e148d6a6ab1d0f2ea6f0");
    failures += check(range.size() == 127, "range golden vector must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(range, &packet, &error), "range golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::RangeStatus,
                      "range vector message type mismatch");
    failures += check(packet.sequence == 103, "range vector sequence mismatch");
    failures += check(packet.fields.value(QStringLiteral("validity")).toString() == QStringLiteral("DEGRADED"),
                      "range validity mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("slantRangeM")).toDouble() - 123.4) < 0.001,
                      "range slant distance mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("slantRangeLowM")).toDouble() - 119.8) < 0.001 &&
                          std::abs(packet.fields.value(QStringLiteral("slantRangeHighM")).toDouble() - 127.1) < 0.001,
                      "range confidence interval mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("relativeBearingDeg")).toDouble() + 12.34) < 0.001,
                      "range relative bearing mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("absoluteBearingDeg")).toDouble() - 347.66) < 0.001,
                      "range absolute bearing mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("northOffsetM")).toDouble() - 102.4) < 0.001 &&
                          std::abs(packet.fields.value(QStringLiteral("eastOffsetM")).toDouble() + 22.7) < 0.001,
                      "range offsets mismatch");
    failures += check(packet.fields.value(QStringLiteral("reasons")).toStringList() ==
                          QStringList{QStringLiteral("single_absolute_range_method")},
                      "range reason registry mismatch");
    failures += check(packet.fields.value(QStringLiteral("sources")).toStringList() ==
                          QStringList{QStringLiteral("pixhawk_agl"), QStringLiteral("camera_ground")},
                      "range source registry mismatch");
    failures +=
        check(packet.fields.value(QStringLiteral("vehicleProfile")).toString() == QStringLiteral("fixed-wing") &&
                  packet.fields.value(QStringLiteral("navigationState")).toString() == QStringLiteral("gps-aided") &&
                  packet.fields.value(QStringLiteral("motionRegime")).toString() == QStringLiteral("cruise"),
              "range fusion profile mismatch");
    failures += check(packet.fields.value(QStringLiteral("sourceContributions")).toList().size() == 2,
                      "range source contributions missing");
    failures +=
        check(packet.fields.value(QStringLiteral("advisoryOnly")).toBool(), "range status must remain advisory-only");
    failures += check(!packet.fields.value(QStringLiteral("flightControlEnabled")).toBool() &&
                          !packet.fields.value(QStringLiteral("physicalReleaseEnabled")).toBool(),
                      "range status must not enable control or physical release");

    // A fresh authenticated envelope must not resurrect an old metric range.
    // RangeStatus frameAgeMs is the uint16 at payload offset 68 (full frame
    // offset 20 + 68 = 88).  Re-sign the mutation so the real decoder gate,
    // rather than authentication, is what rejects it.
    QByteArray boundaryRange = range;
    boundaryRange[88] = static_cast<char>(0x05);
    boundaryRange[89] = static_cast<char>(0xDC);  // exactly 1500 ms is accepted
    boundaryRange = resign(boundaryRange, key);
    failures += check(protocol.decode(boundaryRange, &packet, &error),
                      "range status decoder must accept exact source-age boundary");

    QByteArray staleRange = range;
    staleRange[88] = static_cast<char>(0x05);
    staleRange[89] = static_cast<char>(0xDD);  // 1501 ms > 1.5 s gate
    staleRange = resign(staleRange, key);
    failures += check(!protocol.decode(staleRange, &packet, &error) &&
                          error == QStringLiteral("range-status source frame is stale"),
                      "range status decoder must reject stale source age");

    // dataFreshness uses the common distance codec at 0.1 s resolution and
    // occupies full-frame offsets 90..91.  VALID/DEGRADED range is usable at
    // exactly 1.5 s, but both a value beyond the gate and the unavailable
    // sentinel fail closed even when the authenticated envelope is fresh.
    QByteArray boundaryFreshnessRange = range;
    boundaryFreshnessRange[90] = static_cast<char>(0x00);
    boundaryFreshnessRange[91] = static_cast<char>(0x0F);  // exactly 1.5 s
    boundaryFreshnessRange = resign(boundaryFreshnessRange, key);
    failures += check(protocol.decode(boundaryFreshnessRange, &packet, &error),
                      "range status decoder must accept exact data-freshness boundary");

    QByteArray staleFreshnessRange = range;
    staleFreshnessRange[90] = static_cast<char>(0x00);
    staleFreshnessRange[91] = static_cast<char>(0x10);  // 1.6 s > 1.5 s gate
    staleFreshnessRange = resign(staleFreshnessRange, key);
    failures += check(!protocol.decode(staleFreshnessRange, &packet, &error) &&
                          error == QStringLiteral("range-status data freshness is stale"),
                      "range status decoder must reject stale data freshness");

    QByteArray missingFreshnessRange = range;
    missingFreshnessRange[90] = static_cast<char>(0xFF);
    missingFreshnessRange[91] = static_cast<char>(0xFF);
    missingFreshnessRange = resign(missingFreshnessRange, key);
    failures += check(!protocol.decode(missingFreshnessRange, &packet, &error) &&
                          error == QStringLiteral("range-status data freshness is unavailable"),
                      "range status decoder must reject unavailable data freshness");

    const QByteArray targetGeolocation = vector(
        "4d44011600000000006c00000000000f433a001ee65c7f305ba849ec9b383ba6daf97c12"
        "010100c680573ddeb10d00430032d0434fd62d4fed131097ef34f1e1b3f7");
    failures += check(targetGeolocation.size() == 66, "target geolocation vector must fit the MAVLink TUNNEL payload");
    failures +=
        check(protocol.decode(targetGeolocation, &packet, &error), "target geolocation golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::TargetGeolocationStatus,
                      "target geolocation vector message type mismatch");
    failures += check(packet.sequence == 108, "target geolocation vector sequence mismatch");
    failures += check(packet.fields.value(QStringLiteral("available")).toBool(),
                      "target geolocation must be explicitly GPS qualified");
    failures += check(packet.fields.value(QStringLiteral("reason")).toString() == QStringLiteral("gps_qualified"),
                      "target geolocation qualification reason mismatch");
    failures +=
        check(std::abs(packet.fields.value(QStringLiteral("latitudeDeg")).toDouble() - 1.3008983) < 0.0000001 &&
                  std::abs(packet.fields.value(QStringLiteral("longitudeDeg")).toDouble() - 103.8004493) < 0.0000001,
              "target geolocation coordinates mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("horizontalSigmaM")).toDouble() - 6.7) < 0.001,
                      "target geolocation uncertainty mismatch");

    const QByteArray gnssStatus = vector(
        "4d44011900000000006d00000000000f433a000b010c02030e000c001a0003"
        "a0774b282c477228897879a29a9131dc");
    failures += check(gnssStatus.size() == 47, "GNSS status vector must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(gnssStatus, &packet, &error), "GNSS status golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::GnssStatus,
                      "GNSS status vector message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("qualified")).toBool() &&
                          packet.fields.value(QStringLiteral("reason")).toString() == QStringLiteral("gps_qualified") &&
                          packet.fields.value(QStringLiteral("fixType")).toInt() == 3 &&
                          packet.fields.value(QStringLiteral("satellitesVisible")).toInt() == 14,
                      "GNSS status qualification fields mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("horizontalAccuracyM")).toDouble() - 1.2) < 0.001 &&
                          std::abs(packet.fields.value(QStringLiteral("verticalAccuracyM")).toDouble() - 2.6) < 0.001 &&
                          packet.fields.value(QStringLiteral("sourceAgeMs")).toInt() == 300,
                      "GNSS status diagnostic values mismatch");
    failures += check(packet.fields.value(QStringLiteral("advisoryOnly")).toBool() &&
                          !packet.fields.value(QStringLiteral("flightControlEnabled")).toBool() &&
                          !packet.fields.value(QStringLiteral("physicalReleaseEnabled")).toBool(),
                      "GNSS status must remain display-only");

    const QByteArray targetObservation = vector(
        "4d44011a00000000007300000000000f433a005ce65c7f305ba849ec9b383ba6daf97c12"
        "5a736508010f597402001000000009000004d2041c04ae04f703fd043bfb2e87ce007d0000"
        "0400ffffff1d00017f69010100c680573ddeb10d00430032010c02030e000c001a0003a1b2"
        "c3d4b7124ac2383d3a627f1b8d0913a86de4");
    failures += check(targetObservation.size() == 128,
                      "atomic target observation vector must fit one MAVLink TUNNEL payload");
    failures += check(protocol.decode(targetObservation, &packet, &error),
                      "atomic target observation golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::TargetObservationStatus &&
                          packet.sequence == 115 &&
                          packet.fields.value(QStringLiteral("producerEpoch")).toUInt() == 0xA1B2C3D4U &&
                          packet.fields.value(QStringLiteral("atomicObservation")).toBool(),
                      "atomic target observation envelope mismatch");
    const QVariantMap observationRange = packet.fields.value(QStringLiteral("rangeStatus")).toMap();
    const QVariantMap observationGeolocation =
        packet.fields.value(QStringLiteral("targetGeolocationStatus")).toMap();
    const QVariantMap observationGnss = packet.fields.value(QStringLiteral("gnssStatus")).toMap();
    failures += check(observationRange.value(QStringLiteral("validity")).toString() ==
                              QStringLiteral("DEGRADED") &&
                          std::abs(observationRange.value(QStringLiteral("groundRangeM")).toDouble() -
                                   105.2) < 0.001 &&
                          observationRange.value(QStringLiteral("fusionProfile")).toString() ==
                              QStringLiteral("atomic-observation-v1"),
                      "atomic target observation range fields mismatch");
    failures += check(observationGeolocation.value(QStringLiteral("available")).toBool() &&
                          std::abs(observationGeolocation.value(QStringLiteral("latitudeDeg")).toDouble() -
                                   1.3008983) < 0.0000001 &&
                          std::abs(observationGeolocation.value(QStringLiteral("horizontalSigmaM")).toDouble() -
                                   6.7) < 0.001,
                      "atomic target observation geolocation fields mismatch");
    failures += check(observationGnss.value(QStringLiteral("qualified")).toBool() &&
                          observationGnss.value(QStringLiteral("satellitesVisible")).toInt() == 14 &&
                          observationGnss.value(QStringLiteral("sourceAgeMs")).toInt() == 300,
                      "atomic target observation GNSS fields mismatch");

    const QByteArray forgedAtomicObservation = vector(
        "4d44011a00000000007300000000000f433a005ce65c7f305ba849ec9b383ba6daf97c12"
        "5a736508010f597402001000000009000004d2041c04ae04f703fd043bfb2e87ce007d0000"
        "0400ffffff1d00017f69010100c680573ddeb10d00430032000c02030e000c001a0003a1b2"
        "c3d4e946207ab5b11a924e9dc7865b5339ec");
    failures += check(!protocol.decode(forgedAtomicObservation, &packet, &error) &&
                          error == QStringLiteral("target-observation GNSS content is inconsistent"),
                      "atomic observation must reject qualified coordinates bound to weak GNSS");

    QByteArray zeroEpochObservation = targetObservation;
    zeroEpochObservation.replace(108, 4, QByteArray(4, '\0'));
    zeroEpochObservation = resign(zeroEpochObservation, key);
    failures += check(!protocol.decode(zeroEpochObservation, &packet, &error) &&
                          error == QStringLiteral("target-observation range content is invalid"),
                      "atomic observation must reject a zero producer epoch");

    QByteArray forgedQualifiedGnss = gnssStatus;
    forgedQualifiedGnss[24] = static_cast<char>(0x04);  // only four satellites
    forgedQualifiedGnss = resign(forgedQualifiedGnss, key);
    failures += check(!protocol.decode(forgedQualifiedGnss, &packet, &error) &&
                          error == QStringLiteral("GNSS-status qualification is inconsistent"),
                      "GNSS decoder must reject a falsely qualified weak fix");

    const QByteArray withheldTargetGeolocation = vector(
        "4d44011600000000006d00000000000f433a001ee65c7f305ba849ec9b383ba6daf97c12"
        "00028000000080000000ffff003227a23e2a8f5c69b50b5e05d22a56617b");
    failures += check(protocol.decode(withheldTargetGeolocation, &packet, &error),
                      "withheld target geolocation vector must decode");
    failures += check(
        !packet.fields.value(QStringLiteral("available")).toBool() &&
            packet.fields.value(QStringLiteral("reason")).toString() == QStringLiteral("gps_navigation_not_qualified"),
        "unqualified GPS must withhold target coordinates");
    failures += check(!packet.fields.contains(QStringLiteral("latitudeDeg")) &&
                          !packet.fields.contains(QStringLiteral("longitudeDeg")),
                      "withheld target geolocation must not expose coordinates");

    // An authenticated envelope must not keep an old coordinate alive by
    // advertising a large source-frame age.  The wire limit mirrors the
    // backend GPS freshness gate; mutate the age field and re-sign so this
    // exercises the real decoder rather than an unauthenticated shortcut.
    QByteArray staleTargetGeolocation = targetGeolocation;
    staleTargetGeolocation[48] = static_cast<char>(0x05);
    staleTargetGeolocation[49] = static_cast<char>(0xDD);  // 1501 ms > 1.5 s gate
    staleTargetGeolocation = resign(staleTargetGeolocation, key);
    failures += check(!protocol.decode(staleTargetGeolocation, &packet, &error) &&
                          error == QStringLiteral("target-geolocation-status source frame is stale"),
                      "target geolocation decoder must reject stale source age");

    // Exact geographic poles have no stable local longitude direction. Keep
    // the wire decoder aligned with the map/snapshot domain and reject a
    // freshly authenticated coordinate at +90 degrees instead of deferring
    // the failure until the QML projection layer.
    QByteArray poleTargetGeolocation = targetGeolocation;
    poleTargetGeolocation[38] = static_cast<char>(0x35);
    poleTargetGeolocation[39] = static_cast<char>(0xA4);
    poleTargetGeolocation[40] = static_cast<char>(0xE9);
    poleTargetGeolocation[41] = static_cast<char>(0x00);  // +900000000 E7
    poleTargetGeolocation = resign(poleTargetGeolocation, key);
    failures += check(!protocol.decode(poleTargetGeolocation, &packet, &error) &&
                          error == QStringLiteral("target-geolocation-status content is invalid"),
                      "target geolocation decoder must reject exact pole coordinates");

    QByteArray monocularRange = range;
    // reason bits 28/29 and source bit 6 are appended to the v1 registries so
    // older bit assignments remain stable.
    for (int offset = 93; offset < 111; ++offset) {
        monocularRange[offset] = static_cast<char>(0x00);
    }
    monocularRange[54] = static_cast<char>(0x30);
    monocularRange[55] = static_cast<char>(0x00);
    monocularRange[56] = static_cast<char>(0x00);
    monocularRange[57] = static_cast<char>(0x00);
    monocularRange[58] = static_cast<char>(0x00);
    monocularRange[59] = static_cast<char>(0x40);
    monocularRange = resign(monocularRange, key);
    failures +=
        check(protocol.decode(monocularRange, &packet, &error), "monocular range registry extension must decode");
    failures += check(packet.fields.value(QStringLiteral("reasons")).toStringList() ==
                          QStringList{QStringLiteral("direct_degraded_metric_range"),
                                      QStringLiteral("vertical_reference_unavailable")},
                      "monocular range reason registry mismatch");
    failures += check(
        packet.fields.value(QStringLiteral("sources")).toStringList() == QStringList{QStringLiteral("monocular_size")},
        "monocular range source registry mismatch");

    QByteArray metricDepthRange = monocularRange;
    metricDepthRange[59] = static_cast<char>(0x80);
    metricDepthRange = resign(metricDepthRange, key);
    failures += check(protocol.decode(metricDepthRange, &packet, &error), "metric-depth range source must decode");
    failures += check(packet.fields.value(QStringLiteral("sources")).toStringList() ==
                          QStringList{QStringLiteral("monocular_metric")},
                      "metric-depth source registry mismatch");

    QByteArray invalidRange = range;
    invalidRange[53] = static_cast<char>(0x03);
    invalidRange = resign(invalidRange, key);
    failures +=
        check(!protocol.decode(invalidRange, &packet, &error), "range status must reject unsupported control flags");

    QByteArray geometryUnboundedRange = range;
    geometryUnboundedRange[54] =
        static_cast<char>(static_cast<quint8>(geometryUnboundedRange.at(54)) | 0x80);
    geometryUnboundedRange = resign(geometryUnboundedRange, key);
    failures += check(protocol.decode(geometryUnboundedRange, &packet, &error),
                      "range status must decode geometry uncertainty reason bit 31");
    failures += check(packet.fields.value(QStringLiteral("reasons")).toStringList().contains(
                          QStringLiteral("target_geometry_uncertainty_unbounded")),
                      "range status geometry uncertainty reason mismatch");

    invalidRange = range;
    invalidRange[52] = static_cast<char>(0x03);
    invalidRange = resign(invalidRange, key);
    failures +=
        check(!protocol.decode(invalidRange, &packet, &error), "invalid range status must not carry a distance");

    const QByteArray release = vector(
        "4d44010b00000000006800000000000f433a004fe65c7f305ba849ece65c7f305ba849ec"
        "9b383ba6daf97c12a9f755dc5aa0d90003010004000000000400ffffff1d000003f3ffff"
        "ff2500000008fffffffc002e0015fb2e041c03fd043b001b0273d0e89af4610ff59e85b4"
        "18795c3fecda9d");
    failures += check(release.size() == 115, "release golden vector must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(release, &packet, &error), "release golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::ReleaseStatus,
                      "release vector message type mismatch");
    failures += check(packet.sequence == 104, "release vector sequence mismatch");
    failures += check(packet.fields.value(QStringLiteral("timingStatus")).toString() == QStringLiteral("WINDOW"),
                      "release timing mismatch");
    failures += check(packet.fields.value(QStringLiteral("rangeBindingPresent")).toBool(),
                      "release range binding must be present");
    failures += check(std::abs(packet.fields.value(QStringLiteral("impactNorthOffsetM")).toDouble() - 101.1) < 0.001 &&
                          std::abs(packet.fields.value(QStringLiteral("impactEastOffsetM")).toDouble() + 21.9) < 0.001,
                      "release impact offsets mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("errorEllipseMajorM")).toDouble() - 4.6) < 0.001 &&
                          std::abs(packet.fields.value(QStringLiteral("errorEllipseMinorM")).toDouble() - 2.1) < 0.001,
                      "release error ellipse mismatch");
    failures += check(packet.fields.value(QStringLiteral("reasons")).toStringList() ==
                          QStringList{QStringLiteral("multimodal_release_window_ready")},
                      "release reason registry mismatch");
    failures += check(packet.fields.value(QStringLiteral("advisoryOnly")).toBool() &&
                          !packet.fields.value(QStringLiteral("flightControlEnabled")).toBool() &&
                          !packet.fields.value(QStringLiteral("physicalReleaseEnabled")).toBool(),
                      "release status must remain advisory-only with physical output disabled");

    QByteArray invalidRelease = release;
    invalidRelease[53] = static_cast<char>(0x00);
    invalidRelease = resign(invalidRelease, key);
    failures += check(!protocol.decode(invalidRelease, &packet, &error),
                      "release status must reject inconsistent range binding flags");

    invalidRelease = release;
    invalidRelease[54] = static_cast<char>(static_cast<quint8>(invalidRelease.at(54)) | 0x80);
    invalidRelease = resign(invalidRelease, key);
    failures +=
        check(!protocol.decode(invalidRelease, &packet, &error), "release status must reject unknown reason bits");

    QByteArray invalidPatrol = patrol;
    invalidPatrol[38] = static_cast<char>(0x87);
    invalidPatrol = resign(invalidPatrol, key);
    failures +=
        check(!protocol.decode(invalidPatrol, &packet, &error), "patrol status must reject unsupported presence flags");

    invalidPatrol = patrol;
    invalidPatrol[38] = static_cast<char>(0x06);
    invalidPatrol = resign(invalidPatrol, key);
    failures += check(!protocol.decode(invalidPatrol, &packet, &error),
                      "patrol status must reject target metadata without a primary target");

    invalidPatrol = patrol;
    invalidPatrol[38] = static_cast<char>(0x03);
    invalidPatrol = resign(invalidPatrol, key);
    failures += check(!protocol.decode(invalidPatrol, &packet, &error),
                      "patrol status must reject return advice without its presence flag");

    const QByteArray challenge = vector(
        "4d44010600000000001500000000000f4df80045000000000000000b000000000000000c"
        "000000000000000d000000000000000e000000000000000f000000000000001000000007"
        "00000000000f424000000000000f695001485f77875fa4980410ac5fa437484528");
    failures += check(protocol.decode(challenge, &packet, &error), "authorization challenge vector must decode");
    failures += check(packet.fields.value(QStringLiteral("challengeToken")).toString() == QStringLiteral("11"),
                      "authorization challenge token mismatch");
    failures +=
        check(packet.fields.value(QStringLiteral("pending")).toBool(), "authorization challenge must be pending");

    const QByteArray approachChallenge = vector(
        "4d44010c00000000006e00000000000187040035000000000000006500000000000000ca"
        "000000071111111111114111811111111111111100000000000186a00000000000019a28"
        "0126a630faaaad1f1735caeb7eb5b297b1");
    failures += check(approachChallenge.size() == 89, "approach challenge must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(approachChallenge, &packet, &error), "approach challenge vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::ApproachChallenge,
                      "approach challenge message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("challengeToken")).toString() == QStringLiteral("101"),
                      "approach challenge token mismatch");
    failures += check(packet.fields.value(QStringLiteral("targetRevision")).toUInt() == 7,
                      "approach challenge target revision mismatch");
    failures += check(packet.fields.value(QStringLiteral("selectionCommandId")).toString() ==
                          QStringLiteral("11111111-1111-4111-8111-111111111111"),
                      "approach challenge selection binding mismatch");
    failures += check(packet.fields.value(QStringLiteral("metadataOnly")).toBool() &&
                          !packet.fields.value(QStringLiteral("directPixhawkWrite")).toBool(),
                      "approach challenge must remain a signed Jetson metadata command");

    failures += check(protocol.decode(expectedApproachConfirmation, &packet, &error),
                      "approach confirmation vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::ApproachConfirmation,
                      "approach confirmation message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("continuous")).toBool(),
                      "approach confirmation must carry continuous-slide evidence");
    failures += check(std::abs(packet.fields.value(QStringLiteral("completionFraction")).toDouble() - 1.0) < 0.001,
                      "approach confirmation completion mismatch");
    failures += check(packet.fields.value(QStringLiteral("slideDurationMs")).toUInt() == 800,
                      "approach confirmation duration mismatch");

    const QByteArray approachAck = vector(
        "4d44010e0000000000710000000000018aec000e000000000000012f01000000006f"
        "c8499af2ea936a6af1b64b0cff34b345");
    failures += check(approachAck.size() == 50, "approach acknowledgement must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(approachAck, &packet, &error), "approach acknowledgement vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::ApproachAck,
                      "approach acknowledgement message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("accepted")).toBool() &&
                          packet.fields.value(QStringLiteral("acknowledgedSequence")).toUInt() == 111,
                      "approach acknowledgement correlation mismatch");

    const QByteArray approachStatus = vector(
        "4d44010f0000000000700000000000018aec0022dfe4b591dae819510000000705400000"
        "0000faff8800faff8800b4800002ee001d01d627d69f2f100aea4abdd12ecfbc1c0a");
    failures += check(approachStatus.size() == 70, "approach status must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(approachStatus, &packet, &error), "approach status vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::ApproachStatus,
                      "approach status message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("phase")).toString() == QStringLiteral("CENTERING"),
                      "approach phase mismatch");
    failures += check(packet.fields.value(QStringLiteral("reasons")).toStringList() ==
                          QStringList{QStringLiteral("centering_advice_only")},
                      "approach reason registry mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("yawErrorDeg")).toDouble() - 2.5) < 0.001 &&
                          std::abs(packet.fields.value(QStringLiteral("pitchErrorDeg")).toDouble() + 1.2) < 0.001,
                      "approach optical-axis error mismatch");
    failures += check(std::abs(packet.fields.value(QStringLiteral("groundRangeM")).toDouble() - 75.0) < 0.001,
                      "approach range mismatch");
    failures += check(packet.fields.value(QStringLiteral("advisoryOnly")).toBool() &&
                          packet.fields.value(QStringLiteral("sitlHilOnly")).toBool() &&
                          !packet.fields.value(QStringLiteral("flightControlEnabled")).toBool() &&
                          !packet.fields.value(QStringLiteral("aimControlActive")).toBool() &&
                          !packet.fields.value(QStringLiteral("pilotInputCancelled")).toBool() &&
                          !packet.fields.value(QStringLiteral("physicalReleaseEnabled")).toBool(),
                      "approach status must remain advisory-only with physical output disabled");

    QByteArray productionApproach = approachStatus;
    productionApproach[53] = static_cast<char>(0x03);
    productionApproach = resign(productionApproach, key);
    failures += check(protocol.decode(productionApproach, &packet, &error), "production approach status must decode");
    failures += check(!packet.fields.value(QStringLiteral("advisoryOnly")).toBool() &&
                          !packet.fields.value(QStringLiteral("sitlHilOnly")).toBool() &&
                          packet.fields.value(QStringLiteral("flightControlEnabled")).toBool() &&
                          !packet.fields.value(QStringLiteral("aimControlActive")).toBool() &&
                          !packet.fields.value(QStringLiteral("pilotInputCancelled")).toBool(),
                      "production approach status must report Jetson flight-control authority");

    QByteArray activeAimApproach = approachStatus;
    activeAimApproach[53] = static_cast<char>(0x07);
    activeAimApproach = resign(activeAimApproach, key);
    failures += check(protocol.decode(activeAimApproach, &packet, &error), "active aim approach status must decode");
    failures += check(packet.fields.value(QStringLiteral("aimControlActive")).toBool() &&
                          !packet.fields.value(QStringLiteral("pilotInputCancelled")).toBool(),
                      "active aim approach status flags mismatch");

    QByteArray pilotCancelledApproach = approachStatus;
    pilotCancelledApproach[53] = static_cast<char>(0x0B);
    pilotCancelledApproach = resign(pilotCancelledApproach, key);
    failures +=
        check(protocol.decode(pilotCancelledApproach, &packet, &error), "pilot-cancelled approach status must decode");
    failures += check(!packet.fields.value(QStringLiteral("aimControlActive")).toBool() &&
                          packet.fields.value(QStringLiteral("pilotInputCancelled")).toBool(),
                      "pilot-cancelled approach status flags mismatch");

    QByteArray invalidApproach = approachStatus;
    invalidApproach[53] = static_cast<char>(0x80);
    invalidApproach = resign(invalidApproach, key);
    failures += check(!protocol.decode(invalidApproach, &packet, &error),
                      "approach status must reject unsupported status flags");

    const QByteArray payloadTargetChallenge = vector(
        "4d44011200000000007000000000000186aa004100000000000002bd00000000000002be"
        "0000000700000000000002bf000000081111111111114111811111111111111100000000"
        "000186a00000000000019a2801e5637e173cd1f02289fe27d0a6be7d61");
    failures +=
        check(payloadTargetChallenge.size() == 101, "payload target challenge must fit the MAVLink TUNNEL payload");
    failures +=
        check(protocol.decode(payloadTargetChallenge, &packet, &error), "payload target challenge vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::PayloadTargetChallenge,
                      "payload target challenge message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("selectedTargetRevision")).toUInt() == 7 &&
                          packet.fields.value(QStringLiteral("aimpointTargetRevision")).toUInt() == 8 &&
                          packet.fields.value(QStringLiteral("pending")).toBool(),
                      "payload target challenge binding mismatch");

    failures += check(protocol.decode(expectedPayloadTargetConfirmation, &packet, &error),
                      "payload target confirmation vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::PayloadTargetConfirmation,
                      "payload target confirmation message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("continuous")).toBool() &&
                          packet.fields.value(QStringLiteral("slideDurationMs")).toUInt() == 800,
                      "payload target continuous-slide evidence mismatch");

    const QByteArray payloadTargetAck = vector(
        "4d4401140000000000730000000000018a9c000e0000000000000321010000000071"
        "456dae89909ca6100fbb2c5d13664e49");
    failures +=
        check(protocol.decode(payloadTargetAck, &packet, &error), "payload target acknowledgement vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::PayloadTargetAck &&
                          packet.fields.value(QStringLiteral("accepted")).toBool() &&
                          packet.fields.value(QStringLiteral("acknowledgedSequence")).toUInt() == 113,
                      "payload target acknowledgement correlation mismatch");

    const QByteArray payloadTargetStatus = vector(
        "4d4401150000000000720000000000018a92002c11111111111141118111111111111111"
        "00000000000002be0000000700000000000002bf00000008020028038bf2773d9e4deff1"
        "ca97368e7b4b0870");
    failures += check(payloadTargetStatus.size() == 80, "payload target status must fit the MAVLink TUNNEL payload");
    failures +=
        check(protocol.decode(payloadTargetStatus, &packet, &error), "payload target status vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::PayloadTargetStatus &&
                          packet.fields.value(QStringLiteral("eligibility")).toString() ==
                              QStringLiteral("ELIGIBLE_BURNING_CONTEXT") &&
                          packet.fields.value(QStringLiteral("aimpointPresent")).toBool() &&
                          packet.fields.value(QStringLiteral("confirmationPending")).toBool(),
                      "payload target status content mismatch");
    failures += check(packet.fields.value(QStringLiteral("advisoryOnly")).toBool() &&
                          !packet.fields.value(QStringLiteral("flightControlEnabled")).toBool() &&
                          !packet.fields.value(QStringLiteral("physicalReleaseEnabled")).toBool(),
                      "payload target status must remain advisory-only with physical output disabled");

    const QByteArray targetPool = vector(
        "4d4401100000000000780000000000018b50005c000000090001020201692a3d53861781"
        "031f76656869636c65000000000000000000e5cb199a33334ccc8000fb2e0338002eaa73"
        "34ec51a7eef80401706572736f6e00000000000000000000b29800000000000000008000"
        "ffffffffa3ed7dff9c9cbdce7e0b143de004ae56");
    failures += check(targetPool.size() == 128, "target-pool page must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(targetPool, &packet, &error), "target-pool golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::TargetPoolStatus,
                      "target-pool message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("poolRevision")).toUInt() == 9 &&
                          packet.fields.value(QStringLiteral("pageCount")).toUInt() == 1 &&
                          packet.fields.value(QStringLiteral("totalTrackCount")).toUInt() == 2,
                      "target-pool page metadata mismatch");
    const QVariantList targetEntries = packet.fields.value(QStringLiteral("entries")).toList();
    failures += check(targetEntries.size() == 2, "target-pool entry count mismatch");
    if (targetEntries.size() == 2) {
        const QVariantMap primary = targetEntries.at(0).toMap();
        const QVariantMap background = targetEntries.at(1).toMap();
        failures += check(
            primary.value(QStringLiteral("label")).toString() == QStringLiteral("vehicle") &&
                primary.value(QStringLiteral("state")).toString() == QStringLiteral("TRACKING") &&
                primary.value(QStringLiteral("primary")).toBool() && primary.value(QStringLiteral("locked")).toBool() &&
                primary.value(QStringLiteral("bboxValid")).toBool() &&
                std::abs(primary.value(QStringLiteral("x1")).toDouble() - 0.1) < 0.0001 &&
                std::abs(primary.value(QStringLiteral("relativeBearingDeg")).toDouble() + 12.34) < 0.001 &&
                std::abs(primary.value(QStringLiteral("estimatedRangeM")).toDouble() - 82.4) < 0.001 &&
                std::abs(primary.value(QStringLiteral("targetSpeedMps")).toDouble() - 4.6) < 0.001,
            "target-pool primary entry mismatch");
        failures += check(background.value(QStringLiteral("label")).toString() == QStringLiteral("person") &&
                              background.value(QStringLiteral("state")).toString() == QStringLiteral("OCCLUDED") &&
                              !background.value(QStringLiteral("primary")).toBool() &&
                              background.value(QStringLiteral("locked")).toBool() &&
                              std::isnan(background.value(QStringLiteral("relativeBearingDeg")).toDouble()) &&
                              std::isnan(background.value(QStringLiteral("estimatedRangeM")).toDouble()) &&
                              std::isnan(background.value(QStringLiteral("targetSpeedMps")).toDouble()),
                          "target-pool background entry mismatch");
    }
    failures += check(packet.fields.value(QStringLiteral("advisoryOnly")).toBool() &&
                          !packet.fields.value(QStringLiteral("flightControlEnabled")).toBool() &&
                          !packet.fields.value(QStringLiteral("physicalReleaseEnabled")).toBool(),
                      "target-pool metadata must remain display-only");

    QByteArray operatorTrackedTargetPool = targetPool;
    operatorTrackedTargetPool[37] = static_cast<char>(static_cast<quint8>(operatorTrackedTargetPool.at(37)) | 0x20U);
    operatorTrackedTargetPool = resign(operatorTrackedTargetPool, key);
    failures +=
        check(protocol.decode(operatorTrackedTargetPool, &packet, &error), "target-pool operator-TRK flag must decode");
    if (packet.fields.value(QStringLiteral("entries")).toList().size() == 2) {
        const QVariantList trackedEntries = packet.fields.value(QStringLiteral("entries")).toList();
        failures += check(trackedEntries[0].toMap().value(QStringLiteral("operatorTracked")).toBool() &&
                              !trackedEntries[1].toMap().value(QStringLiteral("operatorTracked")).toBool(),
                          "target-pool operator-TRK flag must remain per target");
    }

    QByteArray invalidTargetPool = targetPool;
    invalidTargetPool[37] = static_cast<char>(0x02);  // Primary without locked.
    invalidTargetPool = resign(invalidTargetPool, key);
    failures += check(!protocol.decode(invalidTargetPool, &packet, &error),
                      "target-pool page must reject primary targets that are not locked");

    const QByteArray sceneContext = vector(
        "4d44011100000000007a0000000000018c7c00320000000bbacfe51c3061b72b0000000000018b82"
        "010001020100009999ffffffff51ebcccc02199a26667333bfff2e14b33208c7174b7584e29825ee5"
        "a92584a36bc");
    failures += check(sceneContext.size() == 86, "scene-context page must fit the MAVLink TUNNEL payload");
    failures += check(protocol.decode(sceneContext, &packet, &error), "scene-context golden vector must decode");
    failures += check(packet.type == MultiDetectOperatorProtocol::MessageType::SceneContextStatus,
                      "scene-context message type mismatch");
    failures += check(packet.fields.value(QStringLiteral("contextRevision")).toUInt() == 11 &&
                          packet.fields.value(QStringLiteral("state")).toString() == QStringLiteral("VALID") &&
                          packet.fields.value(QStringLiteral("totalRegionCount")).toUInt() == 2,
                      "scene-context page metadata mismatch");
    const QVariantList contextEntries = packet.fields.value(QStringLiteral("entries")).toList();
    failures +=
        check(contextEntries.size() == 2 &&
                  contextEntries.at(0).toMap().value(QStringLiteral("label")).toString() == QStringLiteral("road") &&
                  contextEntries.at(1).toMap().value(QStringLiteral("label")).toString() == QStringLiteral("building"),
              "scene-context categorical entries mismatch");
    failures += check(!packet.fields.value(QStringLiteral("confidenceAvailable")).toBool() &&
                          !packet.fields.value(QStringLiteral("targetIdentityAuthority")).toBool() &&
                          packet.fields.value(QStringLiteral("advisoryOnly")).toBool() &&
                          !packet.fields.value(QStringLiteral("flightControlEnabled")).toBool() &&
                          !packet.fields.value(QStringLiteral("physicalReleaseEnabled")).toBool(),
                      "scene-context metadata must remain confidence-free and display-only");

    QByteArray invalidSceneContext = sceneContext;
    invalidSceneContext[40] = static_cast<char>(0x04);
    invalidSceneContext = resign(invalidSceneContext, key);
    failures += check(!protocol.decode(invalidSceneContext, &packet, &error),
                      "scene-context page must reject unknown freshness states");

    QByteArray tampered = track;
    tampered[25] = static_cast<char>(tampered.at(25) ^ 0x01);
    failures += check(!protocol.decode(tampered, &packet, &error), "single-byte mutation must fail authentication");
    failures += check(error.contains(QStringLiteral("authentication")),
                      "tamper rejection must identify authentication failure");

    if (failures == 0) {
        qInfo() << "MultiDetect operator protocol self-test passed";
    }
    return failures == 0 ? 0 : 1;
}
