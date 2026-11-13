import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def mod(monkeypatch):
    for k in ("RWCLU_SENSOR_FIRST_FRAME_S", "MULTIDETECT_SIM_FIRST_FRAME_WAIT_S"):
        monkeypatch.delenv(k, raising=False)
    return importlib.import_module("scripts.collect_sim_teacher_episodes")


def test_default_keeps_builtin_budget(mod):
    assert mod._sensor_first_frame_wait_s() is None


def test_shared_env_applies(mod, monkeypatch):
    monkeypatch.setenv("MULTIDETECT_SIM_FIRST_FRAME_WAIT_S", "240")
    assert mod._sensor_first_frame_wait_s() == 240.0


def test_rwclu_env_wins(mod, monkeypatch):
    monkeypatch.setenv("MULTIDETECT_SIM_FIRST_FRAME_WAIT_S", "240")
    monkeypatch.setenv("RWCLU_SENSOR_FIRST_FRAME_S", "180")
    assert mod._sensor_first_frame_wait_s() == 180.0
