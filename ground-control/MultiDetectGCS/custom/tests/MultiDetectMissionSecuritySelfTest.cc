#include <QtCore/QCoreApplication>
#include <QtCore/QDir>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <QtCore/QSettings>
#include <cstdio>

#include "MultiDetectMissionSecurity.h"

namespace {
int failures = 0;

void check(bool condition, const char* description)
{
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s\n", description);
        ++failures;
    }
}
}  // namespace

int main(int argc, char** argv)
{
    QCoreApplication app(argc, argv);
    QCoreApplication::setOrganizationName(QStringLiteral("MultiDetect"));
    QCoreApplication::setApplicationName(QStringLiteral("MissionSecuritySelfTest"));
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope,
                       QDir::tempPath() + QStringLiteral("/multidetect-security-selftest"));
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
    QJsonObject numbers;
    numbers.insert("A", 1.0);
    numbers.insert("a", 1700000000.0);
    numbers.insert("arr", QJsonArray{0.1, 1e-7, -2.5, 123456789.125,
                                      QJsonArray{QString::fromUtf8("\xE9\x9B\xAA"),
                                                 QString::fromUtf8("\xF0\x9F\x98\x80")}});
    numbers.insert("negative", -42.0);
    numbers.insert("zero", -0.0);
    numbers.insert(QString::fromUtf8("\xC3\xA9"),
                  QString::fromUtf8("\xE9\x9B\xAA\xF0\x9F\x98\x80"));
    numbers.insert(QString::fromUtf8("\xF0\x9F\x98\x80"), QString::fromUtf8("\xC3\xA9"));
    numbers.insert("key_id", "gcs-test");
    const QByteArray numberCanonical = MultiDetectMissionSecurity::canonicalJson(numbers);
    const QByteArray numberExpected = QByteArray::fromHex(
        "7b2241223a312c2261223a313730303030303030302c22617272223a5b302e312c31652d30372c2d322e352c"
        "3132333435363738392e3132352c5b22e99baa222c22f09f9880225d5d2c226b65795f6964223a"
        "226763732d74657374222c226e65676174697665223a2d34322c227a65726f223a302c22c3a9223a22e99baa"
        "f09f9880222c22f09f9880223a22c3a9227d");
    check(numberCanonical == numberExpected, "Python-compatible number/unicode canonical bytes match");
    QJsonObject outOfRange;
    outOfRange.insert(QStringLiteral("value"), 9007199254740992.0);
    check(MultiDetectMissionSecurity::canonicalJson(outOfRange).isEmpty(),
          "integral doubles outside exact JSON range are rejected");
    QString error;
    QByteArray seed;
    for (int index = 0; index < 32; ++index)
        seed.append(static_cast<char>(index));
    QJsonObject numberSigned = numbers;
    const bool numberSigningAvailable = MultiDetectMissionSecurity::signDocument(
        &numberSigned, seed, "gcs-test", &error);
    if (numberSigningAvailable)
        check(numberSigned.value("signature").toString() ==
                  "tO1c50q1FAfJoKT/v2Ss3Inx0lPHQgBn+DPo+ZSWtM/mfY5ENDgFK9uyE1v7URZzDRTc8nfZMqi7H4TJaHVPCA==",
              "Python-compatible number/unicode signature matches");
    if (!MultiDetectMissionSecurity::signGroundCommand(&command, seed, "gcs-test", &error)) {
        // Minimal standalone builds may not provide OpenSSL.  Canonicalization
        // remains tested; the production QGC target must link its crypto backend.
        std::puts("MultiDetectMissionSecuritySelfTest: signing backend unavailable (canonical OK)");
    } else if (command.value("signature_alg").toString() != "ed25519" ||
        command.value("signature").toString() !=
            "Ow/0p9a6YI5GDT/z2OZEdSwhslBXBgya64HVaqMCck1KuLW9OQhpx1vtSfBc1xeXxfU2hckAA5DPpvzq1rSmDQ==")
        return 1;
    QJsonObject outOfRangeCommand = command;
    outOfRangeCommand.insert(QStringLiteral("sequence"), 9007199254740992.0);
    check(!MultiDetectMissionSecurity::signGroundCommand(&outOfRangeCommand, seed, "gcs-test", &error),
          "ground command refuses sequence outside exact JSON range");
    // HMAC ground-command key (onboard --operator-key / fake_gcs_console
    // --command-key): golden vector from mission_security.sign_document.
    MultiDetectMissionSecurity::SigningKey hmacKey;
    check(MultiDetectMissionSecurity::parseSigningKey("hmac:" + seed.toHex(), &hmacKey, &error) &&
              hmacKey.algorithm == "hmac-sha256" && hmacKey.material == seed,
          "hex HMAC key parses");
    MultiDetectMissionSecurity::SigningKey base64Key;
    check(MultiDetectMissionSecurity::parseSigningKey("HMAC:" + seed.toBase64(), &base64Key, &error) &&
              base64Key.material == seed,
          "base64 HMAC key parses");
    check(!MultiDetectMissionSecurity::parseSigningKey("hmac:0011", &base64Key, &error),
          "HMAC key shorter than 16 bytes is refused");
    check(!MultiDetectMissionSecurity::parseSigningKey(seed.toHex(), &base64Key, &error),
          "unprefixed key text is refused");
    check(!MultiDetectMissionSecurity::parseSigningKey("ed25519-public:" + seed.toHex(), &base64Key, &error),
          "a public key cannot sign");
    QJsonObject approve{{"mission_id", "golden-mission"},
                        {"type", "approve_plan"},
                        {"plan_sha256", QStringLiteral("ab").repeated(32)},
                        {"sequence", 3},
                        {"timestamp", 1700000000.5},
                        {"timestamp_version", 1},
                        {"timestamp_domain", "unix"}};
    check(MultiDetectMissionSecurity::signGroundCommand(&approve, hmacKey, "gcs-test", &error) &&
              approve.value("key_id").toString() == "gcs-test" &&
              approve.value("signature_alg").toString() == "hmac-sha256" &&
              approve.value("signature").toString() == "u31BMTjZSMPnE+lvJhOzqY3Qj4czLP8cyN+bv5EbjLk=",
          "HMAC approve_plan signature matches the onboard verifier");
    QJsonObject untimed{{"mission_id", "golden-mission"}, {"type", "approve_plan"}};
    check(!MultiDetectMissionSecurity::signGroundCommand(&untimed, hmacKey, "gcs-test", &error),
          "signed ground command requires the versioned timestamp contract");

    QString sequenceError;
    const QString sequenceMission = QStringLiteral("security-self-test-sequence");
    const quint64 first = MultiDetectMissionSecurity::nextGroundSequence(sequenceMission, &sequenceError);
    const quint64 second = MultiDetectMissionSecurity::nextGroundSequence(sequenceMission, &sequenceError);
    check(first > 0 && second == first + 1, "ground sequence increases without wrapping");
    {
        QSettings restarted;
        Q_UNUSED(restarted);
    }
    const quint64 afterRestart = MultiDetectMissionSecurity::nextGroundSequence(sequenceMission, &sequenceError);
    check(afterRestart == second + 1 && sequenceError.isEmpty(), "ground sequence survives a new QSettings instance");
    std::printf("MultiDetectMissionSecuritySelfTest: %d failures\n", failures);
    return failures == 0 ? 0 : 1;
}
