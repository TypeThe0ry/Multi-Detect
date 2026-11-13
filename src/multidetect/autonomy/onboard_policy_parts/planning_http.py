"""Route candidate planning calls through the owned runner's HTTP guard."""
import os

def planning_http(runner,legacy,url,body,*,timeout,runner_type):
 # runner_type is injected by the scripts facade; policy parts must not import scripts.
 if os.environ.get('MD_GIMBAL_INVOCATION') is None:
  return legacy(url,body,timeout=timeout)
 if not isinstance(runner,runner_type):raise ValueError('candidate planning runner identity unknown')
 return runner._http_json(url,body,timeout=timeout)
