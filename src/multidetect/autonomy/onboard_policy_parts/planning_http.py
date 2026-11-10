"""Route candidate planning calls through the owned runner's HTTP guard."""
import os

def planning_http(runner,legacy,url,body,*,timeout):
 if os.environ.get('MD_GIMBAL_INVOCATION') is None:
  return legacy(url,body,timeout=timeout)
 from scripts.eval_slow_system import LlamaServerRunner
 if not isinstance(runner,LlamaServerRunner):raise ValueError('candidate planning runner identity unknown')
 return runner._http_json(url,body,timeout=timeout)
