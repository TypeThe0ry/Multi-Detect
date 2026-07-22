#pragma once

#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtCore/QVariantMap>

class MultiDetectTargetEvidenceStore final : public QObject
{
    Q_OBJECT

    Q_PROPERTY(int snapshotCount READ snapshotCount NOTIFY snapshotCountChanged)
    Q_PROPERTY(QString storagePath READ storagePath CONSTANT)
    Q_PROPERTY(QString lastError READ lastError NOTIFY lastErrorChanged)

public:
    explicit MultiDetectTargetEvidenceStore(QObject* parent = nullptr);

    int snapshotCount() const { return _snapshotCount; }
    QString storagePath() const { return _storagePath; }
    QString lastError() const { return _lastError; }

    // Stores only user-requested, read-only target evidence locally. This API
    // has no link, vehicle, mission, actuator, or flight-control dependency.
    Q_INVOKABLE bool saveSnapshot(const QVariantMap& snapshot);

signals:
    void snapshotCountChanged();
    void lastErrorChanged();

private:
    static constexpr int kMaximumSnapshots = 500;

    void _loadSnapshotCount();
    void _setLastError(const QString& error);

    int _snapshotCount = 0;
    QString _storagePath;
    QString _lastError;
};
