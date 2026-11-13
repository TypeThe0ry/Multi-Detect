"""双教师共识：YOLOE（快、开放词表）与 Qwen3-VL（慢、语义强）互相校验。

规则（全部是纯函数，CPU 可测）：
  agree      两个教师同类且 IoU >= iou_thr  -> 高置信标签，框取分数加权平均
  single     只有一个教师给出                 -> 不进训练标签，进「不确定」池（训练时可作为忽略区）
  empty      两个教师都没有任何框             -> 该帧可当负样本（只在 --allow-negatives 时启用）
框一律用归一化 [x1, y1, x2, y2]。
"""
from __future__ import annotations


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def fuse(fast: list[dict], slow: list[dict], iou_thr: float = 0.5):
    """fast/slow: [{cls, box, score}]。返回 (agree, single_fast, single_slow)。"""
    used = set()
    agree, single_fast = [], []
    for f in sorted(fast, key=lambda d: -d["score"]):
        best, best_i = 0.0, None
        for j, s in enumerate(slow):
            if j in used or s["cls"] != f["cls"]:
                continue
            v = iou(f["box"], s["box"])
            if v > best:
                best, best_i = v, j
        if best_i is not None and best >= iou_thr:
            used.add(best_i)
            s = slow[best_i]
            wf, ws = f["score"], s["score"]
            w = wf + ws
            box = [(wf * f["box"][k] + ws * s["box"][k]) / w for k in range(4)]
            agree.append({"cls": f["cls"], "box": box, "score": min(wf, ws), "src": "both"})
        else:
            single_fast.append({**f, "src": "fast"})
    single_slow = [{**s, "src": "slow"} for j, s in enumerate(slow) if j not in used]
    return agree, single_fast, single_slow


def to_yolo_line(cls_id: int, box) -> str:
    x1, y1, x2, y2 = box
    return f"{cls_id} {(x1 + x2) / 2:.6f} {(y1 + y2) / 2:.6f} {x2 - x1:.6f} {y2 - y1:.6f}"
