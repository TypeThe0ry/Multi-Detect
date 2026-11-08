"""Runtime image selection stays identical across plans, CLI and raw headers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from multidetect.simulator.px4_image import DEFAULT_GPU_IMAGE, PINNED_IMAGE
from scripts import collect_sim_teacher_episodes as collector
from scripts import run_simulator_docker as docker

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / 'configs/scenarios/river_rescue_vtol_001.scenario.json'


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in ('MULTIDETECT_SIM_IMAGE', 'MULTIDETECT_SIM_GPU', 'MULTIDETECT_SIM_GPU_DEVICES', 'MULTIDETECT_SIM_GPU_AVOID', 'MULTIDETECT_SIM_CPUSET'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('MULTIDETECT_SIM_GPU_DEVICES', '0')
    monkeypatch.setenv('MULTIDETECT_SIM_GPU_AVOID', '1,2,3')
    monkeypatch.setenv('MULTIDETECT_SIM_CPUSET', 'off')


def collection(tmp_path, **kwargs):
    doc = json.loads(SCENARIO.read_text())
    channels = doc['channels']
    return collector.CollectionPlan(str(SCENARIO), str(SCENARIO.parent/channels['runtime_input']['path']), str(SCENARIO.parent/channels['truth']['path']), 'expert_search', 1, 1, 1, str(tmp_path), **kwargs)


@pytest.mark.parametrize(('gpu_render', 'device', 'gpu_env', 'expected'), [
    (None, None, None, PINNED_IMAGE),
    (True, None, None, DEFAULT_GPU_IMAGE),
    (None, None, '1', DEFAULT_GPU_IMAGE),
    (False, None, '1', PINNED_IMAGE),
    (False, '0', None, DEFAULT_GPU_IMAGE),
])
def test_api_and_collection_bind_image_after_actual_gpu_selection(tmp_path, monkeypatch, gpu_render, device, gpu_env, expected):
    if gpu_env is not None:
        monkeypatch.setenv('MULTIDETECT_SIM_GPU', gpu_env)
    options = dict(gpu_render=gpu_render, gpu_device=device)
    plan = docker.build_docker_simulator_plan(SCENARIO, job_dir=tmp_path/'job', **options)
    collected = collection(tmp_path/'collected', **options)
    mapping = collected.to_mapping()
    assert plan.image == mapping['image'] == collected.image == expected
    assert expected in plan.command and expected in mapping['docker_command']
    # Recording uses CollectionPlan.image, so an ambient change after construction
    # must not change the command while leaving its raw header on the old value.
    monkeypatch.setenv('MULTIDETECT_SIM_IMAGE', PINNED_IMAGE if expected == DEFAULT_GPU_IMAGE else DEFAULT_GPU_IMAGE)
    assert expected in collector.docker_command(collected)


@pytest.mark.parametrize('explicit', [None, PINNED_IMAGE, 'multidetect/px4-sitl-gazebo:gpu'])
def test_explicit_api_and_environment_can_select_original_runtime(tmp_path, monkeypatch, explicit):
    old = 'multidetect/px4-sitl-gazebo:gpu'
    monkeypatch.setenv('MULTIDETECT_SIM_IMAGE', old)
    expected = old if explicit is None else explicit
    assert docker.build_docker_simulator_plan(SCENARIO, job_dir=tmp_path/'job', gpu_render=True, image=explicit).image == expected
    assert collection(tmp_path, gpu_render=True, image=explicit).image == expected


@pytest.mark.parametrize('entrypoint', ['docker', 'collector'])
@pytest.mark.parametrize('explicit', [None, PINNED_IMAGE])
def test_cli_records_selected_image_without_launch(tmp_path, monkeypatch, capsys, entrypoint, explicit):
    monkeypatch.setenv('MULTIDETECT_SIM_GPU', '1')
    if entrypoint == 'docker':
        args = ['--scenario', str(SCENARIO), '--job-dir', str(tmp_path/'job')]
        run = docker.main
    else:
        args = ['--scenario', str(SCENARIO), '--teacher', 'expert_search', '--airframe', 'standard_vtol_mono_cam', '--episodes', '1', '--steps-per-episode', '1', '--out', str(tmp_path/'episodes'), '--dry-run']
        run = collector.main
    if explicit is not None:
        args += ['--image', explicit]
    assert run(args) == 0
    mapping = json.loads(capsys.readouterr().out)
    assert mapping['image'] == (explicit or DEFAULT_GPU_IMAGE)
    assert mapping['image'] in mapping['command' if entrypoint == 'docker' else 'docker_command']


def test_rebuild_refuses_existing_output_before_git_or_build(tmp_path):
    # A Docker stub lets this exercise the real script's pre-mutation guard.
    docker_stub = tmp_path/'docker'
    docker_stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\nexit 0\n')
    docker_stub.chmod(0o755)
    calls = tmp_path/'calls'
    env = {**os.environ, 'PATH': str(tmp_path)+':'+os.environ['PATH'], 'CALLS': str(calls)}
    result = subprocess.run(['bash', str(ROOT/'scripts/build_px4_lockstep_candidate.sh'), '/data/scratch/not-created-test', 'multidetect/px4-sitl-gazebo:lockstep-lifetime-r1-rebuild-test'], env=env, text=True, capture_output=True)
    assert result.returncode == 2 and 'Refusing to overwrite existing image' in result.stderr
    assert calls.read_text().splitlines() == ['image inspect multidetect/px4-sitl-gazebo:lockstep-lifetime-r1-rebuild-test']


def test_rebuild_rejects_changed_patch_before_any_docker_call(tmp_path):
    # Copy only the builder and its patch, then alter the patch to model drift.
    (tmp_path/'scripts').mkdir()
    target = tmp_path/'simulation/px4/patches'
    target.mkdir(parents=True)
    script = tmp_path/'scripts/build_px4_lockstep_candidate.sh'
    script.write_bytes((ROOT/'scripts/build_px4_lockstep_candidate.sh').read_bytes())
    patch = ROOT/'simulation/px4/patches/lockstep-lifetime-r1.patch'
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == '43bfe32d5d7572b866b3d411c2394d941b6bcc8e19ace5bdd6e230845cdf0559'
    (target/patch.name).write_bytes(patch.read_bytes()+b'\n')
    result = subprocess.run(['bash', str(script), '/data/scratch/not-created-test'], text=True, capture_output=True)
    assert result.returncode == 2 and 'Unexpected lifetime-r1 patch hash' in result.stderr


@pytest.mark.parametrize('target', ['multidetect/px4-sitl-gazebo:gpu', 'multidetect/px4-sitl-gazebo:lockstep-lifetime-r1'])
def test_rebuild_cannot_target_either_active_tag(target):
    result = subprocess.run(['bash', str(ROOT/'scripts/build_px4_lockstep_candidate.sh'), '/data/scratch/not-created-test', target], text=True, capture_output=True)
    assert result.returncode == 2 and 'Output must be a fresh' in result.stderr
