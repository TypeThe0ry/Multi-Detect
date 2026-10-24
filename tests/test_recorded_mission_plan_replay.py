from pathlib import Path
from scripts.replay_onboard_episode import recover_recorded_mission_plan


def test_recover_recorded_monitor_plan(tmp_path):
    episode = tmp_path / 'episode'
    episode.mkdir()
    (episode / 'steps.jsonl').write_text('{"task_text":"Monitor backpacks","labels":{"onboard":{"mission":{"plan":{"skills":["monitor","return_home"]}}}}}')
    result = recover_recorded_mission_plan(episode)
    assert result['plan_source'] == 'labels.onboard.mission.plan.skills'
    assert [step['skill'] for step in result['plan']['steps']] == ['monitor', 'return_home']


def test_missing_recorded_plan_is_explicit_fallback(tmp_path):
    episode = tmp_path / 'episode'
    episode.mkdir()
    (episode / 'steps.jsonl').write_text('{"task_text":"count cones","labels":{"onboard":{}}}')
    result = recover_recorded_mission_plan(episode)
    assert result['plan_source'] == 'fallback_count'
    assert result['skills'] == ['count', 'return_home']


def test_complete_plan_keeps_branches_and_params(tmp_path):
    episode = tmp_path / 'episode'
    episode.mkdir()
    plan = {'plan_version': 1, 'goal': 'watch', 'steps': [
        {'id': 's1', 'skill': 'monitor', 'params': {'region': 'zone:site', 'query': 'backpack', 'duration_s': 5}, 'on_success': 's2', 'on_fail': 's2'},
        {'id': 's2', 'skill': 'return_home', 'params': {}, 'on_success': None, 'on_fail': None}], 'report': ['alerts']}
    import json
    row = {'task_text': 'watch', 'labels': {'onboard': {'mission': {'plan': plan}}}}
    (episode / 'steps.jsonl').write_text(json.dumps(row))
    result = recover_recorded_mission_plan(episode)
    assert result['plan_source'] == 'labels.onboard.mission.plan.steps'
    assert result['plan']['steps'][0]['params']['duration_s'] == 5
