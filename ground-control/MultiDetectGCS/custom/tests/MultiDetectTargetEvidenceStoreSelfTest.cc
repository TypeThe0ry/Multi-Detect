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

QVariantMap validV6Snapshot()
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
        {QStringLiteral("atomic_observation"), true},
        {QStringLiteral("producer_epoch"), 2712847316.0},
        {QStringLiteral("range_status_sequence"), 912.0},
        {QStringLiteral("range_sent_at_ms"), 1774468800123.0},
        {QStringLiteral("target_geolocation_sequence"), 912.0},
        {QStringLiteral("target_geolocation_sent_at_ms"), 1774468800123.0},
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
        {QStringLiteral("producer_epoch_health_level"), QStringLiteral("HEALTHY")},
        {QStringLiteral("producer_epoch_health_reason"), QStringLiteral("producer_epoch_stable")},
        {QStringLiteral("producer_restart_count"), 0.0},
        {QStringLiteral("retired_epoch_reject_count"), 0.0},
        {QStringLiteral("epoch_transition_reject_count"), 0.0},
        {QStringLiteral("epoch_changed_at_ms"), 1774468800100.0},
        {QStringLiteral("epoch_last_incident_at_ms"), 0.0},
    };
}

void removeEpochHealthFields(QJsonObject* record)
{
    for (const auto& key : {QStringLiteral("producer_epoch_health_level"),
                            QStringLiteral("producer_epoch_health_reason"),
                            QStringLiteral("producer_restart_count"),
                            QStringLiteral("retired_epoch_reject_count"),
                            QStringLiteral("epoch_transition_reject_count"),
                            QStringLiteral("epoch_changed_at_ms"),
                            QStringLiteral("epoch_last_incident_at_ms")}) {
        record->remove(key);
    }
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

QJsonObject legacyV2Record()
{
    QJsonObject record = QJsonObject::fromVariantMap(validV6Snapshot());
    removeEpochHealthFields(&record);
    record.remove(QStringLiteral("atomic_observation"));
    record.remove(QStringLiteral("range_status_sequence"));
    record.remove(QStringLiteral("range_sent_at_ms"));
    record.remove(QStringLiteral("target_geolocation_sequence"));
    record.remove(QStringLiteral("target_geolocation_sent_at_ms"));
    record.insert(QStringLiteral("schema"), QStringLiteral("multidetect.target-evidence.v2"));
    record.insert(QStringLiteral("saved_at_utc"), QStringLiteral("2026-07-24T10:01:00.000Z"));
    record.insert(QStringLiteral("target_id"), QStringLiteral("legacy-v2-target"));
    return record;
}

QJsonObject legacyV3Record()
{
    QJsonObject record = QJsonObject::fromVariantMap(validV6Snapshot());
    removeEpochHealthFields(&record);
    record.remove(QStringLiteral("atomic_observation"));
    record.remove(QStringLiteral("range_status_sequence"));
    record.remove(QStringLiteral("range_sent_at_ms"));
    record.insert(QStringLiteral("schema"), QStringLiteral("multidetect.target-evidence.v3"));
    record.insert(QStringLiteral("saved_at_utc"), QStringLiteral("2026-07-24T10:02:00.000Z"));
    record.insert(QStringLiteral("target_id"), QStringLiteral("legacy-v3-target"));
    return record;
}

QJsonObject legacyV4Record()
{
    QJsonObject record = QJsonObject::fromVariantMap(validV6Snapshot());
    removeEpochHealthFields(&record);
    record.remove(QStringLiteral("producer_epoch"));
    record.insert(QStringLiteral("schema"), QStringLiteral("multidetect.target-evidence.v4"));
    record.insert(QStringLiteral("saved_at_utc"), QStringLiteral("2026-07-24T10:03:00.000Z"));
    record.insert(QStringLiteral("target_id"), QStringLiteral("legacy-v4-target"));
    return record;
}

QJsonObject legacyV5Record()
{
    QJsonObject record = QJsonObject::fromVariantMap(validV6Snapshot());
    removeEpochHealthFields(&record);
    record.insert(QStringLiteral("schema"), QStringLiteral("multidetect.target-evidence.v5"));
    record.insert(QStringLiteral("saved_at_utc"), QStringLiteral("2026-07-24T10:04:00.000Z"));
    record.insert(QStringLiteral("target_id"), QStringLiteral("legacy-v5-target"));
    return record;
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
            seed.write(QJsonDocument(legacyV2Record()).toJson(QJsonDocument::Compact));
            seed.write("\n");
            seed.write(QJsonDocument(legacyV3Record()).toJson(QJsonDocument::Compact));
            seed.write("\n");
            seed.write(QJsonDocument(legacyV4Record()).toJson(QJsonDocument::Compact));
            seed.write("\n");
            seed.write(QJsonDocument(legacyV5Record()).toJson(QJsonDocument::Compact));
            seed.write("\n");
        }
    }

    MultiDetectTargetEvidenceStore store(storagePath);
    failures += check(store.snapshotCount() == 5,
                      "legacy v1, v2, v3, v4, and v5 records must remain readable");

    QVariantMap weakGnss = validV6Snapshot();
    weakGnss.insert(QStringLiteral("gnss_horizontal_accuracy_m"), 6.1);
    failures += check(!store.saveSnapshot(weakGnss), "weak GNSS evidence must be rejected");
    failures += check(store.snapshotCount() == 5, "rejected evidence must not change count");

    QVariantMap mistypedGnss = validV6Snapshot();
    mistypedGnss.insert(QStringLiteral("gnss_qualified"), QStringLiteral("true"));
    failures += check(!store.saveSnapshot(mistypedGnss), "GNSS booleans must remain typed");
    failures += check(store.snapshotCount() == 5, "mistyped evidence must not change count");

    QVariantMap mismatchedFrame = validV6Snapshot();
    mismatchedFrame.insert(QStringLiteral("target_geolocation_sent_at_ms"), 1774468800122.0);
    failures += check(!store.saveSnapshot(mismatchedFrame),
                      "mismatched target and GNSS producer frames must be rejected");
    failures += check(store.snapshotCount() == 5, "frame mismatch must not change count");

    QVariantMap nonAtomic = validV6Snapshot();
    nonAtomic.insert(QStringLiteral("atomic_observation"), false);
    failures += check(!store.saveSnapshot(nonAtomic), "non-atomic evidence must be rejected");
    failures += check(store.snapshotCount() == 5, "non-atomic evidence must not change count");

    QVariantMap missingEpoch = validV6Snapshot();
    missingEpoch.remove(QStringLiteral("producer_epoch"));
    failures += check(!store.saveSnapshot(missingEpoch), "missing producer epoch must be rejected");
    failures += check(store.snapshotCount() == 5, "missing epoch must not change count");

    QVariantMap inconsistentHealth = validV6Snapshot();
    inconsistentHealth.insert(QStringLiteral("producer_epoch_health_level"),
                              QStringLiteral("CRITICAL"));
    failures += check(!store.saveSnapshot(inconsistentHealth),
                      "epoch health severity must match its counters and reason");
    failures += check(store.snapshotCount() == 5,
                      "inconsistent epoch health must not change count");

    failures += check(store.saveSnapshot(validV6Snapshot()),
                      "qualified epoch-health-audited atomic v6 snapshot must save");
    failures += check(store.snapshotCount() == 6, "qualified evidence must increment count");
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
                          QStringLiteral("multidetect.target-evidence.v6"),
                      "new snapshots must use v6 schema");
    failures += check(savedRecord.value(QStringLiteral("gnss_status_sequence")).toInt() == 912,
                      "v6 snapshot must persist GNSS sequence provenance");
    failures += check(savedRecord.value(QStringLiteral("gnss_qualified")).toBool(),
                      "v6 snapshot must persist qualified state");
    failures += check(savedRecord.value(QStringLiteral("atomic_observation")).toBool() &&
                          savedRecord.value(QStringLiteral("range_status_sequence")).toInt() == 912 &&
                          savedRecord.value(QStringLiteral("target_geolocation_sequence")).toInt() == 912,
                      "v6 snapshot must persist one atomic observation identity");
    failures += check(savedRecord.value(QStringLiteral("producer_epoch")).toDouble() == 2712847316.0,
                      "v6 snapshot must persist the producer epoch");
    failures += check(
        savedRecord.value(QStringLiteral("producer_epoch_health_level")).toString() ==
                QStringLiteral("HEALTHY") &&
            savedRecord.value(QStringLiteral("producer_epoch_health_reason")).toString() ==
                QStringLiteral("producer_epoch_stable") &&
            savedRecord.value(QStringLiteral("producer_restart_count")).toDouble() == 0.0 &&
            savedRecord.value(QStringLiteral("epoch_changed_at_ms")).toDouble() ==
                1774468800100.0,
        "v6 snapshot must persist canonical epoch health audit fields");
    failures += check(savedRecord.value(QStringLiteral("target_geolocation_sent_at_ms")).toDouble() ==
                          savedRecord.value(QStringLiteral("gnss_sent_at_ms")).toDouble(),
                      "v6 snapshot must persist the exact producer-frame binding");

    MultiDetectTargetEvidenceStore reloaded(storagePath);
    failures += check(reloaded.snapshotCount() == 6, "v1 through v6 records must reload together");
    failures += check(reloaded.recentSnapshots().size() == 6, "valid records must populate history");

    if (failures == 0) {
        std::puts("MultiDetectTargetEvidenceStoreSelfTest: PASS");
    }
    return failures == 0 ? 0 : 1;
}
