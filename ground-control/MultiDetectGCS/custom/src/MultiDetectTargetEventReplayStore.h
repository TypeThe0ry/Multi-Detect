#pragma once

#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtCore/QVariantList>

// Read-only target-event replay manifest inspector for the native QGC Fly
// View.  The store verifies the hash-bound manifest, event stream and media
// references before exposing metadata to QML.  It deliberately has no
// vehicle, mission, operator-command or actuator dependency.
class MultiDetectTargetEventReplayStore final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(bool loaded READ loaded NOTIFY loadedChanged)
    Q_PROPERTY(bool integrityVerified READ integrityVerified NOTIFY integrityVerifiedChanged)
    Q_PROPERTY(bool truncated READ truncated NOTIFY truncatedChanged)
    Q_PROPERTY(QString manifestName READ manifestName NOTIFY manifestNameChanged)
    Q_PROPERTY(QString bundleId READ bundleId NOTIFY bundleIdChanged)
    Q_PROPERTY(QString sessionId READ sessionId NOTIFY sessionIdChanged)
    Q_PROPERTY(QString startedAtUtc READ startedAtUtc NOTIFY startedAtUtcChanged)
    Q_PROPERTY(int eventCount READ eventCount NOTIFY eventCountChanged)
    Q_PROPERTY(int displayedEventCount READ displayedEventCount NOTIFY displayedEventCountChanged)
    Q_PROPERTY(int mediaCount READ mediaCount NOTIFY mediaCountChanged)
    Q_PROPERTY(int locatableEventCount READ locatableEventCount NOTIFY statisticsChanged)
    Q_PROPERTY(int rangedEventCount READ rangedEventCount NOTIFY statisticsChanged)
    Q_PROPERTY(int mediaEventCount READ mediaEventCount NOTIFY statisticsChanged)
    Q_PROPERTY(int uniqueTargetCount READ uniqueTargetCount NOTIFY statisticsChanged)
    Q_PROPERTY(double captureDurationS READ captureDurationS NOTIFY statisticsChanged)
    Q_PROPERTY(qulonglong mediaBytes READ mediaBytes NOTIFY statisticsChanged)
    Q_PROPERTY(QVariantList events READ events NOTIFY eventsChanged)
    Q_PROPERTY(QString statusText READ statusText NOTIFY statusTextChanged)
    Q_PROPERTY(QString lastError READ lastError NOTIFY lastErrorChanged)

public:
    explicit MultiDetectTargetEventReplayStore(QObject* parent = nullptr);

    bool loaded() const { return _loaded; }
    bool integrityVerified() const { return _integrityVerified; }
    bool truncated() const { return _truncated; }
    QString manifestName() const { return _manifestName; }
    QString bundleId() const { return _bundleId; }
    QString sessionId() const { return _sessionId; }
    QString startedAtUtc() const { return _startedAtUtc; }
    int eventCount() const { return _eventCount; }
    int displayedEventCount() const { return _events.size(); }
    int mediaCount() const { return _mediaCount; }
    int locatableEventCount() const { return _locatableEventCount; }
    int rangedEventCount() const { return _rangedEventCount; }
    int mediaEventCount() const { return _mediaEventCount; }
    int uniqueTargetCount() const { return _uniqueTargetCount; }
    double captureDurationS() const { return _captureDurationS; }
    qulonglong mediaBytes() const { return _mediaBytes; }
    QVariantList events() const { return _events; }
    QString statusText() const { return _statusText; }
    QString lastError() const { return _lastError; }

    // The selected file must be an absolute manifest.json path.  A successful
    // call atomically replaces the previous view; a failed call clears it.
    Q_INVOKABLE bool loadManifest(const QString& path);
    Q_INVOKABLE void clear();

signals:
    void loadedChanged();
    void integrityVerifiedChanged();
    void truncatedChanged();
    void manifestNameChanged();
    void bundleIdChanged();
    void sessionIdChanged();
    void startedAtUtcChanged();
    void eventCountChanged();
    void displayedEventCountChanged();
    void mediaCountChanged();
    void statisticsChanged();
    void eventsChanged();
    void statusTextChanged();
    void lastErrorChanged();

private:
    void _clearState();
    bool _fail(const QString& message);
    void _setStatus(const QString& message);

    bool _loaded = false;
    bool _integrityVerified = false;
    bool _truncated = false;
    QString _manifestName;
    QString _bundleId;
    QString _sessionId;
    QString _startedAtUtc;
    int _eventCount = 0;
    int _mediaCount = 0;
    int _locatableEventCount = 0;
    int _rangedEventCount = 0;
    int _mediaEventCount = 0;
    int _uniqueTargetCount = 0;
    double _captureDurationS = 0.0;
    qulonglong _mediaBytes = 0;
    QVariantList _events;
    QString _statusText;
    QString _lastError;
};
