#include "MultiDetectTargetObservationRecorder.h"

#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QTemporaryDir>
#include <cstdlib>
#include <iostream>
#include <memory>

namespace {

[[noreturn]] void fail(const char* message)
{
    std::cerr << message << '\n';
    std::exit(EXIT_FAILURE);
}

void require(bool condition, const char* message)
{
    if (!condition) {
        fail(message);
    }
}

QVariantMap envelope(qulonglong sentAtMs, quint32 epoch, quint32 sequence)
{
    return {{QStringLiteral("atomicObservation"), true},
            {QStringLiteral("producerEpoch"), epoch},
            {QStringLiteral("sequence"), sequence},
            {QStringLiteral("sentAtMs"), QVariant::fromValue(sentAtMs)},
            {QStringLiteral("sourceAgeMs"), 100u},
            {QStringLiteral("advisoryOnly"), true},
            {QStringLiteral("flightControlEnabled"), false},
            {QStringLiteral("physicalReleaseEnabled"), false}};
}

QVariantMap rangeStatus(qulonglong sentAtMs, quint32 epoch, quint32 sequence,
                        const QString& validity = QStringLiteral("VALID"))
{
    QVariantMap value = envelope(sentAtMs, epoch, sequence);
    value.insert(QStringLiteral("targetId"), QStringLiteral("target-3"));
    value.insert(QStringLiteral("sourceFrameId"), QStringLiteral("frame-88"));
    value.insert(QStringLiteral("validity"), validity);
    return value;
}

QVariantMap geolocationStatus(qulonglong sentAtMs, quint32 epoch, quint32 sequence,
                              bool available = true)
{
    QVariantMap value = envelope(sentAtMs, epoch, sequence);
    value.insert(QStringLiteral("targetId"), QStringLiteral("target-3"));
    value.insert(QStringLiteral("sourceFrameId"), QStringLiteral("frame-88"));
    value.insert(QStringLiteral("available"), available);
    if (available) {
        value.insert(QStringLiteral("latitudeDeg"), 30.2741);
        value.insert(QStringLiteral("longitudeDeg"), 120.1551);
        value.insert(QStringLiteral("horizontalSigmaM"), 4.2);
    }
    return value;
}

QVariantMap gnssStatus(qulonglong sentAtMs, quint32 epoch, quint32 sequence,
                       bool qualified = true)
{
    QVariantMap value = envelope(sentAtMs, epoch, sequence);
    value.insert(QStringLiteral("qualified"), qualified);
    value.insert(QStringLiteral("reason"), qualified ? QStringLiteral("gps_qualified")
                                                      : QStringLiteral("horizontal_accuracy_exceeded"));
    return value;
}

} // namespace

int main(int argc, char* argv[])
{
    std::unique_ptr<QTemporaryDir> temporaryRoot;
    QString rootPath;
    if (argc == 3 && QString::fromLocal8Bit(argv[1]) == QStringLiteral("--emit")) {
        rootPath = QFileInfo(QString::fromLocal8Bit(argv[2])).absoluteFilePath();
        require(QDir().mkpath(rootPath), "emission root must be creatable");
    } else {
        require(argc == 1, "usage: selftest [--emit <output-root>]");
        temporaryRoot = std::make_unique<QTemporaryDir>();
        require(temporaryRoot->isValid(), "temporary root must be valid");
        rootPath = temporaryRoot->path();
    }
    MultiDetectTargetObservationRecorder recorder;
    QString error;
    const QDateTime started = QDateTime::fromString(
        QStringLiteral("2026-09-07T08:00:00.000Z"), Qt::ISODateWithMs);
    require(recorder.start(rootPath, started, 1000, 4, &error),
            "recording must start");
    require(recorder.active(), "recorder must expose active state");

    const QVariantMap summary{{QStringLiteral("active_epoch"), 7u}};
    constexpr qulonglong baseMs = 1788768000000ULL;
    require(recorder.capture(rangeStatus(900, 7, 1), geolocationStatus(900, 7, 1),
                             gnssStatus(900, 7, 1),
                             summary, started, baseMs, baseMs - 50, baseMs - 50,
                             baseMs - 50, &error),
            "first capture must succeed");
    require(recorder.capture(rangeStatus(900, 7, 1), geolocationStatus(900, 7, 1),
                             gnssStatus(900, 7, 1),
                             summary, started.addMSecs(500), baseMs + 500, baseMs - 50,
                             baseMs - 50, baseMs - 50, &error),
            "capture before interval must be a successful no-op");
    require(recorder.sampleCount() == 1, "interval must suppress early capture");
    require(recorder.capture(rangeStatus(1900, 7, 2, QStringLiteral("INVALID")),
                             geolocationStatus(1900, 7, 2, false),
                             gnssStatus(1900, 7, 2, false), summary,
                             started.addMSecs(1000), baseMs + 1000, baseMs + 950,
                             baseMs + 950, baseMs + 950, &error),
            "second due capture must succeed");
    const QVariantMap restartedSummary{{QStringLiteral("active_epoch"), 8u}};
    require(recorder.capture(rangeStatus(3900, 8, 1, QStringLiteral("INVALID")),
                             geolocationStatus(3900, 8, 1, false),
                             gnssStatus(3900, 8, 1, false), restartedSummary,
                             started.addMSecs(3000), baseMs + 3000, baseMs + 2950,
                             baseMs + 2950, baseMs + 2950, &error),
            "delayed third capture must succeed");
    require(recorder.capture(rangeStatus(4900, 8, 2), geolocationStatus(4900, 8, 2),
                             gnssStatus(4900, 8, 2), restartedSummary,
                             started.addMSecs(4000), baseMs + 4000, baseMs + 3950,
                             baseMs + 3950, baseMs + 3950, &error),
            "fourth recovery capture must succeed");
    const QVariantMap liveQuality = recorder.qualitySummary();
    const QVariantMap sampling = liveQuality.value(QStringLiteral("sampling")).toMap();
    const QVariantMap segments = liveQuality.value(QStringLiteral("quality_segments")).toMap();
    require(liveQuality.value(QStringLiteral("active")).toBool(),
            "quality summary must expose active recording");
    require(liveQuality.value(QStringLiteral("sample_count")).toInt() == 4,
            "quality summary sample count must match captures");
    require(liveQuality.value(QStringLiteral("locatable_count")).toInt() == 2,
            "quality summary must count locatable samples");
    require(liveQuality.value(QStringLiteral("longest_non_locatable_run")).toInt() == 2,
            "quality summary must expose the longest non-locatable run");
    require(sampling.value(QStringLiteral("maximum_gap_ms")).toULongLong() == 2000,
            "quality summary must expose the maximum sampling gap");
    require(sampling.value(QStringLiteral("cadence_violation_count")).toInt() == 1,
            "quality summary must flag the delayed sampling interval");
    require(segments.value(QStringLiteral("non_locatable")).toList().size() == 1 &&
                segments.value(QStringLiteral("range_invalid")).toList().size() == 1 &&
                segments.value(QStringLiteral("gnss_unqualified")).toList().size() == 1,
            "quality summary must segment all three quality dimensions");
    require(liveQuality.value(QStringLiteral("epoch_transition_count")).toInt() == 1,
            "quality summary must expose epoch transitions");
    require(!liveQuality.value(QStringLiteral("flight_control_enabled")).toBool(),
            "quality summary must remain read-only");
    require(!liveQuality.value(QStringLiteral("physical_release_enabled")).toBool(),
            "quality summary must never enable physical release");
    require(!recorder.capture(rangeStatus(5900, 8, 3), geolocationStatus(5900, 8, 3),
                              gnssStatus(5900, 8, 3), restartedSummary,
                              started.addMSecs(5000), baseMs + 5000, baseMs + 4950,
                              baseMs + 4950, baseMs + 4950, &error),
            "capacity must fail closed");
    require(error.contains(QStringLiteral("capacity")), "capacity error must be explicit");

    QString manifestPath;
    require(recorder.stop(started.addSecs(5), QStringLiteral("operator_stopped"),
                          &manifestPath, &error),
            "recording must stop atomically");
    require(!recorder.active(), "recorder must expose stopped state");
    require(recorder.sampleCount() == 4, "sample count must remain visible after stop");
    require(!recorder.qualitySummary().value(QStringLiteral("active")).toBool(),
            "quality summary must expose completed recording state");
    QFile manifest(manifestPath);
    require(manifest.open(QIODevice::ReadOnly), "manifest must be readable");
    const QJsonDocument parsed = QJsonDocument::fromJson(manifest.readAll());
    require(parsed.isObject(), "manifest must be a JSON object");
    const QJsonObject document = parsed.object();
    require(document.value(QStringLiteral("schema")).toString() ==
                QStringLiteral("multidetect.target-observation-recording-manifest.v1"),
            "manifest schema must be stable");
    require(document.value(QStringLiteral("sample_count")).toInt() == 4,
            "manifest sample count must match files");
    require(document.value(QStringLiteral("snapshots")).toArray().size() == 4,
            "manifest must hash-bind every snapshot");
    require(!document.value(QStringLiteral("flight_control_enabled")).toBool(),
            "recording must not enable flight control");
    require(!document.value(QStringLiteral("physical_release_enabled")).toBool(),
            "recording must not enable physical release");
    require(QDir(recorder.sessionDirectory())
                .entryList({QStringLiteral("target-observation-diagnostics-*.json")},
                           QDir::Files)
                .size() == 4,
            "session must contain four snapshots");
    require(QDir(recorder.sessionDirectory()).entryList({QStringLiteral("*.tmp")},
                                                        QDir::Files).isEmpty(),
            "atomic writes must leave no temporary files");

    std::cout << "SESSION_DIRECTORY="
              << QDir::toNativeSeparators(recorder.sessionDirectory()).toStdString() << '\n';
    std::cout << "MultiDetectTargetObservationRecorderSelfTest: PASS\n";
    return EXIT_SUCCESS;
}
