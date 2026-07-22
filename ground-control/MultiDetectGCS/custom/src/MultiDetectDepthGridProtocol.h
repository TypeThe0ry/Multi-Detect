#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QHash>
#include <QtCore/QString>
#include <QtCore/QtTypes>

#include <optional>

struct MultiDetectDepthGridFrame
{
    quint32 sequence = 0;
    quint64 sentAtMs = 0;
    quint16 width = 0;
    quint16 height = 0;
    double minimumDepthM = 0.0;
    double maximumDepthM = 0.0;
    bool logarithmicEncoding = false;
    QByteArray quantizedDepth;
};

class MultiDetectDepthGridProtocol
{
public:
    explicit MultiDetectDepthGridProtocol(const QByteArray& hmacKey = {});

    bool configured() const { return _hmacKey.size() >= 32; }
    void reset();

    std::optional<MultiDetectDepthGridFrame> ingest(const QByteArray& datagram, quint64 receivedAtMs,
                                                    QString* error = nullptr);

private:
    struct Fragment
    {
        quint32 sequence = 0;
        quint64 sentAtMs = 0;
        quint16 width = 0;
        quint16 height = 0;
        quint16 index = 0;
        quint16 count = 0;
        quint32 minimumDepthMm = 0;
        quint32 maximumDepthMm = 0;
        bool logarithmicEncoding = false;
        quint32 uncompressedSize = 0;
        quint32 compressedSize = 0;
        quint32 rawCrc32 = 0;
        QByteArray payload;
    };

    bool _decode(const QByteArray& datagram, Fragment* fragment, QString* error) const;
    static quint32 _crc32(const QByteArray& bytes);
    static bool _newerSequence(quint32 candidate, quint32 reference);
    static void _setError(QString* error, const QString& value);

    QByteArray _hmacKey;
    quint32 _pendingSequence = 0;
    bool _pendingSequenceValid = false;
    quint64 _pendingFirstAtMs = 0;
    Fragment _pendingMetadata;
    QHash<int, QByteArray> _pendingFragments;
};
