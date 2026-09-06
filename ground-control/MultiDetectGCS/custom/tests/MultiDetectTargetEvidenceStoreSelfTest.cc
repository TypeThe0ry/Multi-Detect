#include "MultiDetectTargetEvidenceStore.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QFile>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QTemporaryDir>

#include <cstdio>

namespace {

int check(bool condition, const char* message)
{
    if (condition) {
        return 0;
    }
    std::fprintf(stderr, "FAIL: %s\n", message);
    return 1;
}

QVariantMap validV2Snapshot()
{
    return {
        {QStringLiteral("target_id"), QStringLiteral("target-17")},
        {QStringLiteral("target_label"), QStringLiteral("vehicle")},
        {QStringLiteral("source_frame_id"), QStringLiteral("frame-441")},
        {QStringLiteral("latitude_deg"), 30.2740842},
        {QStringLiteral("longitude_deg"), 120.1550701},
        {QStringLiteral("horizontal_sigma_m"), 2.4},
        {QStringLiteral("range_m"), 83.5},
        {QStringLiteral("relative_bearing_deg"), -12.5},
        {QStringLiteral("range_validity"), QStringLiteral("VALID")},
        {QStringLiteral("source_age_ms"), 180.0},
        {QStringLiteral("gnss_status_sequence"), 912.0},
        {QStringLiteral("gnss_sent_at_ms"), 1774468800123.0},
        {QStringLiteral("gnss_qualified"), true},
        {QStringLiteral("gnss_reason"), QStringLiteral("gps_qualified")},
        {QStringLiteral("gnss_position_healthy"), true},
        {QStringLiteral("gnss_fix_type"), 3.0},
        {QStringLiteral("gnss_satellites_visible"), 14.0},
        {QStringLiteral("gnss_horizontal_accuracy_m"), 1.8},
        {QStringLiteral("gnss_vertical_accuracy_m"), 3.1},
        {QStringLiteral("gnss_source_age_ms"), 120.0},
    };
}

QJsonObject legacyV1Record()
{
    return {
        {QStringLiteral("schema"), QStringLiteral("multidetect.target-evidence.v1")},
        {QStringLiteral("saved_at_utc"), QStringLiteral("2026-07-24T10:00:00.000Z")},
        {QStringLiteral("target_id"), QStringLiteral("legacy-target")},
        {QStringLiteral("target_label"), QStringLiteral("legacy")},
        {QStringLiteral("source_frame_id"), QStringLiteral("legacy-frame")},
        {QStringLiteral("latitude_deg"), 30.0},
        {QStringLiteral("longitude_deg"), 120.0},
        {QStringLiteral("horizontal_sigma_m"), 4.0},
        {QStringLiteral("range_validity"), QStringLiteral("UNAVAILABLE")},
    };
}

}  // namespace

int main(int argc, char* argv[])
{
    QCoreApplication application(argc, argv);
    QTemporaryDir temporaryDirectory;
    int failures = check(temporaryDirectory.isValid(), "temporary directory must be available");
    if (!temporaryDirectory.isValid()) {
        return failures;
    }

    const QString storagePath = temporaryDirectory.filePath(QStringLiteral("target-snapshots.jsonl"));
    {
        QFile seed(storagePath);
        failures += check(seed.open(QIODevice::WriteOnly | QIODevice::Text), "legacy seed must open");
        if (seed.isOpen()) {
            seed.write(QJsonDocument(legacyV1Record()).toJson(QJsonDocument::Compact));
            seed.write("\n");
        }
    }

    MultiDetectTargetEvidenceStore store(storagePath);
    failures += check(store.snapshotCount() == 1, "legacy v1 record must remain readable");

    QVariantMap weakGnss = validV2Snapshot();
    weakGnss.insert(QStringLiteral("gnss_horizontal_accuracy_m"), 6.1);
    failures += check(!store.saveSnapshot(weakGnss), "weak GNSS evidence must be rejected");
    failures += check(store.snapshotCount() == 1, "rejected evidence must not change count");

    QVariantMap mistypedGnss = validV2Snapshot();
    mistypedGnss.insert(QStringLiteral("gnss_qualified"), QStringLiteral("true"));
    failures += check(!store.saveSnapshot(mistypedGnss), "GNSS booleans must remain typed");
    failures += check(store.snapshotCount() == 1, "mistyped evidence must not change count");

    failures += check(store.saveSnapshot(validV2Snapshot()), "qualified v2 snapshot must save");
    failures += check(store.snapshotCount() == 2, "qualified evidence must increment count");
    failures += check(store.lastError().isEmpty(), "successful save must clear error");

    QFile persisted(storagePath);
    failures += check(persisted.open(QIODevice::ReadOnly | QIODevice::Text), "evidence file must reopen");
    QByteArray lastLine;
    while (persisted.isOpen() && !persisted.atEnd()) {
        const QByteArray candidate = persisted.readLine().trimmed();
        if (!candidate.isEmpty()) {
            lastLine = candidate;
        }
    }
    const QJsonObject savedRecord = QJsonDocument::fromJson(lastLine).object();
    failures += check(savedRecord.value(QStringLiteral("schema")).toString() ==
                          QStringLiteral("multidetect.target-evidence.v2"),
                      "new snapshots must use v2 schema");
    failures += check(savedRecord.value(QStringLiteral("gnss_status_sequence")).toInt() == 912,
                      "v2 snapshot must persist GNSS sequence provenance");
    failures += check(savedRecord.value(QStringLiteral("gnss_qualified")).toBool(),
                      "v2 snapshot must persist qualified state");

    MultiDetectTargetEvidenceStore reloaded(storagePath);
    failures += check(reloaded.snapshotCount() == 2, "v1 and v2 records must reload together");
    failures += check(reloaded.recentSnapshots().size() == 2, "valid records must populate history");

    if (failures == 0) {
        std::puts("MultiDetectTargetEvidenceStoreSelfTest: PASS");
    }
    return failures == 0 ? 0 : 1;
}
