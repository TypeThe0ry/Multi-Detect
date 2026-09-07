#include "MultiDetectTargetEventReplayStore.h"

#include <QtCore/QCoreApplication>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonValue>
#include <QtCore/QTemporaryDir>

#include <algorithm>
#include <cmath>
#include <cstdio>

namespace {

QByteArray canonicalJson(const QJsonValue& value)
{
    if (value.isNull()) {
        return QByteArrayLiteral("null");
    }
    if (value.isBool()) {
        return value.toBool() ? QByteArrayLiteral("true") : QByteArrayLiteral("false");
    }
    if (value.isString()) {
        QJsonArray wrapper;
        wrapper.append(value);
        const QByteArray encoded = QJsonDocument(wrapper).toJson(QJsonDocument::Compact);
        return encoded.mid(1, encoded.size() - 2);
    }
    if (value.isDouble()) {
        const double number = value.toDouble();
        if (std::isfinite(number) && std::floor(number) == number &&
            std::abs(number) <= 9007199254740991.0) {
            return QByteArray::number(static_cast<qint64>(number));
        }
        return QString::number(number, 'g', 17).toUtf8();
    }
    if (value.isArray()) {
        QByteArray output("[");
        const QJsonArray array = value.toArray();
        for (qsizetype index = 0; index < array.size(); ++index) {
            if (index > 0) {
                output.append(',');
            }
            output.append(canonicalJson(array.at(index)));
        }
        output.append(']');
        return output;
    }
    if (value.isObject()) {
        QByteArray output("{");
        const QJsonObject object = value.toObject();
        QStringList keys = object.keys();
        std::sort(keys.begin(), keys.end());
        for (qsizetype index = 0; index < keys.size(); ++index) {
            if (index > 0) {
                output.append(',');
            }
            QJsonArray keyWrapper;
            keyWrapper.append(QJsonValue(keys.at(index)));
            const QByteArray encodedKey =
                QJsonDocument(keyWrapper).toJson(QJsonDocument::Compact);
            output.append(encodedKey.mid(1, encodedKey.size() - 2));
            output.append(':');
            output.append(canonicalJson(object.value(keys.at(index))));
        }
        output.append('}');
        return output;
    }
    return {};
}

QByteArray sha256(const QByteArray& payload)
{
    return QCryptographicHash::hash(payload, QCryptographicHash::Sha256).toHex();
}

void require(bool condition, const char* message, int* failures)
{
    if (condition) {
        return;
    }
    std::fprintf(stderr, "FAIL: %s\n", message);
    ++*failures;
}

QJsonObject qualifiedGps()
{
    return {
        {QStringLiteral("status"), QStringLiteral("qualified")},
        {QStringLiteral("latitude_deg"), 30.2740842},
        {QStringLiteral("longitude_deg"), 120.1550701},
        {QStringLiteral("horizontal_sigma_m"), 2.0},
        {QStringLiteral("fix_type"), 3},
        {QStringLiteral("satellites_visible"), 10},
        {QStringLiteral("horizontal_accuracy_m"), 2.5},
        {QStringLiteral("vertical_accuracy_m"), 3.5},
        {QStringLiteral("observed_at_s"), 9.5},
    };
}

QJsonObject validEvent(const QByteArray& mediaHash)
{
    QJsonObject video = {
        {QStringLiteral("kind"), QStringLiteral("video_frame")},
        {QStringLiteral("path"), QStringLiteral("media/000000-video_frame.bin")},
        {QStringLiteral("sha256"), QString::fromLatin1(mediaHash)},
        {QStringLiteral("bytes"), 4},
        {QStringLiteral("encoding"), QStringLiteral("raw")},
        {QStringLiteral("width"), 2},
        {QStringLiteral("height"), 2},
    };
    return {
        {QStringLiteral("schema"), QStringLiteral("multidetect.target-event.v1")},
        {QStringLiteral("schema_version"), 1},
        {QStringLiteral("kind"), QStringLiteral("target_event")},
        {QStringLiteral("sequence"), 0},
        {QStringLiteral("event_id"), QStringLiteral("event-1")},
        {QStringLiteral("frame_id"), QStringLiteral("frame-1")},
        {QStringLiteral("target_id"), QStringLiteral("target-1")},
        {QStringLiteral("target_label"), QStringLiteral("vehicle")},
        {QStringLiteral("captured_at_s"), 10.0},
        {QStringLiteral("gps"), qualifiedGps()},
        {QStringLiteral("gps_age_s"), 0.5},
        {QStringLiteral("gps_freshness"), QStringLiteral("fresh")},
        {QStringLiteral("ranging"),
         QJsonObject{{QStringLiteral("validity"), QStringLiteral("VALID")},
                     {QStringLiteral("range_m"), 12.5},
                     {QStringLiteral("relative_bearing_deg"), -4.0},
                     {QStringLiteral("range_kind"), QStringLiteral("slant")}}},
        {QStringLiteral("video"), video},
        {QStringLiteral("depth"), QJsonValue()},
        {QStringLiteral("source_frame_id"), QStringLiteral("frame-source-1")},
        {QStringLiteral("vehicle_altitude_agl_m"), QJsonValue()},
        {QStringLiteral("vehicle_heading_deg"), 180.0},
    };
}

QByteArray writeValidBundle(const QString& rootPath)
{
    const QString mediaRoot = QDir(rootPath).filePath(QStringLiteral("media"));
    QDir().mkpath(mediaRoot);
    const QByteArray mediaPayload("ABCD", 4);
    const QByteArray mediaHash = sha256(mediaPayload);
    QFile media(QDir(mediaRoot).filePath(QStringLiteral("000000-video_frame.bin")));
    if (!media.open(QIODevice::WriteOnly)) {
        return {};
    }
    media.write(mediaPayload);
    media.close();

    const QJsonObject event = validEvent(mediaHash);
    const QByteArray eventStream = canonicalJson(event) + QByteArrayLiteral("\n");
    QFile events(QDir(rootPath).filePath(QStringLiteral("events.jsonl")));
    if (!events.open(QIODevice::WriteOnly)) {
        return {};
    }
    events.write(eventStream);
    events.close();

    QJsonObject manifestWithoutHash = {
        {QStringLiteral("schema"), QStringLiteral("multidetect.target-event-replay.v1")},
        {QStringLiteral("schema_version"), 1},
        {QStringLiteral("kind"), QStringLiteral("target_event_replay_manifest")},
        {QStringLiteral("bundle_id"), QStringLiteral("bundle-1")},
        {QStringLiteral("session_id"), QStringLiteral("019f880f-51a5-7291-bffd-dfb88e2620ce")},
        {QStringLiteral("started_at_utc"), QStringLiteral("2026-09-07T08:00:00.000Z")},
        {QStringLiteral("events"),
         QJsonObject{{QStringLiteral("path"), QStringLiteral("events.jsonl")},
                     {QStringLiteral("sha256"), QString::fromLatin1(sha256(eventStream))},
                     {QStringLiteral("bytes"), eventStream.size()},
                     {QStringLiteral("count"), 1}}},
        {QStringLiteral("media"),
         QJsonObject{{QStringLiteral("root"), QStringLiteral("media")},
                     {QStringLiteral("count"), 1},
                     {QStringLiteral("bytes"), mediaPayload.size()}}},
        {QStringLiteral("claims"),
         QJsonObject{{QStringLiteral("read_only_evidence"), true},
                     {QStringLiteral("contains_flight_control"), false},
                     {QStringLiteral("contains_truth"), false}}},
    };
    QJsonObject manifest = manifestWithoutHash;
    manifest.insert(QStringLiteral("bundle_hash"), QString::fromLatin1(sha256(canonicalJson(manifestWithoutHash))));
    const QByteArray manifestBytes = canonicalJson(manifest) + QByteArrayLiteral("\n");
    const QString manifestPath = QDir(rootPath).filePath(QStringLiteral("manifest.json"));
    QFile manifestFile(manifestPath);
    if (!manifestFile.open(QIODevice::WriteOnly)) {
        return {};
    }
    manifestFile.write(manifestBytes);
    manifestFile.close();
    return manifestPath.toUtf8();
}

} // namespace

int main(int argc, char* argv[])
{
    QCoreApplication application(argc, argv);
    if (argc == 3 && QString::fromLocal8Bit(argv[1]) == QStringLiteral("--load")) {
        MultiDetectTargetEventReplayStore externalStore;
        if (!externalStore.loadManifest(QString::fromLocal8Bit(argv[2]))) {
            std::fprintf(stderr, "FAIL: external bundle rejected: %s\n",
                         externalStore.lastError().toLocal8Bit().constData());
            return 1;
        }
        std::printf("EXTERNAL_EVENTS=%d\n", externalStore.eventCount());
        std::puts("MultiDetectTargetEventReplayStoreSelfTest: EXTERNAL PASS");
        return 0;
    }
    if (argc != 1) {
        std::fprintf(stderr, "usage: selftest [--load <manifest.json>]\n");
        return 1;
    }
    QTemporaryDir temporaryDirectory;
    int failures = 0;
    require(temporaryDirectory.isValid(), "temporary directory must be valid", &failures);
    if (!temporaryDirectory.isValid()) {
        return 1;
    }
    const QString rootPath = temporaryDirectory.path();
    const QString manifestPath = QString::fromUtf8(writeValidBundle(rootPath));
    require(!manifestPath.isEmpty(), "valid bundle fixture must be written", &failures);
    if (manifestPath.isEmpty()) {
        return 1;
    }

    MultiDetectTargetEventReplayStore store;
    require(store.loadManifest(manifestPath), "valid replay bundle must load", &failures);
    require(store.loaded() && store.integrityVerified(),
            "successful load must expose verified state", &failures);
    require(store.eventCount() == 1 && store.mediaCount() == 1 &&
                store.displayedEventCount() == 1,
            "event and media counts must be exposed", &failures);
    const QVariantMap event = store.events().constFirst().toMap();
    require(event.value(QStringLiteral("target_id")).toString() == QStringLiteral("target-1"),
            "event target id must be exposed", &failures);
    require(event.value(QStringLiteral("gps")).toMap().value(QStringLiteral("coordinates_included"))
                .toBool(),
            "qualified GPS coordinates must be exposed", &failures);
    require(event.value(QStringLiteral("ranging")).toMap().value(QStringLiteral("available"))
                .toBool(),
            "valid range must be exposed", &failures);

    QFile media(QDir(rootPath).filePath(QStringLiteral("media/000000-video_frame.bin")));
    require(media.open(QIODevice::WriteOnly | QIODevice::Truncate),
            "media must be tamperable for negative test", &failures);
    media.write("WXYZ", 4);
    media.close();
    require(!store.loadManifest(manifestPath), "tampered media must fail closed", &failures);
    require(!store.loaded() && !store.integrityVerified() && !store.lastError().isEmpty(),
            "failed load must clear state and expose an error", &failures);

    const QString duplicatePath = QDir(rootPath).filePath(QStringLiteral("duplicate.json"));
    QFile duplicate(duplicatePath);
    require(duplicate.open(QIODevice::WriteOnly), "duplicate manifest must open", &failures);
    duplicate.write("{\"schema\":\"x\",\"schema\":\"y\"}");
    duplicate.close();
    require(!store.loadManifest(duplicatePath), "duplicate JSON keys must fail closed", &failures);

    if (failures == 0) {
        std::puts("MultiDetectTargetEventReplayStoreSelfTest: PASS");
        return 0;
    }
    return 1;
}
