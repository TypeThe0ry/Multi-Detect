from __future__ import annotations

import copy
import json
from pathlib import Path

from PIL import Image
import pytest

from scripts.prepare_identity_r11_r2 import prepare, render_focus_crop


def fixture(tmp_path):
    image = tmp_path/'frame.png'
    Image.new('RGB',(100,100),(40,80,120)).save(image)
    def row(identifier, polarity='positive', view='front', world='train-world', target='target'):
        return {'sample_id':identifier,'split':'train','world':world,'image_path':str(image),
                'focus_track_id':'t1','candidates':[{'track_id':'t1','box_xyxy_normalized':[.48,.48,.52,.52]}],
                'view':view,'target_id':target,'task_text':'find target' if polarity=='positive' else 'find another class',
                'label_type':'true_target' if polarity=='positive' else 'confusable_negative',
                'memory':{},'assistant_target':{'decision':'confirm_target' if polarity=='positive' else 'reject_track',
                                               'args':{'track_id':'t1'}}}
    base=tmp_path/'base';base.mkdir()
    # Heldout uses another physical image, so row identity and image identity are both checked.
    heldimage=tmp_path/'held.png';Image.new('RGB',(100,100)).save(heldimage)
    held=row('held',world='held-world');held['image_path']=str(heldimage);held['split']='val'
    for name,values in [('train',[row('base-p'),row('base-n','negative')]),('val',[held]),('test',[])]:
        (base/(name+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in values))
    full=[row(f'{pol}-{v}',pol,v,target=pol) for pol in ('positive','negative')
          for v in ('front','oblique','edge_truncated','recovery')]
    leak=row('held',world='held-world');leak['image_path']=str(heldimage);full.append(leak)
    fullpath=tmp_path/'full.jsonl';fullpath.write_text(''.join(json.dumps(r)+'\n' for r in full))
    for name,count in [('v5',126),('probe',15)]:
        (tmp_path/(name+'.json')).write_text(json.dumps({'tasks':[{'id':f'{name}-{i}','text':f'protected-{name}-{i}'} for i in range(count)]}))
    config={'base_dataset':str(base),'uncapped_train':str(fullpath),
            'heldout':[str(base/'val.jsonl'),str(base/'test.jsonl')],
            'protected_tasks':[str(tmp_path/'v5.json'),str(tmp_path/'probe.json')],
            'crop':{'min_context_px':32,'box_long_side_context_multiplier':2,'output_side_px':224,
                    'original_focus_short_side_lt_px':12,'max_per_polarity':2},
            'four_view_topup':{'max_rows_per_polarity':4}}
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps(config))
    return cfg,row


def test_preparation_balances_crops_and_adds_novel_complete_prompt_view_groups(tmp_path):
    config,_=fixture(tmp_path)
    report=prepare(tmp_path,config,tmp_path/'prepared')
    assert report['crop_rows_per_polarity']==2
    assert report['novel_complete_groups_per_polarity']==1
    assert report['output_train_rows']==14
    assert report['polarity_counts_additions']=={'positive':6,'negative':6}
    out=[json.loads(line) for line in (tmp_path/'prepared/train.jsonl').read_text().splitlines()]
    assert all(r['world']!='held-world' for r in out)
    assert len({r['sample_id'] for r in out})==len(out)
    for r in out:
        if r.get('augmentation'):
            assert len(r['candidates'])==1
            assert all(0<=n<=1 for n in r['candidates'][0]['box_xyxy_normalized'])
    with pytest.raises(FileExistsError):
        prepare(tmp_path,config,tmp_path/'prepared')


def test_crop_geometry_does_not_choose_a_box_from_the_training_label(tmp_path):
    config,row=fixture(tmp_path)
    cfg=json.loads(config.read_text())['crop']
    original=row('same-source')
    changed=copy.deepcopy(original);changed['assistant_target']['decision']='reject_track'
    left=tmp_path/'left';right=tmp_path/'right';left.mkdir();right.mkdir()
    _,a=render_focus_crop(original,tmp_path,left,cfg)
    _,b=render_focus_crop(changed,tmp_path,right,cfg)
    assert a['image_sha256']==b['image_sha256']
    assert a['crop_bounds_original_px']==b['crop_bounds_original_px']


def test_non_train_or_protected_rows_cannot_enter_preparation(tmp_path):
    config,_=fixture(tmp_path)
    cfg=json.loads(config.read_text());source=Path(cfg['uncapped_train'])
    data=[json.loads(line) for line in source.read_text().splitlines()]
    data[0]['split']='val';source.write_text(''.join(json.dumps(r)+'\n' for r in data))
    with pytest.raises(AssertionError,match='non-train'):
        prepare(tmp_path,config,tmp_path/'prepared')
