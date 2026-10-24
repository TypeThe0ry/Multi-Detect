import json

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import AuthenticationKey
from scripts import mission_package_tool as tool


def test_new_defaults_and_option_parsing(capsys):
    assert tool.main(["new", "--zone", "field:25,0,20:open_field",
                      "--place", "gate:40,0:大门 / gate"]) == 0
    package = json.loads(capsys.readouterr().out)
    assert package["risk_level"] == "low"
    assert package["link_loss_policy"] == {"action": "continue"}
    assert package["budget"] == {
        "time_s": 600, "reserve_time_s": 180, "energy_wh": 120,
        "reserve_energy_wh": 30,
    }
    assert package["zones"]["zone:field"]["centre_ne"] == [25, 0]
    assert package["ref_labels"]["poi:gate"] == "大门 / gate"


def test_validate_reports_format_error_in_chinese(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text('{"mission_id": "broken"}', encoding="utf-8")
    assert tool.main(["validate", str(path)]) == 1
    output = capsys.readouterr().out
    assert "校验失败" in output and "缺失字段或未知字段" in output


def test_sign_interoperates_with_onboard_admission_and_rejects_tampering(tmp_path, capsys):
    source = tmp_path / "package.json"
    assert tool.main(["new", "--id", "signed-demo", "-o", str(source)]) == 0
    capsys.readouterr()
    link = tmp_path / "operator-link.json"
    link.write_text(json.dumps({"operator_package_key_hex": "2" * 64}), encoding="utf-8")
    assert tool.main(["sign", str(source), "--operator-link", str(link)]) == 0
    capsys.readouterr()
    signed_path = tmp_path / "package.signed.json"
    signed = json.loads(signed_path.read_text(encoding="utf-8"))
    key = AuthenticationKey("hmac-sha256", bytes.fromhex("2" * 64), "default")
    assert load_mission_package(signed, key=key, require_signature=True).mission_id == "signed-demo"
    assert tool.main(["validate", str(signed_path), "--operator-link", str(link)]) == 0
    assert "校验通过" in capsys.readouterr().out
    signed["task_text"] = "tampered"
    with pytest.raises(ValueError, match="signature verification failed"):
        load_mission_package(signed, key=key, require_signature=True)


def test_show_reports_signature_and_estimated_endurance(tmp_path, capsys):
    path = tmp_path / "package.json"
    assert tool.main(["new", "-o", str(path)]) == 0
    capsys.readouterr()
    assert tool.main(["show", str(path)]) == 0
    result = capsys.readouterr().out
    assert "未签名" in result
    assert "预算可飞时长估算" in result
    assert "7.0 分钟" in result


def test_new_invalid_budget_is_rejected(capsys):
    assert tool.main(["new", "--budget", "60,180,20,30"]) == 2
    assert "budget must exceed reserve" in capsys.readouterr().err
