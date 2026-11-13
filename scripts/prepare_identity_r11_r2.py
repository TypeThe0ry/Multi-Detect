#!/usr/bin/env python3
"""CPU-only, immutable train-only focus crops and novel complete-view replay."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import hashlib
import io
import json
import math
from pathlib import Path
import shutil

from PIL import Image

VIEWS = ('front', 'oblique', 'edge_truncated', 'recovery')
POSITIVE = {'true_target', 'proposal_true', 'confusable_positive'}
NEGATIVE = {'distractor', 'proposal_distractor', 'confusable_negative',
            'counterfactual_negative', 'proposal_counterfactual_negative'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def resolve(root, path):
    return (root / path).resolve()


def polarity(row):
    label, decision = row.get('label_type'), row.get('assistant_target', {}).get('decision')
    if label in POSITIVE and decision in ('confirm_target', 'approach_track'):
        return 'positive'
    if label in NEGATIVE and decision == 'reject_track':
        return 'negative'
    return None


def focus(row):
    # Do not fall back to assistant_target: the crop geometry is model-observable.
    target = row.get('focus_track_id')
    return next((c for c in row.get('candidates', []) if c.get('track_id') == target), None)


def short_side(row, root, dimensions):
    candidate = focus(row)
    if not candidate or not row.get('image_path'):
        return None
    path = resolve(root, row['image_path'])
    if path not in dimensions:
        with Image.open(path) as image:
            dimensions[path] = image.size
    w, h = dimensions[path]
    x1, y1, x2, y2 = candidate['box_xyxy_normalized']
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError(f'invalid candidate box: {row["sample_id"]}')
    return min((x2-x1)*w, (y2-y1)*h)


def stratified(items, limit, key):
    """Deterministic round robin prevents a large world/view from dominating."""
    groups = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    for group in groups.values():
        group.sort(key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
    ordered, offset = [], 0
    while len(ordered) < limit:
        added = False
        for name in sorted(groups):
            group = groups[name]
            if offset < len(group):
                ordered.append(group[offset])
                added = True
                if len(ordered) == limit:
                    break
        if not added:
            break
        offset += 1
    return ordered


def render_focus_crop(row, root, output, cfg):
    candidate = focus(row)
    if candidate is None:
        raise ValueError('a model-observable focus is required')
    if row['assistant_target'].get('args', {}).get('track_id') != candidate['track_id']:
        raise ValueError('the training target is not about the observable focus')
    source = resolve(root, row['image_path'])
    original_bytes = source.read_bytes()
    with Image.open(io.BytesIO(original_bytes)) as source_image:
        image = source_image.convert('RGB')
    w, h = image.size
    box = candidate['box_xyxy_normalized']
    x1, y1, x2, y2 = box[0]*w, box[1]*h, box[2]*w, box[3]*h
    side = max(cfg['min_context_px'], cfg['box_long_side_context_multiplier']*max(x2-x1, y2-y1))
    side = math.ceil(side)
    left, top = math.floor((x1+x2-side)/2), math.floor((y1+y2-side)/2)
    canvas = Image.new('RGB', (side, side), (128, 128, 128))
    sx1, sy1, sx2, sy2 = max(0, left), max(0, top), min(w, left+side), min(h, top+side)
    canvas.paste(image.crop((sx1, sy1, sx2, sy2)), (sx1-left, sy1-top))
    canvas = canvas.resize((cfg['output_side_px'], cfg['output_side_px']), Image.Resampling.LANCZOS)
    identifier = hashlib.sha256((row['sample_id']+'|r11-r2-focus-context-v1').encode()).hexdigest()[:24]
    destination = output / 'crops' / (identifier+'.png')
    destination.parent.mkdir(exist_ok=True)
    canvas.save(destination)
    augmented = copy.deepcopy(row)
    augmented.update(sample_id='r11r2-'+identifier, image_path=str(destination),
                     candidates=[{**candidate, 'box_xyxy_normalized':
                                  [(x1-left)/side, (y1-top)/side, (x2-left)/side, (y2-top)/side]}],
                     image_size={'width': cfg['output_side_px'], 'height': cfg['output_side_px']},
                     augmentation={'kind': 'focus_context_crop', 'source_sample_id': row['sample_id']})
    index = {'kind': 'focus_context_crop', 'source_sample_id': row['sample_id'],
             'sample_id': augmented['sample_id'], 'world': row['world'], 'view': row.get('view'),
             'label_type': row.get('label_type'), 'polarity': polarity(row),
             'source_image': str(source), 'source_image_sha256': hashlib.sha256(original_bytes).hexdigest(),
             'crop_bounds_original_px': [left, top, left+side, top+side],
             'original_short_side_px': min(x2-x1, y2-y1), 'image_path': str(destination),
             'image_sha256': sha(destination)}
    return augmented, index


def prepare(root, config, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f'refusing to replace prepared data: {output}')
    cfg = json.loads(Path(config).read_text())
    base_dir = resolve(root, cfg['base_dataset'])
    full_path = resolve(root, cfg['uncapped_train'])
    base, full = rows(base_dir/'train.jsonl'), rows(full_path)
    assert all(r.get('split') == 'train' for r in base+full), 'non-train source row'
    assert len({r['sample_id'] for r in base}) == len(base), 'duplicate base IDs'
    held = [r for path in cfg['heldout'] for r in rows(resolve(root, path))]
    held_ids = {r['sample_id'] for r in held}
    held_images = {str(resolve(root, r['image_path'])) for r in held if r.get('image_path')}
    held_worlds = {r['world'] for r in held if r.get('image_path')}
    protected_ids, protected_texts = set(), set()
    inputs = [Path(config).resolve(), full_path, *(base_dir/(s+'.jsonl') for s in ('train','val','test'))]
    inputs += [resolve(root, p) for p in cfg['heldout']+cfg['protected_tasks']]
    protected_counts = []
    for path in cfg['protected_tasks']:
        tasks = json.loads(resolve(root, path).read_text())['tasks']
        protected_counts.append(len(tasks))
        protected_ids.update(r.get('sample_id', r.get('id')) for r in tasks)
        protected_texts.update(r.get('task_text', r.get('text')) for r in tasks)
    assert protected_counts == [126, 15], 'protected evaluation counts changed'
    assert not any(r['sample_id'] in protected_ids or r['task_text'] in protected_texts for r in base+full)
    def safe(row):
        return row['sample_id'] not in held_ids and row['world'] not in held_worlds and bool(row.get('image_path')) \
            and str(resolve(root, row['image_path'])) not in held_images
    base_ids = {r['sample_id'] for r in base}
    assert not any(r['sample_id'] in held_ids or (r.get('image_path') and not safe(r)) for r in base), \
        'base image/ID overlaps heldout; review before augmentation'
    pool = {r['sample_id']: r for r in base+full if safe(r)}
    dimensions, small = {}, {'positive': [], 'negative': []}
    base_sizes = Counter()
    for row in pool.values():
        p = polarity(row)
        if p is None or focus(row) is None:
            continue
        size = short_side(row, root, dimensions)
        if row['sample_id'] in base_ids:
            base_sizes[f'{p}|{"<12" if size < 12 else ">=12"}|{row.get("view")}'] += 1
        if size < cfg['crop']['original_focus_short_side_lt_px'] and \
           row['assistant_target'].get('args', {}).get('track_id') == row['focus_track_id']:
            small[p].append(row)
    crop_count = min(cfg['crop']['max_per_polarity'], len(small['positive']), len(small['negative']))
    if not crop_count:
        raise ValueError('no balanced small-focus crop cohort')
    crop_rows = [row for p in ('positive','negative') for row in stratified(small[p], crop_count,
                 lambda r: (r['world'], str(r.get('view')), str(r.get('task_category')), str(r.get('label_type'))))]
    grouped = defaultdict(lambda: defaultdict(list))
    for row in full:
        p = polarity(row)
        if not safe(row) or row['sample_id'] in base_ids or not row.get('target_id') or row.get('view') not in VIEWS or p is None:
            continue
        grouped[(p, row['world'], row['target_id'], row['task_text'])][row['view']].append(row)
    bundles = {'positive': [], 'negative': []}
    for key, views in grouped.items():
        if set(views) == set(VIEWS):
            bundles[key[0]].append([min(views[v], key=lambda r:r['sample_id']) for v in VIEWS])
    group_count = min(cfg['four_view_topup']['max_rows_per_polarity']//4,
                      len(bundles['positive']), len(bundles['negative']))
    topups = [row for p in ('positive','negative') for group in stratified(bundles[p], group_count,
               lambda group: (group[0]['world'], str(group[0].get('confusable_pair_type')))) for row in group]
    output.mkdir(parents=True)
    additions, audit_index = [], []
    for row in crop_rows:
        augmented, index = render_focus_crop(row, root, output, cfg['crop'])
        additions.append(augmented)
        audit_index.append(index)
    additions += topups
    audit_index += [{'kind':'novel_complete_view_replay','source_sample_id':r['sample_id'],
                     'sample_id':r['sample_id'],'world':r['world'],'view':r['view'],
                     'target_id':r['target_id'],'task_text':r['task_text'],'polarity':polarity(r),
                     'source_jsonl':str(full_path)} for r in topups]
    train = base+additions
    assert len({r['sample_id'] for r in train}) == len(train), 'duplicate output sample IDs'
    (output/'train.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in train))
    for split in ('val','test'):
        shutil.copyfile(base_dir/(split+'.jsonl'), output/(split+'.jsonl'))
    (output/'additions-index.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in audit_index))
    report = {'schema_version':1,'crop_policy':'candidate_zoom_v2','base_train_rows':len(base),
              'output_train_rows':len(train),'crop_rows_per_polarity':crop_count,
              'novel_four_view_rows_per_polarity':4*group_count,'novel_complete_groups_per_polarity':group_count,
              'eligible_small_rows':{p:len(v) for p,v in small.items()},'base_focus_size_view_counts':dict(base_sizes),
              'source_train_worlds':len({r['world'] for r in base if r.get('image_path')}),
              'addition_worlds':sorted({r['world'] for r in additions}),
              'heldout_image_worlds':sorted(held_worlds),'heldout_id_image_world_overlap_additions':0,
              'protected_counts':protected_counts,'protected_task_overlap_train':0,
              'input_sha256':{str(p):sha(p) for p in dict.fromkeys(inputs)},
              'output_sha256':{str(output/p):sha(output/p) for p in ('train.jsonl','val.jsonl','test.jsonl','additions-index.jsonl')},
              'view_counts_additions':dict(Counter(str(r.get('view')) for r in additions)),
              'polarity_counts_additions':dict(Counter(polarity(r) for r in additions))}
    (output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.root,args.config,args.output),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
