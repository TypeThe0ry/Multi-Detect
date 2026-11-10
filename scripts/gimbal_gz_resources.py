"""Offline, candidate-only closure and hash validation for Gazebo resources."""
import hashlib,json,os,re,shlex,xml.etree.ElementTree as ET
from pathlib import Path

FUEL=Path('/data/projects/Multi-Detect-env/Multi-Detect/artifacts/cache/gz-fuel/resolved')
MANIFEST=Path(__file__).resolve().parents[1]/'configs/onboard/gimbal-gz-resource-manifest.json'
SAFE_NAME=re.compile(r'^[A-Za-z0-9_.-]+$')

def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 return h.hexdigest()

def preflight(world,fuel=FUEL,manifest=MANIFEST):
 """Reject missing, changed or container-invisible resources before create.

 All model trees are pinned by the source-sealed manifest; links are rejected
 because an absolute host symlink may not resolve in a container bind mount.
 XML model:// references and Collada image and material texture dependencies
 are followed recursively. Unsupported reference forms fail closed.
 """
 world=Path(world);fuel=Path(fuel)
 if not fuel.is_dir() or fuel.is_symlink():raise ValueError('fuel root missing or symlink')
 root=fuel.resolve(strict=True);data=json.loads(Path(manifest).read_bytes())
 if data.get('schema')!=1:raise ValueError('resource manifest version')
 spec=data.get('worlds',{}).get(world.name)
 if not spec or sha(world)!=spec['sha256']:raise ValueError('world not bound or drifted')
 known=data['models'];seen=set();active=set();world_models=set()
 def checked(path):
  path=Path(path)
  try:relative=path.relative_to(root)
  except ValueError:raise ValueError('resource escapes mount') from None
  part=root
  for name in relative.parts:
   if name in ('..','.') or not name:raise ValueError('unsafe resource path')
   part=part/name
   if part.is_symlink():raise ValueError('resource symlink not allowed')
  if not path.exists():raise ValueError('missing resource: '+str(relative))
  return path
 def verify_tree(name):
  if name in active:return
  if name not in known or not SAFE_NAME.fullmatch(name):raise ValueError('unbound model: '+name)
  base=checked(root/name)
  if not base.is_dir():raise ValueError('model root not directory')
  found={}
  for directory,dirs,files in os.walk(base,followlinks=False,onerror=lambda e:(_ for _ in ()).throw(e)):
   for item in sorted(dirs+files):
    p=checked(Path(directory)/item)
    if p.is_file():found[str(p.relative_to(base))]=sha(p)
    elif not p.is_dir():raise ValueError('unsupported resource type')
  if found!=known[name]:raise ValueError('model file inventory/hash drift: '+name)
  active.add(name)
 def uri(value):
  if not value.startswith('model://'):raise ValueError('unsupported resource URI')
  part=value[8:];name,sep,rel=part.partition('/')
  if not SAFE_NAME.fullmatch(name) or (sep and (not rel or '\\' in rel or '%' in rel or any(x in ('','.','..') for x in rel.split('/')))):raise ValueError('unsafe model URI')
  verify_tree(name)
  path=checked(root/name/rel) if sep else checked(root/name/'model.config')
  follow(path,name)
 def relative_file(base,name,value):
  if not value or value.startswith(('/','file:','http:','https:','model:')) or '\\' in value or '%' in value:raise ValueError('unsafe nested resource')
  candidates=(base.parent/value,root/name/'materials/textures'/value)
  for candidate in candidates:
   candidate=Path(os.path.abspath(candidate))
   if not os.path.lexists(candidate):continue
   p=checked(candidate)
   if p.is_file():
    verify_tree(p.relative_to(root).parts[0])
    return p
  raise ValueError('nested resource missing: '+value)
 def follow(path,name):
  path=checked(path)
  name=path.relative_to(root).parts[0]
  if path in seen:return
  seen.add(path)
  if path.is_dir():
   for directory,dirs,files in os.walk(path,followlinks=False,onerror=lambda e:(_ for _ in ()).throw(e)):
    for item in sorted(dirs+files):
     p=checked(Path(directory)/item)
     if p.is_file():follow(p,name)
   return
  suffix=path.suffix.lower()
  if suffix in ('.sdf','.config','.dae'):
   tree=ET.parse(path)
   for e in tree.iter():
    text=(e.text or '').strip()
    if text.startswith('model://'):uri(text)
    for value in e.attrib.values():
     if value.startswith('model://'):uri(value)
    if suffix=='.config' and e.tag.rsplit('}',1)[-1]=='sdf' and text:follow(relative_file(path,name,text),name)
    if suffix=='.dae' and e.tag.rsplit('}',1)[-1]=='image':
     for child in e:
      if child.tag.rsplit('}',1)[-1]=='init_from' and (child.text or '').strip():follow(relative_file(path,name,child.text.strip()),name)
  elif suffix=='.material':
   for line in path.read_text(encoding='utf-8').splitlines():
    stripped=line.strip()
    if stripped.startswith('texture '):
     tokens=shlex.split(stripped,comments=True)
     if len(tokens)<2:raise ValueError('invalid material texture')
     follow(relative_file(path,name,tokens[1]),name)
  elif suffix=='.obj':
   for line in path.read_text(encoding='utf-8').splitlines():
    tokens=shlex.split(line.strip(),comments=True)
    if tokens and tokens[0].lower()=='mtllib':
     if len(tokens)<2:raise ValueError('empty OBJ material library')
     for value in tokens[1:]:
      if value.startswith('model://'):uri(value)
      else:follow(relative_file(path,name,value),name)
  elif suffix=='.mtl':
   for line in path.read_text(encoding='utf-8').splitlines():
    tokens=shlex.split(line.strip(),comments=True)
    if not tokens:continue
    key=tokens[0].lower()
    if key.startswith('map_') or key in ('bump','norm','disp','decal','refl'):
     if len(tokens)!=2 or tokens[1].startswith('-'):raise ValueError('unsupported MTL texture syntax')
     if tokens[1].startswith('model://'):uri(tokens[1])
     else:follow(relative_file(path,name,tokens[1]),name)
 tree=ET.parse(world)
 for e in tree.iter():
  values=[(e.text or '').strip(),*e.attrib.values()]
  for value in values:
   if value.startswith('model://'):
    world_models.add(value[8:].split('/')[0]);uri(value)
 if world_models!=set(spec['models']):raise ValueError('world model set drift')
 return {'world':world.name,'models':sorted(active),'files':len(seen)}
