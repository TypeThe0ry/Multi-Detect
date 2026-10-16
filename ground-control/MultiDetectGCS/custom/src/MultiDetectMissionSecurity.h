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

    static quint64 nextGroundSequence(const QString& missionId);

    static bool signGroundCommand(QJsonObject* command, const QByteArray& privateSeed,
                                  const QString& keyId, QString* error);
};
