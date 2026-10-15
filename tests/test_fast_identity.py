from __future__ import annotations

import json

import pytest
from onboard_factories import make_llama_runner

from scripts import eval_slow_system as slow

PREFIX_TOKENS = [1, 2, 3]
FIRST = {"approach_track": 10, "confirm_target": 11, "reject_track": 12}


def _runner(monkeypatch, top_logprobs, *, merged_first: bool = False):
    calls = []

    def fake_http(url, body=None, timeout=None):
        calls.append((url, body))
        if url.endswith("/detokenize"):
            names = {v: k for k, v in FIRST.items()}
            return {"content": names.get(body["tokens"][0], "?")[:4]}
        if url.endswith("/tokenize"):
            text = body["content"]
            for verdict, token in FIRST.items():
                if text.endswith(verdict + '"'):
                    first = 10 if merged_first else token
                    return {"tokens": [*PREFIX_TOKENS, first, 99]}
            return {"tokens": list(PREFIX_TOKENS)}
        return {"choices": [{"logprobs": {"content": [{"top_logprobs": top_logprobs}]}}]}

    monkeypatch.setattr(slow, "_http_json", fake_http)
    runner = make_llama_runner(port=1, fast_identity=True)
    return runner, calls


def test_fast_identity_picks_the_most_likely_verdict(monkeypatch):
    top = [{"id": 10, "logprob": -3.0}, {"id": 12, "logprob": -0.05}, {"id": 7, "logprob": -0.01}]
    runner, calls = _runner(monkeypatch, top)
    content = [{"type": "text", "text": "x"}]
    answer, _ = runner._fast_identity({"focus_track_id": "track-2"}, content)
    value = json.loads(answer)
    assert value["decision"] == "reject_track"
    assert value["args"] == {"duration_s": None, "region": None, "track_id": "track-2"}
    request = calls[-1][1]
    assert request["max_tokens"] == 1
    assert request["messages"][-1] == {
        "role": "assistant",
        "content": '{"args":{"duration_s":null,"region":null,"track_id":"track-2"},"decision":"',
    }
    assert slow.decision_confidence(runner.last_logprobs) == pytest.approx(0.951, abs=1e-3)


def test_fast_identity_falls_back_when_no_verdict_is_in_the_top_tokens(monkeypatch):
    runner, _ = _runner(monkeypatch, [{"id": 7, "logprob": -0.01}])
    assert runner._fast_identity({"focus_track_id": "t"}, []) is None


def test_fast_identity_refuses_verdicts_that_share_a_first_token(monkeypatch):
    runner, _ = _runner(monkeypatch, [], merged_first=True)
    with pytest.raises(RuntimeError, match="share a first token"):
        runner._fast_identity({"focus_track_id": "t"}, [])


def test_fast_identity_matches_token_text_when_ids_are_missing(monkeypatch):
    top = [{"token": "appr", "logprob": -0.2}, {"token": "reje", "logprob": -1.9}]
    runner, _ = _runner(monkeypatch, top)
    answer, _ = runner._fast_identity({"focus_track_id": "t"}, [])
    assert json.loads(answer)["decision"] == "approach_track"


def test_fast_identity_unexpected_response_shape_falls_back(monkeypatch):
    runner, _ = _runner(monkeypatch, [])
    monkeypatch.setattr(
        slow, "_http_json",
        lambda url, body=None, timeout=None: (
            {"tokens": [1, 2, 3, 10 + len(body["content"]) % 3]} if url.endswith("/tokenize")
            else {"content": "x"} if url.endswith("/detokenize") else {"choices": [{}]}
        ),
    )
    prefix = '{"args":{"duration_s":null,"region":null,"track_id":"t"},"decision":"'
    runner._verdict_tokens[prefix] = FIRST
    assert runner._fast_identity({"focus_track_id": "t"}, []) is None


def test_fast_identity_honours_prompt_cache_setting_and_records_usage(monkeypatch):
    top = [{"id": 12, "logprob": -0.01}]
    runner, calls = _runner(monkeypatch, top)
    runner.cache_prompt = False
    original = slow._http_json

    def with_usage(url, body=None, timeout=None):
        response = original(url, body, timeout)
        if url.endswith("/chat/completions"):
            response["usage"] = {"prompt_tokens": 470, "completion_tokens": 1}
        return response

    monkeypatch.setattr(slow, "_http_json", with_usage)
    runner._fast_identity({"focus_track_id": "t"}, [])
    assert calls[-1][1]["cache_prompt"] is False
    assert runner.last_usage == {"prompt_tokens": 470, "completion_tokens": 1}
