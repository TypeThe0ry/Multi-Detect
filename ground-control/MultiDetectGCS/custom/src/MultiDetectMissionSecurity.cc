#include "MultiDetectMissionSecurity.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QFileInfo>
#include <QtCore/QDir>
#include <QtCore/QLockFile>
#include <QtCore/QMessageAuthenticationCode>
#include <QtCore/QMutex>
#include <QtCore/QMutexLocker>
#include <QtCore/QSettings>
#include <QtCore/QStandardPaths>
#include <algorithm>
#include <charconv>
#include <cmath>
#include <system_error>
#include <limits>

#if defined(MULTIDETECT_HAVE_OPENSSL)
#include <openssl/evp.h>
#endif

namespace {

QByteArray canonicalValue(const QJsonValue& value);

QByteArray quoted(const QString& value)
{
    QJsonArray array;
    array.append(value);
    const QByteArray encoded = QJsonDocument(array).toJson(QJsonDocument::Compact);
    return encoded.mid(1, encoded.size() - 2);
}

QByteArray canonicalValue(const QJsonValue& value)
{
    if (value.isObject()) {
        const QJsonObject object = value.toObject();
        QStringList keys = object.keys();
        std::sort(keys.begin(), keys.end(), [](const QString& left, const QString& right) {
            // UTF-8 byte order preserves Unicode scalar-value order, unlike
            // QString's UTF-16 code-unit order for non-BMP characters.
            return left.toUtf8() < right.toUtf8();
        });
        QByteArray result("{");
        for (int index = 0; index < keys.size(); ++index) {
            if (index)
                result.append(',');
            const QByteArray child = canonicalValue(object.value(keys.at(index)));
            if (child.isEmpty())
                return {};
            result.append(quoted(keys.at(index))).append(':').append(child);
        }
        return result.append('}');
    }
    if (value.isArray()) {
        const QJsonArray array = value.toArray();
        QByteArray result("[");
        for (int index = 0; index < array.size(); ++index) {
            if (index)
                result.append(',');
            const QByteArray child = canonicalValue(array.at(index));
            if (child.isEmpty())
                return {};
            result.append(child);
        }
        return result.append(']');
    }
    if (value.isString())
        return quoted(value.toString());
    if (value.isBool())
        return value.toBool() ? QByteArray("true") : QByteArray("false");
    if (value.isNull() || value.isUndefined())
        return QByteArray("null");
    if (!value.isDouble() || !std::isfinite(value.toDouble()))
        return {};
    const double number = value.toDouble();
    if (!std::isfinite(number))
        return {};
    if (number == 0.0)
        return QByteArray("0");
    if (std::floor(number) == number && std::abs(number) <= 9007199254740991.0)
        return QByteArray::number(static_cast<qint64>(number));
    if (std::floor(number) == number)
        return {};
    char buffer[64] = {};
    const auto converted = std::to_chars(buffer, buffer + sizeof(buffer), number);
    if (converted.ec != std::errc())
        return {};
    QByteArray encoded(buffer, static_cast<int>(converted.ptr - buffer));
    encoded.replace('E', 'e');
    return encoded;
}

bool signEd25519(const QByteArray& message, const QByteArray& seed, QByteArray* signature,
                QString* error)
{
#if defined(MULTIDETECT_HAVE_OPENSSL)
    if (seed.size() != 32) {
        if (error)
            *error = QStringLiteral("Ed25519 private seed must be exactly 32 bytes");
        return false;
    }
    EVP_PKEY* key = EVP_PKEY_new_raw_private_key(EVP_PKEY_ED25519, nullptr,
                                                  reinterpret_cast<const unsigned char*>(seed.constData()),
                                                  static_cast<size_t>(seed.size()));
    EVP_MD_CTX* context = EVP_MD_CTX_new();
    size_t length = 0;
    bool ok = key != nullptr && context != nullptr &&
              EVP_DigestSignInit(context, nullptr, nullptr, nullptr, key) == 1 &&
              EVP_DigestSign(context, nullptr, &length,
                             reinterpret_cast<const unsigned char*>(message.constData()),
                             static_cast<size_t>(message.size())) == 1;
    if (ok) {
        signature->resize(static_cast<qsizetype>(length));
        ok = EVP_DigestSign(context,
                            reinterpret_cast<unsigned char*>(signature->data()), &length,
                            reinterpret_cast<const unsigned char*>(message.constData()),
                            static_cast<size_t>(message.size())) == 1;
        signature->resize(static_cast<qsizetype>(length));
    }
    if (context)
        EVP_MD_CTX_free(context);
    if (key)
        EVP_PKEY_free(key);
    if (!ok && error)
        *error = QStringLiteral("Ed25519 signing is unavailable or failed");
    return ok;
#else
    Q_UNUSED(message)
    Q_UNUSED(seed)
    Q_UNUSED(signature)
    if (error)
        *error = QStringLiteral("Ed25519 signing backend is unavailable in this build");
    return false;
#endif
}

bool sign(QJsonObject* document, const MultiDetectMissionSecurity::SigningKey& key, const QString& keyId,
          QString* error)
{
    const bool ed25519 = key.algorithm == QStringLiteral("ed25519") && key.material.size() == 32;
    const bool hmac = key.algorithm == QStringLiteral("hmac-sha256") && key.material.size() >= 16 &&
                      key.material.size() <= 4096;
    if (!document || !(ed25519 || hmac) || keyId.trimmed().isEmpty()) {
        if (error)
            *error = QStringLiteral("mission signing requires an Ed25519 private seed or HMAC key and key_id");
        return false;
    }
    document->insert(QStringLiteral("key_id"), keyId.trimmed());
    document->remove(QStringLiteral("signature"));
    document->remove(QStringLiteral("signature_alg"));
    const QByteArray payload = MultiDetectMissionSecurity::canonicalJson(
        *document, {QStringLiteral("signature"), QStringLiteral("signature_alg")});
    if (payload.isEmpty()) {
        if (error)
            *error = QStringLiteral("mission document contains a number outside the exact JSON range");
        return false;
    }
    QByteArray signature;
    if (hmac)
        signature = QMessageAuthenticationCode::hash(payload, key.material, QCryptographicHash::Sha256);
    else if (!signEd25519(payload, key.material, &signature, error))
        return false;
    document->insert(QStringLiteral("signature_alg"), key.algorithm);
    document->insert(QStringLiteral("signature"), QString::fromLatin1(signature.toBase64()));
    return true;
}

bool hasGroundTimestampContract(const QJsonObject* command)
{
    constexpr double kMaxJsonExactInteger = 9007199254740991.0;
    const QJsonValue sequence = command ? command->value(QStringLiteral("sequence")) : QJsonValue();
    return command && command->contains(QStringLiteral("mission_id")) &&
           command->contains(QStringLiteral("type")) && command->contains(QStringLiteral("sequence")) &&
           sequence.isDouble() && std::isfinite(sequence.toDouble()) &&
           std::floor(sequence.toDouble()) == sequence.toDouble() && sequence.toDouble() >= 1.0 &&
           sequence.toDouble() <= kMaxJsonExactInteger && command->contains(QStringLiteral("timestamp")) &&
           command->value(QStringLiteral("timestamp_version")).toInt() == 1 &&
           QStringList({QStringLiteral("monotonic"), QStringLiteral("unix")})
               .contains(command->value(QStringLiteral("timestamp_domain")).toString());
}

// Exactly one hex or base64 representation, as mission_security._decode_encoded.
QByteArray decodeKeyText(const QByteArray& text)
{
    if (text.isEmpty())
        return {};
    const bool hex = std::all_of(text.begin(), text.end(), [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    });
    if (hex)
        return text.size() % 2 ? QByteArray() : QByteArray::fromHex(text);
    const auto decoded = QByteArray::fromBase64Encoding(text, QByteArray::AbortOnBase64DecodingErrors);
    return decoded ? decoded.decoded : QByteArray();
}

}  // namespace

void MultiDetectMissionSecurity::SigningKey::wipe()
{
    std::fill(material.begin(), material.end(), '\0');
    material.clear();
    algorithm.clear();
}

bool MultiDetectMissionSecurity::parseSigningKey(const QByteArray& text, SigningKey* key, QString* error)
{
    const QByteArray trimmed = text.trimmed();
    const QByteArray lowered = trimmed.toLower();
    SigningKey parsed;
    if (lowered.startsWith("hmac:")) {
        parsed.algorithm = QStringLiteral("hmac-sha256");
        parsed.material = decodeKeyText(trimmed.mid(5).trimmed());
    } else if (lowered.startsWith("ed25519-private:")) {
        parsed.algorithm = QStringLiteral("ed25519");
        parsed.material = decodeKeyText(trimmed.mid(16).trimmed());
    }
    const bool ok = (parsed.algorithm == QStringLiteral("hmac-sha256") && parsed.material.size() >= 16 &&
                     parsed.material.size() <= 4096) ||
                    (parsed.algorithm == QStringLiteral("ed25519") && parsed.material.size() == 32);
    if (!ok) {
        parsed.wipe();
        if (error)
            *error = QStringLiteral("signing key must be hmac:<hex|base64> (16-4096 bytes) or "
                                    "ed25519-private:<hex|base64> (32 bytes)");
        return false;
    }
    if (key)
        *key = parsed;
    parsed.wipe();
    if (error)
        error->clear();
    return true;
}

bool MultiDetectMissionSecurity::signDocument(QJsonObject* document, const SigningKey& key, const QString& keyId,
                                              QString* error)
{
    return sign(document, key, keyId, error);
}

bool MultiDetectMissionSecurity::signGroundCommand(QJsonObject* command, const SigningKey& key,
                                                   const QString& keyId, QString* error)
{
    if (!hasGroundTimestampContract(command)) {
        if (error)
            *error = QStringLiteral("ground command is missing its versioned timestamp contract");
        return false;
    }
    return sign(command, key, keyId, error);
}

QByteArray MultiDetectMissionSecurity::canonicalJson(const QJsonObject& document,
                                                      const QStringList& excluded)
{
    QJsonObject filtered;
    for (auto it = document.begin(); it != document.end(); ++it) {
        if (!excluded.contains(it.key()))
            filtered.insert(it.key(), it.value());
    }
    return canonicalValue(filtered);
}

bool MultiDetectMissionSecurity::signDocument(QJsonObject* document, const QByteArray& privateSeed,
                                              const QString& keyId, QString* error)
{
    return sign(document, SigningKey{QStringLiteral("ed25519"), privateSeed}, keyId, error);
}

quint64 MultiDetectMissionSecurity::nextGroundSequence(const QString& missionId, QString* error)
{
    static QMutex processMutex;
    QMutexLocker locker(&processMutex);
    const QByteArray digest = QCryptographicHash::hash(missionId.toUtf8(), QCryptographicHash::Sha256).toHex();
    QSettings settingsForPath;
    // On Windows the native QSettings "file" is a registry key
    // (\HKEY_CURRENT_USER\Software\...), whose parent is not a directory: a lock
    // file there can never be created, so every sequence request timed out.
    // Use the settings file's directory only when it really is one.
    QString settingsDirectory;
    const QFileInfo settingsFile(settingsForPath.fileName());
    if (settingsForPath.format() != QSettings::NativeFormat || settingsFile.absoluteDir().exists())
        settingsDirectory = settingsFile.absolutePath();
    if (settingsDirectory.isEmpty() || settingsDirectory == QStringLiteral(".") ||
        !QDir(settingsDirectory).exists())
        settingsDirectory = QStandardPaths::writableLocation(QStandardPaths::AppConfigLocation);
    if (settingsDirectory.isEmpty() || !QDir().mkpath(settingsDirectory)) {
        if (error)
            *error = QStringLiteral("unable to determine GCS settings directory for sequence lock");
        return 0;
    }
    QLockFile sequenceLock(QDir(settingsDirectory).filePath(QStringLiteral(".multidetect-ground-sequence.lock")));
    sequenceLock.setStaleLockTime(30000);
    if (!sequenceLock.tryLock(250)) {
        if (error)
            *error = QStringLiteral("timed out acquiring ground command sequence lock");
        return 0;
    }
    // Reopen after taking the inter-process lock so another GCS process cannot
    // leave this instance with a pre-lock cached value.
    QSettings settings;
    const QString key = QStringLiteral("MultiDetect/ground_sequence/") + QString::fromLatin1(digest);
    const quint64 previous = settings.value(key, 0).toULongLong();
    constexpr quint64 kMaxJsonExactInteger = 9007199254740991ULL;
    if (previous >= kMaxJsonExactInteger) {
        if (error)
            *error = QStringLiteral("ground command sequence exhausted exact JSON integer range");
        return 0;
    }
    const quint64 next = previous + 1;
    settings.setValue(key, QVariant::fromValue(next));
    settings.sync();
    if (settings.status() != QSettings::NoError) {
        if (error)
            *error = QStringLiteral("failed to persist ground command sequence in GCS user settings");
        return 0;
    }
    if (error)
        error->clear();
    return next;
}

bool MultiDetectMissionSecurity::signGroundCommand(QJsonObject* command, const QByteArray& privateSeed,
                                                   const QString& keyId, QString* error)
{
    return signGroundCommand(command, SigningKey{QStringLiteral("ed25519"), privateSeed}, keyId, error);
}
