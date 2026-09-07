#include "MultiDetectTargetEventReplayStore.h"

#include <QtCore/QByteArray>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QDirIterator>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonParseError>
#include <QtCore/QJsonValue>
#include <QtCore/QRegularExpression>
#include <QtCore/QSet>

#include <algorithm>
#include <cmath>
#include <limits>

namespace {

constexpr qint64 kMaximumManifestBytes = 4 * 1024 * 1024;
constexpr qint64 kMaximumEventStreamBytes = 64 * 1024 * 1024;
constexpr qint64 kMaximumMediaBytes = 512LL * 1024LL * 1024LL;
constexpr int kMaximumEvents = 100000;
constexpr int kMaximumDisplayedEvents = 512;
constexpr int kMaximumTextLength = 512;
constexpr double kMaximumExactJsonInteger = 9007199254740991.0;
constexpr double kMaximumGpsAgeS = 1.5;
constexpr double kMaximumHorizontalAccuracyM = 6.0;
constexpr double kMaximumVerticalAccuracyM = 10.0;

const QSet<QString> kMediaEncodings = {
    QStringLiteral("png"), QStringLiteral("jpeg"), QStringLiteral("jpg"),
    QStringLiteral("bmp"), QStringLiteral("npy"), QStringLiteral("raw"),
};

const QSet<QString> kManifestRequired = {
    QStringLiteral("schema"),       QStringLiteral("schema_version"),
    QStringLiteral("kind"),         QStringLiteral("bundle_id"),
    QStringLiteral("session_id"),   QStringLiteral("started_at_utc"),
    QStringLiteral("events"),       QStringLiteral("media"),
    QStringLiteral("claims"),       QStringLiteral("bundle_hash"),
};
const QSet<QString> kManifestOptional = {QStringLiteral("camera_calibration")};
const QSet<QString> kClaimsRequired = {
    QStringLiteral("read_only_evidence"), QStringLiteral("contains_flight_control"),
    QStringLiteral("contains_truth"),
};
const QSet<QString> kEventsRequired = {
    QStringLiteral("path"), QStringLiteral("sha256"), QStringLiteral("bytes"),
    QStringLiteral("count"),
};
const QSet<QString> kMediaRequired = {
    QStringLiteral("root"), QStringLiteral("count"), QStringLiteral("bytes"),
};
const QSet<QString> kCalibrationRequired = {
    QStringLiteral("path"), QStringLiteral("sha256"), QStringLiteral("bytes"),
    QStringLiteral("calibration_id"),
};
const QSet<QString> kEventRequired = {
    QStringLiteral("schema"),          QStringLiteral("schema_version"),
    QStringLiteral("kind"),            QStringLiteral("sequence"),
    QStringLiteral("event_id"),        QStringLiteral("frame_id"),
    QStringLiteral("target_id"),       QStringLiteral("target_label"),
    QStringLiteral("captured_at_s"),   QStringLiteral("gps"),
    QStringLiteral("ranging"),         QStringLiteral("video"),
    QStringLiteral("depth"),           QStringLiteral("source_frame_id"),
    QStringLiteral("vehicle_altitude_agl_m"),
    QStringLiteral("vehicle_heading_deg"),
};
const QSet<QString> kEventOptional = {QStringLiteral("gps_age_s"), QStringLiteral("gps_freshness")};
const QSet<QString> kGpsRequired = {
    QStringLiteral("status"),             QStringLiteral("latitude_deg"),
    QStringLiteral("longitude_deg"),       QStringLiteral("horizontal_sigma_m"),
    QStringLiteral("fix_type"),           QStringLiteral("satellites_visible"),
    QStringLiteral("horizontal_accuracy_m"),
    QStringLiteral("vertical_accuracy_m"),
};
const QSet<QString> kGpsOptional = {QStringLiteral("observed_at_s")};
const QSet<QString> kRangeRequired = {
    QStringLiteral("validity"), QStringLiteral("range_m"),
    QStringLiteral("relative_bearing_deg"), QStringLiteral("range_kind"),
};
const QSet<QString> kMediaReferenceRequired = {
    QStringLiteral("kind"), QStringLiteral("path"), QStringLiteral("sha256"),
    QStringLiteral("bytes"), QStringLiteral("encoding"),
};
const QSet<QString> kMediaReferenceOptional = {QStringLiteral("width"), QStringLiteral("height")};

bool exactKeys(const QJsonObject& object, const QSet<QString>& required,
               const QSet<QString>& optional, QString* error, const QString& name)
{
    const QStringList objectKeys = object.keys();
    const QSet<QString> keys(objectKeys.cbegin(), objectKeys.cend());
    for (const QString& key : required) {
        if (!keys.contains(key)) {
            *error = QStringLiteral("%1 is missing required field %2").arg(name, key);
            return false;
        }
    }
    for (const QString& key : keys) {
        if (!required.contains(key) && !optional.contains(key)) {
            *error = QStringLiteral("%1 contains unknown field %2").arg(name, key);
            return false;
        }
    }
    return true;
}

bool finiteNumber(const QJsonValue& value, double* result)
{
    if (!value.isDouble()) {
        return false;
    }
    const double number = value.toDouble();
    if (!std::isfinite(number)) {
        return false;
    }
    *result = number;
    return true;
}

bool optionalNumber(const QJsonObject& object, const QString& key, double minimum, double maximum,
                   double* result, QString* error, const QString& name)
{
    const QJsonValue value = object.value(key);
    if (value.isNull()) {
        return true;
    }
    if (!finiteNumber(value, result) || *result < minimum || *result > maximum) {
        *error = QStringLiteral("%1.%2 must be a finite number in [%3, %4]")
                     .arg(name, key)
                     .arg(minimum, 0, 'g', 16)
                     .arg(maximum, 0, 'g', 16);
        return false;
    }
    return true;
}

bool requiredNumber(const QJsonObject& object, const QString& key, double minimum, double maximum,
                   double* result, QString* error, const QString& name)
{
    const QJsonValue value = object.value(key);
    if (!finiteNumber(value, result) || *result < minimum || *result > maximum) {
        *error = QStringLiteral("%1.%2 must be a finite number in [%3, %4]")
                     .arg(name, key)
                     .arg(minimum, 0, 'g', 16)
                     .arg(maximum, 0, 'g', 16);
        return false;
    }
    return true;
}

bool requiredInteger(const QJsonObject& object, const QString& key, qint64 minimum, qint64 maximum,
                    qint64* result, QString* error, const QString& name)
{
    double number = 0.0;
    if (!requiredNumber(object, key, static_cast<double>(minimum), static_cast<double>(maximum),
                        &number, error, name) || std::floor(number) != number) {
        *error = QStringLiteral("%1.%2 must be an integer").arg(name, key);
        return false;
    }
    *result = static_cast<qint64>(number);
    return true;
}

bool boundedString(const QJsonObject& object, const QString& key, bool required, QString* result,
                  QString* error, const QString& name)
{
    const QJsonValue value = object.value(key);
    if (value.isNull() || value.isUndefined()) {
        if (required) {
            *error = QStringLiteral("%1.%2 must be a string").arg(name, key);
            return false;
        }
        result->clear();
        return true;
    }
    if (!value.isString()) {
        *error = QStringLiteral("%1.%2 must be a string").arg(name, key);
        return false;
    }
    *result = value.toString().trimmed();
    if (required && result->isEmpty()) {
        *error = QStringLiteral("%1.%2 must not be empty").arg(name, key);
        return false;
    }
    if (result->size() > kMaximumTextLength) {
        *error = QStringLiteral("%1.%2 is too long").arg(name, key);
        return false;
    }
    return true;
}

bool boolValue(const QJsonObject& object, const QString& key, bool expected, QString* error,
               const QString& name)
{
    const QJsonValue value = object.value(key);
    if (!value.isBool() || value.toBool() != expected) {
        *error = QStringLiteral("%1.%2 has an invalid boolean value").arg(name, key);
        return false;
    }
    return true;
}

bool sha256Text(const QJsonObject& object, const QString& key, QByteArray* result, QString* error,
                const QString& name)
{
    QString text;
    if (!boundedString(object, key, true, &text, error, name)) {
        return false;
    }
    const QByteArray encoded = text.toLatin1();
    if (encoded.size() != 64 || !QRegularExpression(QStringLiteral("^[0-9a-fA-F]{64}$"))
                                      .match(text)
                                      .hasMatch()) {
        *error = QStringLiteral("%1.%2 must be a SHA-256 hex string").arg(name, key);
        return false;
    }
    *result = encoded.toLower();
    return true;
}

bool validSessionId(QString* value, QString* error)
{
    const QString normalized = value->trimmed().toLower();
    static const QRegularExpression uuidPattern(
        QStringLiteral("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"));
    if (!uuidPattern.match(normalized).hasMatch()) {
        *error = QStringLiteral("manifest.session_id must be a valid UUID");
        return false;
    }
    *value = normalized;
    return true;
}

bool validAwareTimestamp(const QString& value)
{
    static const QRegularExpression timezoneSuffix(
        QStringLiteral("(?:Z|[+-][0-9]{2}:[0-9]{2})$"));
    if (!timezoneSuffix.match(value).hasMatch()) {
        return false;
    }
    const QDateTime parsedWithMs = QDateTime::fromString(value, Qt::ISODateWithMs);
    if (parsedWithMs.isValid()) {
        return true;
    }
    return QDateTime::fromString(value, Qt::ISODate).isValid();
}

// QJsonDocument accepts duplicate object keys and keeps the last value.  The
// replay contract is hash-bound, so the native reader performs a small lexical
// pass first and rejects duplicates before consuming the parsed document.
class JsonDuplicateScanner final
{
public:
    explicit JsonDuplicateScanner(const QByteArray& payload) : _payload(payload) {}

    bool check(QString* error)
    {
        _error = error;
        skipWhitespace();
        if (!parseValue()) {
            return false;
        }
        skipWhitespace();
        if (_position != _payload.size()) {
            *_error = QStringLiteral("trailing bytes after JSON value");
            return false;
        }
        return true;
    }

private:
    void skipWhitespace()
    {
        while (_position < _payload.size()) {
            const char c = _payload.at(_position);
            if (c != ' ' && c != '\t' && c != '\r' && c != '\n') {
                break;
            }
            ++_position;
        }
    }

    bool parseString(QString* decoded = nullptr)
    {
        if (_position >= _payload.size() || _payload.at(_position) != '"') {
            *_error = QStringLiteral("JSON string expected");
            return false;
        }
        const qsizetype start = _position;
        ++_position;
        bool escaped = false;
        while (_position < _payload.size()) {
            const char c = _payload.at(_position++);
            if (c == '\0' || (static_cast<unsigned char>(c) < 0x20 && !escaped)) {
                *_error = QStringLiteral("invalid control byte in JSON string");
                return false;
            }
            if (!escaped && c == '"') {
                if (decoded != nullptr) {
                    const QByteArray token = _payload.mid(start, _position - start);
                    QJsonParseError parseError;
                    const QJsonDocument wrapper =
                        QJsonDocument::fromJson(QByteArray("[") + token + QByteArray("]"),
                                                &parseError);
                    if (parseError.error != QJsonParseError::NoError || !wrapper.isArray() ||
                        wrapper.array().size() != 1 || !wrapper.array().at(0).isString()) {
                        *_error = QStringLiteral("invalid JSON string");
                        return false;
                    }
                    *decoded = wrapper.array().at(0).toString();
                }
                return true;
            }
            if (escaped) {
                if (c == 'u') {
                    if (_position + 4 > _payload.size()) {
                        *_error = QStringLiteral("truncated unicode escape");
                        return false;
                    }
                    for (int index = 0; index < 4; ++index) {
                        const char hex = _payload.at(_position + index);
                        if (!QRegularExpression(QStringLiteral("^[0-9a-fA-F]$"))
                                 .match(QString(QChar::fromLatin1(hex)))
                                 .hasMatch()) {
                            *_error = QStringLiteral("invalid unicode escape");
                            return false;
                        }
                    }
                    _position += 4;
                } else if (QByteArray("\"\\/bfnrt").contains(c)) {
                    // Valid one-byte JSON escape.
                } else {
                    *_error = QStringLiteral("invalid JSON escape");
                    return false;
                }
                escaped = false;
            } else if (c == '\\') {
                escaped = true;
            }
        }
        *_error = QStringLiteral("unterminated JSON string");
        return false;
    }

    bool parseObject()
    {
        ++_position; // '{'
        skipWhitespace();
        QSet<QString> keys;
        if (_position < _payload.size() && _payload.at(_position) == '}') {
            ++_position;
            return true;
        }
        while (_position < _payload.size()) {
            QString key;
            if (!parseString(&key)) {
                return false;
            }
            if (keys.contains(key)) {
                *_error = QStringLiteral("duplicate JSON object key %1").arg(key);
                return false;
            }
            keys.insert(key);
            skipWhitespace();
            if (_position >= _payload.size() || _payload.at(_position++) != ':') {
                *_error = QStringLiteral("JSON object colon expected");
                return false;
            }
            skipWhitespace();
            if (!parseValue()) {
                return false;
            }
            skipWhitespace();
            if (_position >= _payload.size()) {
                break;
            }
            const char delimiter = _payload.at(_position++);
            if (delimiter == '}') {
                return true;
            }
            if (delimiter != ',') {
                *_error = QStringLiteral("JSON object delimiter expected");
                return false;
            }
            skipWhitespace();
        }
        *_error = QStringLiteral("unterminated JSON object");
        return false;
    }

    bool parseArray()
    {
        ++_position; // '['
        skipWhitespace();
        if (_position < _payload.size() && _payload.at(_position) == ']') {
            ++_position;
            return true;
        }
        while (_position < _payload.size()) {
            if (!parseValue()) {
                return false;
            }
            skipWhitespace();
            if (_position >= _payload.size()) {
                break;
            }
            const char delimiter = _payload.at(_position++);
            if (delimiter == ']') {
                return true;
            }
            if (delimiter != ',') {
                *_error = QStringLiteral("JSON array delimiter expected");
                return false;
            }
            skipWhitespace();
        }
        *_error = QStringLiteral("unterminated JSON array");
        return false;
    }

    bool parseValue()
    {
        skipWhitespace();
        if (_position >= _payload.size()) {
            *_error = QStringLiteral("JSON value expected");
            return false;
        }
        const char c = _payload.at(_position);
        if (c == '{') {
            return parseObject();
        }
        if (c == '[') {
            return parseArray();
        }
        if (c == '"') {
            return parseString();
        }
        if (_payload.mid(_position, 4) == QByteArrayLiteral("true") ||
            _payload.mid(_position, 5) == QByteArrayLiteral("false") ||
            _payload.mid(_position, 4) == QByteArrayLiteral("null")) {
            if (_payload.mid(_position, 4) == QByteArrayLiteral("true") ||
                _payload.mid(_position, 4) == QByteArrayLiteral("null")) {
                _position += 4;
            } else {
                _position += 5;
            }
            return true;
        }
        // Let QJsonDocument perform the complete number grammar validation.
        // Here we only advance to the next structural delimiter.
        if (c == '-' || (c >= '0' && c <= '9')) {
            while (_position < _payload.size()) {
                const char numberByte = _payload.at(_position);
                if (!QByteArrayLiteral("0123456789+-.eE").contains(numberByte)) {
                    break;
                }
                ++_position;
            }
            return true;
        }
        *_error = QStringLiteral("invalid JSON value");
        return false;
    }

    const QByteArray& _payload;
    qsizetype _position = 0;
    QString* _error = nullptr;
};

bool parseStrictJson(const QByteArray& payload, QJsonDocument* document, QString* error)
{
    JsonDuplicateScanner scanner(payload);
    if (!scanner.check(error)) {
        return false;
    }
    QJsonParseError parseError;
    *document = QJsonDocument::fromJson(payload, &parseError);
    if (parseError.error != QJsonParseError::NoError || document->isNull()) {
        *error = QStringLiteral("invalid JSON: %1").arg(parseError.errorString());
        return false;
    }
    return true;
}

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
            std::abs(number) <= kMaximumExactJsonInteger) {
            return QByteArray::number(static_cast<qint64>(number));
        }
        return QString::number(number, 'g', 17).toUtf8();
    }
    if (value.isArray()) {
        QByteArray output("[");
        const QJsonArray array = value.toArray();
        for (int index = 0; index < array.size(); ++index) {
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
        for (int index = 0; index < keys.size(); ++index) {
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

bool readFileBounded(const QString& path, qint64 maximumBytes, QByteArray* payload, QString* error,
                    const QString& name)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        *error = QStringLiteral("unable to open %1: %2").arg(name, file.errorString());
        return false;
    }
    if (file.size() < 0 || file.size() > maximumBytes) {
        *error = QStringLiteral("%1 exceeds the %2-byte limit").arg(name).arg(maximumBytes);
        return false;
    }
    *payload = file.read(maximumBytes + 1);
    if (payload->size() != file.size()) {
        *error = QStringLiteral("unable to read %1 completely").arg(name);
        return false;
    }
    return true;
}

bool safeRelativePath(const QString& path)
{
    if (path.isEmpty() || path.contains(QChar('\\')) || path.contains(QChar('\0')) ||
        path.startsWith(QChar('/')) || path.contains(QChar(':'))) {
        return false;
    }
    const QStringList parts = path.split(QChar('/'), Qt::KeepEmptyParts);
    if (parts.isEmpty()) {
        return false;
    }
    for (const QString& part : parts) {
        if (part.isEmpty() || part == QStringLiteral(".") || part == QStringLiteral("..")) {
            return false;
        }
    }
    return true;
}

bool containedPath(const QString& rootPath, const QString& relativePath, bool directory,
                  QString* resolved, QString* error, const QString& name)
{
    if (!safeRelativePath(relativePath)) {
        *error = QStringLiteral("%1 path must be a safe relative POSIX path").arg(name);
        return false;
    }
    const QFileInfo rootInfo(rootPath);
    const QString canonicalRoot = rootInfo.canonicalFilePath();
    if (canonicalRoot.isEmpty()) {
        *error = QStringLiteral("bundle root is not a regular directory");
        return false;
    }
    const QString candidatePath = QDir(canonicalRoot).absoluteFilePath(relativePath);
    const QFileInfo candidateInfo(candidatePath);
    if (candidateInfo.isSymLink()) {
        *error = QStringLiteral("%1 path must not be a symlink").arg(name);
        return false;
    }
    const QString canonicalCandidate = candidateInfo.canonicalFilePath();
    if (canonicalCandidate.isEmpty()) {
        *error = QStringLiteral("%1 path does not exist").arg(name);
        return false;
    }
    const QString relative = QDir(canonicalRoot).relativeFilePath(canonicalCandidate);
    if (relative == QStringLiteral("..") || relative.startsWith(QStringLiteral("../")) ||
        relative.startsWith(QStringLiteral("..\\"))) {
        *error = QStringLiteral("%1 path escapes the bundle").arg(name);
        return false;
    }
    if (directory ? !candidateInfo.isDir() : !candidateInfo.isFile()) {
        *error = QStringLiteral("%1 path has the wrong file type").arg(name);
        return false;
    }
    *resolved = canonicalCandidate;
    return true;
}

bool verifyBoundFile(const QString& path, const QByteArray& expectedHash, qint64 expectedBytes,
                    qint64 maximumBytes, QString* error, const QString& name)
{
    QByteArray payload;
    if (!readFileBounded(path, maximumBytes, &payload, error, name)) {
        return false;
    }
    if (expectedBytes != payload.size()) {
        *error = QStringLiteral("%1 byte count mismatch").arg(name);
        return false;
    }
    if (sha256(payload) != expectedHash) {
        *error = QStringLiteral("%1 SHA-256 mismatch").arg(name);
        return false;
    }
    return true;
}

bool mediaReference(const QJsonValue& raw, const QString& expectedKind, const QString& rootPath,
                    QSet<QString>* referenced, qint64* mediaBytes, QVariantMap* summary,
                    QString* error, const QString& name)
{
    if (raw.isNull()) {
        summary->insert(QStringLiteral("present"), false);
        return true;
    }
    if (!raw.isObject()) {
        *error = QStringLiteral("%1 must be an object or null").arg(name);
        return false;
    }
    const QJsonObject object = raw.toObject();
    if (!exactKeys(object, kMediaReferenceRequired, kMediaReferenceOptional, error, name)) {
        return false;
    }
    QString kind;
    QString path;
    QString encoding;
    QByteArray expectedHash;
    qint64 expectedBytes = 0;
    if (!boundedString(object, QStringLiteral("kind"), true, &kind, error, name) ||
        kind != expectedKind || !boundedString(object, QStringLiteral("path"), true, &path, error, name) ||
        !safeRelativePath(path) || !path.startsWith(QStringLiteral("media/")) ||
        !sha256Text(object, QStringLiteral("sha256"), &expectedHash, error, name) ||
        !requiredInteger(object, QStringLiteral("bytes"), 1, 64 * 1024 * 1024, &expectedBytes,
                         error, name) ||
        !boundedString(object, QStringLiteral("encoding"), true, &encoding, error, name) ||
        !kMediaEncodings.contains(encoding.toLower())) {
        if (error->isEmpty()) {
            *error = QStringLiteral("%1 media reference is invalid").arg(name);
        }
        return false;
    }
    const QString relativePath = path;
    if (referenced->contains(relativePath)) {
        *error = QStringLiteral("duplicate media path %1").arg(relativePath);
        return false;
    }
    const QString mediaPath = QDir(rootPath).absoluteFilePath(relativePath);
    const QFileInfo mediaInfo(mediaPath);
    if (mediaInfo.isSymLink() || !mediaInfo.isFile()) {
        *error = QStringLiteral("media %1 is not a regular file").arg(relativePath);
        return false;
    }
    QString canonicalPath;
    if (!containedPath(rootPath, relativePath, false, &canonicalPath, error, name)) {
        return false;
    }
    if (!verifyBoundFile(canonicalPath, expectedHash, expectedBytes, 64 * 1024 * 1024, error,
                         QStringLiteral("media %1").arg(relativePath))) {
        return false;
    }
    double width = 0.0;
    double height = 0.0;
    const bool hasWidth = object.contains(QStringLiteral("width"));
    const bool hasHeight = object.contains(QStringLiteral("height"));
    if (hasWidth != hasHeight ||
        (hasWidth && (!requiredNumber(object, QStringLiteral("width"), 1.0, 32768.0, &width,
                                      error, name) ||
                      std::floor(width) != width)) ||
        (hasHeight && (!requiredNumber(object, QStringLiteral("height"), 1.0, 32768.0, &height,
                                       error, name) ||
                       std::floor(height) != height))) {
        *error = QStringLiteral("%1 media dimensions are invalid").arg(name);
        return false;
    }
    referenced->insert(relativePath);
    *mediaBytes += expectedBytes;
    summary->insert(QStringLiteral("present"), true);
    summary->insert(QStringLiteral("path"), relativePath);
    summary->insert(QStringLiteral("encoding"), encoding);
    summary->insert(QStringLiteral("bytes"), expectedBytes);
    return true;
}

bool verifyMediaDirectory(const QString& rootPath, const QString& mediaRoot,
                          const QSet<QString>& referenced, QString* error)
{
    QDirIterator iterator(mediaRoot, QDir::AllEntries | QDir::NoDotAndDotDot,
                          QDirIterator::Subdirectories);
    QSet<QString> actual;
    while (iterator.hasNext()) {
        const QString path = iterator.next();
        const QFileInfo info(path);
        if (info.isSymLink()) {
            *error = QStringLiteral("media directory cannot contain symlinks");
            return false;
        }
        if (info.isFile()) {
            const QString relative = QDir(rootPath).relativeFilePath(info.absoluteFilePath()).replace(
                QChar('\\'), QChar('/'));
            actual.insert(relative);
        } else if (!info.isDir()) {
            *error = QStringLiteral("media directory contains a non-regular entry");
            return false;
        }
    }
    if (actual != referenced) {
        *error = QStringLiteral("media directory contains unreferenced or missing files");
        return false;
    }
    return true;
}

bool gpsSummary(const QJsonValue& raw, double capturedAtS, QVariantMap* result, QString* error)
{
    if (!raw.isObject()) {
        *error = QStringLiteral("event gps must be an object");
        return false;
    }
    const QJsonObject gps = raw.toObject();
    if (!exactKeys(gps, kGpsRequired, kGpsOptional, error, QStringLiteral("gps"))) {
        return false;
    }
    QString status;
    if (!boundedString(gps, QStringLiteral("status"), true, &status, error, QStringLiteral("gps"))) {
        return false;
    }
    status = status.toLower();
    if (status != QStringLiteral("qualified") && status != QStringLiteral("unqualified") &&
        status != QStringLiteral("unavailable")) {
        *error = QStringLiteral("gps.status is invalid");
        return false;
    }
    double latitude = 0.0;
    double longitude = 0.0;
    double sigma = 0.0;
    double fix = 0.0;
    double satellites = 0.0;
    double horizontal = 0.0;
    double vertical = 0.0;
    double observedAt = 0.0;
    const bool hasLatitude = !gps.value(QStringLiteral("latitude_deg")).isNull();
    const bool hasLongitude = !gps.value(QStringLiteral("longitude_deg")).isNull();
    const bool hasSigma = !gps.value(QStringLiteral("horizontal_sigma_m")).isNull();
    const bool hasFix = !gps.value(QStringLiteral("fix_type")).isNull();
    const bool hasSatellites = !gps.value(QStringLiteral("satellites_visible")).isNull();
    const bool hasHorizontal = !gps.value(QStringLiteral("horizontal_accuracy_m")).isNull();
    const bool hasVertical = !gps.value(QStringLiteral("vertical_accuracy_m")).isNull();
    const bool hasObservedAt = gps.contains(QStringLiteral("observed_at_s")) &&
                               !gps.value(QStringLiteral("observed_at_s")).isNull();
    if (hasLatitude != hasLongitude ||
        (hasLatitude && (!requiredNumber(gps, QStringLiteral("latitude_deg"), -90.0, 90.0,
                                         &latitude, error, QStringLiteral("gps")) ||
                         !(latitude > -90.0 && latitude < 90.0))) ||
        (hasLongitude && !requiredNumber(gps, QStringLiteral("longitude_deg"), -180.0, 180.0,
                                         &longitude, error, QStringLiteral("gps"))) ||
        (hasSigma && !requiredNumber(gps, QStringLiteral("horizontal_sigma_m"), 0.0, 100000.0,
                                     &sigma, error, QStringLiteral("gps"))) ||
        (hasFix && (!requiredNumber(gps, QStringLiteral("fix_type"), 0.0, 8.0, &fix, error,
                                    QStringLiteral("gps")) ||
                    std::floor(fix) != fix)) ||
        (hasSatellites && (!requiredNumber(gps, QStringLiteral("satellites_visible"), 0.0, 255.0,
                                           &satellites, error, QStringLiteral("gps")) ||
                           std::floor(satellites) != satellites)) ||
        (hasHorizontal && !requiredNumber(gps, QStringLiteral("horizontal_accuracy_m"), 0.0,
                                          100000.0, &horizontal, error, QStringLiteral("gps"))) ||
        (hasVertical && !requiredNumber(gps, QStringLiteral("vertical_accuracy_m"), 0.0, 100000.0,
                                        &vertical, error, QStringLiteral("gps"))) ||
        (hasObservedAt && !requiredNumber(gps, QStringLiteral("observed_at_s"), 0.0,
                                           kMaximumExactJsonInteger, &observedAt, error,
                                           QStringLiteral("gps")))) {
        return false;
    }
    if (status == QStringLiteral("unavailable") &&
        (hasLatitude || hasLongitude || hasSigma || hasFix || hasSatellites || hasHorizontal ||
         hasVertical)) {
        *error = QStringLiteral("unavailable GPS evidence cannot contain measurements");
        return false;
    }
    if (status == QStringLiteral("qualified") &&
        (!hasLatitude || !hasSigma || !hasFix || fix < 3.0 || !hasSatellites || satellites < 6.0 ||
         satellites > 254.0 || !hasHorizontal || horizontal > kMaximumHorizontalAccuracyM ||
         !hasVertical || vertical > kMaximumVerticalAccuracyM)) {
        *error = QStringLiteral("qualified GPS evidence does not meet the quality gate");
        return false;
    }
    QString freshness = QStringLiteral("unknown");
    double age = 0.0;
    if (hasObservedAt) {
        age = capturedAtS - observedAt;
        if (age < 0.0) {
            *error = QStringLiteral("GPS observation is newer than target capture");
            return false;
        }
        freshness = age <= kMaximumGpsAgeS ? QStringLiteral("fresh") : QStringLiteral("stale");
        if (status == QStringLiteral("qualified") && freshness == QStringLiteral("stale")) {
            *error = QStringLiteral("qualified GPS evidence is stale");
            return false;
        }
    }
    result->insert(QStringLiteral("status"), status);
    result->insert(QStringLiteral("qualified"), status == QStringLiteral("qualified"));
    result->insert(QStringLiteral("freshness"), freshness);
    result->insert(QStringLiteral("coordinates_included"),
                   status == QStringLiteral("qualified") && hasLatitude && hasLongitude && hasSigma);
    if (hasLatitude && hasLongitude && hasSigma) {
        result->insert(QStringLiteral("latitude_deg"), latitude);
        result->insert(QStringLiteral("longitude_deg"), longitude);
        result->insert(QStringLiteral("horizontal_sigma_m"), sigma);
    }
    if (hasObservedAt) {
        result->insert(QStringLiteral("age_s"), age);
    }
    return true;
}

bool rangeSummary(const QJsonValue& raw, QVariantMap* result, QString* error)
{
    if (!raw.isObject()) {
        *error = QStringLiteral("event ranging must be an object");
        return false;
    }
    const QJsonObject range = raw.toObject();
    if (!exactKeys(range, kRangeRequired, {}, error, QStringLiteral("ranging"))) {
        return false;
    }
    QString validity;
    QString kind;
    if (!boundedString(range, QStringLiteral("validity"), true, &validity, error,
                       QStringLiteral("ranging")) ||
        !boundedString(range, QStringLiteral("range_kind"), true, &kind, error,
                       QStringLiteral("ranging"))) {
        return false;
    }
    validity = validity.toUpper();
    kind = kind.toLower();
    if (validity != QStringLiteral("VALID") && validity != QStringLiteral("DEGRADED") &&
        validity != QStringLiteral("UNAVAILABLE")) {
        *error = QStringLiteral("ranging.validity is invalid");
        return false;
    }
    if (kind != QStringLiteral("slant") && kind != QStringLiteral("ground") &&
        kind != QStringLiteral("unknown")) {
        *error = QStringLiteral("ranging.range_kind is invalid");
        return false;
    }
    const bool hasRange = !range.value(QStringLiteral("range_m")).isNull();
    const bool hasBearing = !range.value(QStringLiteral("relative_bearing_deg")).isNull();
    double rangeM = 0.0;
    double bearing = 0.0;
    if ((hasRange && !requiredNumber(range, QStringLiteral("range_m"), 0.0, 1000000.0, &rangeM,
                                     error, QStringLiteral("ranging"))) ||
        (hasBearing && !requiredNumber(range, QStringLiteral("relative_bearing_deg"), -360.0,
                                        360.0, &bearing, error, QStringLiteral("ranging"))) ||
        ((validity == QStringLiteral("VALID") || validity == QStringLiteral("DEGRADED")) &&
         !hasRange) ||
        (validity == QStringLiteral("UNAVAILABLE") && (hasRange || hasBearing))) {
        if (error->isEmpty()) {
            *error = QStringLiteral("ranging evidence is inconsistent");
        }
        return false;
    }
    result->insert(QStringLiteral("validity"), validity);
    result->insert(QStringLiteral("kind"), kind);
    result->insert(QStringLiteral("available"), validity != QStringLiteral("UNAVAILABLE"));
    if (hasRange) {
        result->insert(QStringLiteral("range_m"), rangeM);
    }
    if (hasBearing) {
        result->insert(QStringLiteral("bearing_deg"), bearing);
    }
    return true;
}

} // namespace

MultiDetectTargetEventReplayStore::MultiDetectTargetEventReplayStore(QObject* parent)
    : QObject(parent)
{
    _setStatus(tr("未加载回放"));
}

void MultiDetectTargetEventReplayStore::_clearState()
{
    const bool wasLoaded = _loaded;
    const bool hadIntegrity = _integrityVerified;
    const bool wasTruncated = _truncated;
    const bool hadManifestName = !_manifestName.isEmpty();
    const bool hadBundleId = !_bundleId.isEmpty();
    const bool hadSessionId = !_sessionId.isEmpty();
    const bool hadStartedAt = !_startedAtUtc.isEmpty();
    const bool hadEvents = !_events.isEmpty();
    const bool hadCounts = _eventCount != 0 || _mediaCount != 0;

    _loaded = false;
    _integrityVerified = false;
    _truncated = false;
    _manifestName.clear();
    _bundleId.clear();
    _sessionId.clear();
    _startedAtUtc.clear();
    _eventCount = 0;
    _mediaCount = 0;
    _events.clear();
    _lastError.clear();
    _setStatus(tr("未加载回放"));

    if (wasLoaded) emit loadedChanged();
    if (hadIntegrity) emit integrityVerifiedChanged();
    if (wasTruncated) emit truncatedChanged();
    if (hadManifestName) emit manifestNameChanged();
    if (hadBundleId) emit bundleIdChanged();
    if (hadSessionId) emit sessionIdChanged();
    if (hadStartedAt) emit startedAtUtcChanged();
    if (hadCounts) {
        emit eventCountChanged();
        emit mediaCountChanged();
        emit displayedEventCountChanged();
    }
    if (hadEvents) {
        emit eventsChanged();
        emit displayedEventCountChanged();
    }
    emit lastErrorChanged();
}

void MultiDetectTargetEventReplayStore::clear()
{
    _clearState();
}

bool MultiDetectTargetEventReplayStore::_fail(const QString& message)
{
    _clearState();
    _lastError = message;
    _setStatus(tr("回放校验失败"));
    emit lastErrorChanged();
    return false;
}

void MultiDetectTargetEventReplayStore::_setStatus(const QString& message)
{
    if (_statusText == message) {
        return;
    }
    _statusText = message;
    emit statusTextChanged();
}

bool MultiDetectTargetEventReplayStore::loadManifest(const QString& path)
{
    _clearState();
    const QFileInfo requested(path);
    if (path.trimmed().isEmpty() || !requested.isAbsolute() || requested.isSymLink() ||
        !requested.isFile()) {
        return _fail(tr("manifest 必须是绝对路径下的普通文件"));
    }
    const QString manifestPath = requested.canonicalFilePath();
    const QString rootPath = QFileInfo(manifestPath).canonicalPath();
    if (manifestPath.isEmpty() || rootPath.isEmpty()) {
        return _fail(tr("manifest 路径无法解析"));
    }
    QByteArray manifestBytes;
    QString error;
    if (!readFileBounded(manifestPath, kMaximumManifestBytes, &manifestBytes, &error,
                         QStringLiteral("target replay manifest"))) {
        return _fail(error);
    }
    QJsonDocument document;
    if (!parseStrictJson(manifestBytes, &document, &error) || !document.isObject()) {
        return _fail(error.isEmpty() ? tr("manifest 必须是 JSON 对象") : error);
    }
    const QJsonObject manifest = document.object();
    if (!exactKeys(manifest, kManifestRequired, kManifestOptional, &error,
                   QStringLiteral("manifest"))) {
        return _fail(error);
    }
    if (manifest.value(QStringLiteral("schema")).toString() !=
            QStringLiteral("multidetect.target-event-replay.v1") ||
        manifest.value(QStringLiteral("kind")).toString() !=
            QStringLiteral("target_event_replay_manifest") ||
        manifest.value(QStringLiteral("schema_version")).toInteger() != 1) {
        return _fail(tr("不是支持的 target-event-replay v1 manifest"));
    }
    const QJsonValue claimsValue = manifest.value(QStringLiteral("claims"));
    if (!claimsValue.isObject() ||
        !exactKeys(claimsValue.toObject(), kClaimsRequired, {}, &error, QStringLiteral("claims")) ||
        !boolValue(claimsValue.toObject(), QStringLiteral("read_only_evidence"), true, &error,
                   QStringLiteral("claims")) ||
        !boolValue(claimsValue.toObject(), QStringLiteral("contains_flight_control"), false,
                   &error, QStringLiteral("claims")) ||
        !boolValue(claimsValue.toObject(), QStringLiteral("contains_truth"), false, &error,
                   QStringLiteral("claims"))) {
        return _fail(error.isEmpty() ? tr("claims 不是只读证据") : error);
    }
    QJsonObject withoutHash = manifest;
    const QJsonValue expectedBundleHashValue = withoutHash.take(QStringLiteral("bundle_hash"));
    QByteArray expectedBundleHash;
    if (!expectedBundleHashValue.isString() ||
        !QRegularExpression(QStringLiteral("^[0-9a-fA-F]{64}$"))
             .match(expectedBundleHashValue.toString())
             .hasMatch()) {
        return _fail(tr("bundle_hash 无效"));
    }
    expectedBundleHash = expectedBundleHashValue.toString().toLatin1().toLower();
    if (sha256(canonicalJson(withoutHash)) != expectedBundleHash) {
        return _fail(tr("manifest bundle_hash 不匹配"));
    }

    QString bundleId;
    QString sessionId;
    QString startedAtUtc;
    if (!boundedString(manifest, QStringLiteral("bundle_id"), true, &bundleId, &error,
        QStringLiteral("manifest")) ||
        !boundedString(manifest, QStringLiteral("session_id"), true, &sessionId, &error,
        QStringLiteral("manifest")) ||
        !boundedString(manifest, QStringLiteral("started_at_utc"), true, &startedAtUtc, &error,
        QStringLiteral("manifest"))) {
        return _fail(error);
    }
    if (!validSessionId(&sessionId, &error)) {
        return _fail(error);
    }
    if (!validAwareTimestamp(startedAtUtc)) {
        return _fail(tr("manifest.started_at_utc 必须包含有效时区"));
    }
    const QJsonObject eventsObject = manifest.value(QStringLiteral("events")).toObject();
    if (!exactKeys(eventsObject, kEventsRequired, {}, &error, QStringLiteral("events"))) {
        return _fail(error);
    }
    if (eventsObject.value(QStringLiteral("path")).toString() != QStringLiteral("events.jsonl")) {
        return _fail(tr("events.path 必须是 events.jsonl"));
    }
    QByteArray expectedEventsHash;
    qint64 expectedEventsBytes = 0;
    qint64 expectedEventCount = 0;
    if (!sha256Text(eventsObject, QStringLiteral("sha256"), &expectedEventsHash, &error,
                    QStringLiteral("events")) ||
        !requiredInteger(eventsObject, QStringLiteral("bytes"), 1, kMaximumEventStreamBytes,
                         &expectedEventsBytes, &error, QStringLiteral("events")) ||
        !requiredInteger(eventsObject, QStringLiteral("count"), 1, kMaximumEvents,
                         &expectedEventCount, &error, QStringLiteral("events"))) {
        return _fail(error);
    }
    QString eventsPath;
    if (!containedPath(rootPath, QStringLiteral("events.jsonl"), false, &eventsPath, &error,
                       QStringLiteral("events"))) {
        return _fail(error);
    }
    if (!verifyBoundFile(eventsPath, expectedEventsHash, expectedEventsBytes,
                         kMaximumEventStreamBytes, &error, QStringLiteral("target event stream"))) {
        return _fail(error);
    }
    const QJsonObject mediaObject = manifest.value(QStringLiteral("media")).toObject();
    if (!exactKeys(mediaObject, kMediaRequired, {}, &error, QStringLiteral("media"))) {
        return _fail(error);
    }
    if (mediaObject.value(QStringLiteral("root")).toString() != QStringLiteral("media")) {
        return _fail(tr("media.root 必须是 media"));
    }
    qint64 expectedMediaCount = 0;
    qint64 expectedMediaBytes = 0;
    if (!requiredInteger(mediaObject, QStringLiteral("count"), 0, 200000, &expectedMediaCount,
                         &error, QStringLiteral("media")) ||
        !requiredInteger(mediaObject, QStringLiteral("bytes"), 0, kMaximumMediaBytes,
                         &expectedMediaBytes, &error, QStringLiteral("media"))) {
        return _fail(error);
    }
    QString mediaRoot;
    if (!containedPath(rootPath, QStringLiteral("media"), true, &mediaRoot, &error,
                       QStringLiteral("media.root"))) {
        return _fail(error);
    }

    if (manifest.contains(QStringLiteral("camera_calibration"))) {
        const QJsonValue calibrationValue = manifest.value(QStringLiteral("camera_calibration"));
        if (!calibrationValue.isObject() ||
            !exactKeys(calibrationValue.toObject(), kCalibrationRequired, {}, &error,
                       QStringLiteral("camera_calibration")) ||
            calibrationValue.toObject().value(QStringLiteral("path")).toString() !=
                QStringLiteral("camera-calibration.json")) {
            return _fail(error.isEmpty() ? tr("camera_calibration 无效") : error);
        }
        QByteArray calibrationHash;
        qint64 calibrationBytes = 0;
        if (!sha256Text(calibrationValue.toObject(), QStringLiteral("sha256"), &calibrationHash,
                        &error, QStringLiteral("camera_calibration")) ||
            !requiredInteger(calibrationValue.toObject(), QStringLiteral("bytes"), 1,
                             kMaximumManifestBytes, &calibrationBytes, &error,
                             QStringLiteral("camera_calibration"))) {
            return _fail(error);
        }
        QString calibrationPath;
        if (!containedPath(rootPath, QStringLiteral("camera-calibration.json"), false,
                           &calibrationPath, &error, QStringLiteral("camera_calibration")) ||
            !verifyBoundFile(calibrationPath, calibrationHash, calibrationBytes,
                             kMaximumManifestBytes, &error, QStringLiteral("camera calibration"))) {
            return _fail(error);
        }
    }

    QByteArray eventsBytes;
    if (!readFileBounded(eventsPath, kMaximumEventStreamBytes, &eventsBytes, &error,
                         QStringLiteral("target event stream"))) {
        return _fail(error);
    }
    QList<QByteArray> lines = eventsBytes.split('\n');
    if (!lines.isEmpty() && lines.constLast().isEmpty()) {
        lines.removeLast();
    }
    if (lines.size() != expectedEventCount) {
        return _fail(tr("事件数量不匹配"));
    }
    QSet<QString> eventIds;
    QSet<QString> referencedMedia;
    qint64 observedMediaBytes = 0;
    QVariantList eventSummaries;
    eventSummaries.reserve(qMin<int>(lines.size(), kMaximumDisplayedEvents));
    double previousCapturedAt = -1.0;
    double previousGpsObservedAt = -1.0;
    bool hasPreviousGpsObservedAt = false;
    for (int index = 0; index < lines.size(); ++index) {
        const QByteArray line = lines.at(index).trimmed();
        if (line.isEmpty()) {
            return _fail(tr("事件流包含空行"));
        }
        QJsonDocument eventDocument;
        if (!parseStrictJson(line, &eventDocument, &error) || !eventDocument.isObject()) {
            return _fail(error.isEmpty() ? tr("事件流 JSON 无效") : error);
        }
        const QJsonObject event = eventDocument.object();
        if (!exactKeys(event, kEventRequired, kEventOptional, &error,
                       QStringLiteral("target event"))) {
            return _fail(error);
        }
        if (event.value(QStringLiteral("schema")).toString() !=
                QStringLiteral("multidetect.target-event.v1") ||
            event.value(QStringLiteral("kind")).toString() != QStringLiteral("target_event") ||
            event.value(QStringLiteral("schema_version")).toInteger() != 1) {
            return _fail(tr("事件 schema 不是 target-event v1"));
        }
        qint64 sequence = 0;
        double capturedAt = 0.0;
        QString eventId;
        QString frameId;
        QString targetId;
        QString targetLabel;
        if (!requiredInteger(event, QStringLiteral("sequence"), 0, kMaximumEvents - 1, &sequence,
                             &error, QStringLiteral("target event")) || sequence != index ||
            !requiredNumber(event, QStringLiteral("captured_at_s"), 0.0,
                            std::numeric_limits<double>::max(), &capturedAt, &error,
                            QStringLiteral("target event")) || capturedAt <= previousCapturedAt ||
            !boundedString(event, QStringLiteral("event_id"), true, &eventId, &error,
                           QStringLiteral("target event")) ||
            !boundedString(event, QStringLiteral("frame_id"), true, &frameId, &error,
                           QStringLiteral("target event")) ||
            !boundedString(event, QStringLiteral("target_id"), true, &targetId, &error,
                           QStringLiteral("target event")) ||
            !boundedString(event, QStringLiteral("target_label"), true, &targetLabel, &error,
                           QStringLiteral("target event"))) {
            return _fail(error.isEmpty() ? tr("事件字段无效或未按时间递增") : error);
        }
        if (eventIds.contains(eventId)) {
            return _fail(tr("事件 event_id 重复"));
        }
        eventIds.insert(eventId);
        previousCapturedAt = capturedAt;

        QVariantMap gps;
        if (!gpsSummary(event.value(QStringLiteral("gps")), capturedAt, &gps, &error)) {
            return _fail(error);
        }
        const QJsonObject gpsObject = event.value(QStringLiteral("gps")).toObject();
        if (gpsObject.contains(QStringLiteral("observed_at_s")) &&
            !gpsObject.value(QStringLiteral("observed_at_s")).isNull()) {
            double observedAt = 0.0;
            if (!requiredNumber(gpsObject, QStringLiteral("observed_at_s"), 0.0,
                                kMaximumExactJsonInteger, &observedAt, &error,
                                QStringLiteral("gps")) ||
                (hasPreviousGpsObservedAt && observedAt < previousGpsObservedAt)) {
                return _fail(error.isEmpty() ? tr("GPS 观测时间必须单调不回退") : error);
            }
            previousGpsObservedAt = observedAt;
            hasPreviousGpsObservedAt = true;
        }
        QVariantMap ranging;
        if (!rangeSummary(event.value(QStringLiteral("ranging")), &ranging, &error)) {
            return _fail(error);
        }
        QVariantMap video;
        QVariantMap depth;
        if (!mediaReference(event.value(QStringLiteral("video")), QStringLiteral("video_frame"),
                            rootPath, &referencedMedia, &observedMediaBytes, &video, &error,
                            QStringLiteral("video")) ||
            !mediaReference(event.value(QStringLiteral("depth")), QStringLiteral("depth_map"),
                            rootPath, &referencedMedia, &observedMediaBytes, &depth, &error,
                            QStringLiteral("depth"))) {
            return _fail(error);
        }
        if (!video.value(QStringLiteral("present")).toBool() &&
            !depth.value(QStringLiteral("present")).toBool()) {
            return _fail(tr("事件必须包含视频帧或深度图"));
        }
        if (event.contains(QStringLiteral("gps_age_s"))) {
            const QJsonValue declaredValue = event.value(QStringLiteral("gps_age_s"));
            const bool actualAgePresent = gps.contains(QStringLiteral("age_s"));
            if (declaredValue.isNull()) {
                if (actualAgePresent) {
                    return _fail(tr("gps_age_s 与 GPS 观测时间不匹配"));
                }
            } else {
                double declaredAge = 0.0;
                if (!finiteNumber(declaredValue, &declaredAge) || declaredAge < 0.0 ||
                    !actualAgePresent ||
                    std::abs(declaredAge - gps.value(QStringLiteral("age_s")).toDouble()) > 1.0e-9) {
                    return _fail(tr("gps_age_s 与 GPS 观测时间不匹配"));
                }
            }
        }
        if (event.contains(QStringLiteral("gps_freshness"))) {
            QString declaredFreshness;
            if (!boundedString(event, QStringLiteral("gps_freshness"), true, &declaredFreshness,
                               &error, QStringLiteral("target event")) ||
                declaredFreshness != gps.value(QStringLiteral("freshness")).toString()) {
                return _fail(error.isEmpty() ? tr("gps_freshness 与 GPS 观测时间不匹配") : error);
            }
        }
        QVariantMap summary;
        summary.insert(QStringLiteral("sequence"), sequence);
        summary.insert(QStringLiteral("event_id"), eventId);
        summary.insert(QStringLiteral("frame_id"), frameId);
        summary.insert(QStringLiteral("target_id"), targetId);
        summary.insert(QStringLiteral("target_label"), targetLabel);
        summary.insert(QStringLiteral("captured_at_s"), capturedAt);
        summary.insert(QStringLiteral("gps"), gps);
        summary.insert(QStringLiteral("ranging"), ranging);
        summary.insert(QStringLiteral("video"), video);
        summary.insert(QStringLiteral("depth"), depth);
        if (eventSummaries.size() < kMaximumDisplayedEvents) {
            eventSummaries.append(summary);
        }
    }
    if (referencedMedia.size() != expectedMediaCount || observedMediaBytes != expectedMediaBytes) {
        return _fail(tr("媒体数量或字节数不匹配"));
    }
    if (!verifyMediaDirectory(rootPath, mediaRoot, referencedMedia, &error)) {
        return _fail(error);
    }
    _loaded = true;
    _integrityVerified = true;
    _truncated = lines.size() > kMaximumDisplayedEvents;
    _manifestName = QFileInfo(manifestPath).fileName();
    _bundleId = bundleId;
    _sessionId = sessionId;
    _startedAtUtc = startedAtUtc;
    _eventCount = static_cast<int>(expectedEventCount);
    _mediaCount = static_cast<int>(expectedMediaCount);
    _events = eventSummaries;
    _lastError.clear();
    _setStatus(tr("已验证：清单、事件流与媒体 SHA-256 均通过"));
    emit loadedChanged();
    emit integrityVerifiedChanged();
    emit truncatedChanged();
    emit manifestNameChanged();
    emit bundleIdChanged();
    emit sessionIdChanged();
    emit startedAtUtcChanged();
    emit eventCountChanged();
    emit displayedEventCountChanged();
    emit mediaCountChanged();
    emit eventsChanged();
    emit lastErrorChanged();
    return true;
}
