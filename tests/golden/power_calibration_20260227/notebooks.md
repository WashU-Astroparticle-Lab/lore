---
type: Notebooks
resource: https://github.com/WashU-Astroparticle-Lab/analysis_archive/tree/ac7ee2a15897548f177b740659fde57ae80fd070/DAQ/power_calibration_20260227
source_ref: ac7ee2a15897548f177b740659fde57ae80fd070
timestamp: 2026-05-14T18:08:35Z
tags: [power_calibration_20260227]
---

# power_calibration.ipynb

## Cell 1 [markdown]

## Power calibration notebook – quick usage

This notebook takes Presto sweep logs and Spike channel‑power CSVs and produces:
- combined calibration CSVs (`freq_ghz`, `amp`, `power_dbm`),
- plots of power vs frequency / amplitude,
- a 2D interpolation function and a dense power grid CSV.

**Minimal workflow:**
1. **Run sweep** (Cell 0): configure frequency/amp ranges, then run `run_sweep()` while Spike is logging channel power.
2. **Combine sweep + Spike** (Cell 1): set `SWEEP_CSV`, `SPIKE_CSV`, `TIME_OFFSET_MS`, run to create `*_combined.csv`.
3. **Aggregate + interpolate** (Cell 3): point `CAL_DIR` at the folder with all `*_combined.csv`, run to get plots + `interpolate_power_dbm()`.
4. **Export grid** (Cell 4, optional): run to save `power_cal_interpolated_grid_2d.csv` for use in other tools.
5. **Freq mapping check** (Cell 2, optional): set `MAPPING_CSV` if you want to inspect measured vs Presto frequency mapping.


## Cell 2 [code] (unexecuted)

import time
import csv
from pathlib import Path
from datetime import datetime

import numpy as np
from presto import lockin
from presto.hardware import AdcMode, DacMode

# Folder for output CSVs (created if it doesn't exist)
OUT_DIR = Path("power_cal_data")

# ---- Config ----
PRESTO_ADDRESS = "172.23.20.29"
OUTPUT_PORT = 1
DF = 2e3
DAC_CURRENT_UA = 40_500
SETTLE_TIME_S = 0.5

# External reference clock:
#   False or None  → internal reference clock (default)
#   True           → external reference clock at 10 MHz
#   int or float   → external reference clock at that frequency in Hz (e.g. 100e6 for 100 MHz)
# See https://intermod.pro/manuals/presto/source/lockin.html
EXT_REF_CLK = False

# You set the frequency range and step
FREQ_START_GHZ = 2.35
FREQ_STOP_GHZ = 3.8
FREQ_STEP_GHZ = 0.25      #default(0.25)

# Logarithmically spaced AMP (you set how many points)
AMP_MIN = 0.001           # default(0.001)
AMP_MAX = 1.0
NUM_AMP_POINTS = 50       # greater = finer sweep (default = 50)


def set_tone(freq_hz, amp):
    with lockin.Lockin(address=PRESTO_ADDRESS, ext_ref_clk=EXT_REF_CLK,
                       adc_mode=AdcMode.Mixed, dac_mode=DacMode.Mixed) as lck:
        lck.hardware.set_dac_current(OUTPUT_PORT, DAC_CURRENT_UA)
        lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)
        lck.hardware.configure_mixer(freq_hz, out_ports=OUTPUT_PORT)
        _, df = lck.tune(0.0, DF)
        lck.set_df(df)
        og = lck.add_output_group(OUTPUT_PORT, 1)
        og.set_frequencies(0.0)
        og.set_amplitudes(amp)
        og.set_phases(0.0, 0.0)
        lck.apply_settings()
        time.sleep(SETTLE_TIME_S / 2)
        # Record time at middle of dwell so merge matches to a sample while tone is on
        t_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        time.sleep(SETTLE_TIME_S / 2)
    return t_str


def turn_off():
    with lockin.Lockin(address=PRESTO_ADDRESS, ext_ref_clk=EXT_REF_CLK,
                       adc_mode=AdcMode.Mixed, dac_mode=DacMode.Mixed) as lck:
        og = lck.add_output_group(OUTPUT_PORT, 1)
        og.set_frequencies(0.0)
        og.set_amplitudes(0.0)
        og.set_phases(0.0, 0.0)
        lck.apply_settings()


def run_sweep():
    freqs_ghz = np.arange(FREQ_START_GHZ, FREQ_STOP_GHZ + 1e-9, FREQ_STEP_GHZ)
    if len(freqs_ghz) > 0 and freqs_ghz[-1] < FREQ_STOP_GHZ - 1e-9:
        freqs_ghz = np.append(freqs_ghz, FREQ_STOP_GHZ)
    freqs = freqs_ghz * 1e9
    amps = np.clip(np.logspace(np.log10(AMP_MIN), np.log10(AMP_MAX), NUM_AMP_POINTS), 0.0, 1.0)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = OUT_DIR / f"power_cal_{FREQ_START_GHZ}_{FREQ_STOP_GHZ}GHz.csv"
    rows = []

    n_f, n_a = len(freqs), len(amps)
    print(f"Sweep {FREQ_START_GHZ}–{FREQ_STOP_GHZ} GHz (step {FREQ_STEP_GHZ}), "
          f"AMP log {AMP_MIN}–{AMP_MAX} ({NUM_AMP_POINTS} points) → {n_f * n_a} total")
    print(f"CSV: {out_csv}")

    for i, f in enumerate(freqs):
        print(f"  Freq {i+1}/{n_f} {f/1e9:.2f} GHz")
        for j, a in enumerate(amps):
            t_str = set_tone(f, a)
            rows.append({"freq_ghz": round(f/1e9, 4), "amp": a, "power_dbm": "", "local_time": t_str})
            if (j + 1) % 5 == 0 or j == 0:
                print(f"    AMP {j+1}/{n_a} amp={a:.4g}")

    turn_off()
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["freq_ghz", "amp", "power_dbm", "local_time"])
        w.writeheader()
        w.writerows(rows)
    print(f"Done. Saved {len(rows)} rows to {out_csv}. Fill power_dbm from Spike (same order); use local_time to match Spike log.")

if __name__ == "__main__":
    try:
        run_sweep()
    finally:
        try:
            turn_off()
        except Exception:
            pass

## Cell 3 [code] (unexecuted)

# Match each sweep row to the nearest Spike power sample in time
# (uses `TIME_OFFSET_MS` and `merge_asof(..., direction="backward")` to handle clock skew)

import pandas as pd
from pathlib import Path

# the sweep and spike data paths to merge
SWEEP_CSV = Path("power_cal_data/power_cal_6.2_6.95GHz.csv")
SPIKE_CSV = Path("spike_CSV_data/CP_2026-03-05 16h00m05s.csv")
TOLERANCE_MS = 200
# If sweep clock is ahead of Spike, shift sweep times back (ms) so backward picks on-tone sample (e.g. 25)
TIME_OFFSET_MS = 25

# 1) Load sweep CSV and Spike channel‑power CSV
sweep = pd.read_csv(SWEEP_CSV).drop(columns=["power_dbm"], errors="ignore")
spike = pd.read_csv(SPIKE_CSV, skiprows=4)
spike = spike.rename(columns={"Main_Channel (dBm)": "power_dbm"})
# 2) Parse/normalize Spike timestamps into a real datetime column
#    (Spike uses "dd/mm/yyyy HH:MM:SS:mmm" with a colon before ms)
spike["Time"] = spike["Time"].astype(str).str.replace(r"(\d{2}:\d{2}:\d{2}):(\d{3})$", r"\1.\2", regex=True)
spike["Time"] = pd.to_datetime(spike["Time"], format="%d/%m/%Y %H:%M:%S.%f")
spike = spike[["Time", "power_dbm"]].dropna().sort_values("Time").reset_index(drop=True)
if len(spike) == 0:
    raise ValueError("No power data in Spike CSV")

base_date = spike["Time"].iloc[0].date()
sweep["local_time"] = pd.to_datetime(sweep["local_time"], format="%H:%M:%S.%f").apply(
    lambda t: t.replace(year=base_date.year, month=base_date.month, day=base_date.day)
)
# Optional: shift sweep times back so backward merge picks the right Spike sample (clock skew)
if TIME_OFFSET_MS != 0:
    sweep["_merge_time"] = sweep["local_time"] - pd.Timedelta(milliseconds=TIME_OFFSET_MS)
else:
    sweep["_merge_time"] = sweep["local_time"]

# direction="backward": last Spike at or before (shifted) sweep time
combined = pd.merge_asof(
    sweep.sort_values("_merge_time"),
    spike.rename(columns={"Time": "_merge_time"}),
    on="_merge_time",
    direction="backward",
    tolerance=pd.Timedelta(f"{TOLERANCE_MS}ms"),
)
combined = combined[["freq_ghz", "amp", "power_dbm", "local_time"]]
combined["power_dbm"] = combined["power_dbm"].astype(float).round(4)
combined = combined.sort_values(["freq_ghz", "amp"]).reset_index(drop=True)

OUTPUT_COMBINED = SWEEP_CSV.with_name(SWEEP_CSV.stem + "_combined.csv")
combined.to_csv(OUTPUT_COMBINED, index=False)
print(f"Saved {len(combined)} rows to {OUTPUT_COMBINED}")

## Cell 4 [code] (unexecuted)

# Not very necessary if reference clocks between spectrum analyzer and presto are synced
# Plot Δf = (measured − Presto) vs Presto frequency, one line per bandpass_filter_range

import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path



MAPPING_CSV = Path(r"something")

df = pd.read_csv(MAPPING_CSV)
# Compute offset (measured − Presto) in Hz and sort by Presto frequency
df["delta_f_Hz"] = (df["frequency"] - df["presto_marked_frequency"]) * 1e9  # GHz -> Hz
df = df.sort_values("presto_marked_frequency").reset_index(drop=True)

fig, ax = plt.subplots()
# Plot one line per bandpass_filter_range so band edges are not connected
for band, g in df.groupby("bandpass_filter_range", sort=False):
    ax.plot(g["presto_marked_frequency"], g["delta_f_Hz"], "o-", markersize=4, label=band)
ax.set_xlabel("Presto frequency (GHz)")
ax.set_ylabel(r"$\Delta f$ (Hz)")
ax.set_title("Frequency offset: measured − Presto")
ax.legend(title="bandpass_filter_range")
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.show()

### Output
<Figure size 640x480 with 1 Axes>
[output image: github_images/power_calibration_cell4_out0.png]

## Cell 5 [code] (execution_count=7)

# Merge all `*combined*.csv` files, plot power vs freq/amp, and build a 2D interpolation grid + function
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

# change path to a folder containg all the CSV files to merge (The Merged SWEEP + SPIKE Data to merge into 1 bigger data set)
CAL_DIR = Path(r"C:\Users\axelr\OneDrive\Desktop\power_cali")

# 1) Load all *combined*.csv files and stack them into one DataFrame
files = sorted(CAL_DIR.glob("*combined*.csv"))
if not files:
    raise FileNotFoundError(f"No *combined*.csv in {CAL_DIR}")
print(f"Loading {len(files)} file(s): {[f.name for f in files]}")

dfs = []
for f in files:
    d = pd.read_csv(f)
    d["freq_ghz"] = pd.to_numeric(d["freq_ghz"], errors="coerce")
    d["amp"] = pd.to_numeric(d["amp"], errors="coerce")
    d["power_dbm"] = pd.to_numeric(d["power_dbm"], errors="coerce")
    d = d.dropna(subset=["freq_ghz", "amp", "power_dbm"])
    dfs.append(d)
df = pd.concat(dfs, ignore_index=True)

# 2) If the same (freq, amp) appears in multiple files, keep the row with HIGHEST power (max dBm)
idx = df.groupby(["freq_ghz", "amp"])["power_dbm"].idxmax()
df = df.loc[idx.values].sort_values(["freq_ghz", "amp"]).reset_index(drop=True)

freqs = df["freq_ghz"].unique()
amps = df["amp"].unique()
n_freq, n_amp = len(freqs), len(amps)
print(f"Data: {n_freq} frequencies, {n_amp} amplitudes, {len(df)} points")

# 3) plot: power vs frequency (lines colored by amplitude)
fig1, ax1 = plt.subplots(figsize=(8, 5))
amp_plot = amps if n_amp <= 12 else np.linspace(amps.min(), amps.max(), 10)
for a in amp_plot:
    idx = np.argmin(np.abs(amps - a))
    a_actual = amps[idx]
    sub = df[df["amp"] == a_actual]
    ax1.plot(sub["freq_ghz"], sub["power_dbm"], "o-", markersize=3, label=f"amp={a_actual:.4g}")
ax1.set_xlabel("Frequency (GHz)")
ax1.set_ylabel("Power (dBm)")
ax1.set_title("Power vs frequency (by amplitude) — all bands merged 2.35–9 GHz")
ax1.legend(title="amplitude", fontsize=8)
ax1.grid(True, alpha=0.3)
plt.tight_layout()
plt.show()

# 4) plot: power vs amplitude (lines colored by frequency)
fig2, ax2 = plt.subplots(figsize=(8, 5))
freq_plot = freqs if n_freq <= 12 else np.linspace(freqs.min(), freqs.max(), 10)
for f in freq_plot:
    idx = np.argmin(np.abs(freqs - f))
    f_actual = freqs[idx]
    sub = df[df["freq_ghz"] == f_actual]
    ax2.plot(sub["amp"], sub["power_dbm"], "o-", markersize=3, label=f"{f_actual:.4g} GHz")
ax2.set_xlabel("Amplitude")
ax2.set_ylabel("Power (dBm)")
ax2.set_title("Power vs amplitude (by frequency) — all bands merged 2.35–9 GHz")
ax2.legend(title="frequency", fontsize=8)
ax2.grid(True, alpha=0.3)
plt.tight_layout()
plt.show()


# 5) Build a regular 2D grid (frequency × amplitude) and interpolate power onto it
#    pivot: rows = amp, columns = freq
pivot = df.pivot_table(values="power_dbm", index="amp", columns="freq_ghz", aggfunc="mean")
f_min, f_max = df["freq_ghz"].min(), df["freq_ghz"].max()
a_min, a_max = df["amp"].min(), df["amp"].max()
# Dense regular grid
n_grid = 150
f_grid = np.linspace(f_min, f_max, n_grid)
a_grid = np.linspace(a_min, a_max, n_grid)
# Interpolate linearly along amp and freq to fill the grid
pivot_dense = (
    pivot.reindex(index=a_grid, columns=f_grid)
          .interpolate(method="linear", axis=0)
          .interpolate(method="linear", axis=1)
)
F_grid, A_grid = np.meshgrid(f_grid, a_grid)
Z = pivot_dense.values

# 6) Callable 2D interpolation function: (freq_ghz, amp) -> power_dbm
#    Uses simple bilinear interpolation on the same grid used for the plots.
def interpolate_power_dbm(freq_ghz, amp):
    # Convert to arrays and broadcast to a common shape
    f_arr, a_arr = np.broadcast_arrays(np.asarray(freq_ghz, float), np.asarray(amp, float))
    out = np.full(f_arr.shape, np.nan)

    n_f, n_a = len(f_grid), len(a_grid)
    for idx in np.ndindex(f_arr.shape):
        f = f_arr[idx]
        a = a_arr[idx]
        # Outside grid -> leave NaN
        if f < f_grid[0] or f > f_grid[-1] or a < a_grid[0] or a > a_grid[-1]:
            continue
        # Find surrounding grid cell
        i = np.clip(np.searchsorted(f_grid, f, side="right") - 1, 0, n_f - 2)
        j = np.clip(np.searchsorted(a_grid, a, side="right") - 1, 0, n_a - 2)
        # Local coordinates in [0, 1]
        tf = (f - f_grid[i]) / (f_grid[i + 1] - f_grid[i]) if f_grid[i + 1] != f_grid[i] else 0.0
        ta = (a - a_grid[j]) / (a_grid[j + 1] - a_grid[j]) if a_grid[j + 1] != a_grid[j] else 0.0
        # Corner values
        z00, z10 = Z[j, i], Z[j, i + 1]
        z01, z11 = Z[j + 1, i], Z[j + 1, i + 1]
        # Bilinear interpolation
        out[idx] = (
            (1 - tf) * (1 - ta) * z00
            + tf * (1 - ta) * z10
            + (1 - tf) * ta * z01
            + tf * ta * z11
        )

    return float(out) if np.isscalar(freq_ghz) and np.isscalar(amp) else out

# Example: interpolated power at 7.45 GHz, amplitude 1.0
_ = interpolate_power_dbm(7.00, 1.0)
print(f"interpolate_power_dbm(7.00, 1.0) = {_:.2f} dBm")

# --- 8. Plot the interpolation function (evaluate on grid and plot) ---
Z_fn = interpolate_power_dbm(F_grid.ravel(), A_grid.ravel()).reshape(F_grid.shape)
fig7, ax7 = plt.subplots(figsize=(10, 6))
cf7 = ax7.contourf(F_grid, A_grid, Z_fn, levels=14, cmap="viridis")
cs7 = ax7.contour(F_grid, A_grid, Z_fn, levels=14, colors="k", linewidths=0.4, alpha=0.6)
ax7.clabel(cs7, inline=True, fontsize=8, fmt="%.0f")
ax7.set_xlabel("Frequency (GHz)")
ax7.set_ylabel("Amplitude")
ax7.set_title("2D - Interpolated")
ax7.set_xlim(f_min, f_max)
ax7.set_ylim(a_min, a_max)
ax7.grid(True, alpha=0.3)
plt.colorbar(cf7, ax=ax7, label="Power (dBm)")
plt.tight_layout()
plt.show()

### Output
Loading 7 file(s): ['power_cal_2.35_2.8GHz_combined (1).csv', 'power_cal_3.05_3.05GHz_combined (1).csv', 'power_cal_3.3_4.3GHz_combined.csv', 'power_cal_4.55_4.7GHz_combined.csv', 'power_cal_4.95_5.95GHz_combined (2).csv', 'power_cal_6.2_6.95GHz_combined (5).csv', 'power_cal_7.2_9.0GHz_combined.csv']
Data: 29 frequencies, 50 amplitudes, 1450 points

<Figure size 800x500 with 1 Axes>
[output image: github_images/power_calibration_cell5_out1.png]
<Figure size 800x500 with 1 Axes>
[output image: github_images/power_calibration_cell5_out2.png]
interpolate_power_dbm(7.00, 1.0) = -10.22 dBm

<Figure size 1000x600 with 2 Axes>
[output image: github_images/power_calibration_cell5_out4.png]

## Cell 6 [code] (unexecuted)

# creates a single file `power_cal_interpolator.pkl` that can be loaded

import cloudpickle
from pathlib import Path

OUTPUT_INTERP_PKL = CAL_DIR / "power_cal_interpolator.pkl"

with OUTPUT_INTERP_PKL.open("wb") as f:
    cloudpickle.dump(interpolate_power_dbm, f)

print(f"Saved interpolation function to {OUTPUT_INTERP_PKL}")

### Output
Saved interpolation function to C:\Users\axelr\OneDrive\Desktop\power_cali\power_cal_interpolator.pkl


## Cell 7 [code] (execution_count=13)

import cloudpickle
from pathlib import Path

PICKLE_PATH = Path(r"C:\Users\axelr\OneDrive\Desktop\power_cali\power_cal_interpolator.pkl")

with PICKLE_PATH.open("rb") as f:
    interp = cloudpickle.load(f)

power_dbm = interp(7.45, 1.0)

print(power_dbm)

### Output
-10.87142857142857


## Cell 8 [code] (unexecuted)

# Export the 2D interpolation grid (`F_grid`, `A_grid`, `Z`) to `power_cal_interpolated_grid_2d.csv`

import pandas as pd
from pathlib import Path

# Output path (same folder as combined files by default)
OUTPUT_INTERP_CSV = CAL_DIR / "power_cal_interpolated_grid_2d.csv"

# Flatten grid to long-form table (one row per (freq_ghz, amp) point)
interp_df = pd.DataFrame({
    "freq_ghz": F_grid.ravel(),
    "amp": A_grid.ravel(),
    "power_dbm": Z.ravel(),
})

# Drop any NaNs that might appear at the edges
interp_df = interp_df.dropna(subset=["freq_ghz", "amp", "power_dbm"]).reset_index(drop=True)

interp_df.to_csv(OUTPUT_INTERP_CSV, index=False)
print(f"Saved interpolated grid with {len(interp_df)} points to {OUTPUT_INTERP_CSV}")

### Output
Saved interpolated grid with 22500 points to C:\Users\axelr\OneDrive\Desktop\power_cali\power_cal_interpolated_grid_2d.csv

---

# df_and_if.ipynb

## Cell 1 [markdown]

This notebook shows the relationship between `df` and `if`

## Cell 2 [code] (execution_count=1)

import sys
import os
import numpy as np
import time
import socket

from presto import lockin
from presto.hardware import AdcMode, DacMode
from presto.utils import untwist_downconversion

## Cell 3 [code] (execution_count=2)

OUTPUT_PORT = 1
INPUT_PORT = 1
PRESTO_ADDRESS = "172.23.20.29"

## Cell 4 [markdown]

# Single tone test

## Cell 5 [markdown]

- VBF-2900+
- RBW 1kHz

## Cell 6 [code] (execution_count=41)

DF = 507e3 
AMP = 0.5
FREQ = 3.031e9 - 10e6
IF = 1e6 + 10e6

# Tune IF
with lockin.Lockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    if_tuned, df = lck.tune(IF, DF)
    lck.set_df(df)

    # Turn on output tone
    og = lck.add_output_group(OUTPUT_PORT, 1)  # port, number of frequencies
    og.set_frequencies(if_tuned) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    og.set_phases(0.0, -np.pi/2)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 7 [markdown]

Peak at 3.032156 GHz

## Cell 8 [code] (execution_count=37)

df

### Output
507099.3914807302

## Cell 9 [code] (execution_count=38)

if_tuned

### Output
np.float64(11156186.612576064)

## Cell 10 [code] (execution_count=42)

# Don't tune IF
with lockin.Lockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    _, df = lck.tune(0.0, DF)
    lck.set_df(df)

    # Turn on output tone
    og = lck.add_output_group(OUTPUT_PORT, 1)  # port, number of frequencies
    og.set_frequencies(IF) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    og.set_phases(0.0, -np.pi/2)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 11 [code] (execution_count=43)

df

### Output
507099.3914807302

## Cell 12 [markdown]

Peak at 3.032156 GHz, and lots of spectral leakage

## Cell 13 [code] (execution_count=44)

# Don't reset phase
with lockin.Lockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    _, df = lck.tune(0.0, DF)
    lck.set_df(df)
    lck.set_phase_reset(False)

    # Turn on output tone
    og = lck.add_output_group(OUTPUT_PORT, 1)  # port, number of frequencies
    og.set_frequencies(IF) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    og.set_phases(0.0, -np.pi/2)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 14 [markdown]

Peak at 3.032 GHz

## Cell 15 [code] (execution_count=9)

DF = 807e3 # Just feed a different one
# Don't reset phase
with lockin.Lockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    _, df = lck.tune(0.0, DF)
    lck.set_df(df)
    lck.set_phase_reset(False)

    # Turn on output tone
    og = lck.add_output_group(OUTPUT_PORT, 1)  # port, number of frequencies
    og.set_frequencies(IF) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    og.set_phases(0.0, -np.pi/2)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 16 [markdown]

# Pierre

## Cell 17 [code] (execution_count=18)

farrays = np.array([
    2.754432e9, 2.759668e9,2.800305e9,2.803726e9,2.807920e9,2.822682e9,2.825066e9,2.829387e9,2.832491e9,2.837177e9,2.847013e9,2.849884e9,2.854116e9,2.858591e9,2.863756e9
])
FREQ = np.min(farrays) - 1e8
IF = farrays - FREQ 
FREQ

### Output
np.float64(2654432000.0)

## Cell 18 [code] (execution_count=19)

DF = 1000e3
AMP = np.full(len(IF), 1/(len(IF)))

# Tuned
with lockin.Lockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    if_tuned, df = lck.tune(IF, DF)
    lck.set_df(df)

    # Turn on output tone
    og = lck.add_output_group(OUTPUT_PORT, len(IF))  # port, number of frequencies
    og.set_frequencies(if_tuned) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    RAND_PHASES = np.random.random(len(IF)) * np.pi * 2
    og.set_phases(RAND_PHASES, RAND_PHASES-np.pi/2)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 19 [code] (execution_count=12)

measured = [
    2.754432e9,
    2.759432e9,
    2.800432e9,
    2.803432e9,
    2.807432e9,
    2.822432e9,
    2.825432e9,
    2.829432e9,
    2.832432e9,
    2.837432e9,
    2.847432e9,
    2.849432e9,
    2.854432e9,
    2.858432e9,
    2.863432e9
]

## Cell 20 [code] (execution_count=15)

import matplotlib.pyplot as plt
plt.scatter(farrays, measured - farrays)
plt.plot(farrays, np.zeros(15), color="gray", ls="--")
plt.xlabel("Requested Frequency [Hz]")
plt.ylabel("Frequency Displacement [Hz]")

### Output
Text(0, 0.5, 'Frequency Displacement [Hz]')
<Figure size 640x480 with 1 Axes>
[output image: github_images/df_and_if_cell20_out1.png]

## Cell 21 [code] (execution_count=20)

# Untuned
with lockin.Lockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    _, df = lck.tune(0.0, DF)
    lck.set_df(df)

    # Turn on output tone
    og = lck.add_output_group(OUTPUT_PORT, len(IF))  # port, number of frequencies
    og.set_frequencies(IF) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    RAND_PHASES = np.random.random(len(IF)) * np.pi * 2
    og.set_phases(RAND_PHASES, RAND_PHASES-np.pi/2)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 22 [markdown]

Too many junk peaks due to frequency leakage.

## Cell 23 [code] (execution_count=17)

# Don't reset phase
with lockin.Lockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    _, df = lck.tune(0.0, DF)
    lck.set_df(df)
    lck.set_phase_reset(False)

    # Turn on output tone
    og = lck.add_output_group(OUTPUT_PORT, len(IF))  # port, number of frequencies
    og.set_frequencies(IF) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    RAND_PHASES = np.random.random(len(IF)) * np.pi * 2
    og.set_phases(RAND_PHASES, RAND_PHASES-np.pi/2)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 24 [markdown]

# SymmetricLockin

## Cell 25 [code] (execution_count=79)

DF = 507e3
AMP = 0.5
FREQ = 3.031e9 - 10e6
IF = 1e6 + 10e6
INPUT_PORT_DUMMY = 1 # It is not connected

# Tune IF
with lockin.SymmetricLockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    if_tuned, df = lck.tune(IF, DF)
    lck.set_df(df)

    # Turn on output tone
    og = lck.add_symmetric_group(OUTPUT_PORT, INPUT_PORT_DUMMY, 1)  # port, number of frequencies
    og.set_frequencies(if_tuned) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    og.set_phases(0.0)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 26 [markdown]

Peak at 3.032156 GHz

## Cell 27 [code] (execution_count=80)

DF = 507e3
AMP = 0.5
FREQ = 3.031e9 - 10e6
IF = 1e6 + 10e6
INPUT_PORT_DUMMY = 1 # It is not connected

# Untuned
with lockin.SymmetricLockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    _, df = lck.tune(0.0, DF)
    lck.set_df(df)

    # Turn on output tone
    og = lck.add_symmetric_group(OUTPUT_PORT, INPUT_PORT_DUMMY, 1)  # port, number of frequencies
    og.set_frequencies(IF) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    og.set_phases(0.0)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 28 [markdown]

Peak at 3.032156 GHz, and lots of spectral leakage

## Cell 29 [code] (execution_count=82)

DF = 507e3
AMP = 0.5
FREQ = 3.031e9 - 10e6
IF = 1e6 + 10e6
INPUT_PORT_DUMMY = 1 # It is not connected

# Untuned
with lockin.SymmetricLockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    _, df = lck.tune(0.0, DF)
    lck.set_df(df)
    lck.set_phase_reset(False)


    # Turn on output tone
    og = lck.add_symmetric_group(OUTPUT_PORT, INPUT_PORT_DUMMY, 1)  # port, number of frequencies
    og.set_frequencies(IF) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    og.set_phases(0.0)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 30 [markdown]

At exactly 3.032 GHz

## Cell 31 [markdown]

Conclusion: Symmetric Lockin is useless

## Cell 32 [markdown]

# Frequency Drift

## Cell 33 [markdown]

We want to see if on IQ plane 

## Cell 34 [code] (unexecuted)

---

# operation.ipynb

## Cell 1 [code] (execution_count=1)


import sys
import os
import numpy as np
import time
import socket

from presto import lockin
from presto.hardware import AdcMode, DacMode
from presto.utils import untwist_downconversion

## Cell 2 [code] (execution_count=12)

DF = 2e3
OUTPUT_PORT = 1
INPUT_PORT = 1
PRESTO_ADDRESS = "172.23.20.29"

## Cell 3 [code] (execution_count=11)

AMP = 0.5
FREQ = 3032000425

with lockin.Lockin(
    address=PRESTO_ADDRESS,
    ext_ref_clk=False,
    adc_mode=AdcMode.Mixed,
    dac_mode=DacMode.Mixed,
) as lck:

    lck.hardware.set_dac_current(OUTPUT_PORT, 40_500)  # μA, 2250 to 40500
    lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)

    lck.hardware.configure_mixer(FREQ, out_ports=OUTPUT_PORT)

    _, df = lck.tune(0.0, DF)
    lck.set_df(df)
    
    # lck.set_df(DF)

    # Turn on output tone
    
    og = lck.add_output_group(OUTPUT_PORT, 1)  # port, number of frequencies
    og.set_frequencies(0.0) # Hz, IF frequency
    og.set_amplitudes(AMP) # full-scale units, output amplitude, 0.0 to 1.0
    og.set_phases(0.0, 0.0)  # rad, phase on I and Q port of the digital IQ mixer

    lck.apply_settings()
    
    time.sleep(0.5)

## Cell 4 [code] (execution_count=20)

import socket

def check(host, port):
    try:
        s = socket.create_connection((host, port), timeout=2.0)
        s.close()
        return True
    except OSError:
        return False

for host in ["127.0.0.1", "192.168.2.2", "192.168.2.10"]:
    for port in [51665, 5025]:
        if check(host, port):
            print(f"OPEN: {host}:{port}")
        else:
            print(f"closed: {host}:{port}")

### Output
closed: 127.0.0.1:51665
closed: 127.0.0.1:5025
closed: 192.168.2.2:51665
closed: 192.168.2.2:5025
closed: 192.168.2.10:51665
closed: 192.168.2.10:5025


## Cell 5 [code] (execution_count=283)

import time
import csv
from pathlib import Path
from datetime import datetime

import numpy as np
from presto import lockin
from presto.hardware import AdcMode, DacMode

# Folder for output CSVs (created if it doesn't exist)
OUT_DIR = Path("power_cal_data")

# ---- Config ----
PRESTO_ADDRESS = "172.23.20.29"
OUTPUT_PORT = 1
DF = 2e3
DAC_CURRENT_UA = 40_500    #default(40_500)
SETTLE_TIME_S = 50        #default (0.5)

# You set the frequency range and step
FREQ_START_GHZ = 4.7
FREQ_STOP_GHZ = 4.7
FREQ_STEP_GHZ = 0.25

# Logarithmically spaced AMP (you set how many points)
AMP_MIN = 1.0       # default(0.001)
AMP_MAX = 1.0            # default (1.0)
NUM_AMP_POINTS = 1   # greater = finer sweep (default = 50)


def set_tone(freq_hz, amp):
    with lockin.Lockin(address=PRESTO_ADDRESS, ext_ref_clk=False,
                       adc_mode=AdcMode.Mixed, dac_mode=DacMode.Mixed) as lck:
        lck.hardware.set_dac_current(OUTPUT_PORT, DAC_CURRENT_UA)
        lck.hardware.set_inv_sinc(OUTPUT_PORT, 0)
        lck.hardware.configure_mixer(freq_hz, out_ports=OUTPUT_PORT)
        _, df = lck.tune(0.0, DF)
        lck.set_df(df)
        og = lck.add_output_group(OUTPUT_PORT, 1)
        og.set_frequencies(0.0)
        og.set_amplitudes(amp)
        og.set_phases(0.0, 0.0)
        lck.apply_settings()
        time.sleep(SETTLE_TIME_S / 2)
        # Record time at middle of dwell so merge matches to a sample while tone is on
        t_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        time.sleep(SETTLE_TIME_S / 2)
    return t_str


def turn_off():
    with lockin.Lockin(address=PRESTO_ADDRESS, ext_ref_clk=False,
                       adc_mode=AdcMode.Mixed, dac_mode=DacMode.Mixed) as lck:
        og = lck.add_output_group(OUTPUT_PORT, 1)
        og.set_frequencies(0.0)
        og.set_amplitudes(0.0)
        og.set_phases(0.0, 0.0)
        lck.apply_settings()


def run_sweep():
    freqs_ghz = np.arange(FREQ_START_GHZ, FREQ_STOP_GHZ + 1e-9, FREQ_STEP_GHZ)
    if len(freqs_ghz) > 0 and freqs_ghz[-1] < FREQ_STOP_GHZ - 1e-9:
        freqs_ghz = np.append(freqs_ghz, FREQ_STOP_GHZ)
    freqs = freqs_ghz * 1e9
    amps = np.clip(np.logspace(np.log10(AMP_MIN), np.log10(AMP_MAX), NUM_AMP_POINTS), 0.0, 1.0)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = OUT_DIR / f"power_cal_{FREQ_START_GHZ}_{FREQ_STOP_GHZ}GHz.csv"
    rows = []

    n_f, n_a = len(freqs), len(amps)
    print(f"Sweep {FREQ_START_GHZ}–{FREQ_STOP_GHZ} GHz (step {FREQ_STEP_GHZ}), "
          f"AMP log {AMP_MIN}–{AMP_MAX} ({NUM_AMP_POINTS} points) → {n_f * n_a} total")
    print(f"CSV: {out_csv}")

    for i, f in enumerate(freqs):
        print(f"  Freq {i+1}/{n_f} {f/1e9:.2f} GHz")
        for j, a in enumerate(amps):
            t_str = set_tone(f, a)
            rows.append({"freq_ghz": round(f/1e9, 4), "amp": a, "power_dbm": "", "local_time": t_str})
            if (j + 1) % 5 == 0 or j == 0:
                print(f"    AMP {j+1}/{n_a} amp={a:.4g}")

    turn_off()
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["freq_ghz", "amp", "power_dbm", "local_time"])
        w.writeheader()
        w.writerows(rows)
    print(f"Done. Saved {len(rows)} rows to {out_csv}. Fill power_dbm from Spike (same order); use local_time to match Spike log.")

if __name__ == "__main__":
    try:
        run_sweep()
    finally:
        try:
            turn_off()
        except Exception:
            pass

### Output
Sweep 4.7–4.7 GHz (step 0.25), AMP log 1.0–1.0 (1 points) → 1 total
CSV: power_cal_data\power_cal_4.7_4.7GHz.csv
  Freq 1/1 4.70 GHz

KeyboardInterrupt: 
     82     if (j + 1) % 5 == 0 or j == 0:

Cell In[283], line 44, in set_tone(freq_hz, amp)
     42 og.set_phases(0.0, 0.0)
     43 lck.apply_settings()
---> 44 time.sleep(SETTLE_TIME_S / 2)
     45 # Record time at middle of dwell so merge matches to a sample while tone is on
     46 t_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]

KeyboardInterrupt: 

## Cell 6 [code] (execution_count=237)

from pathlib import Path
print("cwd:", Path.cwd())
print(SWEEP_CSV.exists(), SWEEP_CSV)
print(SPIKE_CSV.exists(), SPIKE_CSV)

### Output
cwd: C:\Users\Karthik\Documents\Lanqing\analysis_archive\DAQ\power_calibration_20260227
False Documents\Lanqing\analysis_archive\DAQ\power_calibration_20260227\power_cal_data\power_cal_2.35_2.8GHz.csv
False Documents\Lanqing\analysis_archive\DAQ\power_calibration_20260227\spike_CSV_data\CP_2026-03-04 11h43m41s.csv


## Cell 7 [code] (execution_count=279)

# Combine sweep CSV with Spike channel-power CSV by time (run after sweep + Spike export)
import pandas as pd
from pathlib import Path

SWEEP_CSV = Path("power_cal_data/power_cal_4.55_4.7GHz.csv")   # from run_sweep()
SPIKE_CSV = Path("spike_CSV_data/CP_2026-03-04 19h18m29s.csv")                  # Spike channel-power export

sweep = pd.read_csv(SWEEP_CSV).drop(columns=["power_dbm"], errors="ignore")
spike = pd.read_csv(SPIKE_CSV, skiprows=4)
spike["Time"] = pd.to_datetime(spike["Time"], format="%d/%m/%Y %H:%M:%S:%f")
spike = spike.rename(columns={"Main_Channel (dBm)": "power_dbm"})
spike = spike[["Time", "power_dbm"]].sort_values("Time").reset_index(drop=True)

base_date = spike["Time"].iloc[0].date()
sweep["local_time"] = pd.to_datetime(sweep["local_time"], format="%H:%M:%S.%f").apply(
    lambda t: t.replace(year=base_date.year, month=base_date.month, day=base_date.day)
)

combined = pd.merge_asof(
    sweep.sort_values("local_time"),
    spike.rename(columns={"Time": "local_time"}),
    on="local_time",
    direction="backward",
    tolerance=pd.Timedelta("200ms"),
)
combined["power_dbm"] = combined["power_dbm"].round(4)
combined = combined[["freq_ghz", "amp", "power_dbm", "local_time"]]
OUTPUT_COMBINED = SWEEP_CSV.with_name(SWEEP_CSV.stem + "_combined.csv")
combined.to_csv(OUTPUT_COMBINED, index=False)
print(f"Saved {len(combined)} rows to {OUTPUT_COMBINED}")

### Output
Saved 100 rows to power_cal_data\power_cal_4.55_4.7GHz_combined.csv