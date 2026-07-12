"""Multi-Detect safety-first mission orchestration prototype."""

from .config import MissionConfig
from .domain import Detection, FrameObservation, VehicleTelemetry

__all__ = ["Detection", "FrameObservation", "MissionConfig", "VehicleTelemetry"]
__version__ = "0.1.0"
