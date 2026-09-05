# Camera calibration assets

`charuco-7x5-40mm-20mm-4200px.png` is the canonical printable ChArUco board for
the Multi-Detect camera/ranging pipeline.

| Property | Value |
| --- | --- |
| Board | 7 × 5 squares, `DICT_5X5_100` |
| Square / marker length | 40 mm / 20 mm |
| Raster size | 4200 × 3000 px |
| Physical board size | 280 × 200 mm (verify after printing) |
| SHA-256 | `66AAF16F10689E9EDFF68B4684128E028AD89B09AABC55AB9CDC4D02521B23AC` |

Print at 100% scale with no fit-to-page resizing. The field collector and the
strict calibration command must use the same board geometry. The board image is
tracked under `configs/` so a fresh clone has the exact input; runtime captures,
reports and calibration JSON remain under the ignored `artifacts/` tree.

To reproduce a candidate with the local OpenCV toolchain, write it to a
temporary path first and compare the dimensions and SHA-256 before replacing
the canonical asset:

```powershell
Set-Location D:\Projects\Multi-Detect
$python = ".\ground-control\MultiDetectGCS\.venv\Scripts\python.exe"
& $python -m multidetect.camera_calibration board `
  --output .\tmp\charuco-7x5-40mm-20mm-4200px.png `
  --width-px 4200 --margin-px 0
```

The printed board is only a calibration input. It does not count as the
required 20 diverse camera views or close the outdoor GNSS/REAL release gates.

## GNSS-to-camera lever arm

The ranging projector accepts four optional fields in the `calibration` object:

```json
{
  "camera_offset_forward_m": 0.42,
  "camera_offset_right_m": -0.08,
  "camera_offset_down_m": 0.16,
  "camera_offset_sigma_m": 0.03
}
```

The first three values are the optical-centre position relative to the
navigation/GNSS reference point, expressed in aircraft body NED
(`forward/right/down`). The body vector is rotated with the measured roll,
pitch and heading before the ground ray is intersected. `camera_offset_sigma_m`
is an isotropic one-sigma survey uncertainty; it widens the height, ground-range
and bearing budgets. All four fields default to `0.0`, so existing calibration
documents keep their historical result. Values are intentionally bounded to
the 100 m domain and must be re-surveyed whenever the camera or GNSS antenna is
relocated. A lever-arm entry is an input correction, not evidence that outdoor
GNSS accuracy or the REAL gate has passed.

The machine-readable shape is [`configs/schemas/camera-calibration.schema.json`](../schemas/camera-calibration.schema.json).
The runtime loader remains the final authority: it rejects duplicate JSON keys,
non-finite numbers, unknown fields and values outside the same 100 m domain.

`boresight_sigma_deg` is the common one-sigma survey uncertainty for each of
the independently measured mount pitch, yaw and roll angles. The runtime
perturbs all three axes through the calibrated body-to-NED ray, combines their
bearing terms in quadrature, and refuses a ground intersection when any
one-sigma endpoint crosses the horizon. This is deliberately geometry-aware:
on a banked fixed wing or an off-axis detection, yaw and roll uncertainty can
also change inferred range and bearing.

When generating a calibration JSON from ChArUco data, pass the surveyed values
to the optional CLI switches so the provenance is retained in both the
calibration document and its report:

```powershell
& $python -m multidetect.camera_calibration calibrate `
  --images .\artifacts\calibration\camera-main-frames `
  --calibration-id camera-main-fixed-v2 `
  --mount-pitch-down-deg 5 `
  --mount-yaw-right-deg 0 `
  --mount-roll-clockwise-deg 0 `
  --boresight-sigma-deg 0.4 `
  --camera-offset-forward-m 0.42 `
  --camera-offset-right-m -0.08 `
  --camera-offset-down-m 0.16 `
  --camera-offset-sigma-m 0.03 `
  --calibration-out .\artifacts\calibration\camera-main-fixed-v2.json `
  --report-out .\artifacts\calibration\camera-main-fixed-v2.report.json
```
