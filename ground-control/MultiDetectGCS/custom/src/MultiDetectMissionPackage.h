#pragma once

#include <QtCore/QJsonObject>
#include <QtCore/QString>

// Mirrors autonomy/mission_package.py and mission_plan.py for preflight admission.
// The aircraft repeats admission before replacing any onboard package.
class MultiDetectMissionPackage final
{
public:
    static bool parse(const QByteArray& json, QJsonObject* document, QString* error);
    static bool validate(const QJsonObject& document, QString* error);
};
