"""LiFePO4 pack physics for the Daly BMS simulator.

Sign convention everywhere: current > 0 charges the pack, current < 0 discharges it.

Each cell has its own capacity (manufacturing spread plus state of health), state of
charge, ohmic resistance, one RC polarisation branch (so voltage sags under load and
relaxes afterwards, as real cells do) and self-discharge. The pack has a single thermal
mass heated by I^2 R losses and balancing resistors, cooling towards ambient.
"""

from __future__ import annotations

import math
import random
import re
from bisect import bisect_right
from dataclasses import dataclass

# Rested open-circuit voltage of a LiFePO4 cell vs state of charge at about 25 °C.
# Flat plateau between 20 and 90 %, steep knees at both ends.
OCV_TABLE = (
    (0.00, 2.500), (0.01, 2.800), (0.03, 3.000), (0.05, 3.100), (0.10, 3.200),
    (0.20, 3.250), (0.30, 3.270), (0.40, 3.285), (0.50, 3.295), (0.60, 3.305),
    (0.70, 3.320), (0.80, 3.330), (0.90, 3.340), (0.95, 3.355), (0.98, 3.380),
    (0.99, 3.400), (1.00, 3.450),
)
OCV_OVERCHARGE_SLOPE = 15.0   # V per unit SOC above 100 %: a 3.60 V CV ends near 101 %
SURFACE_CHARGE_TAU_S = 1200
# Near full, lithium diffusion in the LiFePO4 cathode slows down: the polarisation resistance
# rises steeply above ~90 % SOC. That is what makes a real CV phase taper over 15-30 min
# instead of collapsing in a minute. r1 is multiplied by 1 + GAIN * x^2, x = (soc - 0.9) / 0.1.
TOP_POLAR_GAIN = 30.0
_OCV_SOC = [s for s, _ in OCV_TABLE]

CELL_NOMINAL_V = 3.2
CHARGER_CELL_V = 3.60     # CV setpoint of a LiFePO4 charger (14.4 V for 4S, 57.6 V for 16S)

# name -> series cell count (all LiFePO4)
PRESETS = {"12v": 4, "24v": 8, "36v": 12, "48v": 16}


def ocv(soc: float) -> float:
    """Open-circuit cell voltage (V) for a state of charge in 0..1 (extrapolated steeply outside)."""
    if soc <= 0.0:
        return OCV_TABLE[0][1] + soc * 10
    if soc >= 1.0:
        return OCV_TABLE[-1][1] + (soc - 1.0) * OCV_OVERCHARGE_SLOPE
    k = bisect_right(_OCV_SOC, soc)
    s0, v0 = OCV_TABLE[k - 1]
    s1, v1 = OCV_TABLE[k]
    return v0 + (v1 - v0) * (soc - s0) / (s1 - s0)


@dataclass
class PackSpec:
    name: str
    cells: int = 16
    capacity_ah: float = 100.0         # rated capacity, as configured in the BMS
    soh: float = 0.97                  # real capacity / rated capacity
    temp_sensors: int = 2
    soc: float | None = None           # initial SOC in %, None = random 35..85
    cycles: int | None = None          # initial cycle count, None = random
    ambient_c: float = 27.0
    profile: str = "cycle"
    charge_a: float | None = None      # charger CC current            (default 0.2 C)
    load_a: float | None = None        # discharge current             (default 0.3 C)
    max_charge_a: float | None = None  # charge overcurrent alarm L1   (default 0.5 C)
    max_discharge_a: float | None = None  # discharge overcurrent L1   (default 1 C)
    port: str | None = None            # None = PTY, /dev/... = serial, tcp:[host:]port
    weak: str | None = None            # "CELL:PCT": that cell has PCT % of the others' capacity
    hir: str | None = None             # "CELL:FACTOR": that cell's internal resistance x FACTOR

    @property
    def nominal_v(self) -> float:
        return round(self.cells * CELL_NOMINAL_V, 1)

    def __post_init__(self):
        c = self.capacity_ah
        self.charge_a = self.charge_a if self.charge_a is not None else 0.2 * c
        self.load_a = self.load_a if self.load_a is not None else 0.3 * c
        self.max_charge_a = self.max_charge_a if self.max_charge_a is not None else 0.5 * c
        self.max_discharge_a = (self.max_discharge_a if self.max_discharge_a is not None
                                else 1.0 * c)


_SPEC_KEYS = {
    "ah": ("capacity_ah", float), "cells": ("cells", int), "temps": ("temp_sensors", int),
    "soc": ("soc", float), "soh": ("soh", float), "cycles": ("cycles", int),
    "ambient": ("ambient_c", float), "profile": ("profile", str),
    "charge_a": ("charge_a", float), "load_a": ("load_a", float),
    "max_charge_a": ("max_charge_a", float), "max_discharge_a": ("max_discharge_a", float),
    "weak": ("weak", str), "hir": ("hir", str),
}


def parse_spec(text: str) -> PackSpec:
    """NAME[:PRESET|CELLS][@PORT][,key=value...]

    kit48v                         16S, preset guessed from the name
    kit12v:12v,ah=50,soc=80        4S 50 Ah starting at 80 %
    kit36v:36v@/dev/ttyUSB0        on a real serial port
    kit48v@tcp:7001,profile=drive  on TCP port 7001 (gateway port = socket://<pc>:7001)
    """
    head, *opts = text.split(",")
    head, _, port = head.partition("@")
    name, _, kind = head.partition(":")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError(f"bad pack name {name!r}")
    if not kind:
        m = re.search(r"(12|24|36|48)v", name.lower())
        kind = m.group(0) if m else "48v"
    if kind.isdigit():
        cells = int(kind)
    elif kind.lower() in PRESETS:
        cells = PRESETS[kind.lower()]
    else:
        raise ValueError(f"{name}: unknown preset {kind!r} (use {', '.join(PRESETS)} or a cell count)")
    kw: dict = {"name": name, "cells": cells, "port": port or None}
    for opt in opts:
        key, sep, val = opt.partition("=")
        if not sep or key not in _SPEC_KEYS:
            raise ValueError(f"{name}: bad option {opt!r} (known: {', '.join(_SPEC_KEYS)})")
        field_name, conv = _SPEC_KEYS[key]
        kw[field_name] = conv(val)
    spec = PackSpec(**kw)
    if not 1 <= spec.cells <= 48:
        raise ValueError(f"{name}: Daly supports 1..48 cells")
    if not 0 <= spec.temp_sensors <= 16:
        raise ValueError(f"{name}: temps must be 0..16")
    for key in ("weak", "hir"):
        if getattr(spec, key) is not None:
            cell_factor(getattr(spec, key), spec.cells, key)
    return spec


def cell_factor(text: str, cells: int, what: str = "weak") -> tuple[int, float]:
    """"3:92" -> (2, 0.92) for weak (percent), "2:1.6" -> (1, 1.6) for hir (factor)."""
    try:
        cell, _, val = text.partition(":")
        k = int(cell) - 1
        f = float(val) if val else (70.0 if what == "weak" else 1.6)
    except ValueError:
        raise ValueError(f"{what} must be CELL:{'PCT' if what == 'weak' else 'FACTOR'}, "
                         f"got {text!r}") from None
    if not 0 <= k < cells:
        raise ValueError(f"{what}: cell must be 1..{cells}")
    if what == "weak":
        f /= 100
    if not 0.05 <= f <= 10:
        raise ValueError(f"{what}: value out of range in {text!r}")
    return k, f


@dataclass
class Cell:
    capacity_ah: float
    soc: float          # 0..1 (may go slightly outside when abused)
    r0: float           # ohmic resistance, ohm
    r1: float           # polarisation resistance, ohm
    tau_s: float        # polarisation time constant, s
    leak_per_s: float   # self-discharge, fraction of capacity per second
    v1: float = 0.0     # polarisation voltage, V

    def voltage(self, current: float) -> float:
        return ocv(self.soc) + current * self.r0 + self.v1

    def r1_now(self) -> float:
        x = max(0.0, (self.soc - 0.90) / 0.10)
        return self.r1 * (1 + TOP_POLAR_GAIN * x * x)

    def step(self, current: float, dt: float) -> None:
        self.soc += current * dt / 3600 / self.capacity_ah - self.leak_per_s * dt
        if self.soc > 1.0:    # surface charge above full fades: rests back to ~3.45 V
            self.soc -= (self.soc - 1.0) * (1 - math.exp(-dt / SURFACE_CHARGE_TAU_S))
        self.soc = min(1.03, max(-0.05, self.soc))
        a = math.exp(-dt / self.tau_s)
        self.v1 = self.v1 * a + current * self.r1_now() * (1 - a)


class Pack:
    PATH_OHM = 0.0015        # MOSFETs + shunt + busbars

    def __init__(self, spec: PackSpec, rng: random.Random):
        self.spec = spec
        real_ah = spec.capacity_ah * spec.soh
        soc0 = (spec.soc if spec.soc is not None else rng.uniform(35, 85)) / 100
        r0 = 0.1 / spec.capacity_ah          # 1 mOhm for a 100 Ah prismatic cell
        caps = [real_ah * rng.gauss(1, 0.008) for _ in range(spec.cells)]
        if spec.weak:           # an aged cell, still top-balanced with the others
            k, f = cell_factor(spec.weak, spec.cells, "weak")
            caps[k] *= f
        mean_cap = sum(caps) / len(caps)
        # Top-balanced like an assembled pack: every cell is the same Ah away from full,
        # so cells agree at the top and the capacity spread shows up near empty.
        self.cells = [
            Cell(capacity_ah=cap,
                 soc=min(1.0, max(0.0, 1 - (1 - soc0) * mean_cap / cap + rng.gauss(0, 0.002))),
                 r0=r0 * rng.gauss(1, 0.08), r1=0.6 * r0 * rng.gauss(1, 0.08),
                 tau_s=rng.uniform(40, 70),
                 leak_per_s=rng.uniform(0.01, 0.03) / (30 * 86400))
            for cap in caps
        ]
        if spec.hir:
            k, f = cell_factor(spec.hir, spec.cells, "hir")
            self.cells[k].r0 *= f
            self.cells[k].r1 *= f
        self.nominal_capacity = [c.capacity_ah for c in self.cells]
        self.nominal_r = [(c.r0, c.r1) for c in self.cells]
        self.current = 0.0
        self.temp_c = spec.ambient_c
        self.sensor_offset = [rng.uniform(-0.6, 0.6) for _ in range(spec.temp_sensors)]
        self.heat_cap = spec.cells * spec.capacity_ah * 22.0   # J/K (~2 kg/100 Ah, cp ~1100)
        self.cooling = self.heat_cap / 2400                    # W/K: 40 min time constant

    # --- electrical -------------------------------------------------------------
    def emf(self) -> float:
        return sum(ocv(c.soc) + c.v1 for c in self.cells)

    def resistance(self) -> float:
        return sum(c.r0 for c in self.cells) + self.PATH_OHM

    def voltage(self) -> float:
        return sum(self.cell_voltages()) + self.current * self.PATH_OHM

    def cell_voltages(self) -> list[float]:
        return [c.voltage(self.current) for c in self.cells]

    def soc(self) -> float:
        """True SOC in % (charge-weighted mean of the cells, 0..100)."""
        return min(100.0, max(0.0, 100 * sum(c.soc * c.capacity_ah for c in self.cells) / sum(
            c.capacity_ah for c in self.cells)))

    def cv_current(self, volts: float, max_a: float) -> float:
        """Charge current from a CC/CV charger with the given CV voltage and CC limit."""
        return max(0.0, min(max_a, (volts - self.emf()) / self.resistance()))

    def power_current(self, watts: float) -> float:
        """Discharge current (positive amps) drawn by a constant-power load."""
        e, r = self.emf(), self.resistance()
        disc = e * e - 4 * r * watts
        return (e - math.sqrt(disc)) / (2 * r) if disc > 0 else e / (2 * r)

    # --- time step --------------------------------------------------------------
    def step(self, current: float, dt: float, ambient_c: float,
             bleed: dict[int, float] | None = None) -> None:
        bleed = bleed or {}
        self.current = current
        heat = current * current * self.resistance() + sum(c.v1 * c.v1 / c.r1_now()
                                                           for c in self.cells)
        for k, cell in enumerate(self.cells):
            cell.step(current - bleed.get(k, 0.0), dt)
        heat += sum(a * 3.3 for a in bleed.values())
        self.temp_c += (heat - self.cooling * (self.temp_c - ambient_c)) * dt / self.heat_cap

    def sensor_temps(self) -> list[float]:
        return [self.temp_c + o for o in self.sensor_offset]
