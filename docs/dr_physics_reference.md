# DR Analyst — physics reference

*This document is read by the DR Analyst agent (Phase A) and by the DR-Only Report writer. It contains the physics knowledge needed to interpret dilution refrigerator data.*

## What the channels mean

- **MXC (CMN 172)** — the mixing chamber temperature, measured by a paramagnetic salt (CMN) thermometer. This is the most physically meaningful number: it is the temperature the chip actually sees. Valid below ~400 mK; at base typically 10–50 mK. This is the only channel that reads correctly at true base temperature — RuO2 sensors (Still, 50 mK plate) lose calibration below ~1 K and return garbage values, so their absence from the table at base is normal and expected.
- **Still (~700 mK)** — the still pot, where 3He evaporates to drive circulation. A healthy still runs at 0.6–0.9 K. Values are only valid during cooling/warming transitions; at base the RuO2 calibration fails.
- **4K stage (TT-2450)** — the 4 K cold plate. Valid 2–300 K range. At base it reads garbage (same calibration issue).
- **Pressures (P1–P4)** — P1 is the fore-line. When the pumps are running, P1 should be a few mbar. P1 ≈ 1000 mbar (atmospheric) means the fore-pump was off — no circulation, system warming. Negative values are gauge offsets and can be ignored.

## What base temperature means for the experiment

**Thermal photon population in the qubit/resonator:**
The condition for a clean quantum experiment is kT << hf. For a 5 GHz qubit:
- At 20 mK: kT/h ≈ 415 MHz → hf/kT ≈ 12 → thermal photon occupancy n_th ≈ e^{-12} ≈ 0.0001% — negligible
- At 100 mK: kT/h ≈ 2.1 GHz → hf/kT ≈ 2.4 → n_th ≈ 9% — the qubit has a ~9% chance of being thermally excited even without any drive. This is a serious source of readout error and apparent T1 degradation.
- At 200 mK: n_th ≈ 30% — the qubit is essentially a classical thermal mixture. T1 and T2 measurements at this temperature are not representative of intrinsic coherence.

**Quasiparticle poisoning (for both qubits and KIDs):**
Superconductors have a gap Δ. For aluminum (Al): Δ ≈ 172 μeV ≈ 2 K equivalent. At base temperature, the thermal quasiparticle density is exponentially suppressed: n_qp ∝ exp(-Δ/kT). At 20 mK this is essentially zero and non-equilibrium quasiparticles (from radiation, cosmic rays, phonon bursts) dominate. But if MXC was elevated:
- At 100 mK: thermal n_qp begins to contribute meaningfully → increased quasiparticle-induced T1 loss (1/T1 ∝ n_qp × |g|²) and KID noise floor rises
- At 200 mK: thermal quasiparticles exceed non-equilibrium ones → T1 times collapse, KID responsivity degrades
- This is why even a 50 mK elevation from 20 mK to 70 mK matters: n_qp changes by exp(-Δ/k × (1/70mK - 1/20mK)) ≈ exp(-220), but relative to non-equilibrium background, still significant

**For KID experiments specifically (Mattis-Bardeen physics):**
KID resonant frequency and quality factor both depend on temperature through the complex conductivity σ₁ + iσ₂. Even small temperature increases shift f₀ and degrade Qi via increased quasiparticle density. The Mattis-Bardeen fitting functions in `daq/analysis/mattis_bardeen.py` assume thermal equilibrium — if the DR was not at a stable base temperature during the sweep, fitted Δ₀ and α values will be systematically wrong.

## Anomalies to flag and what they mean

**MXC min > 50 mK when qubit T1/T2 or KID spectroscopy was the goal:**
The system did not reach proper base. Thermal photon population and quasiparticle density are elevated. All coherence times and quality factors measured are lower bounds, not intrinsic values.

**Large spread between MXC min and max (ratio > 3×) within the window:**
The system was either still cooling, had a thermal event (vibration burst, cosmic ray event, pulse tube hiccup), or the measurement was taken during warmup. If data was taken during this instability, resonator fits may have frequency drift baked in. Measurements taken at the coldest point are most reliable.

**MXC median >> MXC min:**
The system spent most of the window warmer than its coldest point — likely cooling down or warming up during the measurement. Reproducibility is questionable.

**Still temperature > 1 K (when readable):**
The still is running hot — reduced 3He circulation (pump issue, partial blockage) or still heater deliberately raised. Cooling power at the MXC is reduced, which explains elevated base temperature.

**P1 (fore-line) at atmospheric (~1000 mbar) during part of the window:**
The fore-pump was off. The dilution circuit was not circulating — the MXC was cooling only from thermal mass, not active cooling. Any measurements during this period were taken with a warming system.

**No valid temperature readings at all:**
The DR was either at room temperature, mid-cooldown, or the Leiden Cryogenics software was not logging. Do not infer cold conditions — state explicitly that DR temperature during this experiment is unknown.

## How to incorporate DR conditions into the report

- **Methods section:** State the base temperature and time window factually. e.g. "The dilution refrigerator reached a base temperature of 14.2 mK (median 30.1 mK over the measurement window) as measured by the CMN 172 thermometer at the mixing chamber."
- **Key Parameters table:** Add a row for MXC temperature (min and median).
- **Results section:** If temperature was stable and cold, note that thermal effects are negligible. If anomalous, quantify the impact using the physics above.
- **Open Questions:** If temperature instability was observed, suggest a follow-up run at confirmed stable base temperature.
