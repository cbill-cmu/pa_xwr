# PROTOCOL: Automated Power-Sweep Study (v2)

This is the bench procedure for the automated parameter-sweep workflow
For background and architecture, see [`DESIGN.md`](DESIGN.md).

---

## 1. Equipment

| Item | Purpose |
|---|---|
| Keysight N6705B DC Power Analyzer (2 channels) | 5 V source + V/I logging for both boards |
| TI mmWave radar EVM (AWR1843, IWR6843, AWR2243, etc.) | Device under test |
| TI DCA1000EVM | LVDS-to-Ethernet capture card |
| Heat sink for the radar | Required for sustained operation |
| Linux PC with USB and Ethernet ports | Runs the sweep stack and xwr |
| Two 5.5 mm × 2.1 mm DC barrel pigtails | Power cables (banana-to-barrel, ~1 m, 18 AWG) |
| 60-pin LVDS ribbon | Radar ↔ DCA1000 |
| Micro-USB cable | Radar CLI to PC |
| Ethernet cable (Cat 5e or better) | DCA1000 ↔ PC |
| FAT32 USB stick, ≤ 8 GB | For exporting CSV from N6705B |

## 2. Hardware setup (example for TI-AWR18430AOPEVM & DCA1000)

1. **Radar:** set switches to functional/DCA1000 mode, install heat sink.
2. **DCA1000:** SW2.5 in `SW_CONFIG`, large side switch in `DC_JACK_5V_IN`.
3. **Cabling:** LVDS ribbon between boards, micro-USB radar→PC, Ethernet
   DCA1000→PC, prep barrel pigtails (verify polarity with a DMM before plugging
   into the EVMs).
4. **N6705B Ch1** wires to the radar's J5 jack; **Ch2** wires to the DCA1000's
   barrel jack. **Do not connect to the EVMs yet.**

## 3. Software setup (one-time)

On the Linux PC:

```bash
# Clone the two repos as siblings.
git clone https://github.com/RadarML/xwr.git
git clone https://github.com/cbill-cmu/pa_xwr.git

# Install:
cd radar-power-study
uv sync
```

Verify Ethernet networking:
- Set the Ethernet adapter connected to the DCA1000 to static IP
  `192.168.33.30` / mask `255.255.255.0` (no gateway).
- Disable the firewall on that interface (`sudo ufw disable` is fine for a
  bench machine).
- Test: `ping 192.168.33.180` should produce no replies — but `ip neigh` after
  trying should list the DCA1000's MAC address (the FPGA doesn't speak ICMP
  but does speak ARP).

Verify the user is in the `dialout` group so the radar UART works:

```bash
groups | grep -q dialout || { sudo usermod -a -G dialout $USER; \
    echo "Group added - log out and back in to apply"; }
```

Once everything's wired and the software is set up, run a dry-run sweep to
verify the orchestration works without hardware:

```bash
uv run rps-sweep --spec sweeps/frame_period_sweep.yaml \
    --dry-run --replicates 1 --skip-prompt
```

If it completes and creates a study folder under `studies/`, the system is
ready.

## 4. Define a sweep

Create or edit a sweep specification YAML under `sweeps/`. Two formats:

### 4.1 1-D sweep (vary one parameter)

```yaml
# sweeps/my_frame_period_sweep.yaml
study_name: my_frame_period_sweep
device: AWR1843

sweep:
  param: frame_period
  values: [100, 50, 25, 20, 10]    # native units of the parameter
  replicates: 3
  segment_duration: 30             # seconds of chirping per data point
  idle_between: 5                  # thermal-recovery gap between segments
  order: random                    # protects against thermal drift
```

### 4.2 Multi-line sweep (vary primary at multiple held values of secondary)

```yaml
study_name: my_period_x_length_sweep
device: AWR1843

sweep:
  primary:
    param: frame_period
    values: [100, 50, 25, 20, 10]
  secondary:
    param: frame_length
    held_at: [64, 128]
  replicates: 3
  segment_duration: 30
  idle_between: 5
  order: random
```

The total number of segments is:

```
len(primary.values) * len(secondary.held_at) * replicates + 2 cal bursts
```

Run a dry-run first to see how long it will take:

```bash
uv run rps-sweep --spec sweeps/my_sweep.yaml --dry-run --skip-prompt
```

The output will include `Expected duration: <N> s (M min)`.

## 5. The actual bench run

### 5.1 Power-on checklist

- [ ] Heat sink installed on radar
- [ ] All cables connected (LVDS, USB, Ethernet) **except** barrel jacks
- [ ] Static IP set on Linux Ethernet adapter
- [ ] FAT32 USB stick in N6705B front port
- [ ] N6705B Ch1: 5.000 V, 3.5 A limit, output **OFF**
- [ ] N6705B Ch2: 5.000 V, 3.5 A limit, output **OFF**
- [ ] Now connect the barrel jacks to J5 (radar) and DCA1000

Turn on both N6705B output channels (Ch1 first, then Ch2). Wait 30 seconds for
the boards to boot and the radar's RF cals to settle.

### 5.2 Configure the datalog

On the N6705B, press **Data Logger** → **Properties** and set:

- **Sample period:** `0.001 s` (1 ms)
- **Duration:** to a value greater than the dry-run's "Expected duration"
  estimate (the script prints this when launched). The default sweep specs
  take about 18 minutes; round up to 1200 s (20 min) to be safe.
- **Filename:** anything — you'll rename it during export.

### 5.3 Synchronized start

Open a terminal on the Linux PC, type the command but **do not press Enter
yet**:

```bash
uv run rps-sweep --spec sweeps/my_sweep.yaml
```

Then:
1. Press **Run** on the N6705B (datalog starts recording).
2. Within 5 seconds, press **Enter** in the terminal (sweep starts).

The script will print a summary and run the start calibration burst (35 s),
then loop through every segment, then run the end calibration burst, then
print stop instructions.

Don't touch anything during the run. The radar may get warm — that's fine as
long as the heat sink is on.

### 5.4 Stop and export

When the script finishes:

1. Wait 5 seconds for the datalog buffer to flush.
2. On the N6705B, press **Stop** on the datalog.
3. **File → Export Data → CSV → External (USB stick)** with any filename.
4. Eject the USB stick, plug into the Linux PC, copy the CSV to:
   ```
   studies/<study-folder>/raw/datalog.csv
   ```
5. Verify the filename is exactly `datalog.csv` (the analyzer looks for that).

## 6. Analyze

```bash
uv run rps-analyze studies/<study-folder>/
```

This will:
1. Load the datalog and detect the calibration burst to align time.
2. Slice the datalog by every segment's wall-clock window.
3. Compute mean V/I/P, peak, energy per segment.
4. Generate the sweep curve, per-segment plots, and a full timeseries plot.
5. Write `results/summary_table.csv` and `results/README.md`.

Everything goes into `studies/<study-folder>/results/`.

## 7. Verify the results

Open the auto-generated `results/README.md` and check:

- **Time anchor method** says `cal_burst` (not `wallclock_fallback`). If it
  fell back, the analyzer couldn't find the cal pulses — segment boundaries
  may be off by a few seconds. The most common cause is forgetting to start
  the datalog *before* pressing Enter on the sweep script.
- **Segments: N ok, 0 failed.** If any segments failed, look at the per-
  segment plots and `capture.log` to see what went wrong.

Open `results/timeseries_full.png` and verify visually:

- Two cal-burst patterns at the start and end (4 visible chirp pulses total).
- A series of operating segments in the middle, color-shaded.
- For each real segment, a clear current step from idle to operating and back.

Open `results/sweep_curve.png` and check:

- Power changes monotonically with the swept parameter (within reason).
- Error bars are small compared to between-point differences (otherwise
  you'd want more replicates or longer segment duration).


## Appendix A — Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Sweep script aborts with "Static validation failed" before prompting | A sweep value violates a device-agnostic constraint (e.g. non-power-of-2 frame_length) | Fix the sweep spec. Validation runs before the datalog starts so no data is wasted. |
| `rps-sweep` hangs at startup with `TimeoutError` from xwr | DCA1000 unreachable | `ping 192.168.33.180` and check Ethernet link / firewall / SW2.5 |
| Many segments marked `failed` | Radar timeout, link drops, or wrong config for the device | Look at `capture.log` for per-segment error messages |
| Cal-burst detection failed | Datalog started after sweep, or radar didn't chirp during cal pulses | Re-run, making sure to press Run on N6705B before Enter on terminal |
| `Mass storage error` exporting CSV | USB stick is not FAT32 | Reformat to FAT32 (≤8 GB stick works best) |
| Two operating clusters at the same parameter look very different | Thermal drift or a single bad replicate | Add more replicates, increase `idle_between`, or repeat the study with the radar pre-warmed |