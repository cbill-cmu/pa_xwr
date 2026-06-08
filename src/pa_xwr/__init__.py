"""pa_xwr — automated power studies of TI mmWave radars."""

__version__ = "0.1.0"

from pa_xwr.capture import SegmentResult, run_segment
from pa_xwr.sweep import Segment, plan_segments, run_sweep

__all__ = [
    "SegmentResult",
    "Segment",
    "plan_segments",
    "run_segment",
    "run_sweep",
]