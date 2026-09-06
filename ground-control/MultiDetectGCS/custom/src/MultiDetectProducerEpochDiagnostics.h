#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QDateTime>
#include <QtCore/QString>
#include <QtCore/QVariantList>
#include <QtCore/QVariantMap>

class MultiDetectProducerEpochDiagnostics final
{
public:
    static constexpr qsizetype maximumEvents() { return 64; }

    void record(const QString& kind, quint32 incomingEpoch, quint32 previousEpoch, quint32 activeEpoch,
                const QString& healthLevel, const QString& healthReason, qulonglong producerRestartCount,
                qulonglong retiredEpochRejectCount, qulonglong epochTransitionRejectCount,
                qulonglong occurredAtMs);

    const QVariantList& events() const { return _events; }
    qulonglong eventCount() const { return _eventCount; }
    qulonglong droppedEventCount() const { return _droppedEventCount; }

    QByteArray json(const QVariantMap& currentSummary, const QDateTime& generatedAtUtc) const;
    bool save(const QString& directoryPath, const QVariantMap& currentSummary,
              const QDateTime& generatedAtUtc, QString* savedPath, QString* error) const;

private:
    QVariantMap _document(const QVariantMap& currentSummary, const QDateTime& generatedAtUtc) const;

    QVariantList _events;
    qulonglong _eventCount = 0;
    qulonglong _droppedEventCount = 0;
};
