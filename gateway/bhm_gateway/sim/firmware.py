"""What the Daly BMS firmware does with the pack: measure, estimate, protect, balance, answer.

Behaviour copied from Daly smart BMS (LiFePO4 profile):

* SOC is coulomb counted from the shunt against the *rated* capacity set in the BMS, so it
  drifts from the truth as the pack ages; it snaps to 100 % at end of charge and to 0 % on a
  cell undervoltage trip.
* A cycle is counted for every rated capacity's worth of Ah discharged.
* Every protection has a level-1 alarm (bit only) and a level-2 protection that switches the
  charge or discharge MOSFET off after a short delay. Voltage trips release on recovery
  voltage or when current flows the other way, temperature trips with hysteresis,
  overcurrent trips by retrying after 32 s, a short circuit only when cleared.
* Passive balancing (~35 mA bleed) while charging, on cells above 3.40 V and 30 mV above the
  lowest cell.

Thresholds below are typical Daly LiFePO4 defaults; change them here to match the values in
your BMS (Daly PC software / SMART BMS app -> parameter settings).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable

from .. import daly
from .model import Pack, PackSpec

FAULT_INDEX = {name: (byte_i, bit_i)
               for byte_i, bits in enumerate(daly.FAULT_BITS)
               for bit_i, name in enumerate(bits) if name}

BALANCE_START_V = 3.40
BALANCE_DIFF_V = 0.030
BALANCE_A = 0.035
FULL_CELL_V = 3.55            # end-of-charge detection for SOC recalibration
STATE_THRESHOLD_A = 0.3       # below this the BMS reports "idle"


@dataclass
class Meas:
    cells_mv: list[int]
    temps_c: list[int]
    current_a: float
    sum_v: float
    gather_v: float
    max_v: float
    min_v: float
    max_t: float
    min_t: float
    soc: float


@dataclass
class Rule:
    key: str                        # fault bits are <key>_l1 / <key>_l2
    value: Callable[[Meas], float]
    l1: float | None
    l2: float | None
    high: bool = True               # True: trips above the level, False: below
    action: str | None = None       # MOS switched off at level 2: "chg", "dis" or None
    release: float | None = None    # level-2 recovery value (default: l2 -/+ hyst)
    hyst: float = 0.0
    release_on: str | None = None   # also release when current flows "charge"/"discharge"
    retry_s: float = 0.0            # timed release (overcurrent)
    delay_s: float = 2.0
    l1_on: bool = False
    l2_on: bool = False
    _pending: float = 0.0
    _tripped_at: float = 0.0

    def _beyond(self, v, level):
        return level is not None and (v >= level if self.high else v <= level)

    def _back(self, v, level):
        return v < level if self.high else v > level

    def update(self, m: Meas, now: float, dt: float) -> None:
        v = self.value(m)
        if v is None:
            return
        h = self.hyst if self.high else -self.hyst
        if self._beyond(v, self.l1):
            self.l1_on = True
        elif self.l1_on and self._back(v, self.l1 - h):
            self.l1_on = False

        if self.l2 is None:
            return
        if not self.l2_on:
            if self._beyond(v, self.l2):
                self._pending += dt
                if self._pending >= self.delay_s:
                    self.l2_on, self._tripped_at, self._pending = True, now, 0.0
            else:
                self._pending = 0.0
            return
        if self.retry_s:
            if now - self._tripped_at >= self.retry_s:
                self.l2_on = False
            return
        release = self.release if self.release is not None else self.l2 - h
        if self._back(v, release):
            self.l2_on = False
        elif self.release_on == "charge" and m.current_a > 1.0:
            self.l2_on = False
        elif self.release_on == "discharge" and m.current_a < -1.0:
            self.l2_on = False


def default_rules(spec: PackSpec) -> list[Rule]:
    n = spec.cells
    valid_temps = lambda m: m.max_t is not None  # noqa: E731
    return [
        Rule("cell_volt_high", lambda m: m.max_v, 3.60, 3.75, action="chg", release=3.50,
             hyst=0.05, release_on="discharge"),
        Rule("cell_volt_low", lambda m: m.min_v, 2.80, 2.50, high=False, action="dis",
             release=3.00, hyst=0.10, release_on="charge"),
        Rule("sum_volt_high", lambda m: m.sum_v, 3.60 * n, 3.70 * n, action="chg",
             release=3.45 * n, hyst=0.05 * n, release_on="discharge"),
        Rule("sum_volt_low", lambda m: m.sum_v, 2.90 * n, 2.60 * n, high=False, action="dis",
             release=3.00 * n, hyst=0.10 * n, release_on="charge"),
        Rule("chg_temp_high", lambda m: m.max_t if valid_temps(m) else None, 55, 60,
             action="chg", release=50, hyst=3),
        Rule("chg_temp_low", lambda m: m.min_t if valid_temps(m) else None, 5, 0, high=False,
             action="chg", release=5, hyst=3),
        Rule("dischg_temp_high", lambda m: m.max_t if valid_temps(m) else None, 60, 65,
             action="dis", release=55, hyst=3),
        Rule("dischg_temp_low", lambda m: m.min_t if valid_temps(m) else None, -10, -20,
             high=False, action="dis", release=-15, hyst=3),
        Rule("chg_overcurrent", lambda m: m.current_a, spec.max_charge_a,
             1.2 * spec.max_charge_a, action="chg", retry_s=32, hyst=0.05 * spec.max_charge_a),
        Rule("dischg_overcurrent", lambda m: -m.current_a, spec.max_discharge_a,
             1.2 * spec.max_discharge_a, action="dis", retry_s=32,
             hyst=0.05 * spec.max_discharge_a),
        Rule("soc_low", lambda m: m.soc, 20, 10, high=False, hyst=3),
        Rule("cell_volt_diff", lambda m: m.max_v - m.min_v, 0.30, 0.50, hyst=0.05),
        Rule("temp_diff", lambda m: (m.max_t - m.min_t) if valid_temps(m) else None, 8, 15,
             hyst=2),
    ]


@dataclass
class Firmware:
    spec: PackSpec
    rng: random.Random
    soc: float = 50.0                   # the BMS's own SOC estimate, %
    cycles: int = 0
    discharged_ah: float = 0.0          # towards the next cycle count
    chg_mos: bool = True
    dis_mos: bool = True
    charger: bool = False
    load: bool = False
    balancing: set[int] = field(default_factory=set)
    rules: list[Rule] = field(default_factory=list)
    # injected faults
    open_wire: set[int] = field(default_factory=set)   # 0-based cells with a broken sense lead
    ntc_open: set[int] = field(default_factory=set)    # 0-based temperature sensors
    short: bool = False
    forced_bits: set[str] = field(default_factory=set)
    meas: Meas | None = None
    now: float = 0.0
    life: int = 0
    _offset_a: float = 0.0

    def __post_init__(self):
        self.rules = self.rules or default_rules(self.spec)
        self._offset_a = self.rng.uniform(-0.08, 0.08)   # shunt zero error

    # --- measurement ------------------------------------------------------------
    def measure(self, pack: Pack) -> Meas:
        g = self.rng.gauss
        cells_v = pack.cell_voltages()
        cells_mv = [0 if k in self.open_wire else max(0, round(v * 1000 + g(0, 1.0)))
                    for k, v in enumerate(cells_v)]
        temps = [-daly.TEMP_OFFSET if k in self.ntc_open else round(t + g(0, 0.25))
                 for k, t in enumerate(pack.sensor_temps())]
        good_t = [t for k, t in enumerate(temps) if k not in self.ntc_open]
        good_v = [mv / 1000 for k, mv in enumerate(cells_mv) if k not in self.open_wire]
        current = round(pack.current + self._offset_a + g(0, 0.03), 1)
        sum_v = sum(cells_mv) / 1000
        return Meas(cells_mv=cells_mv, temps_c=temps, current_a=current, sum_v=sum_v,
                    gather_v=pack.voltage() + g(0, 0.02),
                    max_v=max(good_v) if good_v else 0.0, min_v=min(good_v) if good_v else 0.0,
                    max_t=max(good_t) if good_t else None, min_t=min(good_t) if good_t else None,
                    soc=self.soc)

    # --- one control-loop pass --------------------------------------------------
    def update(self, pack: Pack, dt: float, charger: bool, load: bool) -> None:
        self.now += dt
        self.charger, self.load = charger, load
        m = self.measure(pack)

        # coulomb counting against the rated capacity
        rated = self.spec.capacity_ah
        self.soc += m.current_a * dt / 3600 / rated * 100
        if m.current_a < 0:
            self.discharged_ah += -m.current_a * dt / 3600
            while self.discharged_ah >= rated:
                self.discharged_ah -= rated
                self.cycles += 1
        if (self.charger and m.max_v >= FULL_CELL_V
                and 0 <= m.current_a <= 0.05 * rated):
            self.soc = 100.0                                 # end of charge
        self.soc = min(100.0, max(0.0, self.soc))
        m.soc = self.soc

        for r in self.rules:
            r.update(m, self.now, dt)
        rules = {r.key: r for r in self.rules}
        if rules["cell_volt_high"].l2_on:
            self.soc = 100.0
        if rules["cell_volt_low"].l2_on:
            self.soc = 0.0
        m.soc = self.soc

        self.chg_mos = not any(r.l2_on and r.action == "chg" for r in self.rules)
        self.dis_mos = not self.short and not any(
            r.l2_on and r.action == "dis" for r in self.rules)

        self.balancing = set()
        if m.current_a > STATE_THRESHOLD_A and m.max_v >= BALANCE_START_V:
            self.balancing = {k for k, mv in enumerate(m.cells_mv)
                              if k not in self.open_wire and mv / 1000 >= BALANCE_START_V
                              and mv / 1000 - m.min_v >= BALANCE_DIFF_V}
        self.meas = m

    def bleed(self) -> dict[int, float]:
        return {k: BALANCE_A for k in self.balancing}

    def faults(self) -> list[str]:
        names = set(self.forced_bits)
        for r in self.rules:
            if r.l1_on:
                names.add(f"{r.key}_l1")
            if r.l2_on:
                names.add(f"{r.key}_l2")
        if self.open_wire:
            names.add("cell_collect_dropped")
        if self.ntc_open:
            names.add("cell_temp_sensor_err")
        if self.short:
            names.add("short_circuit_fault")
        if self.soc <= 0 and self.meas and self.meas.min_v < 2.5:
            names.add("low_volt_no_charge")
        return sorted(n for n in names if n in FAULT_INDEX)

    def state(self) -> int:
        i = self.meas.current_a if self.meas else 0
        return 1 if i > STATE_THRESHOLD_A else 2 if i < -STATE_THRESHOLD_A else 0

    # --- wire protocol ----------------------------------------------------------
    def answer(self, cmd: int) -> bytes:
        """Response frame(s) to a request, or b"" for commands Daly ignores."""
        m = self.meas
        if m is None:
            return b""
        u16 = lambda v: [(int(v) >> 8) & 0xFF, int(v) & 0xFF]  # noqa: E731
        out = []

        def frame(data):
            out.append(daly.build_response(cmd, bytes(data)))

        n_cells, n_temps = len(m.cells_mv), len(m.temps_c)
        if cmd == daly.CMD_SOC:
            raw_i = min(65535, max(0, round(daly.CURRENT_OFFSET + m.current_a * 10)))
            frame(u16(round(m.sum_v * 10)) + u16(round(m.gather_v * 10)) + u16(raw_i)
                  + u16(round(self.soc * 10)))
        elif cmd == daly.CMD_CELL_MINMAX:
            hi = max(range(n_cells), key=lambda k: m.cells_mv[k])
            lo = min(range(n_cells), key=lambda k: m.cells_mv[k])
            frame(u16(m.cells_mv[hi]) + [hi + 1] + u16(m.cells_mv[lo]) + [lo + 1, 0, 0])
        elif cmd == daly.CMD_TEMP_MINMAX:
            if n_temps:
                hi = max(range(n_temps), key=lambda k: m.temps_c[k])
                lo = min(range(n_temps), key=lambda k: m.temps_c[k])
                frame([m.temps_c[hi] + 40, hi + 1, m.temps_c[lo] + 40, lo + 1, 0, 0, 0, 0])
            else:
                frame([0] * 8)
        elif cmd == daly.CMD_MOS:
            self.life = (self.life + 1) % 256
            mah = round(self.soc / 100 * self.spec.capacity_ah * 1000)
            frame([self.state(), int(self.chg_mos), int(self.dis_mos), self.life,
                   (mah >> 24) & 0xFF, (mah >> 16) & 0xFF, (mah >> 8) & 0xFF, mah & 0xFF])
        elif cmd == daly.CMD_STATUS:
            dio = int(self.charger) | int(self.load) << 1
            frame([n_cells, n_temps, int(self.charger), int(self.load), dio]
                  + u16(self.cycles) + [0])
        elif cmd == daly.CMD_CELL_VOLTAGES:
            for n in range(math.ceil(n_cells / 3)):
                chunk = m.cells_mv[n * 3:n * 3 + 3] + [0, 0, 0]
                frame([n + 1] + u16(chunk[0]) + u16(chunk[1]) + u16(chunk[2]) + [0])
        elif cmd == daly.CMD_TEMPERATURES:
            for n in range(math.ceil(n_temps / 7)):
                chunk = [t + 40 for t in m.temps_c[n * 7:n * 7 + 7]] + [0] * 7
                frame([n + 1] + chunk[:7])
        elif cmd == daly.CMD_BALANCE:
            bits = [0] * 8
            for k in self.balancing:
                bits[k // 8] |= 1 << (k % 8)
            frame(bits)
        elif cmd == daly.CMD_FAULTS:
            bits = [0] * 8
            names = self.faults()
            for name in names:
                b, i = FAULT_INDEX[name]
                bits[b] |= 1 << i
            frame(bits)   # d7 "fault code" is left 0: its meaning is undocumented
        return b"".join(out)
