"""
sweep.py — Parameter-sweep orchestrator for the power-study v2 system.

Reads a sweep spec, plans the segments, validates each config, prompts the
user to start the N6705B datalog, runs a calibration burst, then runs every
segment back-to-back while logging wall-clock timestamps so the offline
analyzer can segment the datalog after the fact.

The calibration burst at the start and end produces a known idle/chirp/idle
pattern (two distinct chirp pulses separated by an idle gap) that the
analyzer detects to anchor the datalog's time axis to the script's wall-clock
timestamps.

Usage:
    uv run rps-sweep --spec sweeps/frame_period_sweep.yaml
    uv run rps-sweep --spec sweeps/frame_period_sweep.yaml --dry-run
    uv run rps-sweep --spec sweeps/frame_period_sweep.yaml \\
        --replicates 1 --on-error abort
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import hashlib
import itertools
import json
import logging
import random
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pa_xwr import capture

logger = logging.getLogger("sweep")

# -----------------------------------------------------------------------------
# Calibration burst configuration.
#
# The burst produces a current signature that the analyzer locates in the
# datalog to align wall-clock time with datalog time. The pattern is two
# chirp pulses separated by an idle gap - much harder to confuse with random
# transients than a single pulse.
#
# Total burst duration = sum of all phase durations = 35 seconds.
# -----------------------------------------------------------------------------
_CAL_BURST_PATTERN: list[tuple[str, float, bool]] = [
    # (phase_name, duration_seconds, is_chirping)
    ("wait_1",      10.0, False),
    ("cal_pulse_1",  5.0, True),
    ("wait_2",      10.0, False),
    ("cal_pulse_2",  5.0, True),
    ("wait_3",       5.0, False),
]
CAL_BURST_DURATION_S: float = sum(d for _, d, _ in _CAL_BURST_PATTERN)
#: Datalog seconds from cal-burst start to the falling edge of the SECOND
#: cal pulse — this is the anchor point the analyzer looks for.
CAL_BURST_ANCHOR_OFFSET_S: float = 10 + 5 + 10 + 5   # = 30.0

# -----------------------------------------------------------------------------
# Static validation — catch obvious config errors without touching hardware.
# Device-specific runtime constraints (L3 memory, etc.) are caught by xwr at
# run time and reported per-segment.
# -----------------------------------------------------------------------------

POWERS_OF_TWO = {1 << n for n in range(2, 16)}      # 4 .. 32768

#: Parameters whose value must be a power of 2 for any TI mmWave radar
#: (driven by the on-chip radix-2 FFT engine).
_POWER_OF_TWO_PARAMS = {"adc_samples", "frame_length"}

#: Parameters that must be strictly positive numbers.
_POSITIVE_PARAMS = {
    "frequency", "freq_slope", "ramp_end_time", "idle_time", "tx_start_time",
    "adc_start_time", "adc_samples", "sample_rate", "frame_length",
    "frame_period",
}


def validate_config(cfg: dict, segment_label: str) -> list[str]:
    """Check a generated config against device-agnostic rules. Returns a
    list of error strings (empty if OK)."""
    errors: list[str] = []
    radar = cfg.get("radar", {})

    for name in _POSITIVE_PARAMS:
        if name not in radar:
            continue
        v = radar[name]
        if not isinstance(v, (int, float)) or v <= 0:
            errors.append(
                f"{segment_label}: '{name}' must be a positive number, "
                f"got {v!r}")

    for name in _POWER_OF_TWO_PARAMS:
        if name not in radar:
            continue
        v = radar[name]
        if not isinstance(v, int) or v not in POWERS_OF_TWO:
            errors.append(
                f"{segment_label}: '{name}' must be a power of 2 in "
                f"[4, 32768], got {v!r}")

    return errors


# -----------------------------------------------------------------------------
# Sweep planning
# -----------------------------------------------------------------------------

@dataclass
class Segment:
    """One planned (and later, executed) capture segment."""
    seg_id: int
    primary_param: str
    primary_value: Any
    secondary_param: str | None
    secondary_value: Any | None
    replicate: int
    config: dict
    label: str
    start_wallclock_iso: str | None = None
    end_wallclock_iso: str | None = None
    status: str = "planned"
    frames_captured: int = 0
    error: str | None = None


def _coerce_to_template_type(
    device_template: dict,
    radar_param: str,
    swept_value,
):
    """Coerce a swept value to the same Python type as the corresponding
    field in the device template.

    The device template is treated as the schema oracle - whatever Python type
    each field has in `devices/<radar>.yaml` is the type xwr expects. This
    lets users write sweep values as `[25, 50, 100]` (YAML ints) even for
    fields xwr wants as `float`, and avoids the inverse mistake (casting
    everything to float, breaking fields xwr wants as int like `sample_rate`).

    Bools are *not* treated as ints despite Python's bool-is-int inheritance.
    """
    radar = device_template.get("radar", {})
    if radar_param not in radar:
        # Unknown field. Let xwr complain about it rather than silently
        # accepting a misspelled sweep parameter.
        return swept_value

    template_value = radar[radar_param]
    template_type = type(template_value)

    # bool is a subclass of int in Python; don't auto-convert numbers to bool.
    if template_type is bool:
        return swept_value

    if isinstance(swept_value, template_type):
        return swept_value

    # Coerce numeric -> numeric (int <-> float). For other type mismatches,
    # leave the value alone and let validate_config / xwr report the error.
    if isinstance(swept_value, (int, float)) and template_type in (int, float):
        return template_type(swept_value)

    return swept_value


def plan_segments(
    spec: dict,
    device_template: dict,
    replicates_override: int | None,
    seed: int,
) -> tuple[list[Segment], dict]:
    """Build the full ordered list of segments to run.

    Returns (segments, normalized_sweep_settings).
    """
    s = spec["sweep"]
    replicates = replicates_override or s.get("replicates", 3)

    # Detect 1-D vs multi-line (b)-style sweep from the spec shape.
    if "primary" in s and "secondary" in s:
        primary_param = s["primary"]["param"]
        primary_values = list(s["primary"]["values"])
        secondary_param = s["secondary"]["param"]
        secondary_values = list(s["secondary"]["held_at"])
        mode = "multi_line"
    elif "param" in s and "values" in s:
        primary_param = s["param"]
        primary_values = list(s["values"])
        secondary_param = None
        secondary_values = [None]
        mode = "single"
    else:
        raise ValueError(
            "sweep spec must have either {'param', 'values'} or "
            "{'primary', 'secondary'}"
        )

    settings = {
        "mode": mode,
        "primary_param": primary_param,
        "primary_values": primary_values,
        "secondary_param": secondary_param,
        "secondary_values": secondary_values if secondary_param else None,
        "replicates": replicates,
        "segment_duration": s.get("segment_duration", 30),
        "idle_between": s.get("idle_between", 5),
        "order": s.get("order", "random"),
    }

    # Build all (primary, secondary, replicate) combinations.
    points = list(itertools.product(
        primary_values, secondary_values, range(1, replicates + 1)))

    # Apply ordering policy.
    if settings["order"] == "random":
        rng = random.Random(seed)
        rng.shuffle(points)
    elif settings["order"] == "sequential":
        pass  # already in product order
    else:
        raise ValueError(
            f"order must be 'random' or 'sequential', "
            f"got {settings['order']!r}")

    segments: list[Segment] = []
    for seg_id, (pv, sv, rep) in enumerate(points, start=1):
        cfg = copy.deepcopy(device_template)
        cfg["radar"][primary_param] = _coerce_to_template_type(
            device_template, primary_param, pv)
        if secondary_param is not None:
            cfg["radar"][secondary_param] = _coerce_to_template_type(
                device_template, secondary_param, sv)

        if secondary_param is None:
            label = f"{primary_param}={pv} rep={rep}"
        else:
            label = (f"{primary_param}={pv} "
                     f"{secondary_param}={sv} rep={rep}")

        segments.append(Segment(
            seg_id=seg_id,
            primary_param=primary_param,
            primary_value=pv,
            secondary_param=secondary_param,
            secondary_value=sv,
            replicate=rep,
            config=cfg,
            label=label,
        ))

    return segments, settings


# -----------------------------------------------------------------------------
# Study folder setup
# -----------------------------------------------------------------------------

def make_study_folder(
    studies_root: Path,
    study_name: str,
    device_name: str,
) -> Path:
    """Create a fresh study folder, appending _2, _3, ... if needed."""
    today = dt.date.today().isoformat()
    base = studies_root / f"{today}_{device_name}_{study_name}"
    candidate = base
    n = 2
    while candidate.exists():
        candidate = studies_root / f"{base.name}_{n}"
        n += 1
    candidate.mkdir(parents=True)
    (candidate / "raw").mkdir()
    (candidate / "configs").mkdir()
    return candidate


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return f"sha256:{h.hexdigest()}"


# -----------------------------------------------------------------------------
# Segment log (segments.csv)
# -----------------------------------------------------------------------------

_SEGMENTS_HEADER = [
    "seg_id", "replicate", "primary_param", "primary_value",
    "secondary_param", "secondary_value",
    "start_wallclock_iso", "end_wallclock_iso",
    "status", "frames_captured", "error",
]


def init_segments_csv(path: Path) -> None:
    with path.open("w", newline="") as f:
        csv.writer(f).writerow(_SEGMENTS_HEADER)


def append_segment_row(path: Path, seg: Segment) -> None:
    with path.open("a", newline="") as f:
        csv.writer(f).writerow([
            seg.seg_id,
            seg.replicate,
            seg.primary_param,
            seg.primary_value,
            seg.secondary_param or "",
            "" if seg.secondary_value is None else seg.secondary_value,
            seg.start_wallclock_iso or "",
            seg.end_wallclock_iso or "",
            seg.status,
            seg.frames_captured,
            seg.error or "",
        ])


# -----------------------------------------------------------------------------
# Calibration burst
# -----------------------------------------------------------------------------

def run_calibration_burst(
    device_template: dict,
    label: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Run one calibration burst (idle / chirp / idle / chirp / idle).

    Uses the device template's defaults as the chirping config so the burst's
    current signature is reproducible across runs of the same sweep, even if
    the sweep itself varies different parameters.

    Returns a dict describing the phases with wall-clock timestamps. The
    analyzer reads these from calibration.json to align datalog time with
    wall-clock time.
    """
    logger.info("Starting %s calibration burst (%.0f s)...",
                label, CAL_BURST_DURATION_S)
    cal_config = copy.deepcopy(device_template)
    burst_start_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    phases: list[dict] = []
    for phase_name, duration, is_chirping in _CAL_BURST_PATTERN:
        phase_start_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        logger.info("  cal phase '%s' (%.0f s, chirping=%s)",
                    phase_name, duration, is_chirping)

        if is_chirping:
            result = capture.run_segment(
                cal_config, duration, dry_run=dry_run)
            if not result.success:
                logger.warning("Cal phase '%s' chirping failed: %s",
                               phase_name, result.error)
        else:
            time.sleep(duration)

        phase_end_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        phases.append({
            "name": phase_name,
            "duration_s": duration,
            "chirping": is_chirping,
            "start_iso": phase_start_iso,
            "end_iso": phase_end_iso,
        })

    burst_end_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    logger.info("%s calibration burst done.", label.capitalize())

    return {
        "label": label,
        "start_iso": burst_start_iso,
        "end_iso": burst_end_iso,
        "anchor_offset_s": CAL_BURST_ANCHOR_OFFSET_S,
        "phases": phases,
    }


# -----------------------------------------------------------------------------
# The main sweep loop
# -----------------------------------------------------------------------------

def estimate_duration_s(n_segments: int, segment_duration: float,
                       idle_between: float) -> float:
    """Time the user must dedicate to the datalog."""
    # Two cal bursts (start + end) plus the segment sweep itself plus a
    # 30 s buffer for datalog start/stop padding.
    return (
        2 * CAL_BURST_DURATION_S
        + n_segments * (segment_duration + idle_between)
        + 30.0
    )


def run_sweep(
    spec_path: Path,
    devices_dir: Path,
    studies_root: Path,
    *,
    dry_run: bool = False,
    replicates_override: int | None = None,
    device_override: str | None = None,
    on_error: str = "skip",
    seed: int = 42,
    skip_prompt: bool = False,
) -> Path:
    """Run a full sweep. Returns the study folder path."""

    # -- Load spec --
    with spec_path.open() as f:
        spec = yaml.safe_load(f)

    device_name = device_override or spec["device"]
    device_path = devices_dir / f"{device_name}.yaml"
    if not device_path.exists():
        raise FileNotFoundError(
            f"Device template not found: {device_path}\n"
            f"Available templates: "
            f"{sorted(p.stem for p in devices_dir.glob('*.yaml'))}"
        )
    with device_path.open() as f:
        device_template = yaml.safe_load(f)

    # -- Plan segments --
    segments, settings = plan_segments(
        spec, device_template, replicates_override, seed)

    # -- Validate all configs up front --
    all_errors: list[str] = []
    for seg in segments:
        all_errors.extend(validate_config(seg.config, seg.label))

    if all_errors:
        logger.error("Static validation failed for %d issue(s):",
                     len(all_errors))
        for e in all_errors:
            logger.error("  %s", e)
        raise SystemExit(2)

    # -- Set up study folder --
    study_dir = make_study_folder(
        studies_root, spec["study_name"], device_name)
    log_file = study_dir / "capture.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(file_handler)

    logger.info("Study folder: %s", study_dir)

    # Snapshot the spec and device template into the study folder.
    shutil.copy(spec_path, study_dir / "sweep_used.yaml")
    shutil.copy(device_path, study_dir / "device_used.yaml")
    for seg in segments:
        seg_cfg_path = study_dir / "configs" / f"segment_{seg.seg_id:03d}.yaml"
        with seg_cfg_path.open("w") as f:
            yaml.safe_dump(seg.config, f, sort_keys=False)

    segments_csv = study_dir / "segments.csv"
    init_segments_csv(segments_csv)

    # -- Estimate time and prompt --
    total_s = estimate_duration_s(
        len(segments), settings["segment_duration"], settings["idle_between"])
    total_min = total_s / 60.0
    print()
    print("=" * 60)
    print(f"  Sweep plan: {spec['study_name']} on {device_name}")
    print("=" * 60)
    print(f"  Mode:              {settings['mode']}")
    print(f"  Primary param:     {settings['primary_param']}")
    print(f"  Primary values:    {settings['primary_values']}")
    if settings["secondary_param"]:
        print(f"  Secondary param:   {settings['secondary_param']}")
        print(f"  Held at:           {settings['secondary_values']}")
    print(f"  Replicates:        {settings['replicates']}")
    print(f"  Order:             {settings['order']}  (seed={seed})")
    print(f"  Segment duration:  {settings['segment_duration']} s")
    print(f"  Idle between:      {settings['idle_between']} s")
    print(f"  Total segments:    {len(segments)}")
    print(f"  Expected duration: {total_s:.0f} s "
          f"({total_min:.1f} min)")
    print(f"  Study folder:      {study_dir}")
    if dry_run:
        print(f"  Mode:              DRY RUN (no hardware)")
    print("=" * 60)
    print()
    print(f"On the N6705B:")
    print(f"  1. Set datalog sample period: 0.001 s (1 ms)")
    print(f"  2. Set datalog duration:      >= {int(total_s + 60)} s "
          f"(adds 60 s safety margin)")
    print(f"  3. Set filename:              datalog.csv (or any name)")
    print(f"  4. Press Run on the N6705B")
    print(f"  5. Then press Enter here, within 5 seconds")
    print()
    if not skip_prompt:
        input("[Press Enter to begin]")

    # -- Run start calibration burst --
    run_started = dt.datetime.now(dt.timezone.utc).isoformat()
    cal_start = run_calibration_burst(
        device_template, "start", dry_run=dry_run)

    # -- Run segments --
    for seg in segments:
        seg.start_wallclock_iso = dt.datetime.now(
            dt.timezone.utc).isoformat()
        logger.info("Segment %d/%d: %s",
                    seg.seg_id, len(segments), seg.label)

        result = capture.run_segment(
            seg.config, settings["segment_duration"], dry_run=dry_run)

        seg.end_wallclock_iso = dt.datetime.now(
            dt.timezone.utc).isoformat()
        seg.frames_captured = result.frames_captured
        seg.status = "ok" if result.success else "failed"
        seg.error = result.error

        append_segment_row(segments_csv, seg)

        if not result.success:
            logger.warning("Segment %d failed: %s",
                           seg.seg_id, result.error)
            if on_error == "abort":
                logger.error("Aborting sweep (--on-error abort).")
                break

        time.sleep(settings["idle_between"])

    # -- Run end calibration burst --
    cal_end = run_calibration_burst(
        device_template, "end", dry_run=dry_run)
    run_ended = dt.datetime.now(dt.timezone.utc).isoformat()

    # -- Write calibration.json --
    with (study_dir / "calibration.json").open("w") as f:
        json.dump({
            "anchor_offset_s": CAL_BURST_ANCHOR_OFFSET_S,
            "pattern": [
                {"name": n, "duration_s": d, "chirping": c}
                for n, d, c in _CAL_BURST_PATTERN
            ],
            "start_burst": cal_start,
            "end_burst": cal_end,
        }, f, indent=2)

    # -- Write manifest --
    completed = sum(1 for s in segments if s.status == "ok")
    failed = sum(1 for s in segments if s.status == "failed")
    planned = len(segments)
    try:
        import xwr  # type: ignore[import-untyped]
        xwr_version = getattr(xwr, "__version__", "unknown")
    except ImportError:
        xwr_version = "not installed (dry-run only)"

    manifest = {
        "study_name": spec["study_name"],
        "device": device_name,
        "device_template_hash": sha256_of(device_path),
        "sweep_spec_hash": sha256_of(spec_path),
        "xwr_version": xwr_version,
        "python_version": f"{sys.version_info.major}."
                          f"{sys.version_info.minor}."
                          f"{sys.version_info.micro}",
        "run_started_iso": run_started,
        "run_ended_iso": run_ended,
        "segments_planned": planned,
        "segments_completed": completed,
        "segments_failed": failed,
        "settings": settings,
        "seed": seed,
        "dry_run": dry_run,
        "operator_notes": "",
    }
    with (study_dir / "manifest.json").open("w") as f:
        json.dump(manifest, f, indent=2)

    # -- Final instructions --
    print()
    print("=" * 60)
    print(f"  Sweep complete: {completed}/{planned} segments ok, "
          f"{failed} failed")
    print("=" * 60)
    print()
    print(f"Wait ~5 s for the datalog buffer to flush, then on the N6705B:")
    print(f"  1. Stop the datalog")
    print(f"  2. Export it as CSV to your USB stick")
    print(f"  3. Copy to: {study_dir / 'raw' / 'datalog.csv'}")
    print()
    print(f"Then run:")
    print(f"  uv run rps-analyze {study_dir}")
    print()

    return study_dir


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a parameter-sweep power study.")
    parser.add_argument("--spec", type=Path, required=True,
                        help="Path to sweep spec YAML")
    parser.add_argument("--devices-dir", type=Path,
                        default=Path("devices"),
                        help="Folder containing device template YAMLs "
                             "(default: ./devices relative to cwd)")
    parser.add_argument("--studies-root", type=Path,
                        default=Path("studies"),
                        help="Where to create the new study folder "
                             "(default: ./studies relative to cwd)")
    parser.add_argument("--device", type=str, default=None,
                        help="Override the device named in the spec")
    parser.add_argument("--replicates", type=int, default=None,
                        help="Override replicates from the spec")
    parser.add_argument("--on-error", choices=["skip", "abort"],
                        default="skip",
                        help="What to do when a segment fails")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for segment ordering")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without touching hardware")
    parser.add_argument("--skip-prompt", action="store_true",
                        help="Don't wait for Enter before starting "
                             "(useful in scripts/tests)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    )

    args.studies_root.mkdir(parents=True, exist_ok=True)

    try:
        run_sweep(
            spec_path=args.spec,
            devices_dir=args.devices_dir,
            studies_root=args.studies_root,
            dry_run=args.dry_run,
            replicates_override=args.replicates,
            device_override=args.device,
            on_error=args.on_error,
            seed=args.seed,
            skip_prompt=args.skip_prompt,
        )
    except SystemExit:
        raise
    except Exception as e:        # noqa: BLE001
        logger.exception("Sweep failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())