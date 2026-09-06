# ruff: noqa: E501
"""Loopback-only visual workbench for reviewed deployment replay annotation."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import secrets
import threading
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .autonomy.serialization import AutonomyContractError, require_text, strict_json_loads
from .replay_annotation_workspace import (
    annotate_replay_frame,
    inspect_replay_annotation_workspace,
    read_replay_annotation_image,
    replay_annotation_workspace_document,
    review_replay_annotation_workspace,
)

MAX_REQUEST_BYTES = 256_000


@dataclass(frozen=True)
class ReplayAnnotationWorkbench:
    """One isolated HTTP server and its unguessable mutation token."""

    server: ThreadingHTTPServer
    token: str
    url: str

    def serve_forever(self) -> None:
        self.server.serve_forever(poll_interval=0.25)

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def create_replay_annotation_workbench(
    intake_root: str | Path,
    workspace: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ReplayAnnotationWorkbench:
    """Create, but do not start, a token-protected loopback annotation server."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise AutonomyContractError("annotation workbench host must be a loopback IP") from exc
    if not address.is_loopback:
        raise AutonomyContractError("annotation workbench must bind to loopback only")
    if address.version != 4:
        raise AutonomyContractError("annotation workbench currently requires IPv4 loopback")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise AutonomyContractError("annotation workbench port must be in [0, 65535]")
    intake = Path(intake_root).resolve(strict=True)
    requested_workspace = Path(workspace)
    if requested_workspace.is_symlink():
        raise AutonomyContractError("annotation workspace must not be a symbolic link")
    workspace_path = requested_workspace.resolve(strict=True)
    replay_annotation_workspace_document(intake, workspace_path)
    token = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    handler = _handler(intake, workspace_path, token, nonce)
    server = ThreadingHTTPServer((str(address), port), handler)
    server.daemon_threads = True
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/"
    return ReplayAnnotationWorkbench(server=server, token=token, url=url)


def _handler(
    intake: Path,
    workspace: Path,
    token: str,
    nonce: str,
) -> type[BaseHTTPRequestHandler]:
    csp = (
        "default-src 'none'; img-src 'self' blob:; style-src 'unsafe-inline'; "
        f"script-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'; "
        "frame-ancestors 'none'"
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "MultiDetectAnnotationWorkbench/1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            route = urlsplit(self.path)
            try:
                if route.path == "/":
                    html = _HTML.replace("__TOKEN__", token).replace("__NONCE__", nonce)
                    self._send(HTTPStatus.OK, html, "text/html")
                elif route.path == "/api/workspace":
                    self._send_json(HTTPStatus.OK, _workspace_payload(intake, workspace))
                elif route.path == "/api/image":
                    query = parse_qs(route.query, strict_parsing=True)
                    frame_values = query.get("frame_id", [])
                    if len(frame_values) != 1:
                        raise AutonomyContractError("frame_id query is required exactly once")
                    payload, content_type = read_replay_annotation_image(
                        intake, workspace, frame_id=frame_values[0]
                    )
                    self._send(HTTPStatus.OK, payload, content_type, cache=False)
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
            except (AutonomyContractError, OSError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            route = urlsplit(self.path)
            if not hmac.compare_digest(self.headers.get("X-Annotation-Token", ""), token):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid session token"})
                return
            try:
                body = self._read_json()
                if route.path == "/api/annotate":
                    _exact_keys(body, {"frame_id", "annotator", "status", "objects"})
                    result = annotate_replay_frame(
                        intake,
                        workspace,
                        frame_id=require_text(body.get("frame_id"), "frame_id"),
                        annotator=require_text(body.get("annotator"), "annotator"),
                        status=require_text(body.get("status"), "status"),
                        objects=_objects(body.get("objects")),
                    )
                elif route.path == "/api/review":
                    _exact_keys(body, {"reviewer"})
                    result = review_replay_annotation_workspace(
                        intake,
                        workspace,
                        reviewer=require_text(body.get("reviewer"), "reviewer"),
                    )
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"status": result, "workspace": _workspace_payload(intake, workspace)},
                )
            except (AutonomyContractError, OSError, ValueError) as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})

        def _read_json(self) -> dict[str, Any]:
            if self.headers.get_content_type() != "application/json":
                raise AutonomyContractError("request Content-Type must be application/json")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise AutonomyContractError("request Content-Length is required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise AutonomyContractError("request Content-Length is invalid") from exc
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise AutonomyContractError("request body size is invalid")
            raw = self.rfile.read(length)
            try:
                value = strict_json_loads(raw, name="annotation workbench request")
            except (UnicodeDecodeError, ValueError) as exc:
                raise AutonomyContractError("request body must be strict JSON") from exc
            if not isinstance(value, Mapping):
                raise AutonomyContractError("request body must be a JSON object")
            return dict(value)

        def _send_json(self, status: HTTPStatus, value: object) -> None:
            payload = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            self._send(status, payload, "application/json; charset=utf-8")

        def _send(
            self,
            status: HTTPStatus,
            payload: str | bytes,
            content_type: str,
            *,
            cache: bool = False,
        ) -> None:
            body = payload.encode("utf-8") if isinstance(payload, str) else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=60" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _objects(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise AutonomyContractError("objects must be an array")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"label", "bbox"}:
            raise AutonomyContractError("objects require exactly label and bbox")
        result.append({"label": item["label"], "bbox": item["bbox"]})
    return tuple(result)


def _exact_keys(value: Mapping[str, object], required: set[str]) -> None:
    if set(value) != required:
        raise AutonomyContractError(
            "request fields must be exactly: " + ", ".join(sorted(required))
        )


def _workspace_payload(intake: Path, workspace: Path) -> dict[str, object]:
    return {
        "document": replay_annotation_workspace_document(intake, workspace),
        "status": inspect_replay_annotation_workspace(intake, workspace),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("intake", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workbench = create_replay_annotation_workbench(
            args.intake, args.workspace, host=args.host, port=args.port
        )
    except (AutonomyContractError, OSError) as exc:
        print(f"annotation workbench: {exc}")
        return 2
    print(
        json.dumps(
            {
                "event": "replay_annotation_workbench_started",
                "url": workbench.url,
                "loopback_only": True,
                "hardware_io": False,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if args.open_browser:
        threading.Timer(0.25, webbrowser.open, args=(workbench.url,)).start()
    try:
        workbench.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        workbench.server.server_close()
    return 0


_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Multi-Detect Replay Annotation</title>
<style>
:root{color-scheme:dark;--bg:#10141b;--panel:#19212b;--line:#2d3b4c;--accent:#43d5a1;--warn:#ffb74d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#eef4fa;font:14px system-ui,sans-serif}
header{height:54px;padding:9px 16px;display:flex;gap:18px;align-items:center;background:#151c25;border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0}.muted{color:#9db0c4}.layout{display:grid;grid-template-columns:minmax(0,1fr) 330px;height:calc(100vh - 54px)}
.stage{display:flex;align-items:center;justify-content:center;min-width:0;overflow:auto;padding:12px}.canvas-wrap{position:relative;max-width:100%;max-height:100%}
canvas{display:block;max-width:100%;max-height:calc(100vh - 82px);background:#05070a;cursor:crosshair}
aside{padding:14px;background:var(--panel);border-left:1px solid var(--line);overflow:auto}label{display:block;margin:10px 0 5px;color:#b8c8d8}
input,select,button{width:100%;padding:9px;border:1px solid var(--line);border-radius:5px;background:#101720;color:#fff}
button{cursor:pointer;margin-top:8px}button.primary{background:#167a5a;border-color:#29b986}button.warn{background:#78521d;border-color:#bd8130}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}.box{padding:8px;margin:7px 0;border:1px solid var(--line);border-radius:5px}.error{color:#ff7b7b;white-space:pre-wrap}.ok{color:var(--accent)}
</style></head><body>
<header><h1>Multi-Detect 标注工作台</h1><span id="dataset" class="muted"></span><span id="counts"></span></header>
<main class="layout"><section class="stage"><div class="canvas-wrap"><canvas id="canvas"></canvas></div></section>
<aside><div id="identity"></div><label>标注员</label><input id="annotator" autocomplete="off">
<label>类别</label><select id="label"></select><div class="row"><button id="prev">上一帧</button><button id="next">下一帧</button></div>
<button id="save" class="primary">保存框选正例</button><button id="negative" class="warn">确认本帧无目标</button><button id="clear">清除本地未保存框</button>
<div id="boxes"></div><hr><label>独立复核人</label><input id="reviewer" autocomplete="off"><button id="review">全部标完后确认复核</button>
<p class="muted">拖动鼠标框选。保存动作只修改 annotation workspace，不连接相机、飞控或执行器。</p><div id="message"></div></aside></main>
<script nonce="__NONCE__">
const TOKEN='__TOKEN__';let data,index=0,boxes=[],drag=null,img=new Image();const $=id=>document.getElementById(id),canvas=$('canvas'),ctx=canvas.getContext('2d');
async function api(path,options={}){options.headers={...(options.headers||{}),'X-Annotation-Token':TOKEN};let r=await fetch(path,options),v=await r.json();if(!r.ok)throw Error(v.error||r.statusText);return v}
async function reload(keep=true){let old=data?.document.frames[index]?.frame_id;data=await api('/api/workspace');let frames=data.document.frames;if(keep&&old){let n=frames.findIndex(f=>f.frame_id===old);if(n>=0)index=n}renderMeta();await show()}
function renderMeta(){let s=data.status,c=s.status_counts;$('dataset').textContent=`${data.document.dataset_id} / ${data.document.dataset_version}`;$('counts').textContent=`未标 ${c.unannotated} · 正例 ${c.positive} · 负例 ${c.negative}`;let select=$('label');select.replaceChildren();for(let name of data.document.class_names){let option=document.createElement('option');option.textContent=name;option.value=name;select.append(option)}$('annotator').value=data.document.annotator||$('annotator').value;$('reviewer').value=data.document.reviewer||'';$('identity').textContent=`${index+1}/${data.document.frames.length}`}
async function show(){let f=data.document.frames[index];$('identity').textContent=`${index+1}/${data.document.frames.length} · ${f.stratum} · ${f.frame_id}`;boxes=(f.annotation.objects||[]).map(o=>({label:o.label,bbox:[...o.bbox]}));img=await loadImage(`/api/image?frame_id=${encodeURIComponent(f.frame_id)}`);canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;draw();renderBoxes()}
function loadImage(src){return new Promise((ok,bad)=>{let i=new Image();i.onload=()=>ok(i);i.onerror=bad;i.src=src})}
function draw(){ctx.drawImage(img,0,0,canvas.width,canvas.height);ctx.lineWidth=Math.max(2,canvas.width/500);ctx.font=`${Math.max(14,canvas.width/60)}px sans-serif`;for(let b of boxes){let [x1,y1,x2,y2]=b.bbox.map((v,i)=>v*(i%2?canvas.height:canvas.width));ctx.strokeStyle='#43d5a1';ctx.fillStyle='#43d5a1';ctx.strokeRect(x1,y1,x2-x1,y2-y1);ctx.fillText(b.label,x1+3,Math.max(16,y1-4))}if(drag){ctx.strokeStyle='#ffb74d';ctx.strokeRect(drag.x1,drag.y1,drag.x2-drag.x1,drag.y2-drag.y1)}}
function point(e){let r=canvas.getBoundingClientRect();return{x:(e.clientX-r.left)*canvas.width/r.width,y:(e.clientY-r.top)*canvas.height/r.height}}
canvas.onmousedown=e=>{let p=point(e);drag={x1:p.x,y1:p.y,x2:p.x,y2:p.y}};canvas.onmousemove=e=>{if(drag){let p=point(e);drag.x2=p.x;drag.y2=p.y;draw()}};canvas.onmouseup=e=>{if(!drag)return;let p=point(e),x1=Math.min(drag.x1,p.x),x2=Math.max(drag.x1,p.x),y1=Math.min(drag.y1,p.y),y2=Math.max(drag.y1,p.y);drag=null;if(x2-x1>3&&y2-y1>3)boxes.push({label:$('label').value,bbox:[x1/canvas.width,y1/canvas.height,x2/canvas.width,y2/canvas.height]});draw();renderBoxes()};
function renderBoxes(){let root=$('boxes');root.replaceChildren();boxes.forEach((b,i)=>{let row=document.createElement('div');row.className='box';row.append(document.createTextNode(`${i+1}. ${b.label} [${b.bbox.map(x=>x.toFixed(3)).join(', ')}] `));let button=document.createElement('button');button.textContent='删除';button.onclick=()=>{boxes.splice(i,1);draw();renderBoxes()};row.append(button);root.append(row)})}
async function annotate(status){try{let f=data.document.frames[index],body={frame_id:f.frame_id,annotator:$('annotator').value,status,objects:status==='positive'?boxes:[]};await api('/api/annotate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});msg('已原子保存','ok');await reload()}catch(e){msg(e.message,'error')}}
function msg(t,c){$('message').textContent=t;$('message').className=c}
$('save').onclick=()=>annotate('positive');$('negative').onclick=()=>annotate('negative');$('clear').onclick=()=>{boxes=[];draw();renderBoxes()};$('prev').onclick=async()=>{index=(index-1+data.document.frames.length)%data.document.frames.length;await show()};$('next').onclick=async()=>{index=(index+1)%data.document.frames.length;await show()};$('review').onclick=async()=>{try{await api('/api/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewer:$('reviewer').value})});msg('复核已绑定到当前全部标注内容','ok');await reload()}catch(e){msg(e.message,'error')}};reload(false).catch(e=>msg(e.message,'error'));
</script></body></html>"""


__all__ = [
    "ReplayAnnotationWorkbench",
    "build_parser",
    "create_replay_annotation_workbench",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
