#include "MultiDetectMissionWire.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QDataStream>
#include <QtCore/QIODevice>
#include <QtCore/QStringDecoder>
#include <algorithm>
#include <cmath>

namespace {
bool fail(QString* error)
{
    if (error)
        *error = QStringLiteral("任务协议消息长度、编号或数值无效");
    return false;
}
}  // namespace

bool MultiDetectMissionWire::decode(int type, const QByteArray& body, QVariantMap* fields, QString* error)
{
    if (!fields || body.size() > 92)
        return fail(error);
    fields->clear();
    QDataStream in(body);
    in.setByteOrder(QDataStream::BigEndian);
    in.setFloatingPointPrecision(QDataStream::DoublePrecision);
    quint64 token = 0, mission = 0;
    quint16 index = 0, count = 0;
    quint32 total = 0;
    quint8 requestType = 0, accepted = 0, policy = 0;
    if (type == 28 || type == 35 || type == 37) {
        if (body.size() <= 48)
            return fail(error);
        in >> token >> index >> count >> total;
        const quint32 maximum = type == 37 ? 8192u : static_cast<quint32>(kMaximumDocumentBytes);
        if (!token || total == 0 || total > maximum || count != (total + kChunkBytes - 1) / kChunkBytes ||
            index >= count || body.size() - 48 != std::min<int>(kChunkBytes, total - index * kChunkBytes))
            return fail(error);
        fields->insert(QStringLiteral("token"), QVariant::fromValue(token));
        fields->insert(QStringLiteral("index"), index);
        fields->insert(QStringLiteral("count"), count);
        fields->insert(QStringLiteral("total"), total);
        fields->insert(QStringLiteral("digest"), body.mid(16, 32));
        fields->insert(QStringLiteral("data"), body.mid(48));
    } else if (type == 29) {
        if (body.size() < 12)
            return fail(error);
        in >> token >> requestType >> index >> accepted;
        if (!token || accepted > 1 || !(requestType == 28 || requestType == 37 || (requestType >= 30 && requestType <= 34)) ||
            (requestType != 28 && requestType != 37 && index != 0) || index >= (kMaximumDocumentBytes + kChunkBytes - 1) / kChunkBytes)
            return fail(error);
        QStringDecoder utf8(QStringDecoder::Utf8);
        const QString detail = utf8(body.mid(12));
        if (utf8.hasError())
            return fail(error);
        fields->insert(QStringLiteral("token"), QVariant::fromValue(token));
        fields->insert(QStringLiteral("requestType"), requestType);
        fields->insert(QStringLiteral("index"), index);
        fields->insert(QStringLiteral("accepted"), accepted != 0);
        fields->insert(QStringLiteral("detail"), detail);
    } else if (type >= 30 && type <= 34) {
        if (body.size() != (type == 33 ? 25 : 16))
            return fail(error);
        in >> mission >> token;
        if (!mission || !token)
            return fail(error);
        fields->insert(QStringLiteral("missionToken"), QVariant::fromValue(mission));
        fields->insert(QStringLiteral("token"), QVariant::fromValue(token));
        if (type == 33) {
            double timeout = 0;
            in >> policy >> timeout;
            if (policy > 4 || !std::isfinite(timeout) || timeout < 0)
                return fail(error);
            fields->insert(QStringLiteral("policyIndex"), policy);
            fields->insert(QStringLiteral("timeout"), timeout);
        }
    } else if (type == 36) {
        if (body.size() != 10)
            return fail(error);
        in >> token >> index;
        if (!token || index >= (kMaximumDocumentBytes + kChunkBytes - 1) / kChunkBytes)
            return fail(error);
        fields->insert(QStringLiteral("token"), QVariant::fromValue(token));
        fields->insert(QStringLiteral("index"), index);
    } else
        return fail(error);
    if (in.status() != QDataStream::Ok)
        return fail(error);
    fields->insert(QStringLiteral("body"), body);
    if (error)
        error->clear();
    return true;
}

QByteArray MultiDetectMissionWire::chunk(quint64 token, int index, const QByteArray& document)
{
    QByteArray body;
    QDataStream out(&body, QIODevice::WriteOnly);
    out.setByteOrder(QDataStream::BigEndian);
    out << token << quint16(index) << quint16((document.size() + kChunkBytes - 1) / kChunkBytes)
        << quint32(document.size());
    body.append(QCryptographicHash::hash(document, QCryptographicHash::Sha256));
    body.append(document.mid(index * kChunkBytes, kChunkBytes));
    return body;
}

QByteArray MultiDetectMissionWire::command(quint64 mission, quint64 request, int policy, double timeout)
{
    QByteArray body;
    QDataStream out(&body, QIODevice::WriteOnly);
    out.setByteOrder(QDataStream::BigEndian);
    out.setFloatingPointPrecision(QDataStream::DoublePrecision);
    out << mission << request;
    if (policy >= 0)
        out << quint8(policy) << timeout;
    return body;
}

QByteArray MultiDetectMissionWire::telemetryAck(quint64 token, int index)
{
    QByteArray body;
    QDataStream out(&body, QIODevice::WriteOnly);
    out.setByteOrder(QDataStream::BigEndian);
    out << token << quint16(index);
    return body;
}
