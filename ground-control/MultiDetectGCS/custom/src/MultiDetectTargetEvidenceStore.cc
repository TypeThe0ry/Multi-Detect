#include "MultiDetectTargetEvidenceStore.h"

#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonParseError>
#include <QtCore/QJsonValue>
#include <QtCore/QStandardPaths>
#include <cmath>

namespace {

constexpr double kMaximumSnapshotRangeM = 100000.0;
constexpr double kMaximumSnapshotSourceAgeMs = 86400000.0;
constexpr int kMaximumTargetIdLength = 128;
constexpr int kMaximumTargetLabelLength = 128;
constexpr int kMaximumSourceFrameIdLength = 256;
constexpr int kMaximumSavedAtLength = 64;

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
    if (record.value(QStringLiteral("schema")).toString() !=
        QStringLiteral("multidetect.target-evidence.v1")) {
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

    const QFileInfo fileInfo(_storagePath);
    if (!QDir().mkpath(fileInfo.absolutePath())) {
        _setLastError(tr("Unable to create the local snapshot directory."));
        return false;
    }

    QJsonObject record;
    record.insert(QStringLiteral("schema"), QStringLiteral("multidetect.target-evidence.v1"));
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
