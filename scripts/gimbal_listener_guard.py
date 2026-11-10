"""Verify a local HTTP listener belongs to the exact spawned child process."""
import os,socket
from pathlib import Path

def preflight(port):
 sockets=[]
 try:
  for family,address in ((socket.AF_INET,('0.0.0.0',port)),(socket.AF_INET6,('::',port))):
   s=socket.socket(family,socket.SOCK_STREAM);sockets.append(s)
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

def guarded_http(port,pid,ticks,executable,group,fetch):
 before=listener_identity(port,pid,ticks,executable,group)
 try:result=fetch()
 except BaseException:
  if listener_identity(port,pid,ticks,executable,group)!=before:raise ValueError('listener changed during failed response')
  raise
 if listener_identity(port,pid,ticks,executable,group)!=before:raise ValueError('listener changed during response')
 return result
