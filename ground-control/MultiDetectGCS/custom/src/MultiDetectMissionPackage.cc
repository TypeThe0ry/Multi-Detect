#include "MultiDetectMissionPackage.h"

#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonParseError>
#include <QtCore/QRegularExpression>
#include <QtCore/QSet>
#include <QtCore/QStringDecoder>
#include <cmath>
#include <functional>
#include <limits>

namespace {
using Keys = QSet<QString>;
const QStringList policies = {"continue", "continue_then_return", "hold_then_return", "return_now", "land_now"};

bool text(const QJsonValue& v)
{
    return v.isString() && !v.toString().trimmed().isEmpty();
}

int textLength(const QString& value)
{
    return static_cast<int>(value.toUcs4().size());
}

bool number(const QJsonValue& v, double minimum = 0, double maximum = std::numeric_limits<double>::infinity())
{
    return v.isDouble() && std::isfinite(v.toDouble()) && v.toDouble() >= minimum && v.toDouble() <= maximum;
}

bool integer(const QJsonValue& v, int minimum, int maximum)
{
    return number(v, minimum, maximum) && std::floor(v.toDouble()) == v.toDouble();
}

bool object(const QJsonValue& v, const Keys& required, const Keys& optional = {})
{
    if (!v.isObject())
        return false;
    const auto obj = v.toObject();
    const auto names = obj.keys();
    const Keys keys(names.begin(), names.end());
    return (required - keys).isEmpty() && (keys - required - optional).isEmpty();
}

// QJsonDocument otherwise silently keeps the last duplicate key. Inspect the
// already syntactically valid token stream, including escaped object keys.
bool uniqueKeys(const QByteArray& data)
{
    qsizetype pos = 0;
    const auto whitespace = [&]() {
        while (pos < data.size() && QByteArray(" \t\r\n").contains(data[pos]))
            ++pos;
    };
    const auto stringToken = [&]() {
        const qsizetype start = pos++;
        while (pos < data.size()) {
            const char c = data[pos++];
            if (c == '\\')
                ++pos;
            else if (c == '"')
                break;
        }
        return data.mid(start, pos - start);
    };
    std::function<bool()> value = [&]() {
        whitespace();
        if (pos >= data.size())
            return false;
        if (data[pos] == '{') {
            ++pos;
            whitespace();
            Keys seen;
            while (pos < data.size() && data[pos] != '}') {
                const auto keyToken = stringToken();
                const QString key = QJsonDocument::fromJson("[" + keyToken + "]").array()[0].toString();
                if (seen.contains(key))
                    return false;
                seen.insert(key);
                whitespace();
                ++pos;  // colon, verified by QJsonDocument
                whitespace();
                if (Keys({"plan_version", "views", "loops", "max_count"}).contains(key) &&
                    (data[pos] == '-' || (data[pos] >= '0' && data[pos] <= '9'))) {
                    qsizetype end = pos;
                    while (end < data.size() && !QByteArray(",]} \t\r\n").contains(data[end]))
                        ++end;
                    if (!QRegularExpression("^-?(0|[1-9][0-9]*)$")
                             .match(QString::fromLatin1(data.mid(pos, end - pos)))
                             .hasMatch())
                        return false;
                }
                if (!value())
                    return false;
                whitespace();
                if (data[pos] != ',')
                    break;
                ++pos;
                whitespace();
            }
            ++pos;
        } else if (data[pos] == '[') {
            ++pos;
            whitespace();
            while (pos < data.size() && data[pos] != ']') {
                if (!value())
                    return false;
                whitespace();
                if (data[pos] != ',')
                    break;
                ++pos;
            }
            ++pos;
        } else if (data[pos] == '"')
            (void) stringToken();
        else
            while (pos < data.size() && !QByteArray(",]} \t\r\n").contains(data[pos]))
                ++pos;
        return true;
    };
    return value();
}

struct Skill
{
    Keys required;
    QHash<QString, QString> params;
};

const QHash<QString, Skill> skills = {
    {"goto", {{"target_ref"}, {{"target_ref", "ref"}, {"altitude_m", "altitude"}}}},
    {"area_search", {{"region", "query"}, {{"region", "ref"}, {"query", "query"}, {"altitude_m", "altitude"}}}},
    {"count",
     {{"region", "query"},
      {{"region", "ref"}, {"query", "query"}, {"max_count", "count"}, {"altitude_m", "altitude"}}}},
    {"inspect",
     {{"target_ref", "query", "views", "standoff_m"},
      {{"target_ref", "ref"},
       {"query", "query"},
       {"views", "views"},
       {"standoff_m", "distance"},
       {"altitude_m", "altitude"}}}},
    {"patrol",
     {{"route_ref", "query", "loops"},
      {{"route_ref", "ref"},
       {"query", "query"},
       {"loops", "loops"},
       {"duration_s", "duration"},
       {"altitude_m", "altitude"}}}},
    {"survey", {{"region", "overlap"}, {{"region", "ref"}, {"overlap", "overlap"}, {"altitude_m", "altitude"}}}},
    {"follow",
     {{"track_ref", "query"},
      {{"track_ref", "ref"}, {"query", "query"}, {"duration_s", "duration"}, {"altitude_m", "altitude"}}}},
    {"monitor",
     {{"region", "query", "duration_s"},
      {{"region", "ref"}, {"query", "query"}, {"duration_s", "duration"}, {"altitude_m", "altitude"}}}},
    {"record", {{"query"}, {{"query", "query"}, {"max_count", "count"}}}},
    {"hover", {{"duration_s"}, {{"duration_s", "duration"}, {"region", "ref"}, {"altitude_m", "altitude"}}}},
    {"return_home", {{}, {{"altitude_m", "altitude"}}}},
    {"land", {{}, {{"target_ref", "ref"}}}}};

bool param(const QJsonValue& v, const QString& kind)
{
    const QString normalized = v.toString().trimmed();
    if (kind == "ref")
        return text(v) && textLength(normalized) <= 96 && (normalized == "home" || normalized.contains(':'));
    if (kind == "query")
        return text(v) && textLength(normalized) <= 96 &&
               normalized.split(QRegularExpression("\\s+"), Qt::SkipEmptyParts).size() <= 8;
    if (kind == "altitude")
        return number(v, 1, 120);
    if (kind == "distance")
        return number(v, 0.5, 100);
    if (kind == "overlap")
        return number(v, 0.1, 0.95);
    if (kind == "duration")
        return number(v, 1, 86400);
    if (kind == "count")
        return integer(v, 1, 10000);
    if (kind == "views")
        return integer(v, 1, 24);
    if (kind == "loops")
        return integer(v, 1, 100);
    return false;
}
}  // namespace

bool MultiDetectMissionPackage::parseStrictJson(const QByteArray& json, QJsonDocument* document, QString* error)
{
    QStringDecoder utf8(QStringDecoder::Utf8);
    (void) utf8(json);
    QJsonParseError parseError;
    const auto parsed = QJsonDocument::fromJson(json, &parseError);
    if (utf8.hasError() || parseError.error != QJsonParseError::NoError || !uniqueKeys(json)) {
        if (error)
            *error = QStringLiteral("JSON must be valid UTF-8 without duplicate object keys or non-finite numbers");
        return false;
    }
    if (document)
        *document = parsed;
    if (error)
        error->clear();
    return true;
}

bool MultiDetectMissionPackage::parse(const QByteArray& json, QJsonObject* document, QString* error)
{
    QStringDecoder utf8(QStringDecoder::Utf8);
    (void) utf8(json);
    QJsonParseError parseError;
    const auto parsed = QJsonDocument::fromJson(json, &parseError);
    if (utf8.hasError() || parseError.error != QJsonParseError::NoError || !parsed.isObject() || !uniqueKeys(json)) {
        if (error)
            *error = QStringLiteral("任务包必须是有效 JSON 对象，不能有重复字段");
        return false;
    }
    if (!validate(parsed.object(), error))
        return false;
    if (document)
        *document = parsed.object();
    return true;
}

bool MultiDetectMissionPackage::validate(const QJsonObject& doc, QString* error)
{
    const auto fail = [error](const QString& message) {
        if (error)
            *error = message;
        return false;
    };
    if (!object(doc,
                {"mission_id", "places", "zones", "routes", "operating_area", "budget", "home", "risk_level",
                 "link_loss_policy", "oversight_steps"},
                 {"task_text", "plan", "ref_labels", "heartbeat_timeout_s", "key_id", "signature_alg", "signature"}))
        return fail(QStringLiteral("任务包有缺失或未知字段"));
    if (!text(doc["mission_id"]) || !QStringList({"low", "medium", "high"}).contains(doc["risk_level"].toString()))
        return fail(QStringLiteral("任务编号或风险等级无效"));
    if (doc.contains("task_text") && !doc["task_text"].isNull() && !text(doc["task_text"]))
        return fail(QStringLiteral("任务文字不能为空"));
    if (!doc.contains("plan") && !text(doc["task_text"]))
        return fail(QStringLiteral("任务包需要任务文字或机载步骤计划"));
    const auto policy = doc["link_loss_policy"].toObject();
    const QString action = policy["action"].toString();
    const bool timed = action == "continue_then_return" || action == "hold_then_return";
    if (!object(doc["link_loss_policy"], {"action"}, {"timeout_s"}) || !policies.contains(action) ||
        (timed && !policy.contains("timeout_s")) || (policy.contains("timeout_s") && !number(policy["timeout_s"])))
        return fail(QStringLiteral("失联策略或超时无效"));
    if (doc.contains("heartbeat_timeout_s") && !number(doc["heartbeat_timeout_s"], 0.001))
        return fail(QStringLiteral("心跳超时至少为 0.001 秒"));
    const auto area = doc["operating_area"].toObject();
    if (!object(doc["operating_area"],
                {"north_min_m", "north_max_m", "east_min_m", "east_max_m", "min_altitude_m", "max_altitude_m"}))
        return fail(QStringLiteral("作业区域字段无效"));
    for (auto it = area.begin(); it != area.end(); ++it)
        if (!number(it.value(), it.key().startsWith("north") || it.key().startsWith("east")
                                    ? -std::numeric_limits<double>::infinity()
                                    : 0))
            return fail(QStringLiteral("作业区域必须使用有限数值"));
    if (area["north_min_m"].toDouble() >= area["north_max_m"].toDouble() ||
        area["east_min_m"].toDouble() >= area["east_max_m"].toDouble() ||
        area["min_altitude_m"].toDouble() >= area["max_altitude_m"].toDouble())
        return fail(QStringLiteral("作业范围和高度必须有正跨度"));
    const auto budget = doc["budget"].toObject();
    if (!object(doc["budget"], {"time_s", "reserve_time_s", "energy_wh", "reserve_energy_wh"}))
        return fail(QStringLiteral("时间与能量预算字段无效"));
    for (auto it = budget.begin(); it != budget.end(); ++it)
        if (!number(it.value()))
            return fail(QStringLiteral("预算必须为非负有限数值"));
    if (budget["time_s"].toDouble() <= budget["reserve_time_s"].toDouble() ||
        budget["energy_wh"].toDouble() <= budget["reserve_energy_wh"].toDouble())
        return fail(QStringLiteral("预算必须大于返航储备"));
    const auto point = [&area](const QJsonValue& v) {
        if (!v.isArray() || v.toArray().size() != 2)
            return false;
        const auto p = v.toArray();
        return number(p[0], area["north_min_m"].toDouble(), area["north_max_m"].toDouble()) &&
               number(p[1], area["east_min_m"].toDouble(), area["east_max_m"].toDouble());
    };
    if (!point(doc["home"]))
        return fail(QStringLiteral("返航点必须是作业范围内的 [北向米, 东向米]"));
    Keys refs;
    for (const QString& kind : {QString("places"), QString("zones"), QString("routes")}) {
        if (!doc[kind].isObject())
            return fail(QStringLiteral("地点、区域和路线必须是对象"));
        const auto entries = doc[kind].toObject();
        const QString prefix = kind == "places" ? "poi:" : kind == "zones" ? "zone:" : "route:";
        for (auto it = entries.begin(); it != entries.end(); ++it) {
            const QString name = it.key();
            if (!name.startsWith(prefix) || name.size() == prefix.size() || name == "zone:all")
                return fail(QStringLiteral("机载引用名称或保留名称无效：%1").arg(name));
            refs.insert(name);
            if (kind == "places") {
                if (!point(it.value()))
                    return fail(QStringLiteral("地点超出作业区域：%1").arg(name));
            } else if (kind == "routes") {
                if (!it.value().isArray() || it.value().toArray().isEmpty())
                    return fail(QStringLiteral("路线需要航点：%1").arg(name));
                for (const auto& p : it.value().toArray())
                    if (!point(p))
                        return fail(QStringLiteral("路线航点无效：%1").arg(name));
            } else {
                const auto zone = it.value().toObject();
                if (!object(it.value(), {"centre_ne", "radius_m"}, {"risk_tag", "oversight_required"}) ||
                    !point(zone["centre_ne"]) || !number(zone["radius_m"], 0.001) ||
                    (zone.contains("risk_tag") && !zone["risk_tag"].isString()) ||
                    (zone.contains("oversight_required") && !zone["oversight_required"].isBool()))
                    return fail(QStringLiteral("区域字段无效：%1").arg(name));
                const auto centre = zone["centre_ne"].toArray();
                const double n = centre[0].toDouble(), e = centre[1].toDouble(), r = zone["radius_m"].toDouble();
                if (n - r < area["north_min_m"].toDouble() || n + r > area["north_max_m"].toDouble() ||
                    e - r < area["east_min_m"].toDouble() || e + r > area["east_max_m"].toDouble())
                    return fail(QStringLiteral("圆形区域超出作业范围：%1").arg(name));
            }
        }
    }
    if (doc.contains("ref_labels")) {
        if (!doc["ref_labels"].isObject())
            return fail(QStringLiteral("引用显示名称必须是对象"));
        const auto labels = doc["ref_labels"].toObject();
        for (auto it = labels.begin(); it != labels.end(); ++it)
            if (!refs.contains(it.key()) || !it.value().isString())
                return fail(QStringLiteral("显示名称必须引用已有机载地点"));
    }
    if (!doc["oversight_steps"].isArray())
        return fail(QStringLiteral("监督步骤必须为步骤编号数组"));
    for (const auto& v : doc["oversight_steps"].toArray())
        if (!v.isString())
            return fail(QStringLiteral("监督步骤编号必须是文字"));
    if (doc.contains("plan")) {
        const auto plan = doc["plan"].toObject();
        if (!object(doc["plan"], {"plan_version", "goal", "steps", "report"}) || !integer(plan["plan_version"], 1, 1) ||
            !text(plan["goal"]) || textLength(plan["goal"].toString().trimmed()) > 240 || !plan["steps"].isArray() ||
            plan["steps"].toArray().size() < 1 || plan["steps"].toArray().size() > 12 || !plan["report"].isArray() ||
            plan["report"].toArray().size() > 16)
            return fail(QStringLiteral("机载步骤计划结构无效"));
        for (const auto& v : plan["report"].toArray())
            if (!text(v))
                return fail(QStringLiteral("报告项必须为非空文字"));
        QHash<QString, QJsonObject> steps;
        bool terminal = false;
        for (const auto& value : plan["steps"].toArray()) {
            auto step = value.toObject();
            const QString id = step["id"].toString().trimmed(), skillName = step["skill"].toString().trimmed();
            if (!object(value, {"id", "skill", "params", "on_success", "on_fail"}) || !text(step["id"]) ||
                textLength(id) > 32 || steps.contains(id) || !skills.contains(skillName))
                return fail(QStringLiteral("步骤编号或技能无效（投送需要机载能力授权）"));
            step["id"] = id;
            step["skill"] = skillName;
            const auto spec = skills.value(skillName);
            const auto paramKeys = spec.params.keys();
            const Keys allowed(paramKeys.begin(), paramKeys.end());
            if (!object(step["params"], spec.required, allowed - spec.required))
                return fail(QStringLiteral("步骤参数有缺失或未知字段：%1").arg(id));
            const auto params = step["params"].toObject();
            for (auto it = params.begin(); it != params.end(); ++it) {
                if (!param(it.value(), spec.params.value(it.key())))
                    return fail(QStringLiteral("步骤参数类型或范围无效：%1.%2").arg(id, it.key()));
                if (it.key() == "altitude_m" &&
                    !number(it.value(), area["min_altitude_m"].toDouble(), area["max_altitude_m"].toDouble()))
                    return fail(QStringLiteral("步骤高度超出任务包限制：%1").arg(id));
                if (spec.params.value(it.key()) != "ref")
                    continue;
                const QString ref = it.value().toString().trimmed(), key = it.key();
                if (key == "region" && ref != "zone:all" && !doc["zones"].toObject().contains(ref))
                    return fail(QStringLiteral("区域引用不存在：%1").arg(ref));
                if (key == "region" && ref == "zone:all" && (skillName == "survey" || skillName == "monitor"))
                    return fail(QStringLiteral("测绘与监控需要命名区域"));
                if (key == "route_ref" && !doc["routes"].toObject().contains(ref))
                    return fail(QStringLiteral("路线引用不存在"));
                if (key == "track_ref" && !ref.startsWith("track:"))
                    return fail(QStringLiteral("跟踪引用必须为 track:"));
                if (ref == "home" || (key == "region" && ref == "zone:all"))
                    continue;
                if (ref.startsWith("finding:") || ref.startsWith("track:")) {
                    const QString index = ref.section(':', 1);
                    if (!QRegularExpression("^[0-9]+$").match(index).hasMatch() ||
                        QRegularExpression("^0+$").match(index).hasMatch() ||
                        (ref.startsWith("track:") && key != "track_ref"))
                        return fail(QStringLiteral("动态引用无效：%1").arg(ref));
                } else if (!refs.contains(ref) || (key == "target_ref" && doc["routes"].toObject().contains(ref)))
                    return fail(QStringLiteral("位置引用无效：%1").arg(ref));
            }
            for (const QString& key : {QString("on_success"), QString("on_fail")})
                if (!step[key].isNull() && !text(step[key]))
                    return fail(QStringLiteral("后继步骤必须为编号或空值"));
            terminal = terminal || skillName == "return_home" || skillName == "land" ||
                       (skillName == "hover" && step["on_success"].isNull());
            steps.insert(id, step);
        }
        if (!terminal)
            return fail(QStringLiteral("计划需要返航、降落或最终悬停步骤"));
        for (const auto& step : steps)
            for (const QString& key : {QString("on_success"), QString("on_fail")})
                if (!step[key].isNull() && !steps.contains(step[key].toString()))
                    return fail(QStringLiteral("计划引用未知后继步骤"));
        for (const auto& sid : doc["oversight_steps"].toArray())
            if (!steps.contains(sid.toString()))
                return fail(QStringLiteral("监督步骤引用未知步骤"));
        Keys visiting, done;
        std::function<bool(const QString&)> visit = [&](const QString& id) {
            if (done.contains(id))
                return true;
            if (visiting.contains(id)) {
                for (const auto& sid : visiting)
                    if (steps[sid]["skill"] == "patrol")
                        return true;
                return false;
            }
            visiting.insert(id);
            for (const QString& key : {QString("on_success"), QString("on_fail")})
                if (!steps[id][key].isNull() && !visit(steps[id][key].toString()))
                    return false;
            visiting.remove(id);
            done.insert(id);
            return true;
        };
        for (const auto& id : steps.keys())
            if (!visit(id))
                return fail(QStringLiteral("步骤循环需要显式巡逻次数"));
    }
    if (error)
        error->clear();
    return true;
}
