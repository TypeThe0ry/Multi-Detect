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
