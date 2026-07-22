#include "CustomPlugin.h"

#include <QtCore/QApplicationStatic>
#include <QtCore/QFile>
#include <QtCore/QSettings>
#include <QtCore/QUrl>
#include <QtQml/QQmlApplicationEngine>
#include <QtQml/QQmlContext>

#include "MultiDetectOperatorController.h"
#include "MultiDetectTargetEvidenceStore.h"
#include "LinkConfiguration.h"
#include "SettingsManager.h"
#include "TCPLink.h"
#include "VideoSettings.h"

Q_APPLICATION_STATIC(CustomPlugin, _customPluginInstance);

namespace {

constexpr auto kDefaultCameraRtspUrl = "rtsp://192.168.144.108:554/stream=0";
constexpr auto kDefaultVehicleLinkName = "MultiDetect GR01";
constexpr auto kDefaultVehicleHost = "192.168.144.11";
constexpr quint16 kDefaultVehiclePort = 5760;

QString runtimeEnvironmentValue(const char* name)
{
    if (qEnvironmentVariableIsSet(name)) {
        return qEnvironmentVariable(name);
    }
#ifdef Q_OS_WIN
    QSettings userEnvironment(QStringLiteral("HKEY_CURRENT_USER\\Environment"), QSettings::NativeFormat);
    return userEnvironment.value(QString::fromLatin1(name)).toString();
#else
    return {};
#endif
}

}  // namespace

CustomPlugin::CustomPlugin(QObject* parent) : QGCCorePlugin(parent) {}

QGCCorePlugin* CustomPlugin::instance()
{
    return _customPluginInstance();
}

void CustomPlugin::init()
{
    // The custom aircraft has a fixed network topology. Persist one named TCP
    // profile before LinkManager loads its configuration list so every normal
    // boot connects to the GR01/V6X link and keeps retrying while the aircraft
    // network is temporarily absent. Existing unrelated user profiles remain
    // untouched.
    QSettings settings;
    const QString configuredVehicleHost = runtimeEnvironmentValue("MULTIDETECT_VEHICLE_TCP_HOST").trimmed();
    const QString vehicleHost = configuredVehicleHost.isEmpty()
                                    ? QString::fromLatin1(kDefaultVehicleHost)
                                    : configuredVehicleHost;
    bool portOk = false;
    const uint configuredVehiclePort = runtimeEnvironmentValue("MULTIDETECT_VEHICLE_TCP_PORT").toUInt(&portOk);
    const quint16 vehiclePort = portOk && configuredVehiclePort >= 1 && configuredVehiclePort <= 65535
                                    ? static_cast<quint16>(configuredVehiclePort)
                                    : kDefaultVehiclePort;
    const QString settingsRoot = LinkConfiguration::settingsRoot();
    int count = qMax(0, settings.value(settingsRoot + QStringLiteral("/count"), 0).toInt());
    int profileIndex = -1;
    for (int index = 0; index < count; ++index) {
        const QString root = settingsRoot + QStringLiteral("/Link%1").arg(index);
        const QString name = settings.value(root + QStringLiteral("/name")).toString();
        const auto type = static_cast<LinkConfiguration::LinkType>(
            settings.value(root + QStringLiteral("/type"), LinkConfiguration::TypeLast).toInt());
        const QString host = settings.value(root + QStringLiteral("/host")).toString();
        const quint16 port = settings.value(root + QStringLiteral("/port"), 0).toUInt();
        if (name == QString::fromLatin1(kDefaultVehicleLinkName)
            || (type == LinkConfiguration::TypeTcp && host == vehicleHost && port == vehiclePort)) {
            profileIndex = index;
            break;
        }
    }
    if (profileIndex < 0) {
        profileIndex = count++;
        settings.setValue(settingsRoot + QStringLiteral("/count"), count);
    }
    const QString root = settingsRoot + QStringLiteral("/Link%1").arg(profileIndex);
    settings.setValue(root + QStringLiteral("/name"), QString::fromLatin1(kDefaultVehicleLinkName));
    settings.setValue(root + QStringLiteral("/type"), static_cast<int>(LinkConfiguration::TypeTcp));
    settings.setValue(root + QStringLiteral("/auto"), true);
    settings.setValue(root + QStringLiteral("/high_latency"), false);
    settings.setValue(root + QStringLiteral("/host"), vehicleHost);
    settings.setValue(root + QStringLiteral("/port"), vehiclePort);
    settings.sync();
}

void CustomPlugin::cleanup()
{
    if (_operatorController) {
        _operatorController->shutdown();
        delete _operatorController;
        _operatorController = nullptr;
    }
    delete _targetEvidenceStore;
    _targetEvidenceStore = nullptr;
    if (_qmlEngine && _selector) {
        _qmlEngine->removeUrlInterceptor(_selector);
    }
    delete _selector;
    _selector = nullptr;
    _qmlEngine = nullptr;
}

QQmlApplicationEngine* CustomPlugin::createQmlApplicationEngine(QObject* parent)
{
    const QString configuredRtspUrl = runtimeEnvironmentValue("MULTIDETECT_VIDEO_RTSP_URL").trimmed();
    const QString rtspUrl = configuredRtspUrl.isEmpty()
                                ? QString::fromLatin1(kDefaultCameraRtspUrl)
                                : configuredRtspUrl;
    {
        // libproxy otherwise sends private-camera RTSP through the Windows
        // desktop HTTP proxy. Force its environment backend and preserve the
        // existing bypass list while adding this camera host.
        qputenv("PX_FORCE_CONFIG", "config-env");
        QByteArray noProxy = qgetenv("NO_PROXY");
        const QByteArray rtspHost = QUrl(rtspUrl).host().toUtf8();
        if (!rtspHost.isEmpty() && !noProxy.split(',').contains(rtspHost)) {
            if (!noProxy.isEmpty()) {
                noProxy.append(',');
            }
            noProxy.append(rtspHost);
        }
        qputenv("NO_PROXY", noProxy);
        qputenv("no_proxy", noProxy);
        qputenv("QGC_RTSP_FORCE_TCP", "1");
        qputenv("QGC_RTSP_TCP_TIMEOUT_US", "20000000");
        VideoSettings* const video = SettingsManager::instance()->videoSettings();
        video->rtspUrl()->setRawValue(rtspUrl);
        video->videoSource()->setRawValue(VideoSettings::videoSourceRTSP);
        video->streamEnabled()->setRawValue(true);
        video->disableWhenDisarmed()->setRawValue(false);
        video->lowLatencyMode()->setRawValue(true);
        video->rtspAutoReconnect()->setRawValue(true);
    }

    _qmlEngine = QGCCorePlugin::createQmlApplicationEngine(parent);
    _qmlEngine->addImportPath(QStringLiteral("qrc:/qml"));

    if (!_operatorController) {
        _operatorController = new MultiDetectOperatorController(this);
    }
    if (!_targetEvidenceStore) {
        _targetEvidenceStore = new MultiDetectTargetEvidenceStore(this);
    }
    _qmlEngine->rootContext()->setContextProperty(QStringLiteral("multiDetectOperator"), _operatorController);
    _qmlEngine->rootContext()->setContextProperty(
        QStringLiteral("multiDetectTargetEvidenceStore"), _targetEvidenceStore);

    _selector = new CustomOverrideInterceptor();
    _qmlEngine->addUrlInterceptor(_selector);
    return _qmlEngine;
}

QUrl CustomOverrideInterceptor::intercept(const QUrl& url, DataType type)
{
    if ((type == QmlFile || type == UrlString) && url.scheme() == QStringLiteral("qrc")) {
        const QString overridePath = QStringLiteral(":/Custom%1").arg(url.path());
        if (QFile::exists(overridePath)) {
            QUrl result;
            result.setScheme(QStringLiteral("qrc"));
            result.setPath(overridePath.mid(1));
            return result;
        }
    }
    return url;
}
