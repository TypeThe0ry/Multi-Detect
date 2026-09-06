#include "MultiDetectProducerEpochDiagnostics.h"

#include <QtCore/QDir>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QSaveFile>

void MultiDetectProducerEpochDiagnostics::record(
    const QString& kind, quint32 incomingEpoch, quint32 previousEpoch, quint32 activeEpoch,
    const QString& healthLevel, const QString& healthReason, qulonglong producerRestartCount,
    qulonglong retiredEpochRejectCount, qulonglong epochTransitionRejectCount, qulonglong occurredAtMs)
{
    ++_eventCount;
    if (_events.size() >= maximumEvents()) {
        _events.removeFirst();
        ++_droppedEventCount;
    }

    QVariantMap event;
    event.insert(QStringLiteral("event_index"), QVariant::fromValue(_eventCount));
    event.insert(QStringLiteral("occurred_at_ms"), QVariant::fromValue(occurredAtMs));
    event.insert(QStringLiteral("kind"), kind);
    event.insert(QStringLiteral("health_level"), healthLevel);
    event.insert(QStringLiteral("health_reason"), healthReason);
    event.insert(QStringLiteral("incoming_epoch"), incomingEpoch);
    event.insert(QStringLiteral("previous_epoch"), previousEpoch);
    event.insert(QStringLiteral("active_epoch"), activeEpoch);
    event.insert(QStringLiteral("producer_restart_count"), QVariant::fromValue(producerRestartCount));
    event.insert(QStringLiteral("retired_epoch_reject_count"), QVariant::fromValue(retiredEpochRejectCount));
    event.insert(QStringLiteral("epoch_transition_reject_count"),
                 QVariant::fromValue(epochTransitionRejectCount));
    _events.append(event);
}

QVariantMap MultiDetectProducerEpochDiagnostics::_document(
    const QVariantMap& currentSummary, const QDateTime& generatedAtUtc) const
{
    QVariantMap summary = currentSummary;
    summary.insert(QStringLiteral("event_count"), QVariant::fromValue(_eventCount));
    summary.insert(QStringLiteral("retained_event_count"), _events.size());
    summary.insert(QStringLiteral("dropped_event_count"), QVariant::fromValue(_droppedEventCount));

    QVariantMap document;
    document.insert(QStringLiteral("schema"), QStringLiteral("multidetect.producer-epoch-diagnostics.v1"));
    document.insert(QStringLiteral("schema_version"), 1);
    document.insert(QStringLiteral("generated_at_utc"),
                    generatedAtUtc.toUTC().toString(Qt::ISODateWithMs));
    document.insert(QStringLiteral("event_capacity"), maximumEvents());
    document.insert(QStringLiteral("summary"), summary);
    document.insert(QStringLiteral("events"), _events);
    document.insert(QStringLiteral("advisory_only"), true);
    document.insert(QStringLiteral("flight_control_enabled"), false);
    document.insert(QStringLiteral("physical_release_enabled"), false);
    return document;
}

QByteArray MultiDetectProducerEpochDiagnostics::json(
    const QVariantMap& currentSummary, const QDateTime& generatedAtUtc) const
{
    return QJsonDocument::fromVariant(_document(currentSummary, generatedAtUtc))
        .toJson(QJsonDocument::Indented);
}

bool MultiDetectProducerEpochDiagnostics::save(
    const QString& directoryPath, const QVariantMap& currentSummary, const QDateTime& generatedAtUtc,
    QString* savedPath, QString* error) const
{
    if (savedPath) {
        savedPath->clear();
    }
    if (error) {
        error->clear();
    }
    if (directoryPath.trimmed().isEmpty()) {
        if (error) {
            *error = QStringLiteral("Producer epoch diagnostics directory is empty.");
        }
        return false;
    }

    QDir directory(directoryPath);
    if (!directory.exists() && !QDir().mkpath(directory.absolutePath())) {
        if (error) {
            *error = QStringLiteral("Cannot create producer epoch diagnostics directory: %1")
                         .arg(QDir::toNativeSeparators(directory.absolutePath()));
        }
        return false;
    }

    const QString timestamp = generatedAtUtc.toUTC().toString(QStringLiteral("yyyyMMddTHHmmsszzz'Z'"));
    const QString stem = QStringLiteral("producer-epoch-diagnostics-%1-%2")
                             .arg(timestamp)
                             .arg(_eventCount);
    QString outputPath = directory.filePath(stem + QStringLiteral(".json"));
    for (int suffix = 1; QFileInfo::exists(outputPath); ++suffix) {
        if (suffix > 9999) {
            if (error) {
                *error = QStringLiteral("Cannot allocate a unique producer epoch diagnostics filename.");
            }
            return false;
        }
        outputPath = directory.filePath(QStringLiteral("%1-%2.json").arg(stem).arg(suffix));
    }

    const QByteArray payload = json(currentSummary, generatedAtUtc);
    QSaveFile file(outputPath);
    if (!file.open(QIODevice::WriteOnly)) {
        if (error) {
            *error = QStringLiteral("Cannot open producer epoch diagnostics file: %1").arg(file.errorString());
        }
        return false;
    }
    if (file.write(payload) != payload.size()) {
        if (error) {
            *error = QStringLiteral("Cannot write producer epoch diagnostics file: %1").arg(file.errorString());
        }
        file.cancelWriting();
        return false;
    }
    if (!file.commit()) {
        if (error) {
            *error = QStringLiteral("Cannot commit producer epoch diagnostics file: %1").arg(file.errorString());
        }
        return false;
    }

    if (savedPath) {
        *savedPath = QDir::toNativeSeparators(QFileInfo(outputPath).absoluteFilePath());
    }
    return true;
}
