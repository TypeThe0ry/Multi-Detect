#include "MultiDetectDepthGridReceiver.h"

#include <QtCore/QDateTime>
#include <QtNetwork/QHostAddress>
#include <QtNetwork/QUdpSocket>

MultiDetectDepthGridReceiver::MultiDetectDepthGridReceiver(const QByteArray& hmacKey, QObject* parent)
    : QObject(parent), _protocol(hmacKey)
{
    _keepaliveTimer.setInterval(1000);
    connect(&_keepaliveTimer, &QTimer::timeout, this, &MultiDetectDepthGridReceiver::_sendKeepalive);
}

bool MultiDetectDepthGridReceiver::start(quint16 port, const QString& expectedHost, quint16 remotePort)
{
    close();
    if (!_protocol.configured() || port < 1024 || remotePort < 1024 || port == remotePort ||
        expectedHost.trimmed().isEmpty()) {
        _lastError = QStringLiteral("invalid depth-grid receiver configuration");
        return false;
    }
    _expectedHost = expectedHost.trimmed();
    _remotePort = remotePort;
    _socket = new QUdpSocket(this);
    connect(_socket, &QUdpSocket::readyRead, this, &MultiDetectDepthGridReceiver::_readyRead);
    if (!_socket->bind(QHostAddress::AnyIPv4, port, QUdpSocket::ShareAddress | QUdpSocket::ReuseAddressHint)) {
        _lastError = _socket->errorString();
        _socket->deleteLater();
        _socket = nullptr;
        return false;
    }
    _lastError.clear();
    _keepaliveTimer.start();
    _sendKeepalive();
    return true;
}

void MultiDetectDepthGridReceiver::close()
{
    _keepaliveTimer.stop();
    if (_socket != nullptr) {
        _socket->close();
        _socket->deleteLater();
        _socket = nullptr;
    }
    _protocol.reset();
}

void MultiDetectDepthGridReceiver::_sendKeepalive()
{
    if (_socket == nullptr) {
        return;
    }
    const QHostAddress destination(_expectedHost);
    if (destination.isNull()) {
        return;
    }
    _socket->writeDatagram(QByteArrayLiteral("MDPD_HELLO_V1"), destination, _remotePort);
}

void MultiDetectDepthGridReceiver::_readyRead()
{
    if (_socket == nullptr) {
        return;
    }
    const QHostAddress expectedAddress(_expectedHost);
    while (_socket->hasPendingDatagrams()) {
        const qint64 pendingSize = _socket->pendingDatagramSize();
        if (pendingSize <= 0 || pendingSize > 65507) {
            QByteArray discarded(qMax<qint64>(pendingSize, 1), Qt::Uninitialized);
            _socket->readDatagram(discarded.data(), discarded.size());
            continue;
        }
        QByteArray datagram(pendingSize, Qt::Uninitialized);
        QHostAddress sender;
        quint16 senderPort = 0;
        if (_socket->readDatagram(datagram.data(), datagram.size(), &sender, &senderPort) != datagram.size()) {
            continue;
        }
        Q_UNUSED(senderPort)
        if (!expectedAddress.isNull() && sender != expectedAddress) {
            continue;
        }
        const quint64 nowMs = static_cast<quint64>(QDateTime::currentMSecsSinceEpoch());
        QString error;
        const std::optional<MultiDetectDepthGridFrame> frame = _protocol.ingest(datagram, nowMs, &error);
        if (frame.has_value()) {
            emit frameReady(*frame, nowMs);
        } else if (!error.isEmpty()) {
            _lastError = error;
        }
    }
}
