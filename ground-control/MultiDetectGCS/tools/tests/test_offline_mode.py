from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_offline_cli_uses_an_isolated_fail_closed_link_profile() -> None:
    parser_header = _read("src/Utilities/QGCCommandLineParser.h")
    parser_source = _read("src/Utilities/QGCCommandLineParser.cc")
    application = _read("src/QGCApplication.cc")

    assert "bool offlineMode = false" in parser_header
    assert 'kOptOffline       = QLatin1StringView("offline")' in parser_source
    assert "out.offlineMode = parser.isSet(offlineOpt)" in parser_source

    set_path = application.index("QSettings::setPath(")
    construct_settings = application.index("QSettings settings;")
    assert set_path < construct_settings
    assert 'settings.setValue(QStringLiteral("autoConnectUDP"), false)' in application
    assert 'settings.setValue(QStringLiteral("autoConnectPixhawk"), false)' in application
    assert 'settings.setValue(QStringLiteral("LinkConfigurations/count"), 0)' in application
    assert 'settings.setValue(QStringLiteral("forwardMavlink"), false)' in application
    assert 'settings.setValue(QStringLiteral("disableAllPersistence"), true)' in application
    assert 'QDir(isolatedSettingsPath).filePath(QStringLiteral("data"))' in application
    assert 'linkManager->setConnectionsSuspended(tr("offline UI validation mode"))' in application
    assert (
        "if (!_offlineMode && !_isolatedHilMode) {\n"
        "        linkManager->startAutoConnectedLinks();" in application
    )


def test_isolated_hil_uses_clean_settings_without_enabling_an_unsigned_operator_path() -> None:
    parser_header = _read("src/Utilities/QGCCommandLineParser.h")
    parser_source = _read("src/Utilities/QGCCommandLineParser.cc")
    application = _read("src/QGCApplication.cc")
    controller = _read("custom/src/MultiDetectOperatorController.cc")

    assert "bool isolatedHilMode = false" in parser_header
    assert 'kOptIsolatedHil   = QLatin1StringView("isolated-hil")' in parser_source
    assert "out.isolatedHilMode = parser.isSet(isolatedHilOpt)" in parser_source
    assert "_isolatedHilMode(cli.isolatedHilMode)" in application
    assert 'QStringLiteral("isolated-hil-settings")' in application
    assert "if (_offlineMode || _isolatedHilMode)" in application
    assert "if (!_offlineMode && !_isolatedHilMode)" in application
    # The command-line flag only isolates QGC settings and suppresses automatic
    # links.  The production controller remains signed-only; it must not grow a
    # localhost/unsigned metadata transport merely because this test boundary
    # exists.
    assert "application->isolatedHilMode()" not in controller
    assert "software HIL requires the --isolated-hil command-line boundary" not in controller
    assert "MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL" not in controller
    assert "MULTIDETECT_OPERATOR_HIL_UDP_PORT" not in controller


def test_isolated_hil_cpu_video_ab_is_explicit_single_variable_and_fail_closed() -> None:
    parser_header = _read("src/Utilities/QGCCommandLineParser.h")
    parser_source = _read("src/Utilities/QGCCommandLineParser.cc")
    application = _read("src/QGCApplication.cc")

    assert "bool isolatedHilForceCpuVideo = false" in parser_header
    assert (
        'kOptIsolatedHilForceCpuVideo = '
        'QLatin1StringView("isolated-hil-force-cpu-video")'
    ) in parser_source
    assert (
        "out.isolatedHilForceCpuVideo = "
        "parser.isSet(isolatedHilForceCpuVideoOpt)"
    ) in parser_source
    assert "out.isolatedHilForceCpuVideo && !out.isolatedHilMode" in parser_source
    assert "--isolated-hil-force-cpu-video requires --isolated-hil" in parser_source

    clear_settings = application.index("settings.clear();")
    force_cpu_setting = application.index(
        'settings.setValue(QStringLiteral("forceCpuVideoPath"), true);'
    )
    settings_sync = application.index("settings.sync();", force_cpu_setting)
    logging_filter = application.index(
        "QGCLoggingCategoryManager::instance()->installFilter(loggingOptions);"
    )
    explicit_log = application.index("isolated HIL CPU-video A/B enabled")
    assert clear_settings < force_cpu_setting < settings_sync < logging_filter < explicit_log
    assert "_isolatedHilForceCpuVideo(cli.isolatedHilForceCpuVideo)" in application
    assert "if (_isolatedHilForceCpuVideo)" in application

    # CPU routing is the only decoder-path variable changed by this switch.
    assert 'settings.setValue(QStringLiteral("forceVideoDecoder")' not in application
    assert "forceVideoDecoder=default" in application


def test_runtime_toolbar_indicator_does_not_create_an_offline_or_unsigned_hil_shortcut() -> None:
    controller = _read("custom/src/MultiDetectOperatorController.cc")
    state = _read("custom/res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("custom/src/FlyViewCustomLayer.qml")
    indicator = _read(
        "custom/res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml"
    )

    assert "MessageType::RuntimeStatus" in controller
    assert "MAVLINK_IFLAG_SIGNED" in controller
    assert "runtimeDisplayMode" in state
    assert "runtimeDisplayMode" in indicator
    assert "ToolIndicatorPage" in indicator
    assert "runtimeIdentityBadge" not in layer
    assert "application->isolatedHilMode()" not in controller
    assert "MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL" not in controller
    assert "runtimeDisplayMode:" not in layer


def test_replay_runtime_is_a_separate_fail_closed_vehicle_and_action_boundary() -> None:
    plugin = _read("custom/src/CustomPlugin.cc")
    state = _read("custom/res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("custom/src/FlyViewCustomLayer.qml")
    indicator = _read(
        "custom/res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml"
    )

    assert 'runtimeEnvironmentFlagEnabled("MULTIDETECT_DISABLE_VEHICLE_LINK")' in plugin
    assert 'expectedRuntimeMode == QStringLiteral("REPLAY")' in plugin
    assert "if (vehicleLinkIsolationRequested())" in plugin
    assert "settings.remove(root);" in plugin
    assert 'QStringLiteral("multiDetectVehicleLinkDisabled")' in plugin

    assert "readonly property bool replaySafetyActive" in state
    assert "readonly property bool physicalActionsAllowed: !replaySafetyActive" in state
    assert "function _enterReplaySafetyState()" in state
    assert "function replaySafetyStatusText()" in state
    assert "physicalActionsAllowed && vehicleArmed && lckActionable" in state
    assert 'runtimeRemoteMode === "REPLAY"' in state

    for blocked_send in (
        "sendApproachSlideConfirmation",
        "sendPayloadTargetSlideConfirmation",
        "sendAuthorizationDecision",
    ):
        send_offset = state.index(blocked_send)
        function_offset = state.rfind("function ", 0, send_offset)
        assert "if (replaySafetyActive)" in state[function_offset:send_offset]

    # Target metadata remains interactive even when every physical-action gate
    # is closed.  This is not an offline/unsigned transport shortcut.
    assert '_operator.sendTargetSelection("SELECT_TRK"' in state
    assert '_operator.sendTargetSelection("CANCEL_TRK"' in state
    assert "MAVLINK_IFLAG_SIGNED" in _read("custom/src/MultiDetectOperatorController.cc")
    assert "MultiDetectState.replaySafetyActive" in layer
    assert "executionChallengeWaitTimer.stop();" in layer
    assert 'qsTr("环境 REPLAY · 只读回放' in indicator


def test_suspended_link_manager_rejects_every_new_link() -> None:
    link_manager = _read("src/Comms/LinkManager.cc")
    function_start = link_manager.index(
        "bool LinkManager::createConnectedLink(SharedLinkConfigurationPtr &config)"
    )
    function_body = link_manager[function_start : function_start + 500]

    assert "if (!config || _connectionsSuspendedMsg())" in function_body
    assert function_body.index("_connectionsSuspendedMsg()") < function_body.index(
        "config->setSuppressAutoReconnect"
    )
