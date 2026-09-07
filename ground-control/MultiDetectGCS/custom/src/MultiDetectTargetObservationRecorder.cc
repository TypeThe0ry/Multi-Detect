#include "MultiDetectTargetObservationRecorder.h"

#include "MultiDetectTargetObservationDiagnostics.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QSaveFile>
#include <QtCore/QUuid>
#include <utility>

namespace {

void setError(QString* error, const QString& value)
{
    if (error) {
        *error = value;
    }
}

QString fileSha256(const QString& path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return {};
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&file)) {
        return {};
    }
    return QString::fromLatin1(hash.result().toHex());
}

} // namespace

bool MultiDetectTargetObservationRecorder::start(const QString& rootDirectory,
                                                 const QDateTime& startedAtUtc,
                                                 qulonglong intervalMs, int maximumSamples,
                                                 QString* error)
{
    setError(error, {});
    if (_active) {
        setError(error, QStringLiteral("Target observation recording is already active."));
        return false;
    }
    if (rootDirectory.trimmed().isEmpty() || !startedAtUtc.isValid() || intervalMs < 250 ||
        intervalMs > 60000 || maximumSamples < 1 || maximumSamples > kDefaultMaximumSamples) {
        setError(error, QStringLiteral("Target observation recording configuration is invalid."));
        return false;
    }
    QDir root(rootDirectory);
    if (!root.exists() && !QDir().mkpath(root.absolutePath())) {
        setError(error, QStringLiteral("Cannot create target observation recording root: %1")
                            .arg(QDir::toNativeSeparators(root.absolutePath())));
        return false;
    }
    const QString timestamp = startedAtUtc.toUTC().toString(QStringLiteral("yyyyMMddTHHmmsszzz'Z'"));
    const QString sessionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    const QString directory = root.filePath(
        QStringLiteral("target-observation-session-%1-%2").arg(timestamp, sessionId));
    if (!QDir().mkdir(directory)) {
        setError(error, QStringLiteral("Cannot create target observation recording session: %1")
                            .arg(QDir::toNativeSeparators(directory)));
        return false;
    }
    _active = true;
    _sessionId = sessionId;
    _sessionDirectory = QFileInfo(directory).absoluteFilePath();
    _startedAtUtc = startedAtUtc.toUTC();
    _intervalMs = intervalMs;
    _nextCaptureAtMs = 0;
    _maximumSamples = maximumSamples;
    _snapshotNames.clear();
    return true;
}

bool MultiDetectTargetObservationRecorder::capture(
    const QVariantMap& rangeStatus, const QVariantMap& targetGeolocationStatus,
    const QVariantMap& gnssStatus, const QVariantMap& producerEpochSummary,
    const QDateTime& generatedAtUtc, qulonglong localNowMs,
    qulonglong lastRangeReceivedAtMs, qulonglong lastTargetGeolocationReceivedAtMs,
    qulonglong lastGnssReceivedAtMs, QString* error)
{
    setError(error, {});
    if (!_active) {
        setError(error, QStringLiteral("Target observation recording is not active."));
        return false;
    }
    if (_nextCaptureAtMs != 0 && localNowMs < _nextCaptureAtMs) {
        return true;
    }
    if (_snapshotNames.size() >= _maximumSamples) {
        setError(error, QStringLiteral("Target observation recording reached its sample capacity."));
        return false;
    }
    QString savedPath;
    if (!MultiDetectTargetObservationDiagnostics::save(
            _sessionDirectory, rangeStatus, targetGeolocationStatus, gnssStatus,
            producerEpochSummary, generatedAtUtc, localNowMs, lastRangeReceivedAtMs,
            lastTargetGeolocationReceivedAtMs, lastGnssReceivedAtMs, &savedPath, error)) {
        return false;
    }
    _snapshotNames.append(QFileInfo(savedPath).fileName());
    _nextCaptureAtMs = localNowMs + _intervalMs;
    return true;
}

bool MultiDetectTargetObservationRecorder::stop(const QDateTime& endedAtUtc,
                                                const QString& reason,
                                                QString* manifestPath, QString* error)
{
    if (manifestPath) {
        manifestPath->clear();
    }
    setError(error, {});
    if (!_active || !endedAtUtc.isValid() || reason.trimmed().isEmpty()) {
        setError(error, QStringLiteral("Target observation recording stop request is invalid."));
        return false;
    }
    QJsonArray snapshots;
    for (const QString& name : std::as_const(_snapshotNames)) {
        const QString path = QDir(_sessionDirectory).filePath(name);
        const QFileInfo info(path);
        const QString sha256 = fileSha256(path);
        if (!info.isFile() || sha256.size() != 64) {
            setError(error, QStringLiteral("Cannot hash recorded target observation snapshot: %1")
                                .arg(QDir::toNativeSeparators(path)));
            return false;
        }
        snapshots.append(QJsonObject{{QStringLiteral("relative_path"), name},
                                     {QStringLiteral("bytes"), info.size()},
                                     {QStringLiteral("sha256"), sha256}});
    }
    QJsonObject document{{QStringLiteral("schema"),
                          QStringLiteral("multidetect.target-observation-recording-manifest.v1")},
                         {QStringLiteral("schema_version"), 1},
                         {QStringLiteral("session_id"), _sessionId},
                         {QStringLiteral("started_at_utc"),
                          _startedAtUtc.toString(Qt::ISODateWithMs)},
                         {QStringLiteral("ended_at_utc"),
                          endedAtUtc.toUTC().toString(Qt::ISODateWithMs)},
                         {QStringLiteral("interval_ms"), static_cast<qint64>(_intervalMs)},
                         {QStringLiteral("maximum_samples"), _maximumSamples},
                         {QStringLiteral("sample_count"), _snapshotNames.size()},
                         {QStringLiteral("stop_reason"), reason.trimmed()},
                         {QStringLiteral("snapshots"), snapshots},
                         {QStringLiteral("advisory_only"), true},
                         {QStringLiteral("flight_control_enabled"), false},
                         {QStringLiteral("physical_release_enabled"), false}};
    const QString path = QDir(_sessionDirectory).filePath(
        QStringLiteral("target-observation-recording-manifest.json"));
    QSaveFile file(path);
    const QByteArray payload = QJsonDocument(document).toJson(QJsonDocument::Indented);
    if (!file.open(QIODevice::WriteOnly) || file.write(payload) != payload.size() || !file.commit()) {
        file.cancelWriting();
        setError(error, QStringLiteral("Cannot commit target observation recording manifest: %1")
                            .arg(file.errorString()));
        return false;
    }
    if (manifestPath) {
        *manifestPath = QDir::toNativeSeparators(QFileInfo(path).absoluteFilePath());
    }
    _active = false;
    return true;
}
