"""Candidate-only local MobileCLIP binding; no implicit asset downloads."""
import hashlib,os
from pathlib import Path

CLIP_SHA256='a67804d1b0f07b8b9a20c1761ec0847f34660f5fa338ec70e8f3fce68ed95e54'

def configure_candidate_clip():
    path=Path(os.environ.get('MD_GIMBAL_CLIP_FILE',''))
    if not path.is_absolute() or not path.is_file():raise ValueError('candidate MobileCLIP path missing')
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    if h.hexdigest()!=CLIP_SHA256:raise ValueError('candidate MobileCLIP hash mismatch')
    from ultralytics.utils import downloads
    def local_asset(value,*args,**kwargs):
        if str(value)!='mobileclip_blt.ts':raise ValueError('unexpected candidate asset request')
        return str(path)
    def refuse_download(*args,**kwargs):raise RuntimeError('candidate asset download disabled')
    downloads.attempt_download_asset=local_asset
    downloads.safe_download=refuse_download
