import os,socket,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from scripts import gimbal_listener_guard as g

class ListenerTests(unittest.TestCase):
 def setUp(self):
  self.sock=socket.socket();self.sock.bind(('127.0.0.1',0));self.sock.listen();self.addCleanup(self.sock.close)
  self.port=self.sock.getsockname()[1];self.pid=os.getpid();self.start=g.startticks(self.pid)
  self.group=Path('/proc/self/cgroup').read_text().strip()[3:];self.exe=os.readlink('/proc/self/exe')
 def test_local_listener_and_health_binding(self):
  seen=g.listener_identity(self.port,self.pid,self.start,self.exe,self.group)
  self.assertEqual(seen[0],self.pid)
  self.assertEqual(g.guarded_http(self.port,self.pid,self.start,self.exe,self.group,lambda:{'status':'ok'}),{'status':'ok'})
 def test_foreign_pid_and_changed_listener_rejected(self):
  with self.assertRaises(ValueError):g.listener_identity(self.port,self.pid,self.start+1,self.exe,self.group)
  with self.assertRaises(ValueError):g.guarded_http(self.port,self.pid,self.start,self.exe,self.group,lambda:self.sock.close())
 def test_preflight_rejects_occupied_port(self):
  with self.assertRaises(OSError):g.preflight(self.port)
 def test_real_runner_health_path_checks_listener(self):
  from scripts.eval_slow_system import LlamaServerRunner
  from http.server import BaseHTTPRequestHandler,HTTPServer
  import threading
  self.sock.close()
  class Handler(BaseHTTPRequestHandler):
   def do_GET(self):
    self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
   def log_message(self,*args):pass
  server=HTTPServer(('127.0.0.1',0),Handler);self.addCleanup(server.server_close)
  thread=threading.Thread(target=server.handle_request);thread.start()
  runner=LlamaServerRunner.__new__(LlamaServerRunner);runner.port=server.server_port
  runner.process=SimpleNamespace(pid=self.pid);runner._gimbal_startticks=self.start;runner._gimbal_cgroup=self.group;runner._command=[self.exe]
  with patch.dict(os.environ,{'MD_GIMBAL_INVOCATION':'a'*32}):self.assertTrue(runner.probe_health(timeout_s=2))
  thread.join(timeout=2);self.assertFalse(thread.is_alive())
 def test_real_runner_preflight_precedes_spawn(self):
  from scripts.eval_slow_system import LlamaServerRunner
  runner=LlamaServerRunner.__new__(LlamaServerRunner);runner.port=self.port;runner._model=Path('/unused')
  with patch.dict(os.environ,{'MD_GIMBAL_INVOCATION':'a'*32}), patch('scripts.eval_slow_system.subprocess.Popen') as spawn:
   with self.assertRaises(OSError):runner._start()
  spawn.assert_not_called()
if __name__=='__main__':unittest.main()
