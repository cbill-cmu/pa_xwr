"""
verify_radar_frequency.py — Verify the radar is operating at the configured
frame rate, via three views:

  1. A short time-domain zoom (count pulses by eye)
  2. A full-band FFT (sees the whole spectrum)
  3. A zoomed FFT around the expected frequency (peak is unmissable)

Usage:
    # One segment:
    python verify_radar_frequency.py <study_dir> 5

    # Every non-cal segment in the study:
    python verify_radar_frequency.py <study_dir> --all

    # Custom datalog path (defaults to <study_dir>/raw/datalog.csv):
    python verify_radar_frequency.py <study_dir> 5 --datalog path/to/log.csv
    python verify_radar_frequency.py <study_dir> --all --datalog path/to/log.csv

Outputs go to <study_dir>/verify/seg_NNN.png .
"""

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from pa_xwr.analyze import build_anchor, load_n6705b_csv

ZOOM_WINDOW_S = 0.1      # short time-window for the visual zoom
STARTUP_SKIP_S = 4.0     # xwr firmware takes ~3 s to fully start chirping
END_SKIP_S = 1.0


def _find_top_peaks(freqs, magnitude, n=5, min_rel_height=0.1, min_separation_hz=1.0):
    """Return the frequencies of the top N local maxima.

    A local maximum is a bin whose value exceeds both neighbours.
    Filters:
      - Magnitude must be at least `min_rel_height` * global_max
      - Peaks closer than `min_separation_hz` are de-duplicated (keep the
        tallest)
    """
    if len(magnitude) < 3:
        return []
    threshold = magnitude.max() * min_rel_height

    # Local-maximum indices (skip the DC bin at 0).
    local_max = []
    for i in range(1, len(magnitude) - 1):
        if magnitude[i] < threshold:
            continue
        if magnitude[i] > magnitude[i - 1] and magnitude[i] > magnitude[i + 1]:
            local_max.append(i)

    # Sort by descending magnitude.
    local_max.sort(key=lambda i: -magnitude[i])

    # Greedy de-dup by frequency separation.
    selected_freqs = []
    for i in local_max:
        f = freqs[i]
        if any(abs(f - g) < min_separation_hz for g in selected_freqs):
            continue
        selected_freqs.append(f)
        if len(selected_freqs) >= n:
            break

    selected_freqs.sort()
    return selected_freqs


def verify_one_segment(datalog, anchor, seg_row, out_path):
    """Render the 4-panel verify figure for one segment.

    Returns a dict summarizing the verification:
        seg_id, primary_value, expected_fps, peak_freq, error_pct, status
    where status is 'ok', 'warn', 'fail', or 'aliased'.
    """
    seg_id = int(seg_row["seg_id"])
    summary = {
        "seg_id": seg_id,
        "primary_param": seg_row["primary_param"],
        "primary_value": seg_row["primary_value"],
        "expected_fps": None,
        "peak_freq": None,
        "error_pct": None,
        "status": "skipped",
        "note": "",
    }

    if seg_row["primary_param"] == "_calibration":
        summary["note"] = "calibration pulse, not a real segment"
        return summary

    t_start = anchor.to_datalog_time(seg_row["start_wallclock_iso"]) + STARTUP_SKIP_S
    t_end   = anchor.to_datalog_time(seg_row["end_wallclock_iso"]) - END_SKIP_S
    if t_end - t_start < 1.0:
        summary["status"] = "fail"
        summary["note"] = "operating window too short after skip"
        return summary

    op = datalog[(datalog["t"] >= t_start) & (datalog["t"] <= t_end)].copy()
    t = op["t"].values
    i1 = op["i1"].values

    if len(t) < 64:
        summary["status"] = "fail"
        summary["note"] = "not enough samples"
        return summary

    fs = 1.0 / float(np.median(np.diff(t)))
    nyquist = fs / 2

    expected_fps = None
    if seg_row["primary_param"] == "frame_period":
        try:
            expected_fps = 1000.0 / float(seg_row["primary_value"])
        except (TypeError, ValueError):
            expected_fps = None
    summary["expected_fps"] = expected_fps

    aliased = expected_fps is not None and expected_fps > nyquist

    if expected_fps:
        fft_xmax = min(nyquist, max(50.0, expected_fps * 2.0))
        zoom_half_width = max(5.0, expected_fps * 0.3)
        fft_zoom_min = max(0.0, expected_fps - zoom_half_width)
        fft_zoom_max = min(nyquist, expected_fps + zoom_half_width)
    else:
        fft_xmax = min(nyquist, 200.0)
        fft_zoom_min, fft_zoom_max = 0.0, fft_xmax

    i1_centered = i1 - i1.mean()
    window = np.hanning(len(i1_centered))
    spectrum = np.fft.rfft(i1_centered * window)
    freqs = np.fft.rfftfreq(len(i1_centered), d=1.0 / fs)
    magnitude = np.abs(spectrum)

    # Find the "fundamental" peak we care about. Two strategies:
    #
    #   1. If we know the expected frequency, look near it specifically.
    #      The radar's current trace is a rectangular pulse train, so the
    #      FFT has a HARMONIC SERIES: peaks at 1x, 2x, 3x, ... the
    #      fundamental. For some duty cycles the 2nd or 3rd harmonic can
    #      be TALLER than the fundamental — so "highest peak in the band"
    #      is the wrong thing to report. We want the peak near the
    #      expected frequency.
    #
    #   2. If we don't know the expected frequency, fall back to the
    #      tallest peak in the full band.
    if expected_fps and expected_fps <= nyquist:
        # Search window: +/- 30% around the expected frequency.
        search_lo = expected_fps * 0.7
        search_hi = expected_fps * 1.3
        in_window = (freqs >= search_lo) & (freqs <= search_hi)
        if in_window.any():
            window_idx = int(np.argmax(magnitude[in_window]))
            peak_freq = float(freqs[in_window][window_idx])
        else:
            # Shouldn't happen, but fall back gracefully.
            peak_idx = int(np.argmax(magnitude[1:])) + 1
            peak_freq = float(freqs[peak_idx])
    else:
        peak_idx = int(np.argmax(magnitude[1:])) + 1
        peak_freq = float(freqs[peak_idx])

    # Also locate the largest harmonics for diagnostic reporting.
    # Walk the magnitude array and pick the top N local maxima above 10%
    # of the global max.
    top_peaks = _find_top_peaks(freqs, magnitude, n=5, min_rel_height=0.1)
    summary["top_peaks_hz"] = [round(float(f), 2) for f in top_peaks]
    summary["peak_freq"] = peak_freq

    if expected_fps:
        err = abs(peak_freq - expected_fps) / expected_fps * 100
        summary["error_pct"] = err
        if aliased:
            summary["status"] = "aliased"
        elif err < 5.0:
            summary["status"] = "ok"
        elif err < 15.0:
            summary["status"] = "warn"
        else:
            summary["status"] = "fail"
    else:
        summary["status"] = "ok"

    # -------- plot --------
    fig = plt.figure(figsize=(13, 10))
    gs = fig.add_gridspec(3, 2)
    ax_full = fig.add_subplot(gs[0, :])
    ax_zoom = fig.add_subplot(gs[1, :])
    ax_fft  = fig.add_subplot(gs[2, 0])
    ax_fftz = fig.add_subplot(gs[2, 1])

    ax_full.plot(t - t[0], i1, lw=0.3, color="#1f77b4")
    ax_full.set_xlabel("Time (s)")
    ax_full.set_ylabel("Radar current (A)")
    title = f"Seg {seg_id}: {seg_row['primary_param']}={seg_row['primary_value']}"
    sec_p = seg_row.get("secondary_param")
    sec_v = seg_row.get("secondary_value")
    if (sec_p is not None and not pd.isna(sec_p) and str(sec_p) != ""
            and sec_v is not None and not pd.isna(sec_v)):
        title += f"  {sec_p}={sec_v}"
    if expected_fps:
        title += f"  (expected {expected_fps:.1f} pulses/s)"
    ax_full.set_title(title)
    ax_full.grid(alpha=0.3)

    mid_t = (t[0] + t[-1]) / 2
    zoom_start = mid_t - ZOOM_WINDOW_S / 2
    zoom_end = mid_t + ZOOM_WINDOW_S / 2
    zoom = datalog[(datalog["t"] >= zoom_start) & (datalog["t"] <= zoom_end)]
    ax_zoom.plot((zoom["t"] - zoom_start) * 1000, zoom["i1"],
                 marker=".", ms=2, lw=0.6, color="#1f77b4")
    ax_zoom.set_xlabel(f"Time within window (ms) — {ZOOM_WINDOW_S*1000:.0f} ms total")
    ax_zoom.set_ylabel("Radar current (A)")
    if expected_fps:
        expected_pulses = int(round(ZOOM_WINDOW_S * expected_fps))
        ax_zoom.set_title(f"Close-up: expect {expected_pulses} pulses")
    else:
        ax_zoom.set_title("Close-up")
    ax_zoom.grid(alpha=0.3)

    ax_fft.plot(freqs, magnitude, lw=0.6, color="#d62728")
    ax_fft.set_xlim(0, fft_xmax)
    ax_fft.set_yscale("log")
    ax_fft.set_xlabel("Frequency (Hz)")
    ax_fft.set_ylabel("FFT magnitude")
    ax_fft.set_title(f"FFT (0–{fft_xmax:.0f} Hz)")
    ax_fft.grid(alpha=0.3, which="both")
    if expected_fps:
        ax_fft.axvline(expected_fps, color="green", ls="--", lw=1.5,
                        label=f"Expected: {expected_fps:.1f} Hz")
        ax_fft.axvline(peak_freq, color="purple", ls=":", lw=1.5,
                        label=f"Measured peak: {peak_freq:.2f} Hz")
        ax_fft.legend(loc="upper right", fontsize=8)
        # Mark expected harmonics so the user can recognize the series.
        # Light vertical bars at 2x, 3x, 4x ... below Nyquist.
        for k in range(2, 8):
            h = expected_fps * k
            if h > fft_xmax:
                break
            ax_fft.axvline(h, color="#88bb88", ls=":", lw=0.7, alpha=0.6)
        ax_fft.legend(loc="upper right", fontsize=8)

    in_zoom = (freqs >= fft_zoom_min) & (freqs <= fft_zoom_max)
    ax_fftz.plot(freqs[in_zoom], magnitude[in_zoom],
                 lw=0.8, color="#d62728")
    ax_fftz.set_xlim(fft_zoom_min, fft_zoom_max)
    ax_fftz.set_xlabel("Frequency (Hz)")
    ax_fftz.set_ylabel("FFT magnitude (linear)")
    ax_fftz.set_title(f"FFT zoom: {fft_zoom_min:.1f}–{fft_zoom_max:.1f} Hz")
    ax_fftz.grid(alpha=0.3)
    plt.tight_layout()
    out_path.parent.mkdir(exist_ok=True, parents=True)
    plt.savefig(out_path, dpi=140)
    plt.close()
    return summary


_STATUS_GLYPH = {
    "ok":      "OK ",
    "warn":    "WRN",
    "fail":    "BAD",
    "aliased": "ALI",
    "skipped": "-- ",
}


def print_summary_table(rows):
    """Print a compact ASCII summary of every verified segment."""
    if not rows:
        print("(no segments processed)")
        return

    print()
    print("=" * 86)
    print(f"{'seg':>4} {'stat':>4}  "
          f"{'primary':<25} {'expected':>10} {'measured':>10} {'err%':>7}  "
          f"{'top peaks (Hz)':<20}")
    print("-" * 86)
    for r in rows:
        prim = f"{r['primary_param']}={r['primary_value']}"
        exp  = (f"{r['expected_fps']:>8.2f} Hz" if r["expected_fps"]
                else f"{'-':>10}")
        meas = (f"{r['peak_freq']:>8.2f} Hz" if r["peak_freq"]
                else f"{'-':>10}")
        err  = (f"{r['error_pct']:>6.1f}%" if r["error_pct"] is not None
                else f"{'-':>7}")
        glyph = _STATUS_GLYPH.get(r["status"], "?")
        top = r.get("top_peaks_hz") or []
        top_str = ", ".join(f"{f:.1f}" for f in top[:4]) if top else "-"
        line = (f"{r['seg_id']:>4} {glyph:>4}  {prim:<25} {exp} {meas} {err}"
                f"  {top_str:<20}")
        if r["note"]:
            line += f"  ({r['note']})"
        print(line)
    print("=" * 86)

    # Aggregate counts
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    bits = [f"{k}={v}" for k, v in counts.items()]
    print(f"Totals: {', '.join(bits)}")
    print("Legend:")
    print("  OK  = peak within 5% of expected")
    print("  WRN = peak within 5–15% of expected")
    print("  BAD = peak more than 15% off (or other failure)")
    print("  ALI = expected fps above Nyquist — peak alias-ed, not a real reading")
    print("  --  = skipped (e.g. cal pulse)")


def main(study_dir: Path,
         seg_id: int | None,
         all_segments: bool,
         datalog_path: Path | None) -> int:
    # Load study artifacts.
    if datalog_path is None:
        datalog_path = study_dir / "raw" / "datalog.csv"
    # if not datalog_path.exists():
    #     print(f"No datalog at {datalog_path}", file=sys.stderr)
    #     return 1

    print(f"Loading datalog: {study_dir / datalog_path}")
    datalog = load_n6705b_csv(study_dir / datalog_path)
    calibration = json.loads((study_dir / "calibration.json").read_text())
    segs = pd.read_csv(study_dir / "segments.csv")
    anchor = build_anchor(datalog, calibration)

    fs = 1.0 / float(np.median(np.diff(datalog["t"].values)))
    print(f"Datalog sample rate: {fs:.1f} Hz  (Nyquist: {fs/2:.1f} Hz)")
    print()

    verify_dir = study_dir / "verify"
    verify_dir.mkdir(exist_ok=True)

    # Select target segments.
    if all_segments:
        targets = segs[segs["primary_param"] != "_calibration"]
        print(f"Verifying ALL {len(targets)} non-calibration segments → "
              f"{verify_dir}")
    else:
        targets = segs[segs["seg_id"] == seg_id]
        if targets.empty:
            print(f"No segment with seg_id={seg_id} in segments.csv",
                  file=sys.stderr)
            return 1

    # Process each target.
    summaries = []
    for _, seg in targets.iterrows():
        sid = int(seg["seg_id"])
        out_path = verify_dir / f"seg_{sid:03d}.png"
        try:
            s = verify_one_segment(datalog, anchor, seg, out_path)
        except Exception as e:                              # noqa: BLE001
            s = {"seg_id": sid, "primary_param": seg["primary_param"],
                 "primary_value": seg["primary_value"],
                 "expected_fps": None, "peak_freq": None, "error_pct": None,
                 "status": "fail", "note": f"exception: {e}"}
        summaries.append(s)
        if all_segments:
            # Brief one-line progress for each segment.
            glyph = _STATUS_GLYPH.get(s["status"], "?")
            note = f"  ({s['note']})" if s["note"] else ""
            err = f" {s['error_pct']:.1f}%" if s["error_pct"] is not None else ""
            print(f"  seg {sid:>3d} [{glyph}]"
                  f" {s['primary_param']}={s['primary_value']}{err}{note}")

    print_summary_table(summaries)

    if all_segments:
        # Also dump a CSV next to the PNGs for downstream use.
        out_csv = verify_dir / "verify_summary.csv"
        pd.DataFrame(summaries).to_csv(out_csv, index=False)
        print(f"\nSummary CSV: {out_csv}")
    return 0


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Verify the radar is operating at the configured frame rate.")
    p.add_argument("study_dir", type=Path,
                   help="Path to the study folder")
    p.add_argument("seg_id", type=int, nargs="?", default=None,
                   help="Segment ID to verify (omit when using --all)")
    p.add_argument("--all", dest="all_segments", action="store_true",
                   help="Verify every non-calibration segment "
                        "in the study (writes one PNG per segment to "
                        "<study_dir>/verify/ plus a summary CSV)")
    p.add_argument("--datalog", type=Path, default=None,
                   help="Path to the N6705B-exported CSV. "
                        "Defaults to <study_dir>/raw/datalog.csv.")
    args = p.parse_args()

    if args.all_segments and args.seg_id is not None:
        p.error("Pass either a seg_id or --all, not both.")
    if not args.all_segments and args.seg_id is None:
        p.error("Specify a seg_id or use --all.")
    if not args.study_dir.is_dir():
        p.error(f"Study folder not found: {args.study_dir}")

    return main(args.study_dir, args.seg_id, args.all_segments, args.datalog)


if __name__ == "__main__":
    raise SystemExit(_cli())