#pragma once

#include <QtCore/QElapsedTimer>
#include <QtCore/QHash>
#include <QtCore/QJsonObject>
#include <QtCore/QObject>
#include <QtCore/QTimer>
#include <QtCore/QUrl>
#include <QtCore/QVariantList>
#include <QtCore/QSet>
#include <functional>

#include "MultiDetectMissionSecurity.h"

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
    Q_PROPERTY(QVariantMap plan READ plan NOTIFY telemetryChanged)
    Q_PROPERTY(QVariantList findings READ findings NOTIFY telemetryChanged)
    Q_PROPERTY(QVariantList alerts READ alerts NOTIFY telemetryChanged)
    Q_PROPERTY(QVariantMap finalSummary READ finalSummary NOTIFY telemetryChanged)
    Q_PROPERTY(bool finalSummaryComplete READ finalSummaryComplete NOTIFY telemetryChanged)
    Q_PROPERTY(QString finalSummaryState READ finalSummaryState NOTIFY telemetryChanged)
    Q_PROPERTY(int summaryPagesReceived READ summaryPagesReceived NOTIFY telemetryChanged)
    Q_PROPERTY(int summaryPagesTotal READ summaryPagesTotal NOTIFY telemetryChanged)
    Q_PROPERTY(bool onboardCompiling READ onboardCompiling NOTIFY stateChanged)
    Q_PROPERTY(bool planAwaitingDecision READ planAwaitingDecision NOTIFY stateChanged)
    Q_PROPERTY(QString planDecisionState READ planDecisionState NOTIFY stateChanged)

public:
    explicit MultiDetectMissionController(QObject* parent = nullptr);
    ~MultiDetectMissionController() override;

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
    QVariantMap plan() const { return _plan; }
    QVariantList findings() const { return _findings; }
    QVariantList alerts() const { return _alerts; }
    QVariantMap finalSummary() const { return _finalSummary; }
    bool finalSummaryComplete() const { return _finalSummaryComplete; }
    QString finalSummaryState() const { return _finalSummaryState; }
    int summaryPagesReceived() const { return static_cast<int>(_summaryPages.size()); }
    int summaryPagesTotal() const { return _summaryPageCount; }
    // The last package chunk was received and the aircraft is still planning;
    // its second ACK for the same token/index carries the verdict.
    bool onboardCompiling() const { return _pending.type == 28 && _pending.awaitingVerdict; }
    // A plan that asks for approval and has neither been decided nor has a
    // decision in flight.
    bool planAwaitingDecision() const;
    QString planDecisionState() const { return _planDecisionState; }

    void setSender(std::function<bool(int, const QByteArray&)> sender) { _sender = std::move(sender); }

    void setUploadGuard(std::function<bool()> guard) { _uploadGuard = std::move(guard); }

    void shutdown();
    void receive(int type, const QByteArray& body);

    Q_INVOKABLE QString defaultPolicy(const QString& risk) const;
    Q_INVOKABLE bool validate();
    Q_INVOKABLE bool loadFile(const QUrl& url);
    Q_INVOKABLE bool loadEnvironmentPackage();
    Q_INVOKABLE bool saveFile(const QUrl& url);
    Q_INVOKABLE bool upload();
    Q_INVOKABLE bool abortMission();
    Q_INVOKABLE bool pauseMission();
    Q_INVOKABLE bool resumeMission();
    Q_INVOKABLE bool tightenPolicy(const QString& policy, double timeout);
    Q_INVOKABLE bool approvePlan();
    Q_INVOKABLE bool rejectPlan();

signals:
    void packageChanged();
    void stateChanged();
    void telemetryChanged();

private:
    struct Pending
    {
        int type = 0;
        int commandType = 0;
        quint64 token = 0;
        int index = 0;
        QByteArray body;
        qint64 sentAt = 0;
        int attempts = 0;
        QString requestedPolicy;
        double requestedTimeout = 0;
        bool awaitingVerdict = false;
        qint64 verdictDeadline = 0;
        QString planDigest;
    };

    struct Assembly
    {
        QByteArray digest;
        int total = 0;
        QList<QByteArray> chunks;
        qint64 started = 0;
        qint64 deadline = 0;
        qint64 reservedBytes = 0;
    };

    bool _control(int type, const QString& policy = {}, double timeout = 0);
    bool _planDecision(bool approve);
    bool _prepareCommand(QJsonObject* command, QString* error);
    void _beginMission(const QString& missionId);
    void _switchMission(const QString& missionId);
    void _admitUpload(const QString& detail);
    bool _adoptSummaryPage(const QJsonObject& page);
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
    QByteArray _commandDocument;
    MultiDetectMissionSecurity::SigningKey _packageKey;
    MultiDetectMissionSecurity::SigningKey _commandKey;
    QString _keyError;
    QString _missionKeyId;
    QString _uploadMissionId;
    QSet<QString> _decidedPlans;
    QString _planDecisionState;
    bool _requireMissionSignature = false;
    Pending _pending;
    quint64 _uploadToken = 0;
    QVariantMap _telemetry;
    QVariantList _logs;
    QVariantMap _plan;
    QVariantList _findings;
    QVariantList _alerts;
    QVariantMap _finalSummary;
    bool _finalSummaryComplete = false;
    QString _finalSummaryState;
    QHash<int, QByteArray> _summaryPages;
    int _summaryPageCount = 0;
    qint64 _summaryDeadline = -1;
    bool _summaryAssemblyInvalid = false;
    quint64 _lastStatusSequence = 0;
    quint64 _lastLogSequence = 0;
    QHash<quint64, Assembly> _assemblies;
    qint64 _telemetryAssemblyBytes = 0;
    QHash<quint64, QByteArray> _completed;
    QElapsedTimer _clock;
    qint64 _lastStatusAt = -1;
    qint64 _lastHeartbeatAt = -1000;
    bool _fresh = false;
    QTimer _timer;
    std::function<bool(int, const QByteArray&)> _sender;
    std::function<bool()> _uploadGuard;
};
