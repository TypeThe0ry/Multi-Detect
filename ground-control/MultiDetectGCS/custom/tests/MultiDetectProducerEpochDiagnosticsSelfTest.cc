#include <QtCore/QFile>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QTemporaryDir>

#include <cstdio>

#include "MultiDetectProducerEpochDiagnostics.h"

namespace {

int check(bool condition, const char* message)
{
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

}  // namespace

int main()
{
    int failures = 0;
    MultiDetectProducerEpochDiagnostics diagnostics;
    for (quint32 index = 1; index <= 65; ++index) {
        diagnostics.record(index == 1 ? QStringLiteral("epoch_initialized")
                                      : QStringLiteral("producer_restart"),
                           index, index - 1, index, index == 1 ? QStringLiteral("HEALTHY")
                                                               : QStringLiteral("NOTICE"),
                           index == 1 ? QStringLiteral("producer_epoch_stable")
                                      : QStringLiteral("producer_restart_observed"),
                           index - 1, 0, 0, 1000 + index);
    }

    failures += check(diagnostics.eventCount() == 65, "total event count must include evicted entries");
    failures += check(diagnostics.events().size() == 64, "event ring must retain exactly 64 entries");
    failures += check(diagnostics.droppedEventCount() == 1, "event ring must report one dropped entry");
    failures += check(diagnostics.events().first().toMap().value(QStringLiteral("event_index")).toULongLong() == 2,
                      "oldest retained event must have index two");

    QVariantMap summary;
    summary.insert(QStringLiteral("active_epoch"), 65);
    summary.insert(QStringLiteral("health_level"), QStringLiteral("NOTICE"));
    const QDateTime generatedAt = QDateTime::fromString(QStringLiteral("2026-09-07T01:02:03.004Z"), Qt::ISODateWithMs);
    const QByteArray payload = diagnostics.json(summary, generatedAt);
    const QJsonObject document = QJsonDocument::fromJson(payload).object();
    failures += check(document.value(QStringLiteral("schema")).toString()
                          == QStringLiteral("multidetect.producer-epoch-diagnostics.v1"),
                      "schema must identify producer epoch diagnostics v1");
    failures += check(document.value(QStringLiteral("events")).toArray().size() == 64,
                      "serialized diagnostics must contain retained events");
    failures += check(document.value(QStringLiteral("advisory_only")).toBool(),
                      "serialized diagnostics must be advisory-only");
    failures += check(!document.value(QStringLiteral("flight_control_enabled")).toBool(),
                      "serialized diagnostics must not enable flight control");
    failures += check(!document.value(QStringLiteral("physical_release_enabled")).toBool(),
                      "serialized diagnostics must not enable physical release");
    const QJsonObject serializedSummary = document.value(QStringLiteral("summary")).toObject();
    failures += check(serializedSummary.value(QStringLiteral("event_count")).toInteger() == 65,
                      "serialized summary must preserve total event count");
    failures += check(serializedSummary.value(QStringLiteral("dropped_event_count")).toInteger() == 1,
                      "serialized summary must preserve dropped event count");

    QTemporaryDir temporaryDirectory;
    failures += check(temporaryDirectory.isValid(), "temporary diagnostics directory must be valid");
    QString firstPath;
    QString secondPath;
    QString error;
    failures += check(diagnostics.save(temporaryDirectory.path(), summary, generatedAt, &firstPath, &error),
                      "first atomic diagnostics save must pass");
    failures += check(error.isEmpty(), "successful diagnostics save must clear the error");
    failures += check(QFile::exists(firstPath), "first diagnostics file must exist");
    failures += check(diagnostics.save(temporaryDirectory.path(), summary, generatedAt, &secondPath, &error),
                      "second diagnostics save must allocate a unique path");
    failures += check(firstPath != secondPath, "diagnostics save must never overwrite an existing filename");
    QFile savedFile(firstPath);
    failures += check(savedFile.open(QIODevice::ReadOnly), "saved diagnostics file must reopen");
    failures += check(savedFile.readAll() == payload, "saved diagnostics bytes must match canonical JSON");

    if (failures == 0) {
        std::puts("MultiDetectProducerEpochDiagnosticsSelfTest: PASS");
    }
    return failures == 0 ? 0 : 1;
}
