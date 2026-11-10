from types import SimpleNamespace
import pytest
from multidetect.autonomy.onboard_policy_parts.planning_http import planning_http

def test_candidate_refuses_unknown_runner_without_legacy(monkeypatch):
 monkeypatch.setenv('MD_GIMBAL_INVOCATION','a'*32)
 def legacy(*a,**k):pytest.fail('legacy HTTP used')
 with pytest.raises(ValueError,match='runner identity'):
  planning_http(SimpleNamespace(port=9941),legacy,'http://127.0.0.1:9941/health',{},timeout=1)

def test_non_candidate_preserves_legacy(monkeypatch):
 monkeypatch.delenv('MD_GIMBAL_INVOCATION',raising=False)
 assert planning_http(None,lambda url,body,timeout:(url,body,timeout),'url',{'x':1},timeout=3)==('url',{'x':1},3)
