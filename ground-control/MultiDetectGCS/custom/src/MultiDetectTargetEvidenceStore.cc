#include "MultiDetectTargetEvidenceStore.h"

#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonParseError>
#include <QtCore/QJsonValue>
#include <QtCore/QMetaType>
#include <QtCore/QStandardPaths>
#include <cmath>

namespace {

constexpr double kMaximumSnapshotRangeM = 100000.0;
constexpr double kMaximumSnapshotSourceAgeMs = 86400000.0;
constexpr int kMaximumTargetIdLength = 128;
constexpr int kMaximumTargetLabelLength = 128;
constexpr int kMaximumSourceFrameIdLength = 256;
constexpr int kMaximumSavedAtLength = 64;
constexpr double kMaximumExactJsonInteger = 9007199254740991.0;
constexpr double kMaximumGnssSequence = 4294967295.0;

bool finiteNumber(const QVariant& value, double* result)
{
    bool converted = false;
    const double number = value.toDouble(&converted);
    if (!converted || !std::isfinite(number)) {
        return false;
    }
    *result = number;
    return true;
}

QString boundedText(const QVariantMap& snapshot, const char* key, int maximumLength)
{
    return snapshot.value(QString::fromLatin1(key)).toString().trimmed().left(maximumLength);
}

bool validRangeValidity(const QString& validity)
{
    return validity == QStringLiteral("VALID") || validity == QStringLiteral("DEGRADED") ||
           validity == QStringLiteral("UNAVAILABLE");
}

bool validJsonText(
    const QJsonObject& record,
    const QString& key,
    int maximumLength,
    bool required)
{
    const QJsonValue value = record.value(key);
    if (value.isUndefined() || value.isNull()) {
        return !required;
    }
    if (!value.isString()) {
        return false;
    }
    const QString text = value.toString().trimmed();
    return text.size() <= maximumLength && (!required || !text.isEmpty());
}

bool finiteJsonNumber(
    const QJsonObject& record,
    const QString& key,
    double minimum,
    double maximum,
    bool required,
    bool* present,
    double* result)
{
    const QJsonValue value = record.value(key);
    if (value.isUndefined() || value.isNull()) {
        *present = false;
        return !required;
    }
    *present = true;
    if (!value.isDouble()) {
        return false;
    }
    const double number = value.toDouble();
    if (!std::isfinite(number) || number < minimum || number > maximum) {
        return false;
    }
    *result = number;
    return true;
}

bool validSnapshotRecord(const QJsonObject& record)
{
    const QString schema = record.value(QStringLiteral("schema")).toString();
    const bool legacyV1 = schema == QStringLiteral("multidetect.target-evidence.v1");
    const bool gnssV2 = schema == QStringLiteral("multidetect.target-evidence.v2");
    const bool boundV3 = schema == QStringLiteral("multidetect.target-evidence.v3");
    const bool atomicV4 = schema == QStringLiteral("multidetect.target-evidence.v4");
    if (!legacyV1 && !gnssV2 && !boundV3 && !atomicV4) {
        return false;
    }
    if (!validJsonText(record, QStringLiteral("target_id"), kMaximumTargetIdLength, true) ||
        !validJsonText(record, QStringLiteral("target_label"), kMaximumTargetLabelLength, false) ||
        !validJsonText(record, QStringLiteral("source_frame_id"), kMaximumSourceFrameIdLength, false) ||
        !validJsonText(record, QStringLiteral("saved_at_utc"), kMaximumSavedAtLength, true)) {
        return false;
    }

    bool present = false;
    double value = 0.0;
    // Keep the stored coordinate domain identical to the live qualified-map
    // contract: a pole is not a usable local map origin (longitude is
    // undefined there), so exact +/-90 degree latitudes are rejected.
    if (!finiteJsonNumber(record, QStringLiteral("latitude_deg"), -90.0, 90.0, true, &present, &value) ||
        !finiteJsonNumber(record, QStringLiteral("longitude_deg"), -180.0, 180.0, true, &present, &value) ||
        !finiteJsonNumber(record, QStringLiteral("horizontal_sigma_m"), 0.0, 100000.0, true, &present, &value)) {
        return false;
    }
    const double latitude = record.value(QStringLiteral("latitude_deg")).toDouble();
    if (!(latitude > -90.0 && latitude < 90.0)) {
        return false;
    }

    const QJsonValue validityValue = record.value(QStringLiteral("range_validity"));
    if (!validityValue.isString()) {
        return false;
    }
    const QString validity = validityValue.toString().trimmed();
    if (!validRangeValidity(validity)) {
        return false;
    }

    bool rangePresent = false;
    bool bearingPresent = false;
    bool sourceAgePresent = false;
    if (!finiteJsonNumber(record, QStringLiteral("range_m"), 0.0, kMaximumSnapshotRangeM, false,
                          &rangePresent, &value) ||
        !finiteJsonNumber(record, QStringLiteral("relative_bearing_deg"), -360.0, 360.0, false,
                          &bearingPresent, &value) ||
        !finiteJsonNumber(record, QStringLiteral("source_age_ms"), 0.0, kMaximumSnapshotSourceAgeMs,
                          false, &sourceAgePresent, &value)) {
        return false;
    }
    if ((validity == QStringLiteral("VALID") || validity == QStringLiteral("DEGRADED")) &&
        !rangePresent) {
        return false;
    }
    if (validity == QStringLiteral("UNAVAILABLE") && (rangePresent || bearingPresent)) {
        return false;
    }

    if (gnssV2 || boundV3 || atomicV4) {
        bool gnssPresent = false;
        double gnssValue = 0.0;
        const auto validInteger = [&record, &gnssPresent, &gnssValue](
                                      const QString& key, double minimum, double maximum) {
            if (!finiteJsonNumber(record, key, minimum, maximum, true, &gnssPresent, &gnssValue)) {
                return false;
            }
            return std::floor(gnssValue) == gnssValue;
        };
        if (!validInteger(QStringLiteral("gnss_status_sequence"), 0.0, kMaximumGnssSequence) ||
            !validInteger(QStringLiteral("gnss_sent_at_ms"), 0.0, kMaximumExactJsonInteger) ||
            !validInteger(QStringLiteral("gnss_fix_type"), 3.0, 8.0) ||
            !validInteger(QStringLiteral("gnss_satellites_visible"), 6.0, 255.0) ||
            !finiteJsonNumber(record, QStringLiteral("gnss_horizontal_accuracy_m"), 0.0, 6.0,
                              true, &gnssPresent, &gnssValue) ||
            !finiteJsonNumber(record, QStringLiteral("gnss_vertical_accuracy_m"), 0.0, 10.0,
                              true, &gnssPresent, &gnssValue) ||
            !finiteJsonNumber(record, QStringLiteral("gnss_source_age_ms"), 0.0, 1500.0,
                              true, &gnssPresent, &gnssValue)) {
            return false;
        }
        if (!record.value(QStringLiteral("gnss_qualified")).isBool() ||
            !record.value(QStringLiteral("gnss_qualified")).toBool() ||
            !record.value(QStringLiteral("gnss_position_healthy")).isBool() ||
            !record.value(QStringLiteral("gnss_position_healthy")).toBool() ||
            record.value(QStringLiteral("gnss_reason")).toString() != QStringLiteral("gps_qualified")) {
            return false;
        }
        if (boundV3 || atomicV4) {
            double targetSequence = 0.0;
            double targetSentAtMs = 0.0;
            double gnssSentAtMs = 0.0;
            if (!finiteJsonNumber(record, QStringLiteral("target_geolocation_sequence"), 0.0,
                                  kMaximumGnssSequence, true, &gnssPresent, &targetSequence) ||
                std::floor(targetSequence) != targetSequence ||
                !finiteJsonNumber(record, QStringLiteral("target_geolocation_sent_at_ms"), 0.0,
                                  kMaximumExactJsonInteger, true, &gnssPresent, &targetSentAtMs) ||
                std::floor(targetSentAtMs) != targetSentAtMs ||
                !finiteJsonNumber(record, QStringLiteral("gnss_sent_at_ms"), 0.0,
                                  kMaximumExactJsonInteger, true, &gnssPresent, &gnssSentAtMs) ||
                targetSentAtMs != gnssSentAtMs) {
                return false;
            }
        }
        if (atomicV4) {
            double rangeSequence = 0.0;
            double rangeSentAtMs = 0.0;
            double targetSequence = 0.0;
            double targetSentAtMs = 0.0;
            double gnssSequence = 0.0;
            double gnssSentAtMs = 0.0;
            if (!record.value(QStringLiteral("atomic_observation")).isBool() ||
                !record.value(QStringLiteral("atomic_observation")).toBool() ||
                !finiteJsonNumber(record, QStringLiteral("range_status_sequence"), 0.0,
                                  kMaximumGnssSequence, true, &gnssPresent, &rangeSequence) ||
                std::floor(rangeSequence) != rangeSequence ||
                !finiteJsonNumber(record, QStringLiteral("range_sent_at_ms"), 0.0,
                                  kMaximumExactJsonInteger, true, &gnssPresent, &rangeSentAtMs) ||
                std::floor(rangeSentAtMs) != rangeSentAtMs ||
                !finiteJsonNumber(record, QStringLiteral("target_geolocation_sequence"), 0.0,
                                  kMaximumGnssSequence, true, &gnssPresent, &targetSequence) ||
                !finiteJsonNumber(record, QStringLiteral("target_geolocation_sent_at_ms"), 0.0,
                                  kMaximumExactJsonInteger, true, &gnssPresent, &targetSentAtMs) ||
                !finiteJsonNumber(record, QStringLiteral("gnss_status_sequence"), 0.0,
                                  kMaximumGnssSequence, true, &gnssPresent, &gnssSequence) ||
                !finiteJsonNumber(record, QStringLiteral("gnss_sent_at_ms"), 0.0,
                                  kMaximumExactJsonInteger, true, &gnssPresent, &gnssSentAtMs) ||
                rangeSequence != targetSequence || targetSequence != gnssSequence ||
                rangeSentAtMs != targetSentAtMs || targetSentAtMs != gnssSentAtMs) {
                return false;
            }
        }
    }
    return true;
}

bool optionalFiniteNumber(
    const QVariantMap& snapshot,
    const char* key,
    double minimum,
    double maximum,
    bool* present,
    double* result)
{
    const QVariant value = snapshot.value(QString::fromLatin1(key));
    if (!value.isValid() || value.isNull()) {
        *present = false;
        return true;
    }
    *present = true;
    return finiteNumber(value, result) && *result >= minimum && *result <= maximum;
}

}  // namespace

MultiDetectTargetEvidenceStore::MultiDetectTargetEvidenceStore(QObject* parent)
    : QObject(parent)
{
    const QString appDataPath = QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation);
    _storagePath = QDir(appDataPath).filePath(QStringLiteral("snapshots/target-snapshots.jsonl"));
    _loadSnapshotCount();
}

MultiDetectTargetEvidenceStore::MultiDetectTargetEvidenceStore(
    const QString& storagePath,
    QObject* parent)
    : QObject(parent)
    , _storagePath(storagePath)
{
    _loadSnapshotCount();
}

bool MultiDetectTargetEvidenceStore::saveSnapshot(const QVariantMap& snapshot)
{
    if (_snapshotCount >= kMaximumSnapshots) {
        _setLastError(tr("Snapshot limit reached; archive the local evidence file before saving more."));
        return false;
    }

    const QString targetId = boundedText(snapshot, "target_id", kMaximumTargetIdLength);
    if (targetId.isEmpty()) {
        _setLastError(tr("A target ID is required for an evidence snapshot."));
        return false;
    }

    double latitudeDeg = 0.0;
    double longitudeDeg = 0.0;
    double horizontalSigmaM = 0.0;
    if (!finiteNumber(snapshot.value(QStringLiteral("latitude_deg")), &latitudeDeg)
        || !finiteNumber(snapshot.value(QStringLiteral("longitude_deg")), &longitudeDeg)
        || !finiteNumber(snapshot.value(QStringLiteral("horizontal_sigma_m")), &horizontalSigmaM)
        || latitudeDeg <= -90.0 || latitudeDeg >= 90.0
        || longitudeDeg < -180.0 || longitudeDeg > 180.0
        || horizontalSigmaM < 0.0 || horizontalSigmaM > 100000.0) {
        _setLastError(tr("Qualified latitude, longitude, and horizontal uncertainty are required."));
        return false;
    }

    QString rangeValidity = boundedText(snapshot, "range_validity", 48).toUpper();
    if (rangeValidity.isEmpty()) {
        rangeValidity = QStringLiteral("UNAVAILABLE");
    }
    if (!validRangeValidity(rangeValidity)) {
        _setLastError(tr("Range validity must be VALID, DEGRADED, or UNAVAILABLE."));
        return false;
    }

    bool rangePresent = false;
    bool bearingPresent = false;
    bool sourceAgePresent = false;
    double rangeM = 0.0;
    double bearingDeg = 0.0;
    double sourceAgeMs = 0.0;
    if (!optionalFiniteNumber(snapshot, "range_m", 0.0, kMaximumSnapshotRangeM, &rangePresent, &rangeM) ||
        !optionalFiniteNumber(snapshot, "relative_bearing_deg", -360.0, 360.0, &bearingPresent,
                              &bearingDeg) ||
        !optionalFiniteNumber(snapshot, "source_age_ms", 0.0, kMaximumSnapshotSourceAgeMs,
                              &sourceAgePresent, &sourceAgeMs)) {
        _setLastError(tr("Range and source age values are outside the evidence limits."));
        return false;
    }
    if ((rangeValidity == QStringLiteral("VALID") || rangeValidity == QStringLiteral("DEGRADED")) &&
        !rangePresent) {
        _setLastError(tr("Valid or degraded range evidence requires a distance."));
        return false;
    }
    if (rangeValidity == QStringLiteral("UNAVAILABLE") && (rangePresent || bearingPresent)) {
        _setLastError(tr("Unavailable range evidence cannot contain distance or bearing."));
        return false;
    }

    const auto requiredBoolean = [&snapshot](const char* key, bool* result) {
        const QVariant value = snapshot.value(QString::fromLatin1(key));
        if (!value.isValid() || value.isNull() || value.metaType().id() != QMetaType::Bool) {
            return false;
        }
        *result = value.toBool();
        return true;
    };
    const auto requiredInteger = [&snapshot](const char* key, double minimum, double maximum,
                                              double* result) {
        if (!finiteNumber(snapshot.value(QString::fromLatin1(key)), result) ||
            *result < minimum || *result > maximum) {
            return false;
        }
        return std::floor(*result) == *result;
    };
    bool gnssQualified = false;
    bool gnssPositionHealthy = false;
    bool atomicObservation = false;
    double rangeSequence = 0.0;
    double rangeSentAtMs = 0.0;
    double targetGeolocationSequence = 0.0;
    double targetGeolocationSentAtMs = 0.0;
    double gnssSequence = 0.0;
    double gnssSentAtMs = 0.0;
    double gnssFixType = 0.0;
    double gnssSatellitesVisible = 0.0;
    double gnssHorizontalAccuracyM = 0.0;
    double gnssVerticalAccuracyM = 0.0;
    double gnssSourceAgeMs = 0.0;
    const QString gnssReason = boundedText(snapshot, "gnss_reason", 64);
    if (!requiredBoolean("atomic_observation", &atomicObservation) || !atomicObservation ||
        !requiredBoolean("gnss_qualified", &gnssQualified) || !gnssQualified ||
        !requiredBoolean("gnss_position_healthy", &gnssPositionHealthy) || !gnssPositionHealthy ||
        gnssReason != QStringLiteral("gps_qualified") ||
        !requiredInteger("range_status_sequence", 0.0, kMaximumGnssSequence, &rangeSequence) ||
        !requiredInteger("range_sent_at_ms", 0.0, kMaximumExactJsonInteger, &rangeSentAtMs) ||
        !requiredInteger("target_geolocation_sequence", 0.0, kMaximumGnssSequence,
                         &targetGeolocationSequence) ||
        !requiredInteger("target_geolocation_sent_at_ms", 0.0, kMaximumExactJsonInteger,
                         &targetGeolocationSentAtMs) ||
        !requiredInteger("gnss_status_sequence", 0.0, kMaximumGnssSequence, &gnssSequence) ||
        !requiredInteger("gnss_sent_at_ms", 0.0, kMaximumExactJsonInteger, &gnssSentAtMs) ||
        !requiredInteger("gnss_fix_type", 3.0, 8.0, &gnssFixType) ||
        !requiredInteger("gnss_satellites_visible", 6.0, 255.0, &gnssSatellitesVisible) ||
        !finiteNumber(snapshot.value(QStringLiteral("gnss_horizontal_accuracy_m")),
                      &gnssHorizontalAccuracyM) || gnssHorizontalAccuracyM < 0.0 ||
        gnssHorizontalAccuracyM > 6.0 ||
        !finiteNumber(snapshot.value(QStringLiteral("gnss_vertical_accuracy_m")),
                      &gnssVerticalAccuracyM) || gnssVerticalAccuracyM < 0.0 ||
        gnssVerticalAccuracyM > 10.0 ||
        !finiteNumber(snapshot.value(QStringLiteral("gnss_source_age_ms")), &gnssSourceAgeMs) ||
        gnssSourceAgeMs < 0.0 || gnssSourceAgeMs > 1500.0) {
        _setLastError(tr("One authenticated atomic range, geolocation, and GNSS observation is required for a v4 snapshot."));
        return false;
    }
    if (rangeSequence != targetGeolocationSequence || targetGeolocationSequence != gnssSequence ||
        rangeSentAtMs != targetGeolocationSentAtMs || targetGeolocationSentAtMs != gnssSentAtMs) {
        _setLastError(tr("Range, target geolocation, and GNSS evidence must share one authenticated envelope."));
        return false;
    }

    const QFileInfo fileInfo(_storagePath);
    if (!QDir().mkpath(fileInfo.absolutePath())) {
        _setLastError(tr("Unable to create the local snapshot directory."));
        return false;
    }

    QJsonObject record;
    record.insert(QStringLiteral("schema"), QStringLiteral("multidetect.target-evidence.v4"));
    record.insert(QStringLiteral("saved_at_utc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs));
    record.insert(QStringLiteral("target_id"), targetId);
    record.insert(QStringLiteral("target_label"), boundedText(snapshot, "target_label", kMaximumTargetLabelLength));
    record.insert(QStringLiteral("source_frame_id"), boundedText(snapshot, "source_frame_id", kMaximumSourceFrameIdLength));
    record.insert(QStringLiteral("latitude_deg"), latitudeDeg);
    record.insert(QStringLiteral("longitude_deg"), longitudeDeg);
    record.insert(QStringLiteral("horizontal_sigma_m"), horizontalSigmaM);
    record.insert(QStringLiteral("range_validity"), rangeValidity);
    if (rangePresent) {
        record.insert(QStringLiteral("range_m"), rangeM);
    }
    if (bearingPresent) {
        record.insert(QStringLiteral("relative_bearing_deg"), bearingDeg);
    }
    if (sourceAgePresent) {
        record.insert(QStringLiteral("source_age_ms"), sourceAgeMs);
    }
    record.insert(QStringLiteral("atomic_observation"), atomicObservation);
    record.insert(QStringLiteral("range_status_sequence"), rangeSequence);
    record.insert(QStringLiteral("range_sent_at_ms"), rangeSentAtMs);
    record.insert(QStringLiteral("target_geolocation_sequence"), targetGeolocationSequence);
    record.insert(QStringLiteral("target_geolocation_sent_at_ms"), targetGeolocationSentAtMs);
    record.insert(QStringLiteral("gnss_status_sequence"), gnssSequence);
    record.insert(QStringLiteral("gnss_sent_at_ms"), gnssSentAtMs);
    record.insert(QStringLiteral("gnss_qualified"), gnssQualified);
    record.insert(QStringLiteral("gnss_reason"), gnssReason);
    record.insert(QStringLiteral("gnss_position_healthy"), gnssPositionHealthy);
    record.insert(QStringLiteral("gnss_fix_type"), gnssFixType);
    record.insert(QStringLiteral("gnss_satellites_visible"), gnssSatellitesVisible);
    record.insert(QStringLiteral("gnss_horizontal_accuracy_m"), gnssHorizontalAccuracyM);
    record.insert(QStringLiteral("gnss_vertical_accuracy_m"), gnssVerticalAccuracyM);
    record.insert(QStringLiteral("gnss_source_age_ms"), gnssSourceAgeMs);

    QFile output(_storagePath);
    if (!output.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text)) {
        _setLastError(tr("Unable to open the local snapshot file: %1").arg(output.errorString()));
        return false;
    }

    const QByteArray encoded = QJsonDocument(record).toJson(QJsonDocument::Compact);
    const bool written = output.write(encoded) == encoded.size() && output.write("\n") == 1 && output.flush();
    if (!written) {
        _setLastError(tr("Unable to write the local snapshot file: %1").arg(output.errorString()));
        return false;
    }

    ++_snapshotCount;
    QVariantMap recentSnapshot;
    for (auto it = record.constBegin(); it != record.constEnd(); ++it) {
        recentSnapshot.insert(it.key(), it.value().toVariant());
    }
    _appendRecentSnapshot(recentSnapshot);
    _setLastError({});
    emit snapshotCountChanged();
    return true;
}

void MultiDetectTargetEvidenceStore::_loadSnapshotCount()
{
    QFile input(_storagePath);
    if (!input.exists() || !input.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return;
    }

    while (!input.atEnd() && _snapshotCount < kMaximumSnapshots) {
        const QByteArray line = input.readLine().trimmed();
        if (line.isEmpty()) {
            continue;
        }
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
        if (parseError.error == QJsonParseError::NoError && document.isObject() &&
            validSnapshotRecord(document.object())) {
            ++_snapshotCount;
            QVariantMap recentSnapshot;
            const QJsonObject record = document.object();
            for (auto it = record.constBegin(); it != record.constEnd(); ++it) {
                recentSnapshot.insert(it.key(), it.value().toVariant());
            }
            _appendRecentSnapshot(recentSnapshot);
        }
    }
}

void MultiDetectTargetEvidenceStore::_appendRecentSnapshot(const QVariantMap& snapshot)
{
    _recentSnapshots.prepend(snapshot);
    while (_recentSnapshots.size() > kRecentSnapshotLimit) {
        _recentSnapshots.removeLast();
    }
    emit recentSnapshotsChanged();
}

void MultiDetectTargetEvidenceStore::_setLastError(const QString& error)
{
    if (_lastError == error) {
        return;
    }
    _lastError = error;
    emit lastErrorChanged();
}
