# Design Document — Automated Parameter-Sweep Power Study (v2)

**Status:** Draft for approval. Code will not be written until this is signed off.

## 1. Goals

1. **One-button sweeps.** Run a single Linux command that sweeps any chirp
   parameter (or pair of parameters) across user-specified values, with N
   replicates, while a single long N6705B datalog captures everything.
2. **Device-agnostic.** Same sweep script works on AWR1843, AWR1443, AWR2243,
   IWR6843, and other TI radars supported by xwr — by selecting a per-device
   template YAML.
3. **2-D cross-sweeps.** Vary two parameters together (e.g. `frame_period` ×
   `frame_length`) to produce a heatmap of how they jointly affect power.
4. **Reproducible studies.** Each experiment produces a single self-contained
   folder with provenance: every config used, all raw data, every plot, a
   manifest linking them, and a generated README.

## 2. Workflow (what the user actually does)

The bench setup is identical to v1. What changes is what happens after
"everything's powered and ready":

```
┌────────────────────────────────────────────────────────────────────┐
│  1.  User boots Linux PC, opens terminal, navigates to xwr repo.   │
│                                                                    │
│  2.  User runs:                                                    │
│        uv run sweep.py --sweep <spec.yaml>                         │
│                                                                    │
│      The script prints:                                            │
│        "Will run 45 segments, total ~32 minutes (incl. 35s         │
│         calibration burst). Configure N6705B datalog to            │
│         2000s @ 1ms sample period. Press Run on N6705B,            │
│         then press Enter here within 5 seconds."                   │
│                                                                    │
│  3.  User presses Run on N6705B. Press Enter on terminal.          │
│                                                                    │
│  4.  Script runs the calibration burst (35 s) then loops through   │
│      every (param_a, param_b, replicate) point. Each point:        │
│                                                                    │
│         ── idle 5s ── chirp 30s ── idle 5s ── reconfigure ──       │
│                                                                    │
│      Each segment's start/stop wall clock time is logged to        │
│      segments.csv.                                                 │
│                                                                    │
│  5.  Script ends. Prints:                                          │
│        "Sweep done. Wait ~5 s for datalog buffer to flush,         │
│         then stop the datalog and export CSV to                    │
│         <study_path>/raw/datalog.csv"                              │
│                                                                    │
│  6.  User stops datalog, exports CSV to USB stick, copies it       │
│      into the study folder.                                        │
│                                                                    │
│  7.  User runs:                                                    │
│        uv run analyze.py <study_path>                              │
│                                                                    │
│      Script anchors time using the calibration burst, segments     │
│      the long datalog by segments.csv timestamps, computes         │
│      stats per segment, generates plots, writes summary table.     │
└────────────────────────────────────────────────────────────────────┘
```

User actions in total: edit a sweep spec file → press Run on N6705B → press
Enter → wait → export CSV → run analyzer. No N6705B button presses during the
sweep itself.

## 3. Time Synchronization (the new hard problem)

The user starts the datalog and the script as close together as possible, but
there's a 0.5–5 second offset. We need to know where in the datalog each
segment lives. Approach:

**Calibration burst at the start of every sweep.** The script does:

| Phase | Duration | Radar state |
|---|---|---|
| Wait 1 | 10 s | Idle (no chirps) |
| Cal pulse 1 | 5 s | Chirping (baseline config) |
| Wait 2 | 10 s | Idle |
| Cal pulse 2 | 5 s | Chirping (baseline config) |
| Wait 3 | 5 s | Idle |
| **First real sweep segment begins** | | |

This produces a known double-pulse pattern in the current trace. The analyzer
detects the **falling edge of the second cal pulse** (which is always exactly
40 s after the script started its clock) and uses that to align the datalog's
time axis to the script's segment timestamps.

The double pulse is harder to confuse with random startup transients than a
single one. Total cal cost: 35 s out of typically 30+ minutes — under 2%.

**If the calibration detection fails** (signal is unclear, e.g. capture didn't
work at all), the analyzer falls back to wall-clock timestamps and warns the
user that segment boundaries may be off by a few seconds.

## 4. File and Folder Structure

```
power_study/                          (the repository / working directory)
├── DESIGN.md                         this document
├── README.md                         user-facing intro + quickstart
├── PROTOCOL.md                       updated lab procedure document
│
├── devices/                          per-radar template YAMLs (you maintain)
│   ├── AWR1843.yaml
│   ├── AWR1443.yaml
│   ├── IWR6843.yaml
│   └── ...                           add more as needed
│
├── sweeps/                           sweep specification files (you write)
│   ├── frame_period_only.yaml
│   ├── frame_period_x_chirps.yaml    2-D sweep example
│   └── ...
│
├── scripts/
│   ├── sweep.py                      the orchestrator (one entry point)
│   ├── capture.py                    single-segment radar driver (internal)
│   └── analyze.py                    offline analysis + plots
│
└── studies/                          one folder per actual experiment run
    └── 2026-06-15_AWR1843_frame_period_sweep/
        ├── manifest.json             provenance metadata
        ├── sweep_used.yaml           copy of the sweep spec
        ├── device_used.yaml          copy of the device template
        ├── segments.csv              wall-clock log of every segment
        ├── capture.log               full stdout/stderr from sweep.py
        ├── raw/
        │   └── datalog.csv           N6705B export goes here
        └── results/                  generated by analyze.py
            ├── README.md             auto-generated study summary
            ├── summary_table.csv     per-segment stats
            ├── timeseries_full.png   full datalog with segment shading
            ├── sweep_curve.png       1-D plot: param vs power
            ├── heatmap_2D.png        2-D plot (only for 2-D sweeps)
            └── segments/             per-segment time-series plots
                ├── seg_001.png
                └── ...
```

**Key change from v1:** results don't accumulate in shared folders. Each study
is a self-contained folder you can ship to a collaborator or archive.

## 5. Component Designs

### 5.1 Device template format (`devices/AWR1843.yaml`)

A device template is just a complete xwr config YAML, defining sane defaults
for that radar. It defines every parameter xwr needs except those that will
be overridden by the sweep.

```yaml
# devices/AWR1843.yaml
radar:
  device: AWR1843
  port: null                # auto-detect
  frequency: 77.0           # GHz
  idle_time: 6.0            # us
  adc_start_time: 5.7       # us
  ramp_end_time: 34.0       # us
  tx_start_time: 1.0        # us
  freq_slope: 67.012        # MHz/us
  adc_samples: 256
  sample_rate: 10000        # ksps
  frame_length: 64
  frame_period: 50.0        # ms

capture:
  sys_ip: 192.168.33.30
  fpga_ip: 192.168.33.180
  socket_buffer: 6291456
```

To support a new radar, you create a new file in `devices/` with the right
defaults for that device. The sweep script doesn't need to know about the
device — it just substitutes parameters into whichever template you point it
at.

### 5.2 Sweep specification format (`sweeps/frame_period_x_chirps.yaml`)

A sweep spec is a small YAML the user writes to define one experiment:

```yaml
# sweeps/frame_period_x_chirps.yaml
study_name: frame_period_x_chirps    # used to name the output folder
device: AWR1843                       # which template under devices/ to use

sweep:
  # Primary sweep axis: which parameter to vary, and the values to try.
  primary:
    param: frame_period               # name from the device template
    values: [100, 50, 25, 20, 10]     # in the parameter's native units

  # Optional 2-D cross axis. Omit for 1-D sweeps.
  cross:
    param: frame_length
    values: [64, 128]

  replicates: 3                       # default, override on command line if you want
  segment_duration: 30                # seconds of chirping per segment
  idle_between: 5                     # seconds of idle between segments

  # Order in which to run segments. Options:
  #   "primary_first"  - vary primary fastest (recommended)
  #   "cross_first"    - vary cross fastest
  #   "random"         - shuffle for fairness against thermal drift
  order: random
```

This sweep produces: 5 × 2 × 3 = 30 segments. With 30 s chirping + 5 s idle =
35 s per segment, that's 1050 s = 17.5 minutes plus 35 s calibration = ~18 min
total. Plenty short for a single datalog.

**Why `order: random` is the default I'll suggest:** running the same parameter
combination's three replicates back-to-back means they all suffer the same
thermal state. Randomizing means thermal drift is distributed across
parameter values rather than confounded with one of them. For a publication-
grade result, this matters.

### 5.3 The sweep script (`scripts/sweep.py`)

Single entry point. Responsibilities:

1. Parse command-line args (sweep spec path, replicate override, optional
   device override).
2. Load device template + sweep spec.
3. **Validate every parameter combination up front** by calling
   `xwr.XWRSystem(**cfg)` with each generated config and catching exceptions.
   If any combination is invalid for the device, abort with a clear error
   listing the bad point(s) — *before* the user starts the datalog. (Per
   your Q5b: error on invalid.)
4. Compute total expected duration, print it, prompt user to start datalog.
5. Wait for user Enter.
6. Run calibration burst (35 s).
7. For each segment in the planned order:
   - Generate config (template + substitutions).
   - Save it to `studies/<name>/configs/segment_NNN.yaml`.
   - Append a row to `segments.csv` with (seg_id, start_wallclock, params, replicate).
   - Call `capture.py` to run the segment.
   - Append the end_wallclock when done.
   - Sleep `idle_between`.
8. Print stop instructions.

Command-line examples:

```bash
# Use everything from the sweep spec
uv run scripts/sweep.py --spec sweeps/frame_period_only.yaml

# Override replicates for a quick exploratory sweep
uv run scripts/sweep.py --spec sweeps/frame_period_only.yaml --replicates 1

# Override the device (e.g. testing the same sweep on a different radar)
uv run scripts/sweep.py --spec sweeps/frame_period_only.yaml --device IWR6843
```

### 5.4 Capture driver (`scripts/capture.py`)

Mostly the old `MyCapture.py` logic. Now called as a function from `sweep.py`,
not as a standalone script. Handles one segment: setup radar with the given
config, run for `segment_duration` seconds, stop. Reports frames captured.

If a segment errors out (timeout, lost link, etc.), `sweep.py` catches the
exception, logs the failure to `segments.csv`, and either continues with the
next segment or aborts (configurable via `--on-error skip|abort`, default
skip with a warning). This way a single bad config doesn't kill a long sweep.

### 5.5 Analysis script (`scripts/analyze.py`)

Replaces v1's `analyze_power.py`. Run with one argument: the study folder.

```bash
uv run scripts/analyze.py studies/2026-06-15_AWR1843_frame_period_sweep/
```

Steps:

1. Read `manifest.json`, `segments.csv`, `raw/datalog.csv`.
2. Detect the calibration burst's second falling edge in the datalog. This
   pins datalog t=0 to a specific wall-clock time.
3. For each row in segments.csv, slice the datalog to that segment's
   (start, end) window adjusted by the time anchor.
4. Compute per-segment stats: mean V/I/P, peak I, idle I (last 5 s of the
   idle gap *before* this segment), energy.
5. Generate plots:
   - **Full time-series** showing the whole datalog with every segment shaded
     and labeled.
   - **Per-segment** plots in `results/segments/`.
   - **1-D sweep curve** if `cross` is absent: x = primary param value,
     y = mean power, error bars from replicates.
   - **2-D heatmap** if `cross` is present: heatmap of mean power over the
     (primary × cross) grid, plus a separate plot with error bars.
6. Write `results/summary_table.csv` with one row per segment.
7. Generate `results/README.md` summarizing the study.

### 5.6 What is universal across radars?

Things that work the same for every TI radar xwr supports:

- The sweep mechanism (substitute values into device template).
- All analysis (analyze.py doesn't care which radar produced the data).
- The N6705B datalog format and segmentation.

Things that vary per radar (and must live in the device template):

- Frequency band defaults (60 GHz for IWR68xx, 77 GHz for AWR18xx, etc.)
- L3 memory size constraints (handled by xwr's built-in constraint check)
- Number of TX/RX channels
- Maximum sample rate, slope, etc.

What this means: **the sweep script itself stays radar-agnostic.** You can run
the same sweep spec on AWR1843 today and IWR6843 next month by only changing
the `device:` line in the spec, *provided* you've added an `devices/IWR6843.yaml`
template with sensible defaults. The constraint check in xwr will catch any
parameter values that violate the new device's limits and abort with a clear
error.

If a sweep tries an out-of-range value (e.g. an L3-overflow on a small-memory
radar), the validation step in §5.3.3 catches it before the datalog ever
starts.

## 6. Data Formats

### 6.1 `segments.csv`

Append-only log written by sweep.py as it goes. One row per segment.

```
seg_id, replicate, primary_param, primary_value, cross_param, cross_value, \
    start_wallclock_iso, end_wallclock_iso, status, frames_captured, notes
1, 1, frame_period, 100, frame_length, 64, 2026-06-15T13:32:15.421, 2026-06-15T13:32:45.402, ok, 297, ""
2, 1, frame_period, 50, frame_length, 64, 2026-06-15T13:32:50.412, 2026-06-15T13:33:20.391, ok, 596, ""
...
```

Both human-readable and machine-readable. If a segment errors, `status` is
`failed` and `notes` has the error message.

### 6.2 `manifest.json`

Provenance and configuration snapshot.

```json
{
  "study_name": "frame_period_x_chirps",
  "device": "AWR1843",
  "device_template_hash": "sha256:abc123...",
  "sweep_spec_hash": "sha256:def456...",
  "xwr_version": "0.4.3",
  "python_version": "3.11.7",
  "run_started_iso": "2026-06-15T13:31:40.000",
  "run_ended_iso":   "2026-06-15T13:49:32.000",
  "segments_planned": 30,
  "segments_completed": 30,
  "segments_failed": 0,
  "operator_notes": ""
}
```

User can edit `operator_notes` after the run (room temperature, anomalies, etc.).

### 6.3 `summary_table.csv`

Output by analyze.py. One row per segment with full statistics.

```
seg_id, replicate, frame_period, frame_length, radar_mean_P_W, radar_peak_I_A, \
    dca_mean_P_W, total_mean_P_W, total_energy_J, idle_mean_P_W, op_duration_s
```

## 7. Failure modes & how each is handled

| Failure | Detection | Recovery |
|---|---|---|
| Invalid sweep point (xwr constraint fail) | Validated up front by §5.3.3 | Abort sweep with error before starting datalog |
| Radar timeout during a segment | TimeoutError in capture.py | Mark segment failed in segments.csv, continue (or abort per --on-error) |
| DCA1000 link drop mid-sweep | Frame count not increasing | Mark segment failed, retry once, then continue |
| Datalog buffer overflow on N6705B (too long capture) | sweep.py computes required duration up front and refuses if > some limit | User reduces sweep size or accepts coarser sample period |
| Calibration burst not detected by analyzer | Pattern matching fails on full datalog | Fall back to wall-clock timing, warn user |
| User starts datalog too late, misses calibration | Cal pulses appear before datalog t=0 | Analyzer detects this and errors with "datalog appears to start after calibration; rerun with datalog started earlier" |

## 8. Open Questions for You

These are decisions I'm not certain about. Please confirm or override before I
implement.

### Q-A: Do you want the calibration burst at the end too?

Adding a 35 s cal burst at the end (after the last segment) provides a second
time anchor and lets the analyzer measure drift in the datalogger's clock vs.
the script's clock. Useful for very long sweeps (>1 hour). Costs another
35 s. I'd default to **yes** for long sweeps, **no** for short ones.

### Q-B: What's the max sweep duration we should allow?

The N6705B's datalog at 1 ms sample period × 5 columns produces a CSV at
about **170 KB per second**. So a 30-minute sweep = ~300 MB CSV; a 2-hour
sweep = ~1.2 GB. Both are workable but the 2-hour one is annoying to move
around. I'd add a soft warning at 1 hour and a hard refusal at 2 hours
unless the user passes `--allow-huge`. OK?

### Q-C: Plotting library — matplotlib only, or add seaborn/plotly?

v1 uses matplotlib alone. For 2-D heatmaps, matplotlib's `pcolormesh` is fine
but plain. Seaborn would give prettier heatmaps for free. Plotly would let
users interact with the plots in a browser. I'd default to **matplotlib only**
for fewer dependencies, but I want to confirm.

### Q-D: Should I update PROTOCOL.md or write a new one?

The existing PROTOCOL.md document was for the manual 15-run procedure. The
new flow is different enough that I think it's cleaner to **archive the old
PROTOCOL.md as PROTOCOL_v1.md** and write a new PROTOCOL_v2.md focused on the
sweep flow. Confirm.

### Q-E: Should the analyzer also output a LaTeX/Markdown report stub?

For a "scientific report" deliverable, the analyzer could auto-generate a
publication-template markdown file with embedded plots, methodology
paragraph, and a results table. You'd then edit it for narrative. Want this,
or is the auto-generated `results/README.md` summary enough?

### Q-F: Replicate ordering — random or grouped?

I've defaulted `order: random` in the sweep spec for thermal-fairness
reasons (§5.2). The other reasonable default is `primary_first` (run all
replicates of one parameter combination back-to-back). Random is more
defensible in a paper; grouped is easier to debug. Confirm which default
you want.

## 9. Migration from v1

| v1 artifact | What happens in v2 |
|---|---|
| `power_analysis_experiment.md` | Renamed to `PROTOCOL_v1.md`, archived |
| `analyze_power.py` | Replaced by `scripts/analyze.py` (similar logic, new segmentation) |
| `MyCapture.py` | Replaced by `scripts/capture.py` (called internally by sweep.py) |
| `configs/01_light.yaml` etc. | Replaced by `devices/AWR1843.yaml` + a sweep spec |
| 15 separate CSVs (T0_R1.csv etc.) | One `raw/datalog.csv` per study, segmented at analysis time |
| Existing collected data | Re-analyzable with a new `scripts/analyze_v1_csvs.py` shim if you want to keep the old results |

If you have v1 data you want to keep analyzable, tell me and I'll keep a
`legacy/` subfolder with the old analyzer working. Otherwise it can go.

## 10. Implementation Order

I'll build this in stages and check in at each. You can stop me partway if
anything's wrong.

**Stage 1 — Device templates and sweep specs**
- Write `devices/AWR1843.yaml` and one example each of 1-D and 2-D sweep specs
- Get your approval on the formats before writing scripts that depend on them

**Stage 2 — sweep.py (no calibration burst yet)**
- Implement the orchestrator with wall-clock-only timing
- Run end-to-end with a tiny test sweep (2 segments × 1 replicate) to verify
- Iterate based on real run feedback

**Stage 3 — Calibration burst + analyzer**
- Add cal burst to sweep.py
- Implement analyze.py with cal-burst detection, segmentation, and 1-D plots

**Stage 4 — 2-D heatmap and report generation**
- Add 2-D plot support
- Add auto-generated `results/README.md`

**Stage 5 — Documentation**
- Write PROTOCOL_v2.md
- Update README.md
- Generate worked example using a full sweep

Each stage produces something you can run. I'll show you the artifacts at each
stage and we'll iterate before moving on.

## 11. What I Need From You

Please confirm or amend:

1. The folder structure (§4)
2. Sweep spec format (§5.2)
3. Device template format (§5.1)
4. Time-sync via calibration burst (§3)
5. Open questions Q-A through Q-F (§8)
6. Migration plan (§9)
7. Implementation order (§10)

If anything's wrong, push back. Once approved, I'll start Stage 1.