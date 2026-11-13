from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from scripts.compare_identity_user_gate import compare,sha,metric


def bundle(tmp_path, mode='model_control', candidate_side=448):
    evaluator=tmp_path/'eval.py';evaluator.write_text('fixture evaluator')
    runtime=tmp_path/'runtime.py';runtime.write_text('fixture input processor')
    mmproj=tmp_path/'mmproj.bin';mmproj.write_bytes(b'projection')
    image=tmp_path/'image.png';Image.new('RGB',(100,100)).save(image)
    pairs=[]
    for name in ('v1','v3','four_view','r9r_val'):
        folder=tmp_path/name;folder.mkdir()
        data=[]
        for index in range(20):
            positive=index<10
            data.append({'sample_id':f's{index}','split':'test','world':'held',
                'image_path':str(image),'image_size':{'width':100,'height':100},
                'task_text':'find object','focus_track_id':'t','candidates':[{'track_id':'t','box_xyxy_normalized':[.1,.1,.14,.14]}],
                'target_id':'positive' if positive else 'negative',
                'view':('front','oblique','edge_truncated','recovery')[index%4],
                'label_type':'true_target' if positive else 'distractor',
                'assistant_target':{'decision':'confirm_target' if positive else 'reject_track'}})
        dataset=folder/'test.jsonl';dataset.write_text(''.join(json.dumps(r)+'\n' for r in data))
        pair={'name':name,'dataset':str(dataset)}
        for role,nmatch,side in [('baseline',8,448),('candidate',9,candidate_side)]:
            output=folder/(role+'.json')
            predictions=[{'sample_id':r['sample_id'],'world':'held','valid':True,'latency_s':.01,
                          'expected':r['assistant_target']['decision'],
                          'decision':'confirm_target' if i<nmatch else 'reject_track'} for i,r in enumerate(data)]
            output.write_text(json.dumps({'rows':predictions}))
            argv=['python',str(evaluator),'--dataset',str(folder),'--data-root',str(tmp_path),
                  '--output',str(output),'--max-side',str(side),'--crop-policy','candidate_zoom_v2',
                  '--backend','gguf','--identity-only','--fast-identity','--mmproj',str(mmproj)]
            provenance=folder/(role+'-source.json')
            provenance.write_text(json.dumps({'command':argv,'evaluator':str(evaluator),'runtime':str(runtime),
                'evaluator_sha256':sha(evaluator),'runtime_sha256':sha(runtime),'mmproj_sha256':sha(mmproj),
                'result_sha256':sha(output),'dataset_sha256':sha(dataset)}))
            pair[role]=str(output);pair[role+'_provenance']=str(provenance)
        pairs.append(pair)
    path=tmp_path/'bundle.json';path.write_text(json.dumps({'mode':mode,'pairs':pairs}))
    return path


def test_user_criteria_do_not_include_legacy_absolute_point_nine_gate(tmp_path):
    result=compare(bundle(tmp_path))
    assert result['user_rule_point_estimate_status']=='pass_observed_scope'
    assert result['legacy_absolute_diagnostics']['four_view']['candidate_pass'] is False
    assert result['legacy_absolute_diagnostics']['four_view']['used_in_user_rule'] is False
    assert any('no comparable observations' in item for item in result['limitations'])
    assert any('low sample denominator' in item for item in result['limitations'])
    assert result['automatically_replace_model'] is False


def test_strict_non_regression_has_no_legacy_two_percent_tolerance(tmp_path):
    path=bundle(tmp_path);doc=json.loads(path.read_text());pair=doc['pairs'][0]
    output=Path(pair['candidate']);pred=json.loads(output.read_text());pred['rows'][10]['decision']='confirm_target'
    output.write_text(json.dumps(pred))
    source=Path(pair['candidate_provenance']);protocol=json.loads(source.read_text());protocol['result_sha256']=sha(output);source.write_text(json.dumps(protocol))
    result=compare(path)
    assert result['user_rule_point_estimate_status']=='fail'
    rejected=next(m for m in result['metrics'] if m['metric']=='v1.distractor_rejection_rate')
    assert rejected['direction']=='higher' and rejected['status']=='regressed'


def test_matched_model_control_rejects_unmatched_resolution(tmp_path):
    result=compare(bundle(tmp_path,candidate_side=672))
    assert result['user_rule_point_estimate_status']=='pending_evidence'
    assert any('processing mismatch' in issue for issue in result['evidence_issues'])


def test_deployment_resolution_change_is_allowed_with_explicit_config_and_resource_evidence(tmp_path):
    path=bundle(tmp_path,mode='deployment_improvement',candidate_side=672)
    doc=json.loads(path.read_text())
    config=tmp_path/'deployment.json';config.write_text('{"identity_max_side":672}')
    resource=tmp_path/'resource.json';resource.write_text('{"max_side":672,"measurement":"fixture"}')
    doc['deployment']={'config':str(config),'config_sha256':sha(config),
                       'resource_evidence':str(resource),'resource_evidence_sha256':sha(resource),
                       'change':'full-frame 448 -> 672; candidate crops remain 224; review latency/VRAM'}
    path.write_text(json.dumps(doc))
    result=compare(path)
    assert result['user_rule_point_estimate_status']=='pass_observed_scope'
    assert 'combined model' in result['processing']['v3']['attribution']
    resource.unlink()
    assert compare(path)['user_rule_point_estimate_status']=='pending_evidence'


def test_missing_processing_source_or_sample_is_not_a_pass(tmp_path):
    path=bundle(tmp_path);doc=json.loads(path.read_text())
    Path(doc['pairs'][0]['baseline_provenance']).unlink()
    assert compare(path)['user_rule_point_estimate_status']=='pending_evidence'


def test_exact_ten_percentage_points_passes_with_only_machine_precision_allowance(tmp_path):
    report=compare(bundle(tmp_path))
    recall=next(m for m in report['metrics'] if m['metric']=='v3.under12_true_target_recall')
    assert recall['baseline']==.8 and recall['candidate']==.9
    assert recall['delta']<.1  # IEEE subtraction, although the exact fraction gain is 1/10.
    assert recall['status']=='pass_observed'
    assert metric('below',.6,.69999999,'higher',100,100,True,.1)['status']=='regressed'


def test_processing_cannot_be_mixed_across_deployment_evaluation_groups(tmp_path):
    path=bundle(tmp_path,mode='deployment_improvement')
    payload=json.loads(path.read_text())
    provenance=Path(payload['pairs'][0]['candidate_provenance'])
    doc=json.loads(provenance.read_text());index=doc['command'].index('--max-side')+1
    doc['command'][index]='672';provenance.write_text(json.dumps(doc))
    report=compare(path)
    assert report['user_rule_point_estimate_status']=='pending_evidence'
    assert any('across evaluation groups' in issue for issue in report['evidence_issues'])
