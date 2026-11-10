"""Verify a local HTTP listener belongs to the exact spawned child process."""
import http.client,io,json,math,os,socket,threading,time,urllib.error,urllib.parse
from pathlib import Path

def preflight(port):
 sockets=[]
 try:
  for family,address in ((socket.AF_INET,('0.0.0.0',port)),(socket.AF_INET6,('::',port))):
   s=socket.socket(family,socket.SOCK_STREAM);sockets.append(s)
   s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
   if family==socket.AF_INET6:s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
   s.bind(address)
 finally:
  for s in sockets:s.close()

def startticks(pid):return int((Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19])

def listener_identity(port,pid,ticks,executable,worker_group):
 proc=Path('/proc')/str(pid)
 if startticks(pid)!=ticks or (proc/'cgroup').read_text().strip()!='0::'+worker_group or str((proc/'exe').resolve())!=str(Path(executable).resolve()):raise ValueError('llama process identity changed')
 inodes=[]
 for table in ('tcp','tcp6'):
  for line in (Path('/proc/net')/table).read_text().splitlines()[1:]:
   fields=line.split(); local=fields[1]
   if int(local.rsplit(':',1)[1],16)==port and fields[3]=='0A':inodes.append(fields[9])
 if len(inodes)!=1:raise ValueError('listener missing or ambiguous')
 targets=[]
 for fd in (proc/'fd').iterdir():
  try:targets.append(str(fd.readlink()))
  except FileNotFoundError:pass
 if 'socket:['+inodes[0]+']' not in targets:raise ValueError('listener not owned by spawned child')
 if startticks(pid)!=ticks:raise ValueError('listener PID changed')
 return (pid,ticks,worker_group,str(Path(executable).resolve()),inodes[0])

def remaining(deadline):
 value=deadline-time.monotonic()
 if value<=0:raise TimeoutError('HTTP request deadline exceeded')
 return value

def guarded_http(port,pid,ticks,executable,group,fetch,*,deadline=None):
 if deadline is not None:remaining(deadline)
 before=listener_identity(port,pid,ticks,executable,group)
 if deadline is not None:remaining(deadline)
 try:result=fetch()
 except BaseException:
  if deadline is not None:remaining(deadline)
  if listener_identity(port,pid,ticks,executable,group)!=before:raise ValueError('listener changed during failed response')
  raise
 if deadline is not None:remaining(deadline)
 if listener_identity(port,pid,ticks,executable,group)!=before:raise ValueError('listener changed during response')
 if deadline is not None:remaining(deadline)
 return result

def verify_connected_peer(sock,port,pid,ticks,executable,group,*,deadline):
 """Before sending, locate the accepted reverse tuple in the spawned PID."""
 local=sock.getsockname();remote=sock.getpeername()
 if local[0]!='127.0.0.1' or remote[:2]!=('127.0.0.1',port):raise ValueError('HTTP peer mismatch')
 server=f'0100007F:{port:04X}';client=f'0100007F:{local[1]:04X}'
 while True:
  remaining(deadline)
  listener_identity(port,pid,ticks,executable,group)
  matches=[]
  for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
   fields=line.split()
   if fields[1]==server and fields[2]==client and fields[3]=='01':matches.append(fields[9])
  targets=[]
  for fd in (Path('/proc')/str(pid)/'fd').iterdir():
   try:targets.append(str(fd.readlink()))
   except FileNotFoundError:pass
  remaining(deadline)
  if len(matches)==1 and 'socket:['+matches[0]+']' in targets:return True
  time.sleep(min(.01,remaining(deadline)))

def direct_json(url,port,body=None,timeout=180,*,peer_check=None,deadline=None):
 """No environment proxies, redirects, DNS, credentials, or off-target URL."""
 target=urllib.parse.urlsplit(url)
 if target.scheme!='http' or target.hostname!='127.0.0.1' or target.port!=port or target.username is not None or target.password is not None or target.fragment or not target.path.startswith('/'):
  raise ValueError('HTTP target mismatch')
 if deadline is None:
  if not math.isfinite(timeout) or timeout<=0:raise ValueError('invalid timeout')
  deadline=time.monotonic()+timeout
 conn=http.client.HTTPConnection('127.0.0.1',port,timeout=remaining(deadline));timer=None
 try:
  conn.connect()
  if conn.sock is None or conn.sock.getpeername()[:2]!=('127.0.0.1',port):raise ValueError('HTTP peer mismatch')
  if peer_check is None:raise ValueError('accepted peer check required')
  peer_check(conn.sock,deadline)
  conn.sock.settimeout(remaining(deadline))
  sock=conn.sock
  def interrupt():
   try:sock.shutdown(socket.SHUT_RDWR)
   except OSError:pass
  timer=threading.Timer(remaining(deadline),interrupt);timer.daemon=True;timer.start()
  payload=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
  conn.request('POST' if body is not None else 'GET',target.path+('?' + target.query if target.query else ''),body=payload,headers={'Content-Type':'application/json'})
  response=conn.getresponse();data=response.read();remaining(deadline)
  if 300<=response.status<400:raise ValueError('HTTP redirect forbidden')
  if response.status>=400:raise urllib.error.HTTPError(url,response.status,response.reason,response.headers,io.BytesIO(data))
  if not 200<=response.status<300:raise ValueError('HTTP status unexpected')
  return json.loads(data)
 except (OSError,http.client.HTTPException):
  remaining(deadline)
  raise
 finally:
  if timer is not None:timer.cancel()
  conn.close()
