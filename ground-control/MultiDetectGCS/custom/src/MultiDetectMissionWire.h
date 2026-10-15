#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QString>
#include <QtCore/QVariantMap>

// v2 bodies use the existing authenticated MD envelope and TUNNEL 42000.
class MultiDetectMissionWire final
{
public:
    static constexpr int kChunkBytes = 44;
    static constexpr int kMaximumDocumentBytes = 65536;
    static bool decode(int type, const QByteArray& body, QVariantMap* fields, QString* error);
    static QByteArray chunk(quint64 token, int index, const QByteArray& document);
    static QByteArray command(quint64 mission, quint64 request, int policy = -1, double timeout = 0);
    static QByteArray telemetryAck(quint64 token, int index);
};
