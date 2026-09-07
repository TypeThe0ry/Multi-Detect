#include "CustomPlugin.h"

#include <QtCore/QApplicationStatic>
#include <QtCore/QCoreApplication>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QSettings>
#include <QtCore/QUrl>
#include <QtQml/QQmlApplicationEngine>
#include <QtQml/QQmlContext>

#include "MultiDetectOperatorController.h"
#include "MultiDetectTargetEvidenceStore.h"
#include "MultiDetectTargetEventReplayStore.h"
#include "LinkConfiguration.h"
#include "QGCApplication.h"
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

bool runtimeEnvironmentFlagEnabled(const char* name)
{
    if (!qEnvironmentVariableIsSet(name)) {
        return false;
    }

    const QString value = runtimeEnvironmentValue(name).trimmed().toLower();
    // A malformed opt-in must fail closed.  Only explicit false spellings
    // re-enable vehicle I/O once the isolation variable exists.
    return value != QStringLiteral("0") && value != QStringLiteral("false") &&
           value != QStringLiteral("no") && value != QStringLiteral("off");
}

bool runtimeEnvironmentExplicitlyTrue(const char* name)
{
    if (!qEnvironmentVariableIsSet(name)) {
        return false;
    }

    const QString value = qEnvironmentVariable(name).trimmed().toLower();
    return value == QStringLiteral("1") || value == QStringLiteral("true") ||
           value == QStringLiteral("yes") || value == QStringLiteral("on");
}

bool vehicleLinkIsolationRequested()
{
    const QString expectedRuntimeMode =
        runtimeEnvironmentValue("MULTIDETECT_EXPECTED_RUNTIME_MODE").trimmed().toUpper();
    return expectedRuntimeMode == QStringLiteral("REPLAY") ||
           runtimeEnvironmentFlagEnabled("MULTIDETECT_DISABLE_VEHICLE_LINK");
}

bool isDefaultVehicleProfile(const QSettings& settings, const QString& root,
                             const QString& vehicleHost, quint16 vehiclePort)
{
    const QString name = settings.value(root + QStringLiteral("/name")).toString();
    const auto type = static_cast<LinkConfiguration::LinkType>(
        settings.value(root + QStringLiteral("/type"), LinkConfiguration::TypeLast).toInt());
    const QString host = settings.value(root + QStringLiteral("/host")).toString();
    const quint16 port = settings.value(root + QStringLiteral("/port"), 0).toUInt();
    return name == QString::fromLatin1(kDefaultVehicleLinkName) ||
           (type == LinkConfiguration::TypeTcp && host == vehicleHost && port == vehiclePort);
}

}  // namespace

CustomPlugin::CustomPlugin(QObject* parent) : QGCCorePlugin(parent) {}

QGCCorePlugin* CustomPlugin::instance()
{
    return _customPluginInstance();
}

const QVariantList& CustomPlugin::toolBarIndicators()
{
    if (_toolBarIndicatorList.isEmpty()) {
        // A custom build extends the native application indicators; it must
        // never replace QGC's RTK or any future base indicator.
        _toolBarIndicatorList = QGCCorePlugin::toolBarIndicators();
        _toolBarIndicatorList.append(QVariant::fromValue(
            QUrl(QStringLiteral("qrc:/qml/Custom/MultiDetect/MultiDetectToolbarIndicator.qml"))));
    }

    return _toolBarIndicatorList;
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

    if (vehicleLinkIsolationRequested()) {
        // REPLAY is an observation/metadata environment.  Remove the custom
        // GR01/V6X profile before LinkManager loads persistent configurations,
        // and do not recreate it on this boot.  Deliberately keep unrelated
        // user profiles and the ephemeral signed Jetson metadata link intact.
        // Leaving an empty index is supported by LinkManager and avoids
        // rewriting any unrelated, type-specific settings while still making
        // the physical vehicle profile non-persistent and non-autoconnecting.
        for (int index = 0; index < count; ++index) {
            const QString root = settingsRoot + QStringLiteral("/Link%1").arg(index);
            if (isDefaultVehicleProfile(settings, root, vehicleHost, vehiclePort)) {
                settings.remove(root);
            }
        }
        while (count > 0 &&
               !settings.contains(settingsRoot + QStringLiteral("/Link%1/type").arg(count - 1))) {
            --count;
        }
        settings.setValue(settingsRoot + QStringLiteral("/count"), count);
        settings.sync();
        return;
    }

    int profileIndex = -1;
    int freeProfileIndex = -1;
    for (int index = 0; index < count; ++index) {
        const QString root = settingsRoot + QStringLiteral("/Link%1").arg(index);
        if (!settings.contains(root + QStringLiteral("/type"))) {
            if (freeProfileIndex < 0) {
                freeProfileIndex = index;
            }
            continue;
        }
        if (isDefaultVehicleProfile(settings, root, vehicleHost, vehiclePort)) {
            profileIndex = index;
            break;
        }
    }
    if (profileIndex < 0) {
        if (freeProfileIndex >= 0) {
            profileIndex = freeProfileIndex;
        } else {
            profileIndex = count++;
            settings.setValue(settingsRoot + QStringLiteral("/count"), count);
        }
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
    delete _targetEventReplayStore;
    _targetEventReplayStore = nullptr;
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
    if (!_targetEventReplayStore) {
        _targetEventReplayStore = new MultiDetectTargetEventReplayStore(this);
    }
    _qmlEngine->rootContext()->setContextProperty(QStringLiteral("multiDetectOperator"), _operatorController);
    _qmlEngine->rootContext()->setContextProperty(
        QStringLiteral("multiDetectTargetEvidenceStore"), _targetEvidenceStore);
    _qmlEngine->rootContext()->setContextProperty(
        QStringLiteral("multiDetectTargetEventReplayStore"), _targetEventReplayStore);
    _qmlEngine->rootContext()->setContextProperty(
        QStringLiteral("multiDetectVehicleLinkDisabled"), vehicleLinkIsolationRequested());
    // The closed-loop HIL acceptance driver may exercise the same QML state
    // machine that the operator clicks in Fly View.  It is opt-in, requires
    // the explicit isolated-HIL command-line boundary, and never changes the
    // production controller or adds a second UI surface.
    const bool hilUiAutoExercise = QCoreApplication::arguments().contains(QStringLiteral("--isolated-hil")) &&
                                   runtimeEnvironmentFlagEnabled("MULTIDETECT_HIL_UI_AUTO_EXERCISE");
    _qmlEngine->rootContext()->setContextProperty(QStringLiteral("multiDetectHilUiAutoExercise"),
                                                   hilUiAutoExercise);
    const bool hilUiAutoExit = hilUiAutoExercise &&
                               runtimeEnvironmentFlagEnabled("MULTIDETECT_HIL_UI_AUTO_EXIT");
    _qmlEngine->rootContext()->setContextProperty(QStringLiteral("multiDetectHilUiAutoExit"), hilUiAutoExit);
    int hilUiAutoExitDelayMs = 0;
    if (hilUiAutoExit) {
        bool delayValid = false;
        const int configuredDelayMs =
            runtimeEnvironmentValue("MULTIDETECT_HIL_UI_AUTO_EXIT_DELAY_MS").toInt(&delayValid);
        if (delayValid && configuredDelayMs >= 0 && configuredDelayMs <= 15000) {
            hilUiAutoExitDelayMs = configuredDelayMs;
        }
    }
    _qmlEngine->rootContext()->setContextProperty(
        QStringLiteral("multiDetectHilUiAutoExitDelayMs"), hilUiAutoExitDelayMs);
    _qmlEngine->rootContext()->setContextProperty(QStringLiteral("multiDetectHilUiExitController"),
                                                   qobject_cast<QGCApplication*>(QCoreApplication::instance()));
    // Native toolbar acceptance may export the actual QML scene geometry used
    // by the one QGC main window. This is deliberately fail-closed: both the
    // isolated-HIL command-line boundary and an explicitly true process
    // environment value are required. Production launches therefore expose
    // the context property as false and emit no geometry telemetry.
    const bool hilUiGeometryExport =
        QCoreApplication::arguments().contains(QStringLiteral("--isolated-hil")) &&
        runtimeEnvironmentExplicitlyTrue("MULTIDETECT_HIL_UI_GEOMETRY_EXPORT");
    _qmlEngine->rootContext()->setContextProperty(QStringLiteral("multiDetectHilUiGeometryExport"),
                                                   hilUiGeometryExport);

    // An acceptance run may export the pixels rendered by QGC's one native
    // VideoOutput. Do not expose this hook to a normal application launch:
    // the command-line isolation boundary, an explicit true value, and an
    // absolute output prefix are all mandatory.
    const QString configuredVideoGrabPrefix =
        runtimeEnvironmentValue("MULTIDETECT_HIL_VIDEO_GRAB_PREFIX").trimmed();
    const bool hilVideoGrabExport =
        QCoreApplication::arguments().contains(QStringLiteral("--isolated-hil")) &&
        runtimeEnvironmentExplicitlyTrue("MULTIDETECT_HIL_VIDEO_GRAB_EXPORT") &&
        QFileInfo(configuredVideoGrabPrefix).isAbsolute();
    if (hilVideoGrabExport) {
        _qmlEngine->rootContext()->setContextProperty(
            QStringLiteral("multiDetectHilVideoGrabExport"), true);
        _qmlEngine->rootContext()->setContextProperty(
            QStringLiteral("multiDetectHilVideoGrabPrefix"), configuredVideoGrabPrefix);
    }

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
