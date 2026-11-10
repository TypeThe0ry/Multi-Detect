"""Candidate-only local MobileCLIP binding; no implicit asset downloads."""
import hashlib,os
from pathlib import Path

CLIP_SHA256='a67804d1b0f07b8b9a20c1761ec0847f34660f5fa338ec70e8f3fce68ed95e54'
YOLO_SHA256='402d91abcaf924ab22971ac9b9130843109fa8821dfbfd88c0c52d574f9e89a4'

def configure_candidate_clip(weights):
    path=Path(os.environ.get('MD_GIMBAL_CLIP_FILE',''))
    if not path.is_absolute() or not path.is_file():raise ValueError('candidate MobileCLIP path missing')
    yolo=Path(os.environ.get('MD_GIMBAL_YOLOE_FILE',''))
    if not yolo.is_absolute() or not yolo.is_file() or Path(weights).resolve()!=yolo.resolve():raise ValueError('candidate YOLOE path mismatch')
    def validate(p,expected):
        h=hashlib.sha256()
        with p.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        if h.hexdigest()!=expected:raise ValueError('candidate asset hash mismatch')
    validate(path,CLIP_SHA256);validate(yolo,YOLO_SHA256)
    from ultralytics.utils import downloads
    def local_asset(value,*args,**kwargs):
        if str(value)=='mobileclip_blt.ts':return str(path)
        candidate=Path(str(value))
        if candidate.is_absolute() and candidate.resolve()==yolo.resolve():return str(yolo)
        raise ValueError('unexpected candidate asset request')
    def refuse_download(*args,**kwargs):raise RuntimeError('candidate asset download disabled')
    downloads.attempt_download_asset=local_asset
    downloads.safe_download=refuse_download
