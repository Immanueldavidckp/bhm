"""One simulated battery: pack physics + Daly firmware + what is connected to it."""

from __future__ import annotations

import math
import random

from .firmware import FAULT_INDEX, Firmware
from .model import CHARGER_CELL_V, Pack, PackSpec
from .profiles import PROFILES

FAULT_HELP = ("short | open_wire:<cell> | ntc_open:<sensor> | weak:<cell>[:<capacity %>] | "
              "leak:<cell> | <any Daly fault bit name>")


class SimBattery:
    def __init__(self, spec: PackSpec, rng: random.Random):
        if spec.profile not in PROFILES:
            raise ValueError(f"{spec.name}: unknown profile {spec.profile!r} "
                             f"(use {', '.join(PROFILES)})")
        self.spec = spec
        self.rng = rng
        self.pack = Pack(spec, rng)
        cycles = spec.cycles if spec.cycles is not None else rng.randint(5, 300)
        self.bms = Firmware(spec, rng, soc=self.pack.soc(), cycles=cycles)
        self.profile = PROFILES[spec.profile](self, rng)
        self.sim_t = 0.0
        self.ambient_c = spec.ambient_c
        self.comm = {"online": True, "drop": 0.0, "corrupt": 0.0, "latency": 0.04}
        self.requests = 0
        self.bms.update(self.pack, 0.0, charger=False, load=False)

    @property
    def name(self) -> str:
        return self.spec.name

    def ambient(self) -> float:
        """Set ambient plus a +/-2.5 °C day/night swing."""
        return self.ambient_c + 2.5 * math.sin(2 * math.pi * self.sim_t / 86400)

    def step(self, dt: float) -> None:
        kind, amount = self.profile.demand(dt)
        current = 0.0
        if kind == "charge" and self.bms.chg_mos:
            current = self.pack.cv_current(CHARGER_CELL_V * self.spec.cells, amount)
        elif kind == "load" and self.bms.dis_mos:
            current = -amount
        elif kind == "power" and self.bms.dis_mos:
            current = -self.pack.power_current(amount)
        self.pack.step(current, dt, self.ambient(), self.bms.bleed())
        self.bms.update(self.pack, dt, charger=kind == "charge", load=kind in ("load", "power"))
        self.sim_t += dt

    # --- control ----------------------------------------------------------------
    def set(self, key: str, value: str) -> None:
        if key == "profile":
            if value not in PROFILES:
                raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
            self.spec.profile = value
            self.profile = PROFILES[value](self, self.rng)
        elif key == "soc":
            soc = float(value) / 100
            for c in self.pack.cells:
                c.soc, c.v1 = soc, 0.0
            self.bms.soc = float(value)
        elif key == "bms_soc":
            self.bms.soc = float(value)
        elif key == "ambient":
            self.ambient_c = float(value)
        elif key == "temp":
            self.pack.temp_c = float(value)
        elif key in ("load_a", "charge_a", "max_charge_a", "max_discharge_a"):
            setattr(self.spec, key, float(value))
        elif key == "cycles":
            self.bms.cycles = int(value)
        else:
            raise ValueError(f"unknown setting {key!r} (profile, soc, bms_soc, ambient, temp, "
                             "load_a, charge_a, max_charge_a, max_discharge_a, cycles)")

    def _cell(self, arg: str, count: int, what: str) -> int:
        k = int(arg) - 1
        if not 0 <= k < count:
            raise ValueError(f"{what} must be 1..{count}")
        return k

    def fault(self, text: str) -> None:
        name, *args = text.split(":")
        n = self.spec.cells
        if name == "short":
            self.bms.short = True
        elif name == "open_wire":
            self.bms.open_wire.add(self._cell(args[0], n, "cell"))
        elif name == "ntc_open":
            self.bms.ntc_open.add(self._cell(args[0], self.spec.temp_sensors, "sensor"))
        elif name == "weak":
            k = self._cell(args[0], n, "cell")
            pct = float(args[1]) if len(args) > 1 else 70.0
            self.pack.cells[k].capacity_ah = self.pack.nominal_capacity[k] * pct / 100
        elif name == "leak":
            self.pack.cells[self._cell(args[0], n, "cell")].leak_per_s = 0.05 / 86400  # 5 %/day
        elif name in FAULT_INDEX:
            self.bms.forced_bits.add(name)
        else:
            raise ValueError(f"unknown fault {name!r}: {FAULT_HELP}")

    def clear(self, what: str = "all") -> None:
        b = self.bms
        if what in ("all", "short"):
            b.short = False
        if what in ("all", "open_wire"):
            b.open_wire.clear()
        if what in ("all", "ntc_open"):
            b.ntc_open.clear()
        if what in ("all", "weak", "leak"):
            for c, cap in zip(self.pack.cells, self.pack.nominal_capacity):
                if what != "leak":
                    c.capacity_ah = cap
                if what != "weak":
                    c.leak_per_s = min(c.leak_per_s, 0.03 / (30 * 86400))
        if what == "all":
            b.forced_bits.clear()
            for r in b.rules:   # like a BMS reset: unlatch every protection
                r.l1_on = r.l2_on = False
        else:
            b.forced_bits.discard(what)

    def status(self) -> dict:
        m = self.bms.meas
        return {
            "name": self.name, "cells": self.spec.cells, "nominal_v": self.spec.nominal_v,
            "rated_ah": self.spec.capacity_ah, "profile": self.spec.profile,
            "phase": self.profile.phase, "sim_time_h": round(self.sim_t / 3600, 2),
            "charge_a": self.spec.charge_a, "load_a": self.spec.load_a,
            "max_charge_a": self.spec.max_charge_a, "max_discharge_a": self.spec.max_discharge_a,
            "voltage_v": round(self.pack.voltage(), 2), "current_a": m.current_a,
            "bms_soc": round(self.bms.soc, 1), "true_soc": round(self.pack.soc(), 1),
            "cells_mv": m.cells_mv, "temps_c": m.temps_c, "ambient_c": round(self.ambient(), 1),
            "cell_soc": [round(c.soc * 100, 1) for c in self.pack.cells],
            "cell_ah": [round(c.capacity_ah, 1) for c in self.pack.cells],
            "chg_mos": self.bms.chg_mos, "dis_mos": self.bms.dis_mos,
            "balancing": sorted(k + 1 for k in self.bms.balancing),
            "cycles": self.bms.cycles, "faults": self.bms.faults(), "comm": dict(self.comm),
            "requests": self.requests,
        }

    def line(self) -> str:
        m = self.bms.meas
        valid = [mv for mv in m.cells_mv if mv] or [0]
        t = f"{m.max_t:.0f}°C" if m.max_t is not None else "--"
        mos = f"C{'+' if self.bms.chg_mos else '-'}D{'+' if self.bms.dis_mos else '-'}"
        state = ("idle", "charge", "dischg")[self.bms.state()]
        f = ",".join(self.bms.faults()) or "-"
        return (f"{self.name:<8} {self.pack.voltage():6.2f}V {m.current_a:+7.1f}A "
                f"SOC {self.bms.soc:5.1f}% (true {self.pack.soc():5.1f}) "
                f"cells {min(valid)}-{max(valid)}mV Δ{max(valid) - min(valid):<3} {t:>5} "
                f"{state:<6} {self.profile.phase:<10} {mos} {f}")

    # --- persistence (survive restarts like the BMS EEPROM) -----------------------
    def save(self) -> dict:
        return {"cells": self.spec.cells, "cell_soc": [c.soc for c in self.pack.cells],
                "bms_soc": self.bms.soc, "cycles": self.bms.cycles,
                "discharged_ah": self.bms.discharged_ah, "temp_c": self.pack.temp_c}

    def restore(self, d: dict) -> bool:
        if d.get("cells") != self.spec.cells:
            return False
        for c, soc in zip(self.pack.cells, d["cell_soc"]):
            c.soc = soc
        self.bms.soc = d["bms_soc"]
        self.bms.cycles = d["cycles"]
        self.bms.discharged_ah = d.get("discharged_ah", 0.0)
        self.pack.temp_c = d.get("temp_c", self.pack.temp_c)
        self.profile = PROFILES[self.spec.profile](self, self.rng)
        self.bms.update(self.pack, 0.0, charger=False, load=False)
        return True
