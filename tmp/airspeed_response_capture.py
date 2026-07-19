from __future__ import annotations
import json, math, time
from datetime import datetime, timezone
from pathlib import Path
from pymavlink import mavutil

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/evaluation/v6x-airspeed-blow-response-20260716.json'
READY=ROOT/'tmp/airspeed_response_capture.ready'
ENDPOINT='tcp:192.168.144.11:5760'
DURATION=180.0
c=mavutil.mavlink_connection(ENDPOINT,autoreconnect=False,source_system=241,source_component=191)
def eof(): raise EOFError('GR01 TCP closed')
def disc(): raise ConnectionAbortedError('GR01 TCP reset')
c.handle_eof=eof;c.handle_disconnect=disc
hb=c.wait_heartbeat(timeout=8)
if hb is None: raise RuntimeError('heartbeat timeout')
c.target_system=hb.get_srcSystem(); c.target_component=hb.get_srcComponent()
def set_interval(mid,interval):
    c.mav.command_long_send(c.target_system,c.target_component,mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,0,mid,interval,0,0,0,0,0)
for mid,interval in [(29,50000),(74,100000)]: set_interval(mid,interval)
READY.write_text(datetime.now(timezone.utc).isoformat(),encoding='utf-8')
samples=[]; counts={}; start=time.monotonic()
try:
    while time.monotonic()-start<DURATION:
        m=c.recv_match(blocking=True,timeout=.25)
        if m is None: continue
        t=m.get_type(); counts[t]=counts.get(t,0)+1; elapsed=time.monotonic()-start
        if t=='SCALED_PRESSURE':
            q=float(m.press_diff)*100.0
            if math.isfinite(q): samples.append({'t_s':round(elapsed,4),'kind':'pressure','value':q})
        elif t=='VFR_HUD':
            v=float(m.airspeed)
            if math.isfinite(v): samples.append({'t_s':round(elapsed,4),'kind':'airspeed','value':v})
finally:
    for mid in (29,74):
        try:set_interval(mid,0)
        except Exception:pass
    try:c.close()
    except Exception:pass
    READY.unlink(missing_ok=True)
press=[s['value'] for s in samples if s['kind']=='pressure']; speed=[s['value'] for s in samples if s['kind']=='airspeed']
def extrema(xs): return {'count':len(xs),'min':min(xs) if xs else None,'max':max(xs) if xs else None}
result={'event':'px4_airspeed_blow_response','captured_at_utc':datetime.now(timezone.utc).isoformat(),'duration_s':time.monotonic()-start,'identity':{'system_id':hb.get_srcSystem(),'component_id':hb.get_srcComponent(),'armed':bool(hb.base_mode & 128)},'message_counts':counts,'differential_pressure_pa':extrema(press),'airspeed_mps':extrema(speed),'positive_response_detected':bool(press and max(press)>50),'negative_response_detected':bool(press and min(press)<-50),'samples':samples}
OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k!='samples'},ensure_ascii=False,indent=2))
