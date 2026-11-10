import hashlib,os,tempfile,unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from scripts.run_mission_suite import merge_config
from multidetect.autonomy.onboard_policy_parts import runtime_assets

class Tests(unittest.TestCase):
 def test_frozen_suite_runtime_bindings_and_missing_fail(self):
  base={'mission':'from_text'};mission={'id':'x','steps':2}
  env={'MD_GIMBAL_INVOCATION':'a'*32,'MD_GIMBAL_LLAMA_SERVER':'/opt/llama/llama-server','MD_GIMBAL_MMPROJ':'/opt/models/mmproj.gguf'}
  with patch.dict(os.environ,env,clear=True):
   cfg=merge_config(base,mission)
   self.assertEqual(cfg['llama_server'],env['MD_GIMBAL_LLAMA_SERVER']);self.assertEqual(cfg['mmproj'],env['MD_GIMBAL_MMPROJ'])
   self.assertEqual(base,{'mission':'from_text'});self.assertEqual(mission,{'id':'x','steps':2})
   del os.environ['MD_GIMBAL_MMPROJ']
   with self.assertRaises(ValueError):merge_config(base,mission)
 def test_clip_is_local_hashed_and_unexpected_download_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   f=Path(tmp)/'clip';f.write_bytes(b'clip-fixture');digest=hashlib.sha256(f.read_bytes()).hexdigest();yolo=Path(tmp)/'best.pt';yolo.write_bytes(b'yolo-fixture');yolo_hash=hashlib.sha256(yolo.read_bytes()).hexdigest()
   downloads=ModuleType('ultralytics.utils.downloads');utils=ModuleType('ultralytics.utils');utils.downloads=downloads;ultra=ModuleType('ultralytics');ultra.utils=utils
   modules={'ultralytics':ultra,'ultralytics.utils':utils,'ultralytics.utils.downloads':downloads}
   with patch.dict('sys.modules',modules),patch.dict(os.environ,{'MD_GIMBAL_CLIP_FILE':str(f),'MD_GIMBAL_YOLOE_FILE':str(yolo)}),patch.object(runtime_assets,'CLIP_SHA256',digest),patch.object(runtime_assets,'YOLO_SHA256',yolo_hash):
    runtime_assets.configure_candidate_clip(yolo);self.assertEqual(downloads.attempt_download_asset('mobileclip_blt.ts'),str(f));self.assertEqual(downloads.attempt_download_asset(str(yolo)),str(yolo))
    with self.assertRaises(ValueError):downloads.attempt_download_asset('other.pt')
    with self.assertRaises(RuntimeError):downloads.safe_download('https://example.invalid')
    f.write_bytes(b'tampered')
    with self.assertRaises(ValueError):runtime_assets.configure_candidate_clip(yolo)
 def test_proposer_calls_guard_before_model_creation(self):
  from multidetect.autonomy.onboard_policy_parts.proposer import YoloeProposer
  events=[]
  class FakeModel:
   def __init__(self,weights):events.append('model')
   def to(self,device):pass
   def get_text_pe(self,prompts):return []
   def set_classes(self,prompts,embedding):pass
  ultra=ModuleType('ultralytics');ultra.YOLOE=FakeModel
  with patch.dict('sys.modules',{'ultralytics':ultra}),patch.dict(os.environ,{'MD_GIMBAL_INVOCATION':'a'*32}),patch.object(runtime_assets,'configure_candidate_clip',side_effect=lambda weights:events.append('offline_guard')):
   YoloeProposer(Path('/tmp/weights'),confidence=.1,imgsz=640,device='cpu')
  self.assertEqual(events,['offline_guard','model'])
 def test_real_ultralytics_yoloe_load_reaches_local_torch_load(self):
  from ultralytics import YOLOE
  from ultralytics.nn import tasks
  from ultralytics.utils import downloads
  class ReachedLocalLoader(Exception):pass
  with tempfile.TemporaryDirectory() as tmp:
   clip=Path(tmp)/'clip';clip.write_bytes(b'clip-fixture')
   yolo=Path(tmp)/'best.pt';yolo.write_bytes(b'yolo-fixture')
   env={'MD_GIMBAL_CLIP_FILE':str(clip),'MD_GIMBAL_YOLOE_FILE':str(yolo)}
   with patch.dict(os.environ,env),patch.object(runtime_assets,'CLIP_SHA256',hashlib.sha256(clip.read_bytes()).hexdigest()),patch.object(runtime_assets,'YOLO_SHA256',hashlib.sha256(yolo.read_bytes()).hexdigest()),patch.object(downloads,'attempt_download_asset'),patch.object(downloads,'safe_download'),patch.object(tasks,'torch_load',side_effect=ReachedLocalLoader) as loader:
    runtime_assets.configure_candidate_clip(yolo)
    with self.assertRaises(ReachedLocalLoader):YOLOE(str(yolo))
    self.assertEqual(loader.call_args.args[0],str(yolo))
