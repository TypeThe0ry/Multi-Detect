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
    static QByteArray canonicalJson(const QJsonObject& document,
                                    const QStringList& excluded = {});

    static bool signDocument(QJsonObject* document, const QByteArray& privateSeed,
                             const QString& keyId, QString* error);

    // Sequence is persisted in the GCS user's QSettings under a hash of the
    // mission_id. Returns 0 and reports an error when persistence fails or the
    // uint64 sequence is exhausted; sequence 0 is never emitted.
    static quint64 nextGroundSequence(const QString& missionId, QString* error = nullptr);

    static bool signGroundCommand(QJsonObject* command, const QByteArray& privateSeed,
                                  const QString& keyId, QString* error);
};
