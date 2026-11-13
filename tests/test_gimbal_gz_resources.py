import hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from scripts import gimbal_gz_resources as g
from scripts import run_simulator_docker as docker

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.fuel=self.root/'fuel';self.model=self.fuel/'house';(self.model/'meshes').mkdir(parents=True);(self.model/'materials/textures').mkdir(parents=True)
  self.dae=self.model/'meshes/a.dae';self.dae.write_text('<COLLADA><library_images><image><init_from>../materials/textures/a.png</init_from></image></library_images></COLLADA>')
  self.texture=self.model/'materials/textures/a.png';self.texture.write_bytes(b'texture')
  self.world=self.root/'w.sdf';self.world.write_text('<sdf><world><uri>model://house/meshes/a.dae</uri></world></sdf>')
  self.manifest=self.root/'manifest.json';self.write_manifest()
 def write_manifest(self):
  files={str(p.relative_to(self.model)):g.sha(p) for p in self.model.rglob('*') if p.is_file() and not p.is_symlink()}
  self.manifest.write_text(json.dumps({'schema':1,'worlds':{'w.sdf':{'sha256':g.sha(self.world),'models':['house']}},'models':{'house':files}}))
 def check(self):return g.preflight(self.world,self.fuel,self.manifest)
 def test_recurses_from_world_to_collada_texture(self):
  self.assertEqual(self.check()['files'],2)
  self.texture.unlink()
  with self.assertRaises(ValueError):self.check()
 def test_hash_drift_and_missing_reference_fail(self):
  self.texture.write_bytes(b'changed')
  with self.assertRaises(ValueError):self.check()
  self.texture.write_bytes(b'texture');self.dae.write_text('<COLLADA><library_images><image><init_from>missing.png</init_from></image></library_images></COLLADA>');self.write_manifest()
  with self.assertRaises(ValueError):self.check()
 def test_host_only_symlinks_rejected_even_when_host_target_exists(self):
  outside=self.root/'outside.png';outside.write_bytes(b'texture');self.texture.unlink();self.texture.symlink_to(outside)
  with self.assertRaises(ValueError):self.check()
 def test_actual_builder_adds_readonly_fuel_mount_and_env_before_create(self):
  scenario=Path(docker.ROOT)/'configs/scenarios/river_rescue_vtol_001.scenario.json'
  env={'MD_GIMBAL_INVOCATION':'a'*32,'MD_GIMBAL_FUEL_ROOT':str(g.FUEL)}
  with patch.dict(os.environ,env),patch.object(g,'preflight',return_value={}) as preflight:
   plan=docker.build_docker_simulator_plan(scenario,repository_root=docker.ROOT,job_dir=self.root/'job')
  preflight.assert_called_once();self.assertIn('/opt/multidetect-gz-fuel-resolved',dict(plan.environment)['GZ_SIM_RESOURCE_PATH'])
  self.assertTrue(any(str(g.FUEL) in arg and '/opt/multidetect-gz-fuel-resolved' in arg and 'readonly' in arg for arg in plan.command))
 def test_builder_rejects_missing_binding_before_plan(self):
  scenario=Path(docker.ROOT)/'configs/scenarios/river_rescue_vtol_001.scenario.json'
  with patch.dict(os.environ,{'MD_GIMBAL_INVOCATION':'a'*32,'MD_GIMBAL_FUEL_ROOT':'/missing'}),self.assertRaises(docker.DockerSimulatorPlanError):
   docker.build_docker_simulator_plan(scenario,repository_root=docker.ROOT,job_dir=self.root/'job')
 def test_actual_eight_frozen_worlds_resource_closures(self):
  data=json.loads(g.MANIFEST.read_text())
  self.assertEqual(len(data['worlds']),8)
  for name in data['worlds']:
   with self.subTest(world=name):g.preflight(docker.ROOT/'simulation/gazebo/worlds'/name)
  result=g.preflight(docker.ROOT/'simulation/gazebo/worlds/gen_cnt2_w0002.sdf')
  self.assertIn('bus',result['models']);self.assertIn('suv',result['models'])
 def test_obj_mtl_cross_model_texture_missing_fails(self):
  self.dae.unlink();obj=self.model/'meshes/a.obj';obj.write_text('mtllib a.mtl\n')
  mtl=self.model/'meshes/a.mtl';mtl.write_text('map_Kd model://other/materials/textures/wheel.png\n')
  other=self.fuel/'other';(other/'materials/textures').mkdir(parents=True);wheel=other/'materials/textures/wheel.png';wheel.write_bytes(b'wheel')
  self.world.write_text('<sdf><world><uri>model://house/meshes/a.obj</uri></world></sdf>');self.write_manifest()
  data=json.loads(self.manifest.read_text());data['models']['other']={'materials/textures/wheel.png':g.sha(wheel)};self.manifest.write_text(json.dumps(data))
  self.assertIn('other',self.check()['models'])
  wheel.unlink();data['models']['other']={};self.manifest.write_text(json.dumps(data))
  with self.assertRaises(ValueError):self.check()

if __name__=='__main__':unittest.main()
