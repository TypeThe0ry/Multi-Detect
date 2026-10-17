"""方案 B 离线单目测距评估（只读机载录像，不启用闭环）。"""
from __future__ import annotations
import argparse, hashlib, json, math, statistics, time, sys
from collections import Counter, defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from scripts.score_onboard_loop import truth_for_episode
from multidetect.simulator.camera_projection import pixel_ground_point, project_target_bbox, CAMERA_MOUNT_PITCH_RAD, CAMERA_MOUNT_X_M, CAMERA_MOUNT_Z_DOWN_M
from multidetect.autonomy.ground_model import FlatGround
from multidetect.multimodal_ranging import CameraCalibration, AircraftPose, TargetImageObservation, MultiModalRangingEngine, load_camera_calibration
from multidetect.simulator.task_semantics import typical_object_size
from multidetect.domain import BoundingBox, VehicleTelemetry
from multidetect.unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState, CameraMotionEstimate
from multidetect.rgb_slam_range import RgbSlamRangeEstimator
from multidetect.visual_inertial_range import VisualInertialRangeEstimator
from multidetect.attitude_camera_motion import AttitudeCameraMotionEstimator
from multidetect.multimodal_ranging import DirectRangeMeasurement, DirectRangeSource, VerticalMeasurement, VerticalSource

def stats(values):
    v=sorted(float(x) for x in values if math.isfinite(float(x)))
    if not v:return {'count':0,'mean_m':None,'median_m':None,'rmse_m':None,'p95_m':None,'max_m':None}
    return {'count':len(v),'mean_m':statistics.mean(v),'median_m':statistics.median(v),'rmse_m':math.sqrt(statistics.mean(x*x for x in v)),'p95_m':v[max(0,math.ceil(.95*len(v))-1)],'max_m':v[-1]}

def iou(a,b):
    ix=max(0,min(a[2],b[2])-max(a[0],b[0])); iy=max(0,min(a[3],b[3])-max(a[1],b[1])); inter=ix*iy
    un=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter
    return inter/un if un>0 else 0.0

def triangulate(samples):
    if len(samples)<2:return None
    A=[[0.0]*3 for _ in range(3)]; b=[0.0]*3
    for p,r in samples:
        P=[[float(i==j)-r[i]*r[j] for j in range(3)] for i in range(3)]
        for i in range(3):
            b[i]+=sum(P[i][j]*p[j] for j in range(3))
            for j in range(3):A[i][j]+=P[i][j]
    M=[A[i]+[b[i]] for i in range(3)]
    for c in range(3):
        q=max(range(c,3),key=lambda i:abs(M[i][c]))
        if abs(M[q][c])<1e-9:return None
        M[c],M[q]=M[q],M[c]; z=M[c][c]; M[c]=[x/z for x in M[c]]
        for i in range(3):
            if i!=c:
                z=M[i][c]; M[i]=[x-z*y for x,y in zip(M[i],M[c])]
    return tuple(M[i][3] for i in range(3))

def triangulation_quality(samples, point):
    """Return baseline, maximum ray angle, residual and a conservative sigma."""
    if point is None or len(samples) < 2: return None
    baselines=[math.dist(samples[-1][0], s[0]) for s in samples[:-1]]
    angles=[]
    for s in samples[:-1]:
        dot=max(-1.0,min(1.0,sum(samples[-1][1][i]*s[1][i] for i in range(3))))
        angles.append(math.degrees(math.acos(dot)))
    residuals=[]
    front=True
    for pos, direction in samples:
        along=sum((point[i]-pos[i])*direction[i] for i in range(3))
        front = front and along > 0.0
        residuals.append(math.dist(tuple(pos[i]+along*direction[i] for i in range(3)),point))
    baseline=max(baselines,default=0.0); angle=max(angles,default=0.0); residual=max(residuals,default=float('inf'))
    sigma=max(0.5, residual + 1.5*math.dist(point,samples[-1][0])*0.002/max(math.sin(math.radians(angle)),0.05))
    return baseline,angle,residual,front,sigma

def ray(ob, u,v):
    ci=ob['camera_info']; cal=load_camera_calibration(ROOT/'simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json')
    a=ob['attitude']; pose=AircraftPose(float(ob.get('frame_sensor_stamp_ns',0))/1e9,float(a.get('roll_deg',0)),float(a.get('pitch_deg',0)),float(a.get('yaw_deg',0))%360)
    t=TargetImageObservation('x','f',pose.captured_at_s,float(u)/cal.width_px,float(v)/cal.height_px); return MultiModalRangingEngine.target_ray_ned(calibration=cal,pose=pose,target=t)

def flat_range(ob,u,v,ground_offset=0.0):
    p=dict(ob)
    # Pass no GroundModel so pixel_ground_point uses the requested z_up plane;
    # FlatGround always means z=0 and would erase the terrain-bias experiment.
    q=pixel_ground_point(u,v,pose=p,camera_info=p['camera_info'],image_size=(int(p['camera_info']['width']),int(p['camera_info']['height'])),ground_up_m=ground_offset,ground_model=None)
    if q is None:return None
    # pixel_ground_point returns the N/E intersection on a plane at z_up=ground_offset.
    # Keep the requested plane height in the returned 3-D point (the previous smoke
    # implementation silently discarded it, making all bias cases identical).
    return (q[0], q[1], -float(ground_offset)) if q is not None else None

def seg_boxes(path, truth):
    cache_dir=ROOT/'tmp/ranging-eval/cache'; cache_dir.mkdir(parents=True,exist_ok=True)
    key=hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]; cp=cache_dir/f'{key}.json'
    try:
        if cp.is_file() and cp.stat().st_mtime >= path.stat().st_mtime:
            return json.loads(cp.read_text(encoding='utf-8'))
    except Exception: pass
    try:
        from multidetect.sim_teacher.segmentation import boxes_from_segmentation, load_labels_map
        b=boxes_from_segmentation(load_labels_map(path),truth,min_area_px=4,min_box_px=2)
        result={str(k):list(v['box']) for k,v in b.items()}; cp.write_text(json.dumps(result),encoding='utf-8'); return result
    except Exception:return {}

def error_row(pred, truth_pos, camera_pos):
    if pred is None:return None
    horizontal=math.hypot(pred[0]-truth_pos[0],pred[1]-truth_pos[1])
    slant=math.dist(pred,camera_pos); true_slant=math.dist(truth_pos,camera_pos)
    return horizontal,abs(slant-true_slant),slant,true_slant

def frame_time(ob, fallback):
    value=ob.get('frame_sensor_stamp_ns')
    return float(value)/1e9 if isinstance(value,(int,float)) and value>=0 else float(fallback)

def make_telemetry(ob, t):
    p=ob.get('pose',{}); a=ob.get('attitude',{}); v=ob.get('velocity') or [0,0,0]; n,e,d=(float(x) for x in (p.get('local_ned_m') or [0,0,0])[:3])
    return VehicleTelemetry(altitude_agl_m=max(0.1,-d),roll_deg=float(a.get('roll_deg',0)),pitch_deg=float(a.get('pitch_deg',0)),ground_speed_mps=math.hypot(float(v[0]),float(v[1])),in_allowed_zone=None,geofence_healthy=True,position_healthy=True,link_healthy=True,flight_mode_allows_deploy=None,release_zone_clear=None,heading_deg=float(a.get('yaw_deg',0))%360,attitude_observed_at_s=t,velocity_north_mps=float(v[0]),velocity_east_mps=float(v[1]),velocity_observed_at_s=t,local_north_m=n,local_east_m=e,local_down_m=d,local_position_observed_at_s=t,airspeed_mps=math.nan,airspeed_observed_at_s=math.nan)

def make_track(track, width, height, t, index):
    b=track['bbox']; box=BoundingBox(float(b[0])/width,float(b[1])/height,float(b[2])/width,float(b[3])/height)
    return UnifiedTrackSnapshot(track_id=str(track.get('id','track')),state=UnifiedTrackState.TRACKING,label='target',bbox=box,predicted_bbox=box,first_seen_at_s=max(0,t-index),last_seen_at_s=t,state_changed_at_s=t,observation_count=index+1,missed_frame_count=0,confidence=1.0,tracking_quality=1.0,velocity_x_s=0.0,velocity_y_s=0.0,appearance_sample_count=0,last_appearance_distance=None,reid_confirmed=True,locked=False,primary=True,actionable=True)

def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument('root',type=Path); ap.add_argument('--output',type=Path,required=True); ap.add_argument('--scenarios',type=Path); ap.add_argument('--max-depth-frames',type=int,default=300); ap.add_argument('--max-episodes',type=int,default=0); ap.add_argument('--max-samples',type=int,default=0); ap.add_argument('--video-only',action='store_true',help='evaluate continuous video/ manifests with the onboard temporal estimators'); ap.add_argument('--calibration',type=Path,default=ROOT/'simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json'); ap.add_argument('--depth-model',type=Path,default=None,help='optional pinned metric-depth ONNX model for every tenth video frame'); ap.add_argument('--resume',action='store_true',help='reuse segmentation cache and overwrite output atomically'); args=ap.parse_args(argv)
    episodes=sorted({p.parent for p in args.root.rglob('episode.json')});
    if args.max_episodes: episodes=episodes[:args.max_episodes]
    if args.video_only:
        from multidetect.continuous_video_eval import evaluate_video_episode
        reports=[]
        for ep in episodes:
            manifest=ep/'video'/'manifest.jsonl'
            if not manifest.is_file(): continue
            truth_path = args.scenarios
            world, truth = truth_for_episode(ep, truth_path)
            if truth is not None:
                reports.append(evaluate_video_episode(ep, truth, calibration_path=args.calibration, depth_model=args.depth_model))
        report={"schema_version": 1, "mode": "continuous_video", "episodes": reports, "frame_count": sum(r["frames"] for r in reports)}
        args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps({'output':str(args.output),'frame_count':report['frame_count']},ensure_ascii=False)); return 0
    methods=['flat_ground','flat_bias_2m','flat_bias_-2m','flat_bias_5m','flat_bias_-5m','flat_bias_10m','flat_bias_-10m','known_size','rgb_slam','visual_inertial','vi_monocular_size','step_triangulation','fusion','fusion_g1','fusion_g3','fusion_g5','fusion_bias_0','fusion_bias_2','fusion_bias_-2','fusion_bias_5','fusion_bias_-5','fusion_bias_10','fusion_bias_-10','depth_raw','depth_tri_calibrated']
    vals={m:[] for m in methods}; slants={m:[] for m in methods}; rejects=Counter(); bins=defaultdict(list); rows_count=0; assoc=Counter(); depth_candidates=[]; depth_errors=[]; depth_times=[]; hist=defaultdict(list); fusion_cov=defaultdict(lambda:[[],[]]); tri_bins=defaultdict(list); tri_times=[]; step_hist=defaultdict(list); episode_index=0
    depth_buckets=defaultdict(list); depth_full_buckets=defaultdict(list); depth_target_buckets=defaultdict(list); tri_cov=[[],[]]
    model=None; depth_status='not_loaded'; model_path=Path('D:/Projects/Multi-Detect/artifacts/evaluation/depth-anything-v2-metric-outdoor-small.onnx')
    depth_sha=hashlib.sha256(model_path.read_bytes()).hexdigest() if model_path.is_file() else None
    for ep in episodes:
        world,truth=truth_for_episode(ep,args.scenarios)
        if truth is None: rejects['episode_missing_truth']+=1; continue
        targets={str(t['target_id']):t for t in truth.get('targets',[])}; step_path=ep/'steps.jsonl'
        try: lines=[json.loads(x) for x in step_path.read_text(encoding='utf-8').splitlines() if x.strip()]
        except Exception: rejects['episode_bad_steps']+=1; continue
        rgb_est=RgbSlamRangeEstimator(); vi_est=VisualInertialRangeEstimator(); motion_est=None; previous_boxes={}; frame_index=0; step_hist=defaultdict(list); episode_index+=1; prev_t=None
        for row in lines:
            if args.max_samples and rows_count >= args.max_samples: break
            ob=row.get('obs_before') or {}; tracks=((row.get('labels') or {}).get('onboard') or {}).get('tracks') or []
            if not tracks: continue
            seg=ob.get('segmentation_path'); sp=ep/str(seg).replace('\\','/') if seg else None; boxes=seg_boxes(sp,truth) if sp and sp.is_file() else {}
            boxes={k:v for k,v in boxes.items() if k in targets}
            source='segmentation' if boxes else 'projected_truth_bbox'; assoc[source]+=len(tracks)
            if not boxes:
                for tid,t in targets.items():
                    b=project_target_bbox(target_ned=(t['pose']['y_north_m'],t['pose']['x_east_m'],t['pose']['z_up_m']),pose=ob,camera_info=ob['camera_info'],image_size=(1280,960),target_size_m=tuple(t.get('size_m',(3,1.8,1.5))),target_yaw_rad=t['pose'].get('yaw_rad',0),target_bottom_offset_m=t.get('bottom_offset_m'))
                    if b is not None:boxes[tid]=list(b)
            cam=ob['pose']['local_ned_m']; cam=(float(cam[0]),float(cam[1]),float(cam[2])); tnow=frame_time(ob,frame_index*0.1); telemetry=make_telemetry(ob,tnow)
            frame_dt = None if prev_t is None else tnow-prev_t
            if frame_dt is not None:
                rejects['frame_dt_ms_bins/'+('lt350' if frame_dt<0.35 else '350_1000' if frame_dt<1.0 else 'ge1000')]+=1
            prev_t=tnow
            for tr in tracks:
                bb=tr.get('bbox'); match=max(((iou(bb,b),tid) for tid,b in boxes.items()),default=(0,None))
                if match[1] is None or match[0]<0.1: rejects['track_no_image_association']+=1; continue
                tid=match[1]; tp=targets[tid]['pose']; truthp=(float(tp['y_north_m']),float(tp['x_east_m']),-float(tp.get('z_up_m',0))); u=(bb[0]+bb[2])/2; v=(bb[1]+bb[3])/2; rows_count+=1
                r=ray(ob,u,v); truth_slant=math.dist(truthp,cam); stage=((row.get('labels') or {}).get('onboard') or {}).get('state_before','unknown')
                # Step-spaced multi-view geometry: only current and historical
                # associated rays are used.  No truth position enters admission.
                step_hist[tid].append((cam,r,tnow)); step_hist[tid]=step_hist[tid][-8:]
                tri_t0=time.perf_counter(); tri_point=triangulate([(x[0],x[1]) for x in step_hist[tid]])
                tq=triangulation_quality([(x[0],x[1]) for x in step_hist[tid]],tri_point); tri_times.append((time.perf_counter()-tri_t0)*1000)
                step_pred=None; tri_sigma=None
                if tq:
                    baseline,angle,residual,front,tri_sigma=tq
                    if baseline>=0.5 and angle>=0.5 and residual<=5.0 and front:
                        step_pred=tri_point; e=error_row(step_pred,truthp,cam)
                        if e:
                            vals['step_triangulation'].append(e[0]); slants['step_triangulation'].append(e[1]); bins[('step_triangulation',stage)].append(e[0]); tri_bins['baseline_m'].append(baseline); tri_bins['ray_angle_deg'].append(angle); tri_bins['sigma_68'].append(tri_sigma)
                            true_slant=math.dist(truthp,cam); pred_slant=math.dist(step_pred,cam); tri_cov[0].append(abs(pred_slant-true_slant)<=tri_sigma); tri_cov[1].append(abs(pred_slant-true_slant)<=1.96*tri_sigma)
                    else:
                        rejects['step_triangulation_quality_gate']+=1
                else: rejects['step_triangulation_insufficient_views']+=1
                def add(m,p,sigma=None):
                    e=error_row(p,truthp,cam)
                    if e: vals[m].append(e[0]); slants[m].append(e[1]); bins[(m,stage)].append(e[0])
                    else: rejects[m+'_invalid']+=1
                g=flat_range(ob,u, v, 0.0); add('flat_ground',g)
                for d in (2,5,10):
                    for signed in (d,-d):
                        q=flat_range(ob,u,v,signed); add(f'flat_bias_{signed}m',q)
                size=typical_object_size(row.get('task_text') or '',default=2.0); px=max(1.0,min(bb[2]-bb[0],bb[3]-bb[1])); rng=float(ob['camera_info']['fx'])*size/px; p=tuple(cam[i]+r[i]*rng for i in range(3)); add('known_size',p)
                trk=make_track(tr, int(ob['camera_info']['width']), int(ob['camera_info']['height']), tnow, frame_index)
                cal=load_camera_calibration(ROOT/'simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json')
                if motion_est is None: motion_est=AttitudeCameraMotionEstimator(cal)
                cm=motion_est.update(telemetry,captured_at_s=tnow)
                try:
                    rm=rgb_est.observe(track=trk,telemetry=telemetry,calibration=cal,frame_id=f'{ep.name}-{frame_index}',captured_at_s=tnow,camera_motion=cm)
                    if rm: add('rgb_slam',tuple(cam[i]+r[i]*rm.slant_range_m for i in range(3))); rgb_slant=rm.slant_range_m
                    else:
                        rejects['rgb_slam_rejected']+=1
                        if cm is None: rejects['rgb_slam_reason_no_attitude_motion']+=1
                        elif frame_dt is not None and frame_dt>0.35: rejects['rgb_slam_reason_step_interval_gt350ms']+=1
                        elif len(step_hist.get(tid,()))<4: rejects['rgb_slam_reason_fewer_than_4_keyframes']+=1
                        else: rejects['rgb_slam_reason_baseline_or_ray_gate']+=1
                        rgb_slant=None
                except Exception: rejects['rgb_slam_exception']+=1; rgb_slant=None
                try:
                    vm=vi_est.observe(track=trk,telemetry=telemetry,calibration=cal,frame_id=f'{ep.name}-{frame_index}',captured_at_s=tnow)
                    if vm and vm.source is DirectRangeSource.VIO:
                        add('visual_inertial',tuple(cam[i]+r[i]*vm.slant_range_m for i in range(3))); vi_slant=vm.slant_range_m
                    elif vm:
                        rejects['visual_inertial_monocular_size_fallback']+=1; vi_slant=None
                        e=error_row(tuple(cam[i]+r[i]*vm.slant_range_m for i in range(3)),truthp,cam)
                        if e: vals['vi_monocular_size'].append(e[0]); slants['vi_monocular_size'].append(e[1])
                    else:
                        rejects['visual_inertial_rejected']+=1
                        if frame_dt is not None and frame_dt>0.35: rejects['visual_inertial_reason_step_interval_gt350ms']+=1
                        elif len(step_hist.get(tid,()))<5: rejects['visual_inertial_reason_fewer_than_5_samples']+=1
                        else: rejects['visual_inertial_reason_span_or_baseline_gate']+=1
                        vi_slant=None
                except Exception: rejects['visual_inertial_exception']+=1; vi_slant=None
                direct=[]
                if rm: direct.append(rm)
                # RGB-SLAM and VIO share the same image/vehicle-motion evidence.  Do
                # not count both as independent measurements in the fusion filter.
                if vm and vm.source is DirectRangeSource.VIO and not rm: direct.append(vm)
                size_m=typical_object_size(row.get('task_text') or '',default=2.0); size_rng=float(ob['camera_info']['fx'])*size_m/max(1.0,min(bb[2]-bb[0],bb[3]-bb[1]))
                if step_pred is not None and tri_sigma is not None:
                    direct.append(DirectRangeMeasurement(DirectRangeSource.VIO,str(tr.get('id')),math.dist(step_pred,cam),max(1.0,tri_sigma),tnow))
                if not any(x.source is DirectRangeSource.MONOCULAR_SIZE for x in direct): direct.append(DirectRangeMeasurement(DirectRangeSource.MONOCULAR_SIZE,str(tr.get('id')),size_rng,max(5.0,size_rng*.75),tnow))
                try:
                    pose=AircraftPose(tnow,float(ob.get('attitude',{}).get('roll_deg',0)),float(ob.get('attitude',{}).get('pitch_deg',0)),float(ob.get('attitude',{}).get('yaw_deg',0))%360)
                    target=TargetImageObservation(str(tr.get('id')),'frame',tnow,float(u)/1280,float(v)/960)
                    for gsigma in (1.0,3.0,5.0):
                        fname='fusion_g'+str(int(gsigma))
                        assumed_height=max(.1,-cam[2])
                        sol=MultiModalRangingEngine().solve(calibration=cal,pose=pose,target=target,vertical_measurements=(VerticalMeasurement(VerticalSource.GROUND_PLANE,assumed_height,gsigma,tnow),),direct_measurements=tuple(direct),now_s=tnow)
                        if sol.slant_range_m is not None:
                            fp=tuple(cam[i]+r[i]*sol.slant_range_m for i in range(3)); add(fname,fp)
                            if fname=='fusion_g3': add('fusion',fp)
                            lo,hi=sol.slant_range_ci95_m or (math.nan,math.nan); fusion_cov[fname][0].append(bool(math.isfinite(lo) and lo<=truth_slant<=hi))
                            if math.isfinite(lo): fusion_cov[fname][1].append(lo+(hi-lo)*.5/1.96<=truth_slant<=hi-(hi-lo)*.5/1.96)
                    # Terrain-height sensitivity with the calibrated sigma=3 m.
                    for boff in (0,2,-2,5,-5,10,-10):
                        fname='fusion_bias_'+str(boff)
                        assumed_height=max(.1,-cam[2]-boff)
                        sol=MultiModalRangingEngine().solve(calibration=cal,pose=pose,target=target,vertical_measurements=(VerticalMeasurement(VerticalSource.GROUND_PLANE,assumed_height,3.0,tnow),),direct_measurements=tuple(direct),now_s=tnow)
                        if sol.slant_range_m is not None:
                            fp=tuple(cam[i]+r[i]*sol.slant_range_m for i in range(3)); add(fname,fp)
                except Exception: rejects['fusion_exception']+=1
                db = '<10' if truth_slant < 10 else ('10-20' if truth_slant < 20 else ('20-40' if truth_slant < 40 else '>40'))
                depth_buckets[db].append((ep,ob,bb,tid,truth_slant,stage,r,cam))
                frame_index+=1
    # Stratified depth sample (distance bins) with a hard global cap.
    if args.max_depth_frames:
        quota=max(1,args.max_depth_frames//4)
        for db in ('<10','10-20','20-40','>40'):
            depth_candidates.extend(depth_buckets.get(db,[])[:quota])
        if len(depth_candidates)<args.max_depth_frames:
            rest=[x for db,v in depth_buckets.items() for x in v[quota:]]
            depth_candidates.extend(rest[:args.max_depth_frames-len(depth_candidates)])
    # Optional CPU ONNX depth, isolated from geometry pass.
    if depth_candidates:
        try:
            import onnxruntime as ort, numpy as np
            import cv2
            so=ort.SessionOptions(); so.intra_op_num_threads=2; so.inter_op_num_threads=1
            session=ort.InferenceSession(str(model_path),so,providers=['CPUExecutionProvider']); model=session; depth_status='CPUExecutionProvider'
            inp=session.get_inputs()[0].name
            for ep,ob,bb,tid,truth_slant,stage,r,cam in depth_candidates:
                rp=ob.get('rgb_path'); path=ep/str(rp).replace('\\','/') if rp else None
                if not path or not path.is_file():continue
                img=cv2.imread(str(path)); t0=time.perf_counter(); rgb=cv2.cvtColor(img,cv2.COLOR_BGR2RGB); x=cv2.resize(rgb,(518,518),interpolation=cv2.INTER_CUBIC).astype(np.float32)/255.; x=(x-np.array([.485,.456,.406],np.float32))/np.array([.229,.224,.225],np.float32); out=session.run(None,{inp:np.transpose(x,(2,0,1))[None]})[0].squeeze(); elapsed=(time.perf_counter()-t0)*1000; depth_times.append(elapsed)
                try:
                    dp=ob.get("depth_path"); dpath=ep/str(dp).replace("\\","/") if dp else None
                    if dpath and dpath.is_file():
                        gt=np.load(dpath, mmap_mode="r").astype(np.float32)
                        gt2=cv2.resize(gt,(out.shape[1],out.shape[0]),interpolation=cv2.INTER_NEAREST)
                        finite=np.isfinite(gt2)&(gt2>0)&np.isfinite(out)&(out>0)
                        for lo,hi,label in ((0,10,"<10"),(10,20,"10-20"),(20,40,"20-40"),(40,float("inf"),">40")):
                            m=finite&(gt2>=lo)&(gt2<hi)
                            if np.any(m): depth_full_buckets[label].extend(np.abs(out[m]-gt2[m]).astype(float).tolist())
                except Exception: rejects["depth_truth_compare"]+=1
                h,w=out.shape; x0=max(0,int(bb[0]/1280*w)); x1=min(w,int(bb[2]/1280*w)); y0=max(0,int(bb[1]/960*h)); y1=min(h,int(bb[3]/960*h)); a=out[y0:y1,x0:x1]; raw=float(np.median(a[np.isfinite(a)])) if a.size else None
                if raw is None or raw<=0:continue
                # The ONNX model predicts optical-axis depth. Convert to Euclidean
                # slant along the box-centre ray before comparing with truth.
                xn=(float((bb[0]+bb[2])/2)-539.936)/539.936; yn=(float((bb[1]+bb[3])/2)-479.5)/539.936
                raw_slant=raw*math.sqrt(1.0+xn*xn+yn*yn)
                derr=abs(raw_slant-truth_slant); vals['depth_raw'].append(derr); slants['depth_raw'].append(derr); depth_errors.append((raw_slant,truth_slant)); db='lt10' if truth_slant<10 else ('10_20' if truth_slant<20 else ('20_40' if truth_slant<40 else 'ge40')); depth_target_buckets[db].append(derr)
            if depth_errors:
                # Causal calibration: fit only on the first half of the sampled
                # trajectory, then score the second half.  No current/future
                # truth is used to choose a triangulation window.
                cut=max(1,len(depth_errors)//2); train=depth_errors[:cut]; test=depth_errors[cut:]
                xs,ys=zip(*train); scale=sum(x*y for x,y in zip(xs,ys))/max(1e-9,sum(x*x for x in xs))
                calibrated=[abs(scale*x-y) for x,y in test]
                vals['depth_tri_calibrated'].extend(calibrated); slants['depth_tri_calibrated'].extend(calibrated)
        except Exception as e: depth_status='unavailable:'+type(e).__name__; rejects['depth_runtime']+=1
    # Fusion values above come from MultiModalRangingEngine.solve() per frame;
    # do not overwrite them with the flat-ground baseline.
    report={
        "schema_version":1,
        "execution":{"cpu_only":True,"cuda_visible_devices":"-1","onnx_providers":["CPUExecutionProvider"],"ort_threads":2},
        "sample_count":rows_count,"episodes":len(episodes),"association":dict(assoc),
        "methods":{m:{"horizontal_error":stats(vals[m]),"slant_error":stats(slants[m]),"valid_output_rate":len(vals[m])/max(1,rows_count)} for m in methods},
        "buckets":{f"{m}/{s}":stats(v) for (m,s),v in bins.items()},"reject_reasons":dict(rejects),
        "step_triangulation":{"geometry":{"baseline_m":stats(tri_bins['baseline_m']),"ray_angle_deg":stats(tri_bins['ray_angle_deg']),"sigma_m":stats(tri_bins['sigma_68'])},"cpu_ms":stats(tri_times),"coverage_68":sum(tri_cov[0])/max(1,len(tri_cov[0])),"coverage_95":sum(tri_cov[1])/max(1,len(tri_cov[1]))},
        "fusion_coverage":{k:{"coverage_68":sum(v[1])/max(1,len(v[1])),"coverage_95":sum(v[0])/max(1,len(v[0]))} for k,v in fusion_cov.items()},
        "depth":{"status":depth_status,"frames":len(depth_errors),"model_sha256":depth_sha,"cpu_ms":stats(depth_times),"target_box_error_by_distance":{k:stats(v) for k,v in depth_target_buckets.items()},"full_frame_truth_error_by_distance":{k:stats(v) for k,v in depth_full_buckets.items()},"orin_historical_ms_p50_p95":"68-82 ms/次（历史实测，非本机 CPU 结论）"},
        "notes":["triangulation uses only historical track centres and recorded Pixhawk local-NED positions; no truth is used for windows or boxes.","Visual-inertial path uses observed platform motion and classifies DirectRangeSource.VIO separately from monocular-size fallback.","metric-depth optical-axis output is converted to centre-ray slant before target scoring; full-frame truth comparison remains optical-axis.","projected_truth_bbox rows are sensitivity analysis only."]}
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps({'output':str(args.output),'sample_count':rows_count,'depth':depth_status},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())




