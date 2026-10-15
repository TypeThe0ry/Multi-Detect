#pragma once

#include <QtCore/QElapsedTimer>
#include <QtCore/QHash>
#include <QtCore/QJsonObject>
#include <QtCore/QObject>
#include <QtCore/QTimer>
#include <QtCore/QUrl>
#include <QtCore/QVariantList>
#include <functional>

class MultiDetectMissionController final : public QObject
{
    Q_OBJECT
    Q_PROPERTY(QString packageJson READ packageJson WRITE setPackageJson NOTIFY packageChanged)
    Q_PROPERTY(bool valid READ valid NOTIFY packageChanged)
    Q_PROPERTY(QString validationMessage READ validationMessage NOTIFY packageChanged)
    Q_PROPERTY(QString uploadState READ uploadState NOTIFY stateChanged)
    Q_PROPERTY(double uploadProgress READ uploadProgress NOTIFY stateChanged)
    Q_PROPERTY(bool busy READ busy NOTIFY stateChanged)
    Q_PROPERTY(QString activeMissionId READ activeMissionId NOTIFY stateChanged)
    Q_PROPERTY(QVariantMap telemetry READ telemetry NOTIFY telemetryChanged)
    Q_PROPERTY(QVariantList logEntries READ logEntries NOTIFY telemetryChanged)
    Q_PROPERTY(bool telemetryFresh READ telemetryFresh NOTIFY telemetryChanged)
    Q_PROPERTY(QString currentPolicy READ currentPolicy NOTIFY telemetryChanged)
    Q_PROPERTY(double currentPolicyTimeout READ currentPolicyTimeout NOTIFY telemetryChanged)

public:
    explicit MultiDetectMissionController(QObject* parent = nullptr);

    QString packageJson() const { return _json; }

    void setPackageJson(const QString& json);

    bool valid() const { return _valid; }

    QString validationMessage() const { return _validation; }

    QString uploadState() const { return _state; }

    double uploadProgress() const { return _progress; }

    bool busy() const { return _pending.type != 0; }

    QString activeMissionId() const { return _activeMissionId; }

    QVariantMap telemetry() const { return _telemetry; }

    QVariantList logEntries() const { return _logs; }

    bool telemetryFresh() const;

    QString currentPolicy() const { return _currentPolicy; }

    double currentPolicyTimeout() const { return _currentPolicyTimeout; }

    void setSender(std::function<bool(int, const QByteArray&)> sender) { _sender = std::move(sender); }

    void setUploadGuard(std::function<bool()> guard) { _uploadGuard = std::move(guard); }

    void shutdown();
    void receive(int type, const QByteArray& body);

    Q_INVOKABLE QString defaultPolicy(const QString& risk) const;
    Q_INVOKABLE bool validate();
    Q_INVOKABLE bool loadFile(const QUrl& url);
    Q_INVOKABLE bool saveFile(const QUrl& url);
    Q_INVOKABLE bool upload();
    Q_INVOKABLE bool abortMission();
    Q_INVOKABLE bool pauseMission();
    Q_INVOKABLE bool resumeMission();
    Q_INVOKABLE bool tightenPolicy(const QString& policy, double timeout);

signals:
    void packageChanged();
    void stateChanged();
    void telemetryChanged();

private:
    struct Pending
    {
        int type = 0;
        quint64 token = 0;
        int index = 0;
        QByteArray body;
        qint64 sentAt = 0;
        int attempts = 0;
        QString requestedPolicy;
        double requestedTimeout = 0;
    };

    struct Assembly
    {
        QByteArray digest;
        int total = 0;
        QList<QByteArray> chunks;
        qint64 started = 0;
    };

    bool _control(int type, const QString& policy = {}, double timeout = 0);
    bool _sendPending();
    bool _fail(const QString& message);
    bool _adopt(const QByteArray& json);
    void _poll();
    void _beginChunk();
    static QString _localPath(const QUrl& url);
    QString _json;
    QJsonObject _document;
    bool _valid = false;
    QString _validation;
    QString _state = QStringLiteral("尚未上传");
    double _progress = 0;
    QString _activeMissionId;
    QString _currentPolicy;
    double _currentPolicyTimeout = 0;
    QByteArray _uploadDocument;
    Pending _pending;
    quint64 _uploadToken = 0;
    QVariantMap _telemetry;
    QVariantList _logs;
    quint64 _lastStatusSequence = 0;
    quint64 _lastLogSequence = 0;
    QHash<quint64, Assembly> _assemblies;
    QHash<quint64, QByteArray> _completed;
    QElapsedTimer _clock;
    qint64 _lastStatusAt = -1;
    qint64 _lastHeartbeatAt = -1000;
    bool _fresh = false;
    QTimer _timer;
    std::function<bool(int, const QByteArray&)> _sender;
    std::function<bool()> _uploadGuard;
};
