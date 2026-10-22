#pragma once

#include <QtCore/QJsonObject>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QByteArray>

// Shared GCS-side implementation of the mission-security JSON contract.
// Canonical bytes deliberately do not use QJsonDocument's insertion order.
class MultiDetectMissionSecurity final
{
public:
    // A locally provisioned signing key.  The algorithm comes from the key
    // text prefix (as in mission_security.load_key), never from a document.
    struct SigningKey
    {
        QString algorithm;  // "ed25519" (private seed) or "hmac-sha256"
        QByteArray material;

        bool isValid() const { return !algorithm.isEmpty() && !material.isEmpty(); }
        void wipe();
    };

    // Accepts "hmac:<hex|base64>" (16..4096 bytes, as onboard) or
    // "ed25519-private:<hex|base64>" (32-byte seed).
    static bool parseSigningKey(const QByteArray& text, SigningKey* key, QString* error);

    static QByteArray canonicalJson(const QJsonObject& document,
                                    const QStringList& excluded = {});

    static bool signDocument(QJsonObject* document, const SigningKey& key, const QString& keyId,
                             QString* error);
    static bool signGroundCommand(QJsonObject* command, const SigningKey& key, const QString& keyId,
                                  QString* error);

    static bool signDocument(QJsonObject* document, const QByteArray& privateSeed,
                             const QString& keyId, QString* error);

    // Sequence is persisted in the GCS user's QSettings under a hash of the
    // mission_id. Returns 0 and reports an error when persistence fails or the
    // uint64 sequence is exhausted; sequence 0 is never emitted.
    static quint64 nextGroundSequence(const QString& missionId, QString* error = nullptr);

    static bool signGroundCommand(QJsonObject* command, const QByteArray& privateSeed,
                                  const QString& keyId, QString* error);
};
