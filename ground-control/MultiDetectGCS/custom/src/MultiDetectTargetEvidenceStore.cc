#include "MultiDetectTargetEvidenceStore.h"

#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonParseError>
#include <QtCore/QStandardPaths>
#include <cmath>

namespace {

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

void addOptionalFiniteNumber(
    QJsonObject* record,
    const QVariantMap& snapshot,
    const char* key,
    double minimum,
    double maximum)
{
    double value = 0.0;
    if (finiteNumber(snapshot.value(QString::fromLatin1(key)), &value) && value >= minimum && value <= maximum) {
        record->insert(QString::fromLatin1(key), value);
    }
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

    const QString targetId = boundedText(snapshot, "target_id", 128);
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
        || latitudeDeg < -90.0 || latitudeDeg > 90.0
        || longitudeDeg < -180.0 || longitudeDeg > 180.0
        || horizontalSigmaM < 0.0 || horizontalSigmaM > 100000.0) {
        _setLastError(tr("Qualified latitude, longitude, and horizontal uncertainty are required."));
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
    record.insert(QStringLiteral("target_label"), boundedText(snapshot, "target_label", 128));
    record.insert(QStringLiteral("source_frame_id"), boundedText(snapshot, "source_frame_id", 256));
    record.insert(QStringLiteral("latitude_deg"), latitudeDeg);
    record.insert(QStringLiteral("longitude_deg"), longitudeDeg);
    record.insert(QStringLiteral("horizontal_sigma_m"), horizontalSigmaM);
    record.insert(QStringLiteral("range_validity"), boundedText(snapshot, "range_validity", 48));
    addOptionalFiniteNumber(&record, snapshot, "range_m", 0.0, 100000.0);
    addOptionalFiniteNumber(&record, snapshot, "relative_bearing_deg", -360.0, 360.0);
    addOptionalFiniteNumber(&record, snapshot, "source_age_ms", 0.0, 86400000.0);

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
        if (parseError.error == QJsonParseError::NoError && document.isObject()
            && document.object().value(QStringLiteral("schema")).toString()
                == QStringLiteral("multidetect.target-evidence.v1")) {
            ++_snapshotCount;
        }
    }
}

void MultiDetectTargetEvidenceStore::_setLastError(const QString& error)
{
    if (_lastError == error) {
        return;
    }
    _lastError = error;
    emit lastErrorChanged();
}
