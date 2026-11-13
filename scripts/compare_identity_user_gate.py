#!/usr/bin/env python3
"""Compare observed user criteria; keep legacy absolute gates and attribution separate."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from scripts.eval_slow_system import decision_metrics, size_bucket_metrics, _candidate_short_side_px
from scripts.eval_r21id_identity import evaluate as four_view_evaluate
from multidetect.autonomy.onboard_decision import identity_of

REQUIRED=('v1','v3','four_view','r9r_val')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def option(command,name,default=None):
    return command[command.index(name)+1] if name in command else default


def processing(path,result,dataset):
    """Require a recorded command and hashes; legacy harnesses omit resolution."""
    doc=read(path);command=doc['command']
    if not isinstance(command,list) or not all(isinstance(x,str) for x in command):
        raise ValueError('provenance command must be an argv array')
    for name in ('--dataset','--output','--max-side','--crop-policy','--backend','--data-root','--identity-only','--fast-identity'):
        if name not in command:raise ValueError(f'explicit processing source missing {name}')
    if Path(option(command,'--dataset')).resolve()!=Path(dataset).parent.resolve() or \
       Path(option(command,'--output')).resolve()!=Path(result).resolve():
        raise ValueError('recorded evaluation input/output paths do not match the compared files')
    for field,actual in [('result_sha256',sha(result)),('dataset_sha256',sha(dataset)),
                         ('evaluator_sha256',sha(doc['evaluator'])),('runtime_sha256',sha(doc['runtime']))]:
        if doc.get(field)!=actual:raise ValueError(f'processing provenance hash mismatch: {field}')
    mmproj=option(command,'--mmproj')
    if mmproj is None or doc.get('mmproj_sha256')!=sha(mmproj):
        raise ValueError('missing/unverified projection processing source')
    return {'max_side':int(option(command,'--max-side')),'crop_policy':option(command,'--crop-policy'),
            'backend':option(command,'--backend'),'identity_variant':option(command,'--identity-variant','row/default'),
            'fast_identity':True,'evaluator_sha256':doc['evaluator_sha256'],'runtime_sha256':doc['runtime_sha256'],
            'dataset_sha256':doc['dataset_sha256'],'mmproj_sha256':doc['mmproj_sha256']}


def metric(name,b,c,direction,n_b,n_c,required=False,minimum_delta=0):
    numeric=all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (b,c))
    measurable=numeric and n_b>0 and n_c>0
    delta=c-b if measurable else None
    status='not_evaluable'
    if measurable:
        # Machine-precision allowance only: e.g. 0.9-0.8 is slightly below 0.1.
        status='pass_observed' if ((delta+1e-12>=minimum_delta) if direction=='higher' else delta<=-minimum_delta+1e-12) else 'regressed'
    return {'metric':name,'baseline':b,'candidate':c,'delta':delta,'direction':direction,
            'baseline_n':n_b,'candidate_n':n_c,'minimum_delta':minimum_delta,'required':required,
            'status':status,'low_sample_warning':measurable and min(n_b,n_c)<10}


def compare(bundle_path):
    bundle=read(bundle_path);mode=bundle.get('mode')
    if mode not in ('model_control','deployment_improvement'):raise ValueError('choose an explicit comparison mode')
    result={'mode':mode,'criteria':'under12 recall delta >=0.10; other observed identity metrics do not regress',
            'processing':{},'metrics':[],'evidence_issues':[],'limitations':[],
            'legacy_absolute_diagnostics':{},'automatically_replace_model':False}
    pairs={p['name']:p for p in bundle.get('pairs',[])}
    missing=set(REQUIRED)-set(pairs)
    if missing:result['evidence_issues'].append('missing evaluation groups: '+','.join(sorted(missing)))
    for name,pair in pairs.items():
        if name not in REQUIRED:raise ValueError(f'unknown evaluation group {name}')
        try:
            dataset=Path(pair['dataset']);data=[json.loads(l) for l in dataset.read_text().splitlines() if l.strip()]
            by_id={r['sample_id']:r for r in data}
            if len(by_id)!=len(data):raise ValueError('duplicate dataset IDs')
            docs=[read(pair[role]) for role in ('baseline','candidate')]
            protocols=[processing(pair[role+'_provenance'],pair[role],dataset) for role in ('baseline','candidate')]
            a,b=protocols
            compare_a,compare_b=dict(a),dict(b)
            if mode=='deployment_improvement':compare_a.pop('max_side');compare_b.pop('max_side')
            if compare_a!=compare_b:raise ValueError('processing mismatch for selected comparison mode')
            result['processing'][name]={'baseline':a,'candidate':b,
                'attribution':'weights under matched processing' if mode=='model_control' else 'combined model and declared deployment processing'}
            for doc in docs:
                predictions=doc.get('rows',[])
                if len(predictions)!=len(data) or len({r['sample_id'] for r in predictions})!=len(data) or {r['sample_id'] for r in predictions}!=set(by_id):
                    raise ValueError('prediction IDs/count differ from frozen dataset')
                if any(r.get('harness_error') or r.get('expected')!=by_id[r['sample_id']]['assistant_target']['decision'] for r in predictions):
                    raise ValueError('harness error or changed expected labels')
            if name in ('v1','v3'):
                measured=[decision_metrics(doc['rows']) for doc in docs]
                expected=list(docs[0]['rows'])
                ns={'identity_accuracy':sum(identity_of(r['expected']) is not None for r in expected),
                    'distractor_rejection_rate':sum(r['expected']=='reject_track' for r in expected),
                    'identity_match_recall':sum(identity_of(r['expected'])=='match' for r in expected),
                    'json_validity':len(expected)}
                for key,n in ns.items():
                    result['metrics'].append(metric(name+'.'+key,measured[0][key],measured[1][key],'higher',n,n,True))
                if name=='v3':
                    root=Path(option(read(pair['baseline_provenance'])['command'],'--data-root'))
                    image_data=[{**r,'image_path':str((root/r['image_path']).resolve())} if r.get('image_path') else r for r in data]
                    buckets=[size_bucket_metrics(doc['rows'],image_data)['<12'] for doc in docs]
                    small_ids={r['sample_id'] for r in image_data if (size:=_candidate_short_side_px(r)) is not None and size<12
                               and identity_of(r['assistant_target']['decision'])=='match'}
                    n=len(small_ids)
                    result['metrics'].append(metric('v3.under12_true_target_recall',buckets[0]['true_target_recall'],
                        buckets[1]['true_target_recall'],'higher',n,n,True,.10))
            else:
                report=four_view_evaluate(dataset,Path(pair['baseline']),Path(pair['candidate']),'baseline','candidate')
                for key,direction in [('multiview_consistency','higher'),('recovery_rate','higher'),
                                      ('true_target_false_reject','lower'),('distractor_false_accept','lower'),
                                      ('fire_hydrant_false_reject','lower')]:
                    a,b=report['baseline'][key],report['candidate'][key]
                    item=metric(name+'.'+key,a['value'],b['value'],direction,a['count'],b['count'])
                    item.update(baseline_ci95=a.get('ci95'),candidate_ci95=b.get('ci95'))
                    result['metrics'].append(item)
                a,b=report['baseline'],report['candidate']
                result['metrics'].append(metric(name+'.average_recovery_observations',a['average_recovery_observations'],
                    b['average_recovery_observations'],'lower',a['recovery_rate']['count'],b['recovery_rate']['count']))
                result['legacy_absolute_diagnostics'][name]={'candidate_pass':report['candidate_pass'],
                    'reasons':report['reasons'],'thresholds':report['thresholds'],
                    'used_in_user_rule':False}
        except (OSError,ValueError,KeyError,TypeError,IndexError) as exc:
            result['evidence_issues'].append(name+': '+str(exc))
    # A deployment must use one processing setup across all evaluation groups.
    for role in ('baseline','candidate'):
        signatures=[{k:v for k,v in item[role].items() if k!='dataset_sha256'} for item in result['processing'].values()]
        if signatures and any(s!=signatures[0] for s in signatures[1:]):
            result['evidence_issues'].append(role+': processing differs across evaluation groups; do not mix scopes')
    if mode=='deployment_improvement':
        deployment=bundle.get('deployment',{})
        for label in ('config','resource_evidence'):
            try:
                if deployment[label+'_sha256']!=sha(deployment[label]):raise ValueError('hash mismatch')
            except (OSError,ValueError,KeyError) as exc:
                result['evidence_issues'].append('deployment '+label+': '+str(exc))
        result['deployment_change']=deployment
    observed=[m for m in result['metrics'] if m['status']=='regressed']
    result['observed_regressions']=[m['metric'] for m in observed]
    missing_metrics=[m for m in result['metrics'] if m['status']=='not_evaluable']
    result['limitations'] += [m['metric']+': no comparable observations; not marked pass' for m in missing_metrics]
    result['limitations'] += [m['metric']+f': low sample denominator {m["baseline_n"]}/{m["candidate_n"]}; point estimates only'
                              for m in result['metrics'] if m['low_sample_warning']]
    if result['evidence_issues'] or any(m['required'] for m in missing_metrics):
        result['user_rule_point_estimate_status']='pending_evidence'
    elif observed:
        result['user_rule_point_estimate_status']='fail'
    else:
        result['user_rule_point_estimate_status']='pass_observed_scope'
    result['recommendation']='review limitations and actual deployment before replacement' if result['user_rule_point_estimate_status']=='pass_observed_scope' else 'keep deployed r3 pending evidence or improved candidate'
    return result


def markdown(report):
    lines=['# 身份模型用户门槛比较','',f'口径：{report["mode"]}；结论：**{report["user_rule_point_estimate_status"]}**。',
           '','用户条件：<12px真目标召回至少+10pp；其它已观测身份指标零退步。拒绝真目标越低越好，拒绝干扰物越高越好。',
           '','| 指标 | 基线 | 候选 | 差值 | 方向 | n基线/候选 | 判定 |',
           '| --- | ---: | ---: | ---: | --- | ---: | --- |']
    for m in report['metrics']:
        lines.append(f'| {m["metric"]} | {m["baseline"]} | {m["candidate"]} | {m["delta"]} | {m["direction"]} | {m["baseline_n"]}/{m["candidate_n"]} | {m["status"]} |')
    lines += ['','处理来源、分辨率、部署配置/算力证据见JSON。deployment_improvement包含处理收益，不能表述为纯权重收益。',
              '','## 待验收/小样本边界','']+[f'- {x}' for x in report['evidence_issues']+report['limitations']]
    lines += ['','## 旧绝对门槛（仅风险，不计入本次用户门槛）','',json.dumps(report['legacy_absolute_diagnostics'],ensure_ascii=False,indent=2),
              '', '本工具不切换现役配置，不自动开始训练。']
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--output-json',type=Path,required=True)
    parser.add_argument('--output-md',type=Path,required=True)
    args=parser.parse_args();report=compare(args.bundle)
    args.output_json.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    args.output_md.write_text(markdown(report))
    print(report['user_rule_point_estimate_status'])


if __name__=='__main__':main()
