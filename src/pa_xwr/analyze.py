"""
analyze.py — Offline analysis of a power-study run.

Reads a study folder produced by `rps-sweep`, anchors the long N6705B datalog
to the script's wall-clock timestamps using the calibration burst, segments
the datalog according to segments.csv, computes per-segment statistics, and
generates plots plus a summary report.

Usage:
    uv run rps-analyze studies/<study-folder>/
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

logger = logging.getLogger("analyze")

# -----------------------------------------------------------------------------
# CSV loading (handles both N6705B export variants we've seen in practice)
# -----------------------------------------------------------------------------

#: Fallback supply voltage used if a CSV is current-only.
SUPPLY_VOLTAGE_V = 5.0


def load_n6705b_csv(path: Path) -> pd.DataFrame:
    """Load a Keysight N6705B Datalogger CSV export.

    Auto-detects the two formats observed in the field:
      A) Time-indexed, comma-separated:  Time (s), Volt 1, Curr 1, ...
      B) Sample-indexed, tab-separated, with `Sample interval: X` metadata.

    Returns columns: t (s, normalized to start at 0), v1, i1, v2, i2, p1, p2.
    """
    sample_interval = None
    header_row = None
    delimiter = None

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        m = re.search(r"sample\s*interval[:\s]+([0-9.eE+\-]+)",
                      line, re.IGNORECASE)
        if m:
            try:
                sample_interval = float(m.group(1))
            except ValueError:
                pass

        s = line.lstrip().lstrip('"')
        if (re.match(r"(time|sample)\b", s, re.IGNORECASE)
                and re.search(r"(volt|curr)", line, re.IGNORECASE)):
            header_row = i
            delimiter = "\t" if "\t" in line else ","
            break

    if header_row is None:
        raise ValueError(f"No Time/Sample header row found in {path}")

    df = pd.read_csv(path, skiprows=header_row, sep=delimiter,
                     engine="python")
    df.columns = [c.strip().strip('"') for c in df.columns]

    def find_col(patterns: list[str]) -> str | None:
        for p in patterns:
            for c in df.columns:
                if re.search(p, c, re.IGNORECASE):
                    return c
        return None

    t_col = find_col([r"^\s*time", r"^\s*sample"])
    is_sample = (t_col is not None
                 and re.match(r"sample", t_col, re.IGNORECASE) is not None)
    v1_col = find_col([r"volt.*1", r"v1"])
    i1_col = find_col([r"curr.*1", r"i1"])
    v2_col = find_col([r"volt.*2", r"v2"])
    i2_col = find_col([r"curr.*2", r"i2"])

    if t_col is None or i1_col is None:
        raise ValueError(
            f"Required columns missing in {path}. Found: {list(df.columns)}")

    raw_t = pd.to_numeric(df[t_col], errors="coerce")
    if is_sample:
        if sample_interval is None:
            sample_interval = 0.001
            logger.warning(
                "No 'Sample interval' in %s; assuming %.3f s",
                path, sample_interval)
        t = raw_t * sample_interval
    else:
        t = raw_t

    out = pd.DataFrame({
        "t":  t,
        "v1": (pd.to_numeric(df[v1_col], errors="coerce")
               if v1_col else SUPPLY_VOLTAGE_V),
        "i1": pd.to_numeric(df[i1_col], errors="coerce"),
        "v2": (pd.to_numeric(df[v2_col], errors="coerce")
               if v2_col else SUPPLY_VOLTAGE_V),
        "i2": (pd.to_numeric(df[i2_col], errors="coerce")
               if i2_col else np.nan),
    }).dropna(subset=["t", "i1"]).reset_index(drop=True)

    out["t"] = out["t"] - out["t"].iloc[0]
    out["p1"] = out["v1"] * out["i1"]
    out["p2"] = out["v2"] * out["i2"]
    return out


# -----------------------------------------------------------------------------
# Time anchoring via calibration burst detection
# -----------------------------------------------------------------------------

@dataclass
class TimeAnchor:
    """Maps wall-clock ISO timestamps to datalog seconds."""
    cal_start_wallclock: dt.datetime    # script's UTC time at cal burst start
    datalog_t_at_anchor: float          # datalog seconds at the anchor event
    anchor_offset_s: float              # offset from cal start to anchor (=30)
    method: str                          # "cal_burst" or "wallclock_fallback"

    def to_datalog_time(self, iso: str) -> float:
        """Convert a wall-clock ISO string to datalog time in seconds."""
        wc = dt.datetime.fromisoformat(iso)
        delta_from_cal_start = (wc - self.cal_start_wallclock).total_seconds()
        # When delta_from_cal_start == anchor_offset_s (30), we should be at
        # datalog_t_at_anchor. So:
        return delta_from_cal_start - self.anchor_offset_s + self.datalog_t_at_anchor


def detect_cal_burst_anchor(
    datalog: pd.DataFrame,
    cal_metadata: dict,
) -> tuple[float, str] | None:
    """Find the falling edge of the SECOND cal pulse in the start burst.

    Returns (datalog_time_seconds, method) where method describes how the
    detection went, or None if no plausible cal-burst pattern is found.
    """
    # Combine both channels for a stronger signal.
    i_total = (datalog["i1"].fillna(0) + datalog["i2"].fillna(0)).values
    t = datalog["t"].values

    # Limit search to the first 60 s of the datalog (cal burst should fit
    # comfortably in 35 s starting near t=0).
    search_mask = t < 60.0
    if search_mask.sum() < 100:
        logger.warning("Datalog too short to detect cal burst")
        return None
    t_search = t[search_mask]
    i_search = i_total[search_mask]

    # Idle level: median of the first 8 s (should be pre-cal-pulse idle).
    idle_mask = t_search < 8.0
    if idle_mask.sum() < 10:
        return None
    idle_level = float(np.median(i_search[idle_mask]))

    # Operating level: 90th percentile across the full search window.
    op_level = float(np.percentile(i_search, 90))

    delta = op_level - idle_level
    if delta < 0.05:
        logger.warning(
            "Cal burst not detected: current delta too small "
            "(idle=%.3f A, op=%.3f A). Falling back to wall-clock.",
            idle_level, op_level)
        return None

    threshold = idle_level + 0.4 * delta

    # Find threshold crossings.
    above = i_search > threshold
    crossings = np.diff(above.astype(int))
    rising_idx = np.where(crossings == 1)[0] + 1
    falling_idx = np.where(crossings == -1)[0] + 1
    rising_t = t_search[rising_idx] if len(rising_idx) else np.array([])
    falling_t = t_search[falling_idx] if len(falling_idx) else np.array([])

    # Look for the pattern: rise, fall ~5s later, rise ~10s after that,
    # fall ~5s after that. Tolerances are loose so the user pressing
    # Run on the N6705B with up to a few seconds of slop still works.
    for r1 in rising_t:
        if r1 > 25:        # cal pulse 1 should rise within first ~25 s
            break
        f1 = falling_t[(falling_t > r1) & (falling_t < r1 + 9)]
        if len(f1) == 0:
            continue
        f1 = f1[0]
        r2 = rising_t[(rising_t > f1 + 6) & (rising_t < f1 + 14)]
        if len(r2) == 0:
            continue
        r2 = r2[0]
        f2 = falling_t[(falling_t > r2) & (falling_t < r2 + 9)]
        if len(f2) == 0:
            continue
        f2 = f2[0]
        logger.info(
            "Cal burst pattern found: rise=%.2f,%.2f fall=%.2f,%.2f",
            r1, r2, f1, f2)
        return float(f2), "cal_burst"

    logger.warning(
        "No cal burst pattern matched. Falling back to wall-clock alignment.")
    return None


def build_anchor(datalog: pd.DataFrame, cal_metadata: dict) -> TimeAnchor:
    """Construct a TimeAnchor mapping wall-clock to datalog time."""
    cal_start_iso = cal_metadata["start_burst"]["start_iso"]
    cal_start_wc = dt.datetime.fromisoformat(cal_start_iso)
    anchor_offset = cal_metadata.get("anchor_offset_s", 30.0)

    detected = detect_cal_burst_anchor(datalog, cal_metadata)
    if detected is not None:
        datalog_t_at_anchor, method = detected
    else:
        # Fallback: assume datalog t=0 ≈ cal burst start. The "anchor" is
        # at anchor_offset seconds in.
        datalog_t_at_anchor = anchor_offset
        method = "wallclock_fallback"

    return TimeAnchor(
        cal_start_wallclock=cal_start_wc,
        datalog_t_at_anchor=datalog_t_at_anchor,
        anchor_offset_s=anchor_offset,
        method=method,
    )


# -----------------------------------------------------------------------------
# Segment statistics
# -----------------------------------------------------------------------------

def compute_segment_stats(seg_df: pd.DataFrame) -> dict:
    """Compute V/I/P statistics over one segment's datalog slice."""
    if len(seg_df) < 5:
        return {
            "n_samples": len(seg_df),
            "radar_mean_V": np.nan, "radar_mean_I_A": np.nan,
            "radar_peak_I_A": np.nan, "radar_mean_P_W": np.nan,
            "dca_mean_V": np.nan, "dca_mean_I_A": np.nan,
            "dca_peak_I_A": np.nan, "dca_mean_P_W": np.nan,
            "total_mean_P_W": np.nan, "total_peak_I_A": np.nan,
            "total_energy_J": np.nan,
        }
    dt_s = float(np.median(np.diff(seg_df["t"].values)))
    p_total = seg_df["p1"] + seg_df["p2"]
    return {
        "n_samples": len(seg_df),
        "radar_mean_V":   float(seg_df["v1"].mean()),
        "radar_mean_I_A": float(seg_df["i1"].mean()),
        "radar_peak_I_A": float(seg_df["i1"].max()),
        "radar_mean_P_W": float(seg_df["p1"].mean()),
        "dca_mean_V":     float(seg_df["v2"].mean()),
        "dca_mean_I_A":   float(seg_df["i2"].mean()),
        "dca_peak_I_A":   float(seg_df["i2"].max()),
        "dca_mean_P_W":   float(seg_df["p2"].mean()),
        "total_mean_P_W": float(p_total.mean()),
        "total_peak_I_A": float((seg_df["i1"] + seg_df["i2"]).max()),
        "total_energy_J": float(p_total.sum() * dt_s),
    }


# -----------------------------------------------------------------------------
# Plotting
# -----------------------------------------------------------------------------

def plot_full_timeseries(
    datalog: pd.DataFrame,
    segments: list[dict],
    anchor: TimeAnchor,
    out_path: Path,
) -> None:
    """Full datalog with every segment shaded and the cal bursts marked."""
    fig, axes = plt.subplots(3, 1, figsize=(13, 8), sharex=True)
    ax_v, ax_i, ax_p = axes

    ax_v.plot(datalog["t"], datalog["v1"], lw=0.4,
              color="#1f77b4", label="Radar")
    ax_v.plot(datalog["t"], datalog["v2"], lw=0.4,
              color="#d62728", label="DCA1000")
    ax_v.set_ylabel("Voltage (V)")
    ax_v.legend(loc="upper right", fontsize=8)
    ax_v.grid(alpha=0.3)

    ax_i.plot(datalog["t"], datalog["i1"], lw=0.4, color="#1f77b4")
    ax_i.plot(datalog["t"], datalog["i2"], lw=0.4, color="#d62728")
    ax_i.set_ylabel("Current (A)")
    ax_i.grid(alpha=0.3)

    p_total = datalog["p1"] + datalog["p2"]
    ax_p.plot(datalog["t"], datalog["p1"], lw=0.4,
              color="#1f77b4", alpha=0.8)
    ax_p.plot(datalog["t"], datalog["p2"], lw=0.4,
              color="#d62728", alpha=0.8)
    ax_p.plot(datalog["t"], p_total, lw=0.5,
              color="#000000", alpha=0.7, label="Total")
    ax_p.set_ylabel("Power (W)")
    ax_p.set_xlabel("Datalog time (s)")
    ax_p.legend(loc="upper right", fontsize=8)
    ax_p.grid(alpha=0.3)

    # Shade segment windows.
    for seg in segments:
        if seg["status"] != "ok":
            continue
        t0 = anchor.to_datalog_time(seg["start_wallclock_iso"])
        t1 = anchor.to_datalog_time(seg["end_wallclock_iso"])
        for ax in axes:
            ax.axvspan(t0, t1, color="#44aa44", alpha=0.10)

    # Mark cal-burst anchor.
    for ax in axes:
        ax.axvline(anchor.datalog_t_at_anchor, color="#aa4444",
                   ls="--", lw=0.6, alpha=0.6)
    ax_v.text(anchor.datalog_t_at_anchor, ax_v.get_ylim()[1],
              " cal anchor", color="#aa4444", fontsize=7,
              va="top", ha="left")

    fig.suptitle(
        f"Full datalog  (anchor method: {anchor.method})", y=0.995)
    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()


def plot_segment(
    seg_df: pd.DataFrame,
    seg_meta: dict,
    stats: dict,
    out_path: Path,
) -> None:
    """V/I/P over one segment's slice."""
    fig, axes = plt.subplots(3, 1, figsize=(10, 6.5), sharex=True)
    ax_v, ax_i, ax_p = axes

    label = seg_meta.get("label",
                          f"seg {seg_meta.get('seg_id', '?')}")

    ax_v.plot(seg_df["t"], seg_df["v1"], lw=0.5,
              color="#1f77b4", label="Radar")
    ax_v.plot(seg_df["t"], seg_df["v2"], lw=0.5,
              color="#d62728", label="DCA1000")
    ax_v.set_ylabel("Voltage (V)")
    ax_v.set_title(label)
    ax_v.legend(loc="upper right", fontsize=8)
    ax_v.grid(alpha=0.3)

    ax_i.plot(seg_df["t"], seg_df["i1"], lw=0.5, color="#1f77b4")
    ax_i.plot(seg_df["t"], seg_df["i2"], lw=0.5, color="#d62728")
    ax_i.set_ylabel("Current (A)")
    ax_i.grid(alpha=0.3)

    ax_p.plot(seg_df["t"], seg_df["p1"], lw=0.5,
              color="#1f77b4", alpha=0.85)
    ax_p.plot(seg_df["t"], seg_df["p2"], lw=0.5,
              color="#d62728", alpha=0.85)
    ax_p.plot(seg_df["t"], seg_df["p1"] + seg_df["p2"],
              lw=0.7, color="#000000", alpha=0.7, label="Total")
    ax_p.set_ylabel("Power (W)")
    ax_p.set_xlabel("Datalog time (s)")
    ax_p.legend(loc="upper right", fontsize=8)
    ax_p.grid(alpha=0.3)

    annot = (f"radar mean = {stats['radar_mean_P_W']:.2f} W   "
             f"DCA mean = {stats['dca_mean_P_W']:.2f} W   "
             f"total = {stats['total_mean_P_W']:.2f} W   "
             f"peak I = {stats['total_peak_I_A']:.2f} A")
    fig.text(0.99, 0.01, annot, ha="right", va="bottom",
             family="monospace", fontsize=8)

    plt.tight_layout(rect=[0, 0.04, 1, 1])
    plt.savefig(out_path, dpi=110)
    plt.close()


def plot_sweep_curve(
    enriched_segments: list[dict],
    settings: dict,
    out_path: Path,
) -> None:
    """Sweep curve: primary parameter on x, mean total power on y.

    For multi-line sweeps, one line per secondary value. Error bars show
    replicate standard deviation.
    """
    fig, ax = plt.subplots(figsize=(9.5, 5.5))

    df = pd.DataFrame([s for s in enriched_segments if s["status"] == "ok"])
    if df.empty:
        ax.text(0.5, 0.5, "No successful segments to plot",
                ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        plt.savefig(out_path, dpi=120)
        plt.close()
        return

    primary_name = settings["primary_param"]
    secondary_name = settings.get("secondary_param")

    # Coerce stringified values back to numeric where possible (CSV roundtrip).
    pv_numeric = pd.to_numeric(df["primary_value"], errors="coerce")
    if not pv_numeric.isna().all():
        df["primary_value"] = pv_numeric.fillna(df["primary_value"])
    if secondary_name:
        sv_numeric = pd.to_numeric(df["secondary_value"], errors="coerce")
        if not sv_numeric.isna().all():
            df["secondary_value"] = sv_numeric.fillna(df["secondary_value"])

    if secondary_name:
        groups = df.groupby(
            ["secondary_value", "primary_value"])["total_mean_P_W"].agg(
                ["mean", "std", "count"]).reset_index()
        for sec_val, sub in groups.groupby("secondary_value"):
            sub = sub.sort_values("primary_value")
            ax.errorbar(
                sub["primary_value"], sub["mean"],
                yerr=sub["std"].fillna(0),
                marker="o", capsize=4, lw=1.5,
                label=f"{secondary_name} = {sec_val}",
            )
    else:
        groups = df.groupby("primary_value")["total_mean_P_W"].agg(
            ["mean", "std", "count"]).reset_index()
        groups = groups.sort_values("primary_value")
        ax.errorbar(
            groups["primary_value"], groups["mean"],
            yerr=groups["std"].fillna(0),
            marker="o", capsize=4, lw=1.5, color="#1f77b4",
        )

    ax.set_xlabel(primary_name)
    ax.set_ylabel("Mean total power (W)")
    ax.set_title(
        f"Power vs {primary_name}"
        + (f" at multiple {secondary_name} values" if secondary_name else "")
        + f"  (error bars: std across {settings['replicates']} replicates)"
    )
    ax.grid(alpha=0.3)
    if secondary_name:
        ax.legend(title=secondary_name)
    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()


# -----------------------------------------------------------------------------
# Loaders for the study-folder artifacts
# -----------------------------------------------------------------------------

def load_segments_csv(path: Path) -> list[dict]:
    out = []
    with path.open() as f:
        for row in csv.DictReader(f):
            # Convert numeric-looking columns
            for k in ("seg_id", "replicate", "frames_captured"):
                row[k] = int(row[k]) if row[k] else 0
            for k in ("primary_value", "secondary_value"):
                if row[k] == "":
                    row[k] = None
                else:
                    try:
                        # Keep as int if possible, otherwise float
                        f_val = float(row[k])
                        row[k] = int(f_val) if f_val.is_integer() else f_val
                    except ValueError:
                        pass     # leave as string
            if not row["secondary_param"]:
                row["secondary_param"] = None
            row["label"] = (
                f"{row['primary_param']}={row['primary_value']}"
                + (f" {row['secondary_param']}={row['secondary_value']}"
                   if row["secondary_param"] else "")
                + f" rep={row['replicate']}"
            )
            out.append(row)
    return out


# -----------------------------------------------------------------------------
# Main analysis pipeline
# -----------------------------------------------------------------------------

def analyze_study(study_path: Path) -> Path:
    """Analyze one study folder. Returns the results folder path."""
    study_path = study_path.resolve()
    results_dir = study_path / "results"
    results_dir.mkdir(exist_ok=True)
    (results_dir / "segments").mkdir(exist_ok=True)

    # -- Load study artifacts --
    with (study_path / "manifest.json").open() as f:
        manifest = json.load(f)
    with (study_path / "calibration.json").open() as f:
        cal_metadata = json.load(f)
    segments = load_segments_csv(study_path / "segments.csv")

    datalog_path = study_path / "raw" / "datalog.csv"
    if not datalog_path.exists():
        raise FileNotFoundError(
            f"No datalog at {datalog_path}. Export it from the N6705B "
            f"and place it there before analyzing.")
    logger.info("Loading datalog: %s", datalog_path)
    datalog = load_n6705b_csv(datalog_path)
    logger.info("Datalog: %d samples over %.1f s",
                len(datalog), datalog["t"].iloc[-1])

    # -- Anchor time --
    anchor = build_anchor(datalog, cal_metadata)
    logger.info("Time anchor: method=%s, datalog_t_at_anchor=%.2fs",
                anchor.method, anchor.datalog_t_at_anchor)

    # -- Per-segment analysis --
    enriched: list[dict] = []
    for seg in segments:
        if seg["status"] != "ok":
            enriched.append({**seg, **compute_segment_stats(
                pd.DataFrame(columns=["t", "v1", "i1", "v2", "i2",
                                       "p1", "p2"]))})
            continue
        t0 = anchor.to_datalog_time(seg["start_wallclock_iso"])
        t1 = anchor.to_datalog_time(seg["end_wallclock_iso"])
        m = (datalog["t"] >= t0) & (datalog["t"] <= t1)
        seg_df = datalog[m].reset_index(drop=True)

        stats = compute_segment_stats(seg_df)
        enriched.append({**seg, **stats,
                          "datalog_t_start": t0, "datalog_t_end": t1})

        seg_label = (f"seg_{seg['seg_id']:03d}_"
                     f"{seg['primary_param']}_{seg['primary_value']}"
                     + (f"_{seg['secondary_param']}_{seg['secondary_value']}"
                        if seg['secondary_param'] else "")
                     + f"_rep{seg['replicate']}")
        plot_segment(seg_df, seg, stats,
                     results_dir / "segments" / f"{seg_label}.png")

    # -- Full timeseries plot --
    plot_full_timeseries(datalog, enriched, anchor,
                         results_dir / "timeseries_full.png")

    # -- Sweep curve --
    plot_sweep_curve(enriched, manifest["settings"],
                     results_dir / "sweep_curve.png")

    # -- Summary CSV --
    summary_path = results_dir / "summary_table.csv"
    write_summary_csv(enriched, summary_path)

    # -- README --
    write_results_readme(manifest, anchor, enriched,
                          results_dir / "README.md")

    logger.info("Analysis complete. Results in: %s", results_dir)
    return results_dir


def write_summary_csv(enriched: list[dict], path: Path) -> None:
    """One row per segment with all stats."""
    cols = [
        "seg_id", "replicate", "primary_param", "primary_value",
        "secondary_param", "secondary_value", "status",
        "frames_captured", "n_samples",
        "datalog_t_start", "datalog_t_end",
        "radar_mean_V", "radar_mean_I_A", "radar_peak_I_A",
        "radar_mean_P_W",
        "dca_mean_V", "dca_mean_I_A", "dca_peak_I_A", "dca_mean_P_W",
        "total_mean_P_W", "total_peak_I_A", "total_energy_J",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in enriched:
            w.writerow({c: row.get(c, "") for c in cols})


def _dataframe_to_markdown(df, float_fmt: str = ".3f") -> list[str]:
    """Render a pandas DataFrame as GitHub-flavored markdown table lines.

    Reimplements the small subset of `df.to_markdown()` we need so we don't
    have to depend on the optional `tabulate` package, which has been
    fragile across pandas/Python versions.
    """
    import math

    def cell(val) -> str:
        if val is None:
            return ""
        if isinstance(val, float):
            if math.isnan(val):
                return ""
            return f"{val:{float_fmt}}"
        return str(val)

    headers = [str(c) for c in df.columns]
    rows = [[cell(v) for v in row] for row in df.itertuples(index=False)]

    # Column widths for alignment (purely cosmetic; markdown renderers
    # don't care, but it makes the raw .md readable).
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            if len(c) > widths[i]:
                widths[i] = len(c)

    def fmt_row(cells):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    lines = [fmt_row(headers),
             "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for r in rows:
        lines.append(fmt_row(r))
    return lines


def write_results_readme(
    manifest: dict,
    anchor: TimeAnchor,
    enriched: list[dict],
    path: Path,
) -> None:
    """Auto-generate a small markdown report for this study's results."""
    settings = manifest["settings"]
    ok = [e for e in enriched if e["status"] == "ok"]
    failed = [e for e in enriched if e["status"] != "ok"]

    # Per-primary-value aggregates for the table
    primary = settings["primary_param"]
    secondary = settings.get("secondary_param")
    df = pd.DataFrame(ok)

    if not df.empty:
        # Coerce stringified values back to numeric where possible.
        pv_numeric = pd.to_numeric(df["primary_value"], errors="coerce")
        if not pv_numeric.isna().all():
            df["primary_value"] = pv_numeric.fillna(df["primary_value"])
        if secondary:
            sv_numeric = pd.to_numeric(df["secondary_value"], errors="coerce")
            if not sv_numeric.isna().all():
                df["secondary_value"] = sv_numeric.fillna(df["secondary_value"])

            agg = df.groupby(
                ["secondary_value", "primary_value"]
            )["total_mean_P_W"].agg(["mean", "std", "count"]).reset_index()
            agg = agg.rename(columns={
                "primary_value": primary,
                "secondary_value": secondary,
            })
        else:
            agg = df.groupby("primary_value")["total_mean_P_W"].agg(
                ["mean", "std", "count"]).reset_index()
            agg = agg.rename(columns={"primary_value": primary})
    else:
        agg = pd.DataFrame()

    lines: list[str] = []
    lines.append(f"# Study: {manifest['study_name']}")
    lines.append("")
    lines.append(f"- Device: `{manifest['device']}`")
    lines.append(f"- xwr version: `{manifest['xwr_version']}`")
    lines.append(f"- Run started: {manifest['run_started_iso']}")
    lines.append(f"- Run ended:   {manifest['run_ended_iso']}")
    lines.append(f"- Segments: {len(ok)} ok, {len(failed)} failed")
    lines.append(f"- Time anchor method: **{anchor.method}**")
    if manifest.get("dry_run"):
        lines.append("- **DRY RUN** — no real hardware data")
    lines.append("")
    lines.append("## Sweep configuration")
    lines.append("")
    lines.append(f"- Primary parameter: `{primary}` "
                 f"with values {settings['primary_values']}")
    if secondary:
        lines.append(f"- Secondary parameter: `{secondary}` "
                     f"held at {settings['secondary_values']}")
    lines.append(f"- Replicates: {settings['replicates']}")
    lines.append(f"- Segment duration: {settings['segment_duration']} s")
    lines.append(f"- Idle between: {settings['idle_between']} s")
    lines.append(f"- Order: {settings['order']} "
                 f"(seed={manifest.get('seed', '?')})")
    lines.append("")
    lines.append("## Aggregate results")
    lines.append("")
    if not agg.empty:
        lines.extend(_dataframe_to_markdown(agg, float_fmt=".3f"))
    else:
        lines.append("_No successful segments to aggregate._")
    lines.append("")
    lines.append("## Plots")
    lines.append("")
    lines.append("- [Full datalog](timeseries_full.png)")
    lines.append("- [Sweep curve](sweep_curve.png)")
    lines.append("- Per-segment plots in [`segments/`](segments/)")
    lines.append("")
    lines.append("## Data")
    lines.append("")
    lines.append("- `summary_table.csv` — every segment's stats")
    lines.append("- `../segments.csv` — wall-clock segment log from sweep.py")
    lines.append("- `../manifest.json` — provenance metadata")
    lines.append("- `../calibration.json` — cal burst phase timestamps")
    lines.append("")

    path.write_text("\n".join(lines))


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a power-study run.")
    parser.add_argument("study_dir", type=Path,
                        help="Path to the study folder")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    )

    if not args.study_dir.is_dir():
        logger.error("Study folder not found: %s", args.study_dir)
        return 1
    try:
        analyze_study(args.study_dir)
    except Exception as e:
        logger.exception("Analysis failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())