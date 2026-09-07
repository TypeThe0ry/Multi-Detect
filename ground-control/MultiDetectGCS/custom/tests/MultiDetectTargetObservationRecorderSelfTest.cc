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

QVariantMap envelope(qulonglong sentAtMs)
{
    return {{QStringLiteral("atomicObservation"), true},
            {QStringLiteral("producerEpoch"), 7u},
            {QStringLiteral("sequence"), 42u},
            {QStringLiteral("sentAtMs"), QVariant::fromValue(sentAtMs)},
            {QStringLiteral("sourceAgeMs"), 100u},
            {QStringLiteral("advisoryOnly"), true},
            {QStringLiteral("flightControlEnabled"), false},
            {QStringLiteral("physicalReleaseEnabled"), false}};
}

QVariantMap rangeStatus(qulonglong sentAtMs)
{
    QVariantMap value = envelope(sentAtMs);
    value.insert(QStringLiteral("targetId"), QStringLiteral("target-3"));
    value.insert(QStringLiteral("sourceFrameId"), QStringLiteral("frame-88"));
    value.insert(QStringLiteral("validity"), QStringLiteral("VALID"));
    return value;
}

QVariantMap geolocationStatus(qulonglong sentAtMs)
{
    QVariantMap value = envelope(sentAtMs);
    value.insert(QStringLiteral("targetId"), QStringLiteral("target-3"));
    value.insert(QStringLiteral("sourceFrameId"), QStringLiteral("frame-88"));
    value.insert(QStringLiteral("available"), true);
    value.insert(QStringLiteral("latitudeDeg"), 30.2741);
    value.insert(QStringLiteral("longitudeDeg"), 120.1551);
    value.insert(QStringLiteral("horizontalSigmaM"), 4.2);
    return value;
}

QVariantMap gnssStatus(qulonglong sentAtMs)
{
    QVariantMap value = envelope(sentAtMs);
    value.insert(QStringLiteral("qualified"), true);
    value.insert(QStringLiteral("reason"), QStringLiteral("gps_qualified"));
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
    require(recorder.start(rootPath, started, 1000, 2, &error),
            "recording must start");
    require(recorder.active(), "recorder must expose active state");

    const QVariantMap summary{{QStringLiteral("active_epoch"), 7u}};
    constexpr qulonglong baseMs = 1788768000000ULL;
    require(recorder.capture(rangeStatus(900), geolocationStatus(900), gnssStatus(900),
                             summary, started, baseMs, baseMs - 50, baseMs - 50,
                             baseMs - 50, &error),
            "first capture must succeed");
    require(recorder.capture(rangeStatus(900), geolocationStatus(900), gnssStatus(900),
                             summary, started.addMSecs(500), baseMs + 500, baseMs - 50,
                             baseMs - 50, baseMs - 50, &error),
            "capture before interval must be a successful no-op");
    require(recorder.sampleCount() == 1, "interval must suppress early capture");
    require(recorder.capture(rangeStatus(1900), geolocationStatus(1900), gnssStatus(1900),
                             summary, started.addMSecs(1000), baseMs + 1000, baseMs + 950,
                             baseMs + 950, baseMs + 950, &error),
            "second due capture must succeed");
    require(!recorder.capture(rangeStatus(2900), geolocationStatus(2900), gnssStatus(2900),
                              summary, started.addMSecs(2000), baseMs + 2000, baseMs + 1950,
                              baseMs + 1950, baseMs + 1950, &error),
            "capacity must fail closed");
    require(error.contains(QStringLiteral("capacity")), "capacity error must be explicit");

    QString manifestPath;
    require(recorder.stop(started.addSecs(3), QStringLiteral("operator_stopped"),
                          &manifestPath, &error),
            "recording must stop atomically");
    require(!recorder.active(), "recorder must expose stopped state");
    require(recorder.sampleCount() == 2, "sample count must remain visible after stop");
    QFile manifest(manifestPath);
    require(manifest.open(QIODevice::ReadOnly), "manifest must be readable");
    const QJsonDocument parsed = QJsonDocument::fromJson(manifest.readAll());
    require(parsed.isObject(), "manifest must be a JSON object");
    const QJsonObject document = parsed.object();
    require(document.value(QStringLiteral("schema")).toString() ==
                QStringLiteral("multidetect.target-observation-recording-manifest.v1"),
            "manifest schema must be stable");
    require(document.value(QStringLiteral("sample_count")).toInt() == 2,
            "manifest sample count must match files");
    require(document.value(QStringLiteral("snapshots")).toArray().size() == 2,
            "manifest must hash-bind every snapshot");
    require(!document.value(QStringLiteral("flight_control_enabled")).toBool(),
            "recording must not enable flight control");
    require(!document.value(QStringLiteral("physical_release_enabled")).toBool(),
            "recording must not enable physical release");
    require(QDir(recorder.sessionDirectory())
                .entryList({QStringLiteral("target-observation-diagnostics-*.json")},
                           QDir::Files)
                .size() == 2,
            "session must contain two snapshots");
    require(QDir(recorder.sessionDirectory()).entryList({QStringLiteral("*.tmp")},
                                                        QDir::Files).isEmpty(),
            "atomic writes must leave no temporary files");

    std::cout << "SESSION_DIRECTORY="
              << QDir::toNativeSeparators(recorder.sessionDirectory()).toStdString() << '\n';
    std::cout << "MultiDetectTargetObservationRecorderSelfTest: PASS\n";
    return EXIT_SUCCESS;
}
