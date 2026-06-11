# radar-power-study

Automated power-consumption studies of TI mmWave radars (AWR1843, IWR6843,
AWR1443, etc.) using a Keysight N6705B DC power analyzer.

The radar capture itself uses the [xwr](https://github.com/RadarML/xwr)
library, which lives in a separate repository and is pulled in as a
dependency.

## Repository structure

```
pa_xwr/
├── pyproject.toml                  package definition + xwr dependency
├── README.md                       this file
├── DESIGN.md                       architecture decisions
├── PROTOCOL.md                     lab procedure
│
├── src/pa_xwr/                     the Python package
│   ├── sweep.py                    orchestrator (the main entry point)
│   ├── capture.py                  single-segment radar driver
│   ├── analyze.py                  offline analysis + plots
|   └── FFT_zoom.py                 Close-up section of analysis plots with FFT information
│
├── devices/                        one YAML template per radar variant
│   └── AWR1843.yaml
│   
├── sweeps/                         experiment specifications (EDITABLE)
│   ├── frame_period_sweep.yaml
│   ├── low_frame_rate_x_frame_length.yaml
│   └── constant_frame_rate_x_frame_length.yaml
|
└── studies/                        gitignored; outputs of each run go here
```

## Install

This repository expects [`uv`](https://github.com/astral-sh/uv) and assumes
the xwr repo is checked out next to this one:

```
~/projects/
├── xwr/                            ← the xwr repo
└── pa_xwr/                         ← this repo
```

If your xwr clone is somewhere else, edit the `path = "../xwr"` line in
`pyproject.toml`.

To install:

```bash
cd radar-power-study
uv sync
```

This creates a `.venv` with this package and xwr both installed in editable
mode.

## Quickstart

```bash
# Dry-run a sweep to verify everything is wired correctly (no hardware needed)
uv run rps-sweep --spec sweeps/frame_period_sweep.yaml --dry-run --replicates 1

# Real sweep against actual hardware
uv run rps-sweep --spec sweeps/frame_period_sweep.yaml

# Analyze the resulting study folder
uv run rps-analyze studies/<generated-folder-name>/
```

Read [PROTOCOL.md](PROTOCOL.md) for the full bench procedure. Read
[DESIGN.md](DESIGN.md) for architecture rationale.
