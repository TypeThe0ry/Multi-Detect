#pragma once

#include <QtCore/QJsonObject>
#include <QtCore/QJsonDocument>
#include <QtCore/QString>

// Mirrors autonomy/mission_package.py and mission_plan.py for preflight admission.
// The aircraft repeats admission before replacing any onboard package.
class MultiDetectMissionPackage final
{
public:
    static bool parse(const QByteArray& json, QJsonObject* document, QString* error);
    // Decode JSON with the same duplicate-member and finite-number policy used
    // by Python strict_json_loads.  This is intentionally schema agnostic for
    // telemetry/log documents.
    static bool parseStrictJson(const QByteArray& json, QJsonDocument* document, QString* error);
    static bool validate(const QJsonObject& document, QString* error);
};
