import os,socket,sys,time,unittest
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
 def test_preflight_allows_restart_after_server_time_wait(self):
  listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  listener.bind(('127.0.0.1',0));listener.listen();port=listener.getsockname()[1]
  client=socket.create_connection(('127.0.0.1',port),timeout=2)
  accepted,_=listener.accept();accepted.close();client.recv(1);client.close();listener.close()
  rows=Path('/proc/net/tcp').read_text().splitlines()[1:]
  self.assertTrue(any(line.split()[1].endswith(':%04X'%port) and line.split()[3]=='06' for line in rows))
  g.preflight(port)
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
  with patch.dict(os.environ,{'MD_GIMBAL_INVOCATION':'a'*32,'http_proxy':'http://127.0.0.1:1','HTTP_PROXY':'http://127.0.0.1:1','no_proxy':'','NO_PROXY':''}):self.assertTrue(runner.probe_health(timeout_s=2))
  thread.join(timeout=2);self.assertFalse(thread.is_alive())
 def test_redirect_and_off_target_rejected(self):
  from http.server import BaseHTTPRequestHandler,HTTPServer
  import threading
  self.sock.close()
  class Handler(BaseHTTPRequestHandler):
   def do_GET(self):self.send_response(302);self.send_header('Location','http://127.0.0.1:1/health');self.end_headers()
   def log_message(self,*args):pass
  server=HTTPServer(('127.0.0.1',0),Handler);self.addCleanup(server.server_close)
  thread=threading.Thread(target=server.handle_request);thread.start()
  with self.assertRaisesRegex(ValueError,'redirect'):
   g.direct_json(f'http://127.0.0.1:{server.server_port}/health',server.server_port,timeout=2,peer_check=lambda sock,deadline:g.verify_connected_peer(sock,server.server_port,self.pid,self.start,self.exe,self.group,deadline=deadline))
  thread.join(timeout=2);self.assertFalse(thread.is_alive())
  with self.assertRaises(ValueError):g.direct_json('http://localhost:1/health',1,timeout=2)
 def test_peer_failure_precedes_http_send(self):
  import threading
  received=[]
  def accept():
   conn,_=self.sock.accept()
   with conn:received.append(conn.recv(1024))
  thread=threading.Thread(target=accept);thread.start()
  def reject(sock,deadline):raise ValueError('foreign peer')
  with self.assertRaisesRegex(ValueError,'foreign peer'):
   g.direct_json(f'http://127.0.0.1:{self.port}/health',self.port,{'secret':'payload'},2,peer_check=reject)
  thread.join(timeout=2);self.assertEqual(received,[b''])
 def test_nonaccepting_listener_obeys_shared_deadline(self):
  from scripts.eval_slow_system import LlamaServerRunner
  runner=LlamaServerRunner.__new__(LlamaServerRunner);runner.port=self.port
  runner.process=SimpleNamespace(pid=self.pid);runner._gimbal_startticks=self.start;runner._gimbal_cgroup=self.group;runner._command=[self.exe]
  began=time.monotonic()
  with patch.dict(os.environ,{'MD_GIMBAL_INVOCATION':'a'*32}):self.assertFalse(runner.probe_health(timeout_s=.1))
  self.assertLess(time.monotonic()-began,.35)
 def test_slow_body_obeys_total_deadline(self):
  from scripts.eval_slow_system import LlamaServerRunner
  from http.server import BaseHTTPRequestHandler,HTTPServer
  import threading
  self.sock.close()
  class Handler(BaseHTTPRequestHandler):
   def do_GET(self):
    self.send_response(200);self.send_header('Content-Length','8');self.end_headers()
    self.wfile.write(b'{');self.wfile.flush();time.sleep(.35)
    try:self.wfile.write(b'       ')
    except OSError:pass
   def log_message(self,*args):pass
  server=HTTPServer(('127.0.0.1',0),Handler);self.addCleanup(server.server_close)
  thread=threading.Thread(target=server.handle_request);thread.start()
  runner=LlamaServerRunner.__new__(LlamaServerRunner);runner.port=server.server_port
  runner.process=SimpleNamespace(pid=self.pid);runner._gimbal_startticks=self.start;runner._gimbal_cgroup=self.group;runner._command=[self.exe]
  began=time.monotonic()
  with patch.dict(os.environ,{'MD_GIMBAL_INVOCATION':'a'*32}):self.assertFalse(runner.probe_health(timeout_s=.1))
  self.assertLess(time.monotonic()-began,.3);thread.join(timeout=2)
 def test_real_runner_preflight_precedes_spawn(self):
  from scripts.eval_slow_system import LlamaServerRunner
  runner=LlamaServerRunner.__new__(LlamaServerRunner);runner.port=self.port;runner._model=Path('/unused')
  with patch.dict(os.environ,{'MD_GIMBAL_INVOCATION':'a'*32}), patch('scripts.eval_slow_system.subprocess.Popen') as spawn:
   with self.assertRaises(OSError):runner._start()
  spawn.assert_not_called()
if __name__=='__main__':unittest.main()
