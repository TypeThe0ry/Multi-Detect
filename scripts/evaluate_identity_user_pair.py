#!/usr/bin/env python3
"""Prepare or execute paired local evaluations and record actual processing provenance."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from scripts.compare_identity_user_gate import compare,markdown,processing


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--baseline-side',type=int,default=448)
    parser.add_argument('--candidate-side',type=int,default=448)
    parser.add_argument('--mode',choices=('model_control','deployment_improvement'),default='model_control')
    parser.add_argument('--deployment-json',type=Path)
    parser.add_argument('--execute',action='store_true',help='actually launch local GPU evaluation; default only prints plan')
    args=parser.parse_args()
    if args.mode=='model_control' and args.baseline_side!=args.candidate_side:
        parser.error('model_control requires matched resolutions; choose deployment_improvement explicitly')
    root=args.root.resolve();artifacts=root/'artifacts';out=args.out.resolve()
    datasets={'v1':artifacts/'datasets/flight-identity-eval-v1/test.jsonl',
              'v3':artifacts/'datasets/flight-identity-eval-v2-v3prop/test.jsonl',
              'four_view':artifacts/'r21id-r3/pipeline/r15-additions/test.jsonl',
              'r9r_val':out/'r9r-val-input/test.jsonl'}
    evaluator=ROOT/'scripts/eval_slow_system.py';runtime=ROOT/'scripts/slow_system_runtime.py'
    mmproj=artifacts/'cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf'
    server=artifacts/'cache/llama-cpp/b11552/llama-server'
    python=root/'.venv-vlm/bin/python'
    if args.execute:
        out.mkdir(parents=True,exist_ok=True)
        datasets['r9r_val'].parent.mkdir(exist_ok=True)
        rows=[json.loads(line) for line in (artifacts/'r21id-r10/data/r15-additions-mix/val.jsonl').read_text().splitlines()]
        datasets['r9r_val'].write_text(''.join(json.dumps({**r,'split':'test'},ensure_ascii=False)+'\n' for r in rows))
    pairs=[]
    for name,dataset in datasets.items():
        pair={'name':name,'dataset':str(dataset)}
        for role,model,side in [('baseline',args.baseline,args.baseline_side),('candidate',args.candidate,args.candidate_side)]:
            result=out/(name+'-'+role+'.json');provenance=out/(name+'-'+role+'-processing.json')
            command=[str(python),str(evaluator),'--dataset',str(dataset.parent),'--data-root',str(root),
                     '--backend','gguf','--device','cuda:0','--identity-only','--fast-identity',
                     '--crop-policy','candidate_zoom_v2','--max-side',str(side),'--identity-variant','default',
                     '--gguf-model',str(model.resolve()),'--mmproj',str(mmproj),'--llama-server',str(server),
                     '--port','9951','--output',str(result)]
            print(json.dumps({'comparison':name,'role':role,'command':command,'execute':args.execute}),flush=True)
            pair[role]=str(result);pair[role+'_provenance']=str(provenance)
            if not args.execute:continue
            if result.exists():
                if not provenance.exists():raise RuntimeError(f'unverified old output; use fresh --out: {result}')
                processing(provenance,result,dataset)
                continue
            evidence={'command':command,'evaluator':str(evaluator),'runtime':str(runtime),
                      'evaluator_sha256':sha(evaluator),'runtime_sha256':sha(runtime),
                      'mmproj_sha256':sha(mmproj),'dataset_sha256':sha(dataset)}
            subprocess.run(command,cwd=ROOT,check=True)
            for key,path in [('evaluator_sha256',evaluator),('runtime_sha256',runtime),('dataset_sha256',dataset)]:
                if sha(path)!=evidence[key]:raise RuntimeError(f'input changed during evaluation: {path}')
            evidence['result_sha256']=sha(result)
            provenance.write_text(json.dumps(evidence,indent=2)+'\n')
        pairs.append(pair)
    if args.execute:
        bundle={'mode':args.mode,'pairs':pairs}
        if args.deployment_json:bundle['deployment']=json.loads(args.deployment_json.read_text())
        bundle_path=out/'comparison-bundle.json';bundle_path.write_text(json.dumps(bundle,indent=2)+'\n')
        report=compare(bundle_path)
        (out/'user-gate.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        (out/'user-gate.md').write_text(markdown(report))
        print(report['user_rule_point_estimate_status'])


if __name__=='__main__':main()
