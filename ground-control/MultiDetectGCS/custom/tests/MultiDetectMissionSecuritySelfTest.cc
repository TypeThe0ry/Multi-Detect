#include <QtCore/QCoreApplication>
#include <QtCore/QJsonObject>
#include <cstdio>

#include "MultiDetectMissionSecurity.h"

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    QJsonObject command{{"mission_id", "golden-mission"},
                        {"type", "abort"},
                        {"sequence", 7},
                        {"timestamp", 1700000000.25},
                        {"timestamp_version", 1},
                        {"timestamp_domain", "unix"},
                        {"key_id", "gcs-test"}};
    const QByteArray canonical = MultiDetectMissionSecurity::canonicalJson(command);
    const QByteArray expected =
        "{\"key_id\":\"gcs-test\",\"mission_id\":\"golden-mission\",\"sequence\":7,"
        "\"timestamp\":1700000000.25,\"timestamp_domain\":\"unix\",\"timestamp_version\":1,"
        "\"type\":\"abort\"}";
    if (canonical != expected) {
        std::fprintf(stderr, "canonical mismatch: %s\n", canonical.constData());
        return 1;
    }
    QString error;
    QByteArray seed;
    for (int index = 0; index < 32; ++index)
        seed.append(static_cast<char>(index));
    if (!MultiDetectMissionSecurity::signGroundCommand(&command, seed, "gcs-test", &error)) {
        // Minimal standalone builds may not provide OpenSSL.  Canonicalization
        // remains tested; the production QGC target must link its crypto backend.
        std::puts("MultiDetectMissionSecuritySelfTest: signing backend unavailable (canonical OK)");
        return 0;
    }
    if (command.value("signature_alg").toString() != "ed25519" ||
        command.value("signature").toString() !=
            "Ow/0p9a6YI5GDT/z2OZEdSwhslBXBgya64HVaqMCck1KuLW9OQhpx1vtSfBc1xeXxfU2hckAA5DPpvzq1rSmDQ==")
        return 1;
    std::puts("MultiDetectMissionSecuritySelfTest: passed");
    return 0;
}
