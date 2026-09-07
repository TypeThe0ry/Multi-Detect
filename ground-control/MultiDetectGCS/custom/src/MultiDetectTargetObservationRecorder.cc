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
#include <algorithm>
#include <limits>
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

QVariantList qualitySegments(const QVariantList& samples, const QString& kind)
{
    const auto affected = [&kind](const QVariantMap& sample) {
        if (kind == QStringLiteral("non_locatable")) {
            return !sample.value(QStringLiteral("locatable")).toBool();
        }
        if (kind == QStringLiteral("range_invalid")) {
            return sample.value(QStringLiteral("range_validity")).toString() != QStringLiteral("VALID");
        }
        return !sample.value(QStringLiteral("gnss_qualified")).toBool();
    };

    QVariantList segments;
    int start = -1;
    for (int index = 0; index <= samples.size(); ++index) {
        const bool currentAffected = index < samples.size() && affected(samples.at(index).toMap());
        if (currentAffected && start < 0) {
            start = index;
        }
        if (currentAffected || start < 0) {
            continue;
        }
        const QVariantMap first = samples.at(start).toMap();
        const QVariantMap last = samples.at(index - 1).toMap();
        QVariantMap segment{{QStringLiteral("kind"), kind},
                            {QStringLiteral("start_index"), start},
                            {QStringLiteral("end_index"), index - 1},
                            {QStringLiteral("sample_count"), index - start},
                            {QStringLiteral("started_at_utc"),
                             first.value(QStringLiteral("generated_at_utc"))},
                            {QStringLiteral("ended_at_utc"),
                             last.value(QStringLiteral("generated_at_utc"))},
                            {QStringLiteral("start_local_now_ms"),
                             first.value(QStringLiteral("local_now_ms"))},
                            {QStringLiteral("end_local_now_ms"),
                             last.value(QStringLiteral("local_now_ms"))},
                            {QStringLiteral("duration_ms"),
                             last.value(QStringLiteral("local_now_ms")).toULongLong() -
                                 first.value(QStringLiteral("local_now_ms")).toULongLong()}};
        segments.append(segment);
        start = -1;
    }
    return segments;
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
    _qualitySamples.clear();
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
    const QVariantMap diagnostics = MultiDetectTargetObservationDiagnostics::document(
        rangeStatus, targetGeolocationStatus, gnssStatus, producerEpochSummary,
        generatedAtUtc, localNowMs, lastRangeReceivedAtMs,
        lastTargetGeolocationReceivedAtMs, lastGnssReceivedAtMs);
    QString savedPath;
    if (!MultiDetectTargetObservationDiagnostics::save(
            _sessionDirectory, rangeStatus, targetGeolocationStatus, gnssStatus,
            producerEpochSummary, generatedAtUtc, localNowMs, lastRangeReceivedAtMs,
            lastTargetGeolocationReceivedAtMs, lastGnssReceivedAtMs, &savedPath, error)) {
        return false;
    }
    _snapshotNames.append(QFileInfo(savedPath).fileName());
    const QVariantMap readiness = diagnostics.value(QStringLiteral("readiness")).toMap();
    const QVariantMap recordedRangeStatus = diagnostics.value(QStringLiteral("range_status")).toMap();
    const QVariantMap recordedGnssStatus = diagnostics.value(QStringLiteral("gnss_status")).toMap();
    QVariantMap qualitySample{{QStringLiteral("index"), _qualitySamples.size()},
                              {QStringLiteral("generated_at_utc"),
                               diagnostics.value(QStringLiteral("generated_at_utc"))},
                              {QStringLiteral("local_now_ms"), QVariant::fromValue(localNowMs)},
                              {QStringLiteral("locatable"),
                               readiness.value(QStringLiteral("locatable")).toBool()},
                              {QStringLiteral("range_validity"),
                               recordedRangeStatus.value(QStringLiteral("validity"))},
                              {QStringLiteral("gnss_qualified"),
                               recordedGnssStatus.value(QStringLiteral("qualified")).toBool()},
                              {QStringLiteral("producer_epoch"), QVariant()}};
    if (readiness.value(QStringLiteral("atomic_identity_matched")).toBool()) {
        qualitySample.insert(QStringLiteral("producer_epoch"),
                             recordedRangeStatus.value(QStringLiteral("producerEpoch")));
    }
    _qualitySamples.append(qualitySample);
    _nextCaptureAtMs = localNowMs + _intervalMs;
    return true;
}

QVariantMap MultiDetectTargetObservationRecorder::qualitySummary() const
{
    const int sampleCount = _qualitySamples.size();
    int locatableCount = 0;
    int currentNonLocatableRun = 0;
    int longestNonLocatableRun = 0;
    qulonglong minimumGapMs = std::numeric_limits<qulonglong>::max();
    qulonglong maximumGapMs = 0;
    qulonglong totalGapMs = 0;
    int cadenceViolationCount = 0;
    QVariantList gapEvents;
    QVariantList epochTransitions;
    QVariant previousEpoch;
    const qulonglong cadenceToleranceMs = std::max<qulonglong>(250, _intervalMs / 2);
    const qulonglong cadenceLimitMs = _intervalMs + cadenceToleranceMs;

    for (int index = 0; index < sampleCount; ++index) {
        const QVariantMap sample = _qualitySamples.at(index).toMap();
        if (sample.value(QStringLiteral("locatable")).toBool()) {
            ++locatableCount;
            currentNonLocatableRun = 0;
        } else {
            ++currentNonLocatableRun;
            longestNonLocatableRun = std::max(longestNonLocatableRun, currentNonLocatableRun);
        }
        if (index > 0) {
            const QVariantMap preceding = _qualitySamples.at(index - 1).toMap();
            const qulonglong gapMs = sample.value(QStringLiteral("local_now_ms")).toULongLong() -
                                      preceding.value(QStringLiteral("local_now_ms")).toULongLong();
            minimumGapMs = std::min(minimumGapMs, gapMs);
            maximumGapMs = std::max(maximumGapMs, gapMs);
            totalGapMs += gapMs;
            if (gapMs > cadenceLimitMs) {
                ++cadenceViolationCount;
                gapEvents.append(QVariantMap{{QStringLiteral("previous_index"), index - 1},
                                             {QStringLiteral("current_index"), index},
                                             {QStringLiteral("gap_ms"), QVariant::fromValue(gapMs)},
                                             {QStringLiteral("expected_interval_ms"),
                                              QVariant::fromValue(_intervalMs)}});
            }
        }
        const QVariant epoch = sample.value(QStringLiteral("producer_epoch"));
        if (!epoch.isValid() || epoch.isNull()) {
            continue;
        }
        if (previousEpoch.isValid() && previousEpoch != epoch) {
            epochTransitions.append(
                QVariantMap{{QStringLiteral("sample_index"), index},
                            {QStringLiteral("local_now_ms"),
                             sample.value(QStringLiteral("local_now_ms"))},
                            {QStringLiteral("generated_at_utc"),
                             sample.value(QStringLiteral("generated_at_utc"))},
                            {QStringLiteral("from_epoch"), previousEpoch},
                            {QStringLiteral("to_epoch"), epoch}});
        }
        previousEpoch = epoch;
    }

    QVariantMap sampling{{QStringLiteral("interval_count"), std::max(0, sampleCount - 1)},
                         {QStringLiteral("minimum_gap_ms"), QVariant()},
                         {QStringLiteral("maximum_gap_ms"), QVariant()},
                         {QStringLiteral("mean_gap_ms"), QVariant()},
                         {QStringLiteral("expected_interval_ms"), QVariant::fromValue(_intervalMs)},
                         {QStringLiteral("cadence_tolerance_ms"),
                          QVariant::fromValue(cadenceToleranceMs)},
                         {QStringLiteral("cadence_violation_count"), cadenceViolationCount},
                         {QStringLiteral("gap_events"), gapEvents}};
    if (sampleCount > 1) {
        sampling.insert(QStringLiteral("minimum_gap_ms"), QVariant::fromValue(minimumGapMs));
        sampling.insert(QStringLiteral("maximum_gap_ms"), QVariant::fromValue(maximumGapMs));
        sampling.insert(QStringLiteral("mean_gap_ms"),
                        static_cast<double>(totalGapMs) / static_cast<double>(sampleCount - 1));
    }
    qulonglong observationDurationMs = 0;
    if (sampleCount > 1) {
        observationDurationMs =
            _qualitySamples.constLast().toMap().value(QStringLiteral("local_now_ms")).toULongLong() -
            _qualitySamples.constFirst().toMap().value(QStringLiteral("local_now_ms")).toULongLong();
    }
    QVariantMap segments{{QStringLiteral("non_locatable"),
                          qualitySegments(_qualitySamples, QStringLiteral("non_locatable"))},
                         {QStringLiteral("range_invalid"),
                          qualitySegments(_qualitySamples, QStringLiteral("range_invalid"))},
                         {QStringLiteral("gnss_unqualified"),
                          qualitySegments(_qualitySamples, QStringLiteral("gnss_unqualified"))}};
    return {{QStringLiteral("schema"),
             QStringLiteral("multidetect.target-observation-recording-live-quality.v1")},
            {QStringLiteral("schema_version"), 1},
            {QStringLiteral("active"), _active},
            {QStringLiteral("sample_count"), sampleCount},
            {QStringLiteral("locatable_count"), locatableCount},
            {QStringLiteral("non_locatable_count"), sampleCount - locatableCount},
            {QStringLiteral("locatable_rate"),
             sampleCount > 0 ? static_cast<double>(locatableCount) / sampleCount : 0.0},
            {QStringLiteral("longest_non_locatable_run"), longestNonLocatableRun},
            {QStringLiteral("observation_duration_ms"), QVariant::fromValue(observationDurationMs)},
            {QStringLiteral("sampling"), sampling},
            {QStringLiteral("quality_segments"), segments},
            {QStringLiteral("epoch_transition_count"), epochTransitions.size()},
            {QStringLiteral("epoch_transitions"), epochTransitions},
            {QStringLiteral("advisory_only"), true},
            {QStringLiteral("flight_control_enabled"), false},
            {QStringLiteral("physical_release_enabled"), false}};
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
