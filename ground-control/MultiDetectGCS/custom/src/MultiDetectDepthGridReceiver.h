#pragma once

#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtCore/QTimer>

#include "MultiDetectDepthGridProtocol.h"

class QUdpSocket;

class MultiDetectDepthGridReceiver final : public QObject
{
    Q_OBJECT

public:
    explicit MultiDetectDepthGridReceiver(const QByteArray& hmacKey, QObject* parent = nullptr);

    bool start(quint16 port, const QString& expectedHost, quint16 remotePort);
    void close();
    void reset() { _protocol.reset(); }
    QString lastError() const { return _lastError; }

signals:
    void frameReady(const MultiDetectDepthGridFrame& frame, quint64 receivedAtMs);

private slots:
    void _readyRead();
    void _sendKeepalive();

private:
    MultiDetectDepthGridProtocol _protocol;
    QUdpSocket* _socket = nullptr;
    QString _expectedHost;
    quint16 _remotePort = 0;
    QString _lastError;
    QTimer _keepaliveTimer;
};
