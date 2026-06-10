"""
verify_radar_frequency.py — Verify the radar is operating at the configured
frame rate, via three views:

  1. A short time-domain zoom (count pulses by eye)
  2. A full-band FFT (sees the whole spectrum)
  3. A zoomed FFT around the expected frequency (peak is unmissable)

Usage:
    python FFT_zoom.py <study_dir> <seg_id>
    $ uv run python <FFT_zoom.py path> <studies_dir> <seg_ID> 

"""

import datetime as dt
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from pa_xwr.analyze import build_anchor, load_n6705b_csv

ZOOM_WINDOW_S = 0.1      # short time-window for the visual zoom
STARTUP_SKIP_S = 4.0     # the xwr firmware appears to take ~3 s to fully start
END_SKIP_S = 1.0


def main(study_dir: Path, seg_id: int, datalog_path: Path | None = None) -> None:
    if datalog_path is None:
        datalog_path = study_dir / "raw" / "datalog.csv"
    datalog = load_n6705b_csv(datalog_path)
    manifest = json.loads((study_dir / "manifest.json").read_text())
    calibration = json.loads((study_dir / "calibration.json").read_text())
    segs = pd.read_csv(study_dir / "segments.csv")
    anchor = build_anchor(datalog, calibration)

    matching = segs[segs["seg_id"] == seg_id]
    if matching.empty:
        sys.exit(f"No segment with seg_id={seg_id}")
    seg = matching.iloc[0]
    if seg["primary_param"] == "_calibration":
        sys.exit(f"Segment {seg_id} is a calibration pulse, not real data.")

    t_start = anchor.to_datalog_time(seg["start_wallclock_iso"]) + STARTUP_SKIP_S
    t_end   = anchor.to_datalog_time(seg["end_wallclock_iso"]) - END_SKIP_S
    if t_end - t_start < 1.0:
        sys.exit(f"Operating window too short after startup skip.")

    op = datalog[(datalog["t"] >= t_start) & (datalog["t"] <= t_end)].copy()
    t = op["t"].values
    i1 = op["i1"].values

    fs = 1.0 / float(np.median(np.diff(t)))
    nyquist = fs / 2
    print(f"Datalog sample rate: {fs:.1f} Hz  (Nyquist: {nyquist:.1f} Hz)")

    expected_fps = None
    if seg["primary_param"] == "frame_period":
        expected_fps = 1000.0 / float(seg["primary_value"])
        print(f"Expected frame rate: {expected_fps:.2f} Hz  "
              f"(from frame_period={seg['primary_value']} ms)")

    if expected_fps and expected_fps > nyquist:
        print(f"WARNING: expected {expected_fps:.1f} Hz > Nyquist "
              f"{nyquist:.1f} Hz. The peak will alias; re-record the "
              f"datalog at a faster sample period to see this rate.")

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

    peak_idx = int(np.argmax(magnitude[1:])) + 1
    peak_freq = freqs[peak_idx]

    fig = plt.figure(figsize=(13, 10))
    gs = fig.add_gridspec(3, 2)
    ax_full = fig.add_subplot(gs[0, :])
    ax_zoom = fig.add_subplot(gs[1, :])
    ax_fft  = fig.add_subplot(gs[2, 0])
    ax_fftz = fig.add_subplot(gs[2, 1])

    ax_full.plot(t - t[0], i1, lw=0.3, color="#1f77b4")
    ax_full.set_xlabel("Time (s)")
    ax_full.set_ylabel("Radar current (A)")
    title = f"Seg {seg_id}: {seg['primary_param']}={seg['primary_value']}"
    if expected_fps:
        title += f"  (expected {expected_fps:.1f} pulses/s)"
    ax_full.set_title(title)
    ax_full.grid(alpha=0.3)

    mid_t = (t[0] + t[-1]) / 2
    zoom_start, zoom_end = mid_t - ZOOM_WINDOW_S / 2, mid_t + ZOOM_WINDOW_S / 2
    zoom = datalog[(datalog["t"] >= zoom_start) & (datalog["t"] <= zoom_end)]
    ax_zoom.plot((zoom["t"] - zoom_start) * 1000, zoom["i1"],
                 marker=".", ms=2, lw=0.6, color="#1f77b4")
    ax_zoom.set_xlabel(f"Time within window (ms) — {ZOOM_WINDOW_S*1000:.0f} ms total")
    ax_zoom.set_ylabel("Radar current (A)")
    if expected_fps:
        expected_pulses = int(round(ZOOM_WINDOW_S * expected_fps))
        ax_zoom.set_title(
            f"Close-up: expect {expected_pulses} pulses in this window")
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

    in_zoom = (freqs >= fft_zoom_min) & (freqs <= fft_zoom_max)
    ax_fftz.plot(freqs[in_zoom], magnitude[in_zoom],
                 lw=0.8, color="#d62728")
    ax_fftz.set_xlim(fft_zoom_min, fft_zoom_max)
    ax_fftz.set_xlabel("Frequency (Hz)")
    ax_fftz.set_ylabel("FFT magnitude (linear)")
    ax_fftz.set_title(f"FFT zoom: {fft_zoom_min:.1f}–{fft_zoom_max:.1f} Hz")
    ax_fftz.grid(alpha=0.3)

    plt.tight_layout()
    out_path = study_dir / "results" / f"verify_seg{seg_id}.png"
    out_path.parent.mkdir(exist_ok=True)
    plt.savefig(out_path, dpi=140)
    plt.close()
    print(f"Wrote {out_path}")
    print()
    print(f"Dominant frequency (full band): {peak_freq:.2f} Hz")
    if expected_fps:
        err = abs(peak_freq - expected_fps) / expected_fps * 100
        print(f"Expected:                       {expected_fps:.2f} Hz  "
              f"({err:.1f}% error)")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(
        description="Verify radar frame rate via FFT of a study's datalog")
    p.add_argument("study_dir", type=Path,
                   help="Path to the study folder")
    p.add_argument("seg_id", type=int,
                   help="Segment ID to analyze (from segments.csv)")
    p.add_argument("--datalog", type=Path, default=None,
                   help="Override datalog path "
                        "(default: <study_dir>/raw/datalog.csv)")
    args = p.parse_args()
    main(args.study_dir, args.seg_id, args.datalog)