#pragma once

#include <QtCore/QDateTime>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QVariantMap>

class MultiDetectTargetObservationRecorder final
{
public:
    static constexpr qulonglong kDefaultIntervalMs = 1000;
    static constexpr int kDefaultMaximumSamples = 3600;

    bool start(const QString& rootDirectory, const QDateTime& startedAtUtc,
               qulonglong intervalMs = kDefaultIntervalMs,
               int maximumSamples = kDefaultMaximumSamples, QString* error = nullptr);
    bool capture(const QVariantMap& rangeStatus, const QVariantMap& targetGeolocationStatus,
                 const QVariantMap& gnssStatus, const QVariantMap& producerEpochSummary,
                 const QDateTime& generatedAtUtc, qulonglong localNowMs,
                 qulonglong lastRangeReceivedAtMs, qulonglong lastTargetGeolocationReceivedAtMs,
                 qulonglong lastGnssReceivedAtMs, QString* error = nullptr);
    bool stop(const QDateTime& endedAtUtc, const QString& reason,
              QString* manifestPath = nullptr, QString* error = nullptr);

    bool active() const { return _active; }
    int sampleCount() const { return _snapshotNames.size(); }
    QString sessionDirectory() const { return _sessionDirectory; }
    qulonglong intervalMs() const { return _intervalMs; }
    int maximumSamples() const { return _maximumSamples; }

private:
    bool _active = false;
    QString _sessionId;
    QString _sessionDirectory;
    QDateTime _startedAtUtc;
    qulonglong _intervalMs = kDefaultIntervalMs;
    qulonglong _nextCaptureAtMs = 0;
    int _maximumSamples = kDefaultMaximumSamples;
    QStringList _snapshotNames;
};
