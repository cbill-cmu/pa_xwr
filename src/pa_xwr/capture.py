"""
capture.py — Single-segment radar capture.

Drives one (config, duration) capture. Called by sweep.py for each segment of
a sweep. Can also be run directly for a one-off capture, mirroring the old
MyCapture.py for backward compatibility.

This module hides the xwr API behind two functions so we can also
substitute a mock implementation in --dry-run mode without changing call
sites.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SegmentResult:
    """Result of one radar capture segment."""

    success: bool
    frames_captured: int
    duration_actual_s: float
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "frames_captured": self.frames_captured,
            "duration_actual_s": round(self.duration_actual_s, 3),
            "error": self.error,
        }


def run_segment(
    config: dict,
    duration_s: float,
    dry_run: bool = False,
) -> SegmentResult:
    """
    Run one capture segment.

    Args:
        config: xwr config dict (the result of loading a YAML).
                Top-level keys: 'radar', 'capture'.
        duration_s: how long to chirp, in seconds.
        dry_run: if True, simulate the capture without touching hardware.
                 Used for testing the orchestration logic.

    Returns:
        SegmentResult capturing whether it worked and how many frames came in.

    On any radar/DCA1000 error, returns success=False with the exception
    message in `error`. The caller (sweep.py) decides whether to abort or
    continue the sweep.
    """
    if dry_run:
        return _run_segment_dry(config, duration_s)
    return _run_segment_real(config, duration_s)


def _run_segment_real(config: dict, duration_s: float) -> SegmentResult:
    """Actual hardware capture. Imports xwr lazily so the script can run
    without xwr installed (e.g. for --dry-run on the analysis machine)."""
    import xwr

    t0 = time.monotonic()
    n_frames = 0
    awr = None

    try:
        awr = xwr.XWRSystem(**config)
        logger.info("sensorStart sent. Capturing for %.1f s", duration_s)
        for _ in awr.stream():
            n_frames += 1
            if time.monotonic() - t0 > duration_s:
                break
        awr.stop()
        elapsed = time.monotonic() - t0
        logger.info(
            "sensorStop sent. Captured %d frames in %.1f s",
            n_frames, elapsed,
        )
        return SegmentResult(True, n_frames, elapsed)

    except Exception as e:        # noqa: BLE001 - intentional broad catch
        elapsed = time.monotonic() - t0
        logger.error("Segment failed after %.1f s: %s", elapsed, e)
        # Try to stop the radar cleanly if we made it that far.
        if awr is not None:
            try:
                awr.stop()
            except Exception:     # noqa: BLE001, S110
                pass
        return SegmentResult(False, n_frames, elapsed, error=str(e))


def _run_segment_dry(config: dict, duration_s: float) -> SegmentResult:
    """Simulated capture for testing the orchestrator. Sleeps for the
    requested duration and pretends frames were captured at the configured
    frame rate."""
    radar_cfg = config.get("radar", {})
    frame_period_ms = radar_cfg.get("frame_period", 50.0)
    expected_fps = 1000.0 / frame_period_ms

    logger.info("[DRY-RUN] Pretending to capture for %.1f s "
                "at %.1f fps", duration_s, expected_fps)
    time.sleep(duration_s)
    n_frames = int(expected_fps * duration_s)
    return SegmentResult(True, n_frames, duration_s)


# -----------------------------------------------------------------------------
# CLI entry point for backward compatibility with MyCapture.py
# -----------------------------------------------------------------------------

def _main() -> int:
    import sys
    import yaml

    parser = argparse.ArgumentParser(
        description="Run a single radar capture segment.")
    parser.add_argument("config", help="Path to xwr config YAML")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="Capture duration in seconds (default 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate the capture without hardware")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    )

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    result = run_segment(cfg, args.duration, dry_run=args.dry_run)
    print(result.to_dict())
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(_main())