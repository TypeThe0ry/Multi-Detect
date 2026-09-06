#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QDateTime>
#include <QtCore/QString>
#include <QtCore/QVariantMap>

class MultiDetectTargetObservationDiagnostics final
{
public:
    static constexpr qulonglong maximumAgeMs() { return 1500; }

    static QVariantMap document(const QVariantMap& rangeStatus,
                                const QVariantMap& targetGeolocationStatus,
                                const QVariantMap& gnssStatus,
                                const QVariantMap& producerEpochSummary,
                                const QDateTime& generatedAtUtc,
                                qulonglong localNowMs,
                                qulonglong lastRangeReceivedAtMs,
                                qulonglong lastTargetGeolocationReceivedAtMs,
                                qulonglong lastGnssReceivedAtMs);

    static QByteArray json(const QVariantMap& rangeStatus,
                           const QVariantMap& targetGeolocationStatus,
                           const QVariantMap& gnssStatus,
                           const QVariantMap& producerEpochSummary,
                           const QDateTime& generatedAtUtc,
                           qulonglong localNowMs,
                           qulonglong lastRangeReceivedAtMs,
                           qulonglong lastTargetGeolocationReceivedAtMs,
                           qulonglong lastGnssReceivedAtMs);

    static bool save(const QString& directoryPath,
                     const QVariantMap& rangeStatus,
                     const QVariantMap& targetGeolocationStatus,
                     const QVariantMap& gnssStatus,
                     const QVariantMap& producerEpochSummary,
                     const QDateTime& generatedAtUtc,
                     qulonglong localNowMs,
                     qulonglong lastRangeReceivedAtMs,
                     qulonglong lastTargetGeolocationReceivedAtMs,
                     qulonglong lastGnssReceivedAtMs,
                     QString* savedPath,
                     QString* error);
};
