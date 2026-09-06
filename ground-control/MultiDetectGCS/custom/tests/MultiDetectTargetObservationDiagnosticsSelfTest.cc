#include "MultiDetectTargetObservationDiagnostics.h"

#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QSaveFile>
#include <QtCore/QTemporaryDir>
#include <cstdlib>
#include <iostream>

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

QVariantMap envelope()
{
    return {{QStringLiteral("atomicObservation"), true},
            {QStringLiteral("producerEpoch"), 7u},
            {QStringLiteral("sequence"), 42u},
            {QStringLiteral("sentAtMs"), QVariant::fromValue<qulonglong>(9000)},
            {QStringLiteral("sourceAgeMs"), QVariant::fromValue<quint16>(120)},
            {QStringLiteral("advisoryOnly"), true},
            {QStringLiteral("flightControlEnabled"), false},
            {QStringLiteral("physicalReleaseEnabled"), false}};
}

QVariantMap rangeStatus()
{
    QVariantMap status = envelope();
    status.insert(QStringLiteral("targetId"), QStringLiteral("target-3"));
    status.insert(QStringLiteral("sourceFrameId"), QStringLiteral("frame-88"));
    status.insert(QStringLiteral("validity"), QStringLiteral("VALID"));
    status.insert(QStringLiteral("slantRangeM"), 87.5);
    return status;
}

QVariantMap geolocationStatus()
{
    QVariantMap status = envelope();
    status.insert(QStringLiteral("targetId"), QStringLiteral("target-3"));
    status.insert(QStringLiteral("sourceFrameId"), QStringLiteral("frame-88"));
    status.insert(QStringLiteral("available"), true);
    status.insert(QStringLiteral("latitudeDeg"), 30.2741);
    status.insert(QStringLiteral("longitudeDeg"), 120.1551);
    status.insert(QStringLiteral("horizontalSigmaM"), 4.2);
    return status;
}

QVariantMap gnssStatus()
{
    QVariantMap status = envelope();
    status.insert(QStringLiteral("qualified"), true);
    status.insert(QStringLiteral("fixType"), 6);
    status.insert(QStringLiteral("satellitesVisible"), 18);
    return status;
}

QVariantMap makeDocument(const QVariantMap& range, const QVariantMap& geolocation,
                         const QVariantMap& gnss, qulonglong localNowMs = 10000,
                         qulonglong receivedAtMs = 9900)
{
    return MultiDetectTargetObservationDiagnostics::document(
        range, geolocation, gnss, {{QStringLiteral("active_epoch"), 7u}},
        QDateTime::fromString(QStringLiteral("2026-09-07T06:30:00.000Z"), Qt::ISODateWithMs),
        localNowMs, receivedAtMs, receivedAtMs, receivedAtMs);
}

} // namespace

int main(int argc, char* argv[])
{
    const QVariantMap valid = makeDocument(rangeStatus(), geolocationStatus(), gnssStatus());
    const QVariantMap validReadiness = valid.value(QStringLiteral("readiness")).toMap();
    require(validReadiness.value(QStringLiteral("locatable")).toBool(),
            "valid atomic observation must be locatable");
    require(validReadiness.value(QStringLiteral("coordinates_included")).toBool(),
            "valid observation must include coordinates");
    require(valid.value(QStringLiteral("target_geolocation_status")).toMap().contains(
                QStringLiteral("latitudeDeg")),
            "valid diagnostic must contain latitude");

    QVariantMap badGnss = gnssStatus();
    badGnss.insert(QStringLiteral("qualified"), false);
    badGnss.insert(QStringLiteral("reason"), QStringLiteral("horizontal_accuracy_exceeded"));
    const QVariantMap unqualified = makeDocument(rangeStatus(), geolocationStatus(), badGnss);
    const QVariantMap unqualifiedReadiness = unqualified.value(QStringLiteral("readiness")).toMap();
    require(!unqualifiedReadiness.value(QStringLiteral("locatable")).toBool(),
            "unqualified GNSS must fail closed");
    require(!unqualified.value(QStringLiteral("target_geolocation_status")).toMap().contains(
                QStringLiteral("latitudeDeg")),
            "failed diagnostic must omit latitude even when input carried it");
    require(!unqualified.value(QStringLiteral("target_geolocation_status")).toMap().contains(
                QStringLiteral("longitudeDeg")),
            "failed diagnostic must omit longitude even when input carried it");
    require(!unqualified.value(QStringLiteral("target_geolocation_status")).toMap().contains(
                QStringLiteral("horizontalSigmaM")),
            "failed diagnostic must omit uncertainty even when input carried it");

    QVariantMap mismatchedGnss = gnssStatus();
    mismatchedGnss.insert(QStringLiteral("sequence"), 43u);
    const QVariantMap mismatched = makeDocument(rangeStatus(), geolocationStatus(), mismatchedGnss);
    const QVariantMap mismatchReadiness = mismatched.value(QStringLiteral("readiness")).toMap();
    require(!mismatchReadiness.value(QStringLiteral("locatable")).toBool(),
            "mismatched sequence must fail closed");
    require(mismatchReadiness.value(QStringLiteral("reasons")).toStringList().contains(
                QStringLiteral("atomic_identity_mismatch")),
            "mismatched sequence reason must be retained");

    const QVariantMap stale = makeDocument(rangeStatus(), geolocationStatus(), gnssStatus(), 12000, 9900);
    require(!stale.value(QStringLiteral("readiness")).toMap().value(
                 QStringLiteral("locatable")).toBool(),
            "stale local receipt must fail closed");

    QTemporaryDir temporaryDirectory;
    require(temporaryDirectory.isValid(), "temporary directory must be available");
    QString savedPath;
    QString error;
    require(MultiDetectTargetObservationDiagnostics::save(
                temporaryDirectory.path(), rangeStatus(), geolocationStatus(), badGnss,
                {{QStringLiteral("active_epoch"), 7u}}, QDateTime::currentDateTimeUtc(),
                10000, 9900, 9900, 9900, &savedPath, &error),
            "fail-closed diagnostic must still save successfully");
    QFile savedFile(savedPath);
    require(savedFile.open(QIODevice::ReadOnly), "saved diagnostic must be readable");
    QJsonParseError parseError;
    const QJsonDocument savedDocument = QJsonDocument::fromJson(savedFile.readAll(), &parseError);
    require(parseError.error == QJsonParseError::NoError && savedDocument.isObject(),
            "saved diagnostic must be valid JSON");
    require(!savedDocument.object().value(QStringLiteral("readiness")).toObject()
                 .value(QStringLiteral("coordinates_included")).toBool(),
            "saved failed diagnostic must declare coordinates omitted");
    require(QDir(temporaryDirectory.path()).entryList({QStringLiteral("*.json")}, QDir::Files).size() == 1,
            "atomic save must leave exactly one JSON file");
    require(QDir(temporaryDirectory.path()).entryList({QStringLiteral("*.tmp")}, QDir::Files).isEmpty(),
            "atomic save must not leave temporary files");

    if (argc == 3 && QByteArray(argv[1]) == QByteArrayLiteral("--emit")) {
        constexpr qulonglong emittedNowMs = 1788762600000ULL;
        const QVariantMap emitted = makeDocument(
            rangeStatus(), geolocationStatus(), gnssStatus(), emittedNowMs, emittedNowMs - 100);
        QSaveFile output(QString::fromLocal8Bit(argv[2]));
        require(output.open(QIODevice::WriteOnly), "emitted diagnostic output must open");
        require(output.write(QJsonDocument::fromVariant(emitted).toJson(QJsonDocument::Indented)) > 0,
                "emitted diagnostic output must be written");
        require(output.commit(), "emitted diagnostic output must commit atomically");
    }

    std::cout << "MultiDetectTargetObservationDiagnosticsSelfTest: PASS\n";
    return EXIT_SUCCESS;
}
