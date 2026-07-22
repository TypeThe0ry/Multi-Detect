#pragma once

#include <QtQml/QQmlAbstractUrlInterceptor>

#include "QGCCorePlugin.h"

class QQmlApplicationEngine;
class MultiDetectOperatorController;
class MultiDetectTargetEvidenceStore;

class CustomOverrideInterceptor final : public QQmlAbstractUrlInterceptor
{
public:
    QUrl intercept(const QUrl& url, DataType type) final;
};

class CustomPlugin final : public QGCCorePlugin
{
    Q_OBJECT

public:
    explicit CustomPlugin(QObject* parent = nullptr);

    static QGCCorePlugin* instance();

    void init() final;
    void cleanup() final;
    QQmlApplicationEngine* createQmlApplicationEngine(QObject* parent) final;

private:
    QQmlApplicationEngine* _qmlEngine = nullptr;
    CustomOverrideInterceptor* _selector = nullptr;
    MultiDetectOperatorController* _operatorController = nullptr;
    MultiDetectTargetEvidenceStore* _targetEvidenceStore = nullptr;
};
