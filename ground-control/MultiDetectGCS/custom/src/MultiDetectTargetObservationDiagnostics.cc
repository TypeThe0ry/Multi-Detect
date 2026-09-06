#include "MultiDetectTargetObservationDiagnostics.h"

#include <QtCore/QDir>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QMetaType>
#include <QtCore/QSaveFile>
#include <QtCore/QStringList>
#include <cmath>
#include <utility>

namespace {

void addReason(QStringList* reasons, const QString& reason)
{
    if (!reasons->contains(reason)) {
        reasons->append(reason);
    }
}

bool strictUnsigned(const QVariantMap& status, const QString& key, qulonglong* value)
{
    const QVariant candidate = status.value(key);
    switch (candidate.metaType().id()) {
    case QMetaType::UChar:
        *value = candidate.value<uchar>();
        return true;
    case QMetaType::UShort:
        *value = candidate.value<ushort>();
        return true;
    case QMetaType::UInt:
        *value = candidate.toUInt();
        return true;
    case QMetaType::ULongLong:
        *value = candidate.toULongLong();
        return true;
    case QMetaType::Int: {
        const int number = candidate.toInt();
        if (number < 0) {
            return false;
        }
        *value = static_cast<qulonglong>(number);
        return true;
    }
    case QMetaType::SChar: {
        const signed char number = candidate.value<signed char>();
        if (number < 0) {
            return false;
        }
        *value = static_cast<qulonglong>(number);
        return true;
    }
    case QMetaType::Short: {
        const short number = candidate.value<short>();
        if (number < 0) {
            return false;
        }
        *value = static_cast<qulonglong>(number);
        return true;
    }
    case QMetaType::LongLong: {
        const qlonglong number = candidate.toLongLong();
        if (number < 0) {
            return false;
        }
        *value = static_cast<qulonglong>(number);
        return true;
    }
    default:
        return false;
    }
}

bool strictBool(const QVariantMap& status, const QString& key, bool* value)
{
    const QVariant candidate = status.value(key);
    if (candidate.metaType().id() != QMetaType::Bool) {
        return false;
    }
    *value = candidate.toBool();
    return true;
}

bool finiteNumber(const QVariantMap& status, const QString& key, double* value)
{
    const QVariant candidate = status.value(key);
    switch (candidate.metaType().id()) {
    case QMetaType::Double:
    case QMetaType::Float:
    case QMetaType::Int:
    case QMetaType::UInt:
    case QMetaType::LongLong:
    case QMetaType::ULongLong:
        *value = candidate.toDouble();
        return std::isfinite(*value);
    default:
        return false;
    }
}

QVariantMap sanitized(const QVariantMap& source, const QStringList& allowedKeys)
{
    QVariantMap result;
    for (const QString& key : allowedKeys) {
        const auto iterator = source.constFind(key);
        if (iterator != source.constEnd()) {
            result.insert(key, iterator.value());
        }
    }
    return result;
}

bool validateAtomicIdentity(const QVariantMap& rangeStatus,
                            const QVariantMap& geolocationStatus,
                            const QVariantMap& gnssStatus,
                            QStringList* reasons)
{
    bool atomic = false;
    for (const auto& status : {rangeStatus, geolocationStatus, gnssStatus}) {
        bool currentAtomic = false;
        if (!strictBool(status, QStringLiteral("atomicObservation"), &currentAtomic) || !currentAtomic) {
            atomic = false;
            addReason(reasons, QStringLiteral("atomic_observation_required"));
            break;
        }
        atomic = true;
    }

    qulonglong rangeEpoch = 0;
    qulonglong geolocationEpoch = 0;
    qulonglong gnssEpoch = 0;
    qulonglong rangeSequence = 0;
    qulonglong geolocationSequence = 0;
    qulonglong gnssSequence = 0;
    qulonglong rangeSentAtMs = 0;
    qulonglong geolocationSentAtMs = 0;
    qulonglong gnssSentAtMs = 0;
    const bool identityTyped =
        strictUnsigned(rangeStatus, QStringLiteral("producerEpoch"), &rangeEpoch) &&
        strictUnsigned(geolocationStatus, QStringLiteral("producerEpoch"), &geolocationEpoch) &&
        strictUnsigned(gnssStatus, QStringLiteral("producerEpoch"), &gnssEpoch) &&
        strictUnsigned(rangeStatus, QStringLiteral("sequence"), &rangeSequence) &&
        strictUnsigned(geolocationStatus, QStringLiteral("sequence"), &geolocationSequence) &&
        strictUnsigned(gnssStatus, QStringLiteral("sequence"), &gnssSequence) &&
        strictUnsigned(rangeStatus, QStringLiteral("sentAtMs"), &rangeSentAtMs) &&
        strictUnsigned(geolocationStatus, QStringLiteral("sentAtMs"), &geolocationSentAtMs) &&
        strictUnsigned(gnssStatus, QStringLiteral("sentAtMs"), &gnssSentAtMs);
    if (!identityTyped) {
        addReason(reasons, QStringLiteral("atomic_identity_invalid"));
        return false;
    }
    if (rangeEpoch == 0 || rangeEpoch != geolocationEpoch || rangeEpoch != gnssEpoch ||
        rangeSequence != geolocationSequence || rangeSequence != gnssSequence ||
        rangeSentAtMs != geolocationSentAtMs || rangeSentAtMs != gnssSentAtMs) {
        addReason(reasons, QStringLiteral("atomic_identity_mismatch"));
        return false;
    }
    return atomic;
}

void validateSourceAge(const QVariantMap& status, const QString& statusName, QStringList* reasons)
{
    qulonglong sourceAgeMs = 0;
    if (!strictUnsigned(status, QStringLiteral("sourceAgeMs"), &sourceAgeMs)) {
        addReason(reasons, statusName + QStringLiteral("_source_age_invalid"));
    } else if (sourceAgeMs > MultiDetectTargetObservationDiagnostics::maximumAgeMs()) {
        addReason(reasons, statusName + QStringLiteral("_source_stale"));
    }
}

QVariantMap localReceiveEntry(qulonglong receivedAtMs, qulonglong localNowMs,
                              const QString& statusName, QStringList* reasons)
{
    QVariantMap entry;
    entry.insert(QStringLiteral("received_at_ms"), QVariant::fromValue(receivedAtMs));
    if (receivedAtMs == 0 || receivedAtMs > localNowMs) {
        entry.insert(QStringLiteral("age_ms"), QVariant());
        addReason(reasons, statusName + QStringLiteral("_local_receive_invalid"));
        return entry;
    }
    const qulonglong ageMs = localNowMs - receivedAtMs;
    entry.insert(QStringLiteral("age_ms"), QVariant::fromValue(ageMs));
    if (ageMs > MultiDetectTargetObservationDiagnostics::maximumAgeMs()) {
        addReason(reasons, statusName + QStringLiteral("_local_receive_stale"));
    }
    return entry;
}

} // namespace

QVariantMap MultiDetectTargetObservationDiagnostics::document(
    const QVariantMap& rangeStatus, const QVariantMap& targetGeolocationStatus,
    const QVariantMap& gnssStatus, const QVariantMap& producerEpochSummary,
    const QDateTime& generatedAtUtc, qulonglong localNowMs,
    qulonglong lastRangeReceivedAtMs, qulonglong lastTargetGeolocationReceivedAtMs,
    qulonglong lastGnssReceivedAtMs)
{
    QStringList reasons;
    if (rangeStatus.isEmpty()) {
        addReason(&reasons, QStringLiteral("range_status_missing"));
    }
    if (targetGeolocationStatus.isEmpty()) {
        addReason(&reasons, QStringLiteral("target_geolocation_status_missing"));
    }
    if (gnssStatus.isEmpty()) {
        addReason(&reasons, QStringLiteral("gnss_status_missing"));
    }

    const QString validity = rangeStatus.value(QStringLiteral("validity")).toString().trimmed().toUpper();
    if (validity != QStringLiteral("VALID") && validity != QStringLiteral("DEGRADED")) {
        addReason(&reasons, QStringLiteral("range_not_qualified"));
    }
    bool geolocationAvailable = false;
    if (!strictBool(targetGeolocationStatus, QStringLiteral("available"), &geolocationAvailable) ||
        !geolocationAvailable) {
        addReason(&reasons, QStringLiteral("target_geolocation_unavailable"));
    }
    bool gnssQualified = false;
    if (!strictBool(gnssStatus, QStringLiteral("qualified"), &gnssQualified) || !gnssQualified) {
        addReason(&reasons, QStringLiteral("gnss_not_qualified"));
    }

    const bool atomicIdentityMatched = validateAtomicIdentity(
        rangeStatus, targetGeolocationStatus, gnssStatus, &reasons);

    const QString rangeTargetId = rangeStatus.value(QStringLiteral("targetId")).toString().trimmed();
    const QString geolocationTargetId =
        targetGeolocationStatus.value(QStringLiteral("targetId")).toString().trimmed();
    const QString rangeFrameId = rangeStatus.value(QStringLiteral("sourceFrameId")).toString().trimmed();
    const QString geolocationFrameId =
        targetGeolocationStatus.value(QStringLiteral("sourceFrameId")).toString().trimmed();
    if (rangeTargetId.isEmpty() || rangeTargetId != geolocationTargetId) {
        addReason(&reasons, QStringLiteral("target_id_mismatch"));
    }
    if (rangeFrameId.isEmpty() || rangeFrameId != geolocationFrameId) {
        addReason(&reasons, QStringLiteral("source_frame_id_mismatch"));
    }

    validateSourceAge(rangeStatus, QStringLiteral("range"), &reasons);
    validateSourceAge(targetGeolocationStatus, QStringLiteral("target_geolocation"), &reasons);
    validateSourceAge(gnssStatus, QStringLiteral("gnss"), &reasons);

    QVariantMap localReceive;
    localReceive.insert(QStringLiteral("range"),
                        localReceiveEntry(lastRangeReceivedAtMs, localNowMs, QStringLiteral("range"), &reasons));
    localReceive.insert(QStringLiteral("target_geolocation"),
                        localReceiveEntry(lastTargetGeolocationReceivedAtMs, localNowMs,
                                          QStringLiteral("target_geolocation"), &reasons));
    localReceive.insert(QStringLiteral("gnss"),
                        localReceiveEntry(lastGnssReceivedAtMs, localNowMs, QStringLiteral("gnss"), &reasons));

    double latitudeDeg = 0.0;
    double longitudeDeg = 0.0;
    double horizontalSigmaM = 0.0;
    const bool coordinatesValid =
        finiteNumber(targetGeolocationStatus, QStringLiteral("latitudeDeg"), &latitudeDeg) &&
        latitudeDeg > -90.0 && latitudeDeg < 90.0 &&
        finiteNumber(targetGeolocationStatus, QStringLiteral("longitudeDeg"), &longitudeDeg) &&
        longitudeDeg >= -180.0 && longitudeDeg <= 180.0 &&
        finiteNumber(targetGeolocationStatus, QStringLiteral("horizontalSigmaM"), &horizontalSigmaM) &&
        horizontalSigmaM >= 0.0;
    if (!coordinatesValid) {
        addReason(&reasons, QStringLiteral("coordinates_invalid"));
    }

    const bool locatable = reasons.isEmpty();
    QVariantMap geolocation = sanitized(
        targetGeolocationStatus,
        {QStringLiteral("targetId"), QStringLiteral("sourceFrameId"), QStringLiteral("available"),
         QStringLiteral("reason"), QStringLiteral("sourceAgeMs"), QStringLiteral("atomicObservation"),
         QStringLiteral("producerEpoch"), QStringLiteral("sequence"), QStringLiteral("sentAtMs"),
         QStringLiteral("advisoryOnly"), QStringLiteral("flightControlEnabled"),
         QStringLiteral("physicalReleaseEnabled")});
    if (locatable) {
        geolocation.insert(QStringLiteral("latitudeDeg"), latitudeDeg);
        geolocation.insert(QStringLiteral("longitudeDeg"), longitudeDeg);
        geolocation.insert(QStringLiteral("horizontalSigmaM"), horizontalSigmaM);
    }

    bool statusFresh = !rangeStatus.isEmpty() && !targetGeolocationStatus.isEmpty() && !gnssStatus.isEmpty();
    for (const QString& reason : std::as_const(reasons)) {
        if (reason.contains(QStringLiteral("source_age_invalid")) ||
            reason.endsWith(QStringLiteral("_source_stale")) ||
            reason.contains(QStringLiteral("_local_receive_"))) {
            statusFresh = false;
            break;
        }
    }

    QVariantMap readiness;
    readiness.insert(QStringLiteral("locatable"), locatable);
    readiness.insert(QStringLiteral("reasons"), reasons);
    readiness.insert(QStringLiteral("atomic_identity_matched"), atomicIdentityMatched);
    readiness.insert(QStringLiteral("status_fresh"), statusFresh);
    readiness.insert(QStringLiteral("coordinates_included"), locatable);

    QVariantMap document;
    document.insert(QStringLiteral("schema"),
                    QStringLiteral("multidetect.target-observation-diagnostics.v1"));
    document.insert(QStringLiteral("schema_version"), 1);
    document.insert(QStringLiteral("generated_at_utc"),
                    generatedAtUtc.toUTC().toString(Qt::ISODateWithMs));
    document.insert(QStringLiteral("local_now_ms"), QVariant::fromValue(localNowMs));
    document.insert(QStringLiteral("maximum_age_ms"), QVariant::fromValue(maximumAgeMs()));
    document.insert(QStringLiteral("readiness"), readiness);
    document.insert(QStringLiteral("producer_epoch_summary"), producerEpochSummary);
    document.insert(QStringLiteral("range_status"),
                    sanitized(rangeStatus,
                              {QStringLiteral("targetId"), QStringLiteral("calibrationId"),
                               QStringLiteral("sourceFrameId"), QStringLiteral("validity"),
                               QStringLiteral("reasons"), QStringLiteral("sources"),
                               QStringLiteral("rejectedSources"), QStringLiteral("slantRangeM"),
                               QStringLiteral("groundRangeM"), QStringLiteral("slantRangeLowM"),
                               QStringLiteral("slantRangeHighM"), QStringLiteral("groundRangeLowM"),
                               QStringLiteral("groundRangeHighM"), QStringLiteral("relativeBearingDeg"),
                               QStringLiteral("absoluteBearingDeg"), QStringLiteral("bearingSigmaDeg"),
                               QStringLiteral("northOffsetM"), QStringLiteral("eastOffsetM"),
                               QStringLiteral("sourceAgeMs"), QStringLiteral("dataFreshnessS"),
                               QStringLiteral("sensorConsistency"), QStringLiteral("sourceContributions"),
                               QStringLiteral("fusionProfile"), QStringLiteral("vehicleProfile"),
                               QStringLiteral("navigationState"), QStringLiteral("motionRegime"),
                               QStringLiteral("atomicObservation"), QStringLiteral("producerEpoch"),
                               QStringLiteral("sequence"), QStringLiteral("sentAtMs"),
                               QStringLiteral("advisoryOnly"), QStringLiteral("flightControlEnabled"),
                               QStringLiteral("physicalReleaseEnabled")}));
    document.insert(QStringLiteral("target_geolocation_status"), geolocation);
    document.insert(QStringLiteral("gnss_status"),
                    sanitized(gnssStatus,
                              {QStringLiteral("qualified"), QStringLiteral("reason"),
                               QStringLiteral("positionHealthyAvailable"), QStringLiteral("positionHealthy"),
                               QStringLiteral("fixTypeAvailable"), QStringLiteral("fixType"),
                               QStringLiteral("satellitesAvailable"), QStringLiteral("satellitesVisible"),
                               QStringLiteral("horizontalAccuracyAvailable"),
                               QStringLiteral("horizontalAccuracyM"),
                               QStringLiteral("verticalAccuracyAvailable"), QStringLiteral("verticalAccuracyM"),
                               QStringLiteral("sourceAgeAvailable"),
                               QStringLiteral("headingAccuracyDeg"), QStringLiteral("sourceAgeMs"),
                               QStringLiteral("atomicObservation"), QStringLiteral("producerEpoch"),
                               QStringLiteral("sequence"), QStringLiteral("sentAtMs"),
                               QStringLiteral("advisoryOnly"), QStringLiteral("flightControlEnabled"),
                               QStringLiteral("physicalReleaseEnabled")}));
    document.insert(QStringLiteral("local_receive"), localReceive);
    document.insert(QStringLiteral("advisory_only"), true);
    document.insert(QStringLiteral("flight_control_enabled"), false);
    document.insert(QStringLiteral("physical_release_enabled"), false);
    return document;
}

QByteArray MultiDetectTargetObservationDiagnostics::json(
    const QVariantMap& rangeStatus, const QVariantMap& targetGeolocationStatus,
    const QVariantMap& gnssStatus, const QVariantMap& producerEpochSummary,
    const QDateTime& generatedAtUtc, qulonglong localNowMs,
    qulonglong lastRangeReceivedAtMs, qulonglong lastTargetGeolocationReceivedAtMs,
    qulonglong lastGnssReceivedAtMs)
{
    return QJsonDocument::fromVariant(document(
               rangeStatus, targetGeolocationStatus, gnssStatus, producerEpochSummary,
               generatedAtUtc, localNowMs, lastRangeReceivedAtMs,
               lastTargetGeolocationReceivedAtMs, lastGnssReceivedAtMs))
        .toJson(QJsonDocument::Indented);
}

bool MultiDetectTargetObservationDiagnostics::save(
    const QString& directoryPath, const QVariantMap& rangeStatus,
    const QVariantMap& targetGeolocationStatus, const QVariantMap& gnssStatus,
    const QVariantMap& producerEpochSummary, const QDateTime& generatedAtUtc,
    qulonglong localNowMs, qulonglong lastRangeReceivedAtMs,
    qulonglong lastTargetGeolocationReceivedAtMs, qulonglong lastGnssReceivedAtMs,
    QString* savedPath, QString* error)
{
    if (savedPath) {
        savedPath->clear();
    }
    if (error) {
        error->clear();
    }
    if (directoryPath.trimmed().isEmpty()) {
        if (error) {
            *error = QStringLiteral("Target observation diagnostics directory is empty.");
        }
        return false;
    }

    QDir directory(directoryPath);
    if (!directory.exists() && !QDir().mkpath(directory.absolutePath())) {
        if (error) {
            *error = QStringLiteral("Cannot create target observation diagnostics directory: %1")
                         .arg(QDir::toNativeSeparators(directory.absolutePath()));
        }
        return false;
    }

    const QString timestamp = generatedAtUtc.toUTC().toString(QStringLiteral("yyyyMMddTHHmmsszzz'Z'"));
    const QString stem = QStringLiteral("target-observation-diagnostics-%1").arg(timestamp);
    QString outputPath = directory.filePath(stem + QStringLiteral(".json"));
    for (int suffix = 1; QFileInfo::exists(outputPath); ++suffix) {
        if (suffix > 9999) {
            if (error) {
                *error = QStringLiteral("Cannot allocate a unique target observation diagnostics filename.");
            }
            return false;
        }
        outputPath = directory.filePath(QStringLiteral("%1-%2.json").arg(stem).arg(suffix));
    }

    const QByteArray payload = json(rangeStatus, targetGeolocationStatus, gnssStatus,
                                    producerEpochSummary, generatedAtUtc, localNowMs,
                                    lastRangeReceivedAtMs, lastTargetGeolocationReceivedAtMs,
                                    lastGnssReceivedAtMs);
    QSaveFile file(outputPath);
    if (!file.open(QIODevice::WriteOnly) || file.write(payload) != payload.size()) {
        if (error) {
            *error = QStringLiteral("Cannot write target observation diagnostics file: %1")
                         .arg(file.errorString());
        }
        file.cancelWriting();
        return false;
    }
    if (!file.commit()) {
        if (error) {
            *error = QStringLiteral("Cannot commit target observation diagnostics file: %1")
                         .arg(file.errorString());
        }
        return false;
    }
    if (savedPath) {
        *savedPath = QDir::toNativeSeparators(QFileInfo(outputPath).absoluteFilePath());
    }
    return true;
}
