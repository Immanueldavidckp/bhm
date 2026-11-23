"""What is connected to each simulated pack: charger and load behaviour over time.

A profile returns what the outside world wants each step:
    ("idle", 0)  ("load", amps)  ("power", watts)  ("charge", cc_amps)
The engine then applies the BMS MOSFET state, so a tripped BMS really cuts the current.

  cycle      discharge at load_a down to 3.0 V/cell, rest, CC/CV charge to full, rest, repeat
  drive      inverter-style random loads (idle .. 0.85 C surges), recharge when low
  charge     charger permanently connected (CC/CV, then holds at CV)
  discharge  constant load_a until the BMS cuts it off
  idle       nothing connected
"""

from __future__ import annotations

import random

REST_S = 20 * 60


class Profile:
    name = ""

    def __init__(self, battery, rng: random.Random):
        self.b = battery
        self.rng = rng
        self.phase = self.name
        self.t = 0.0

    def go(self, phase: str) -> None:
        self.phase, self.t = phase, 0.0

    def demand(self, dt: float) -> tuple[str, float]:
        raise NotImplementedError

    # helpers -----------------------------------------------------------------
    def low(self, per_cell: float) -> bool:
        return self.b.pack.voltage() <= per_cell * self.b.spec.cells or not self.b.bms.dis_mos

    def charge_finished(self) -> bool:
        """Charger tail-current cut-off (or the BMS refused the charge)."""
        if self.t < 60:
            return False
        tail = 0.05 * self.b.spec.capacity_ah
        return self.b.pack.current < tail


class Idle(Profile):
    name = "idle"

    def demand(self, dt):
        return "idle", 0.0


class Charge(Profile):
    name = "charge"

    def demand(self, dt):
        return "charge", self.b.spec.charge_a


class Discharge(Profile):
    name = "discharge"

    def demand(self, dt):
        return "load", self.b.spec.load_a


class Cycle(Profile):
    name = "cycle"

    def __init__(self, battery, rng):
        super().__init__(battery, rng)
        self.go("discharge" if battery.pack.soc() > 50 else "charge")

    def demand(self, dt):
        self.t += dt
        if self.phase == "discharge" and self.low(3.00):
            self.go("rest_empty")
        elif self.phase == "rest_empty" and self.t >= REST_S:
            self.go("charge")
        elif self.phase == "charge" and self.charge_finished():
            self.go("rest_full")
        elif self.phase == "rest_full" and self.t >= REST_S:
            self.go("discharge")

        if self.phase == "discharge":
            return "load", self.b.spec.load_a
        if self.phase == "charge":
            return "charge", self.b.spec.charge_a
        return "idle", 0.0


class Drive(Profile):
    name = "drive"
    # (C-rate, weight): inverter feeding lights, fans, a motor, the odd surge
    LEVELS = ((0.0, 25), (0.05, 25), (0.15, 25), (0.35, 15), (0.6, 8), (0.85, 2))

    def __init__(self, battery, rng):
        super().__init__(battery, rng)
        self.watts = 0.0
        self.seg_left = 0.0
        self.go("drive")

    def demand(self, dt):
        self.t += dt
        if self.phase == "drive" and self.low(3.05):
            self.go("recharge")
        elif self.phase == "recharge" and self.charge_finished():
            self.go("drive")
            self.seg_left = 0
        if self.phase == "recharge":
            return "charge", self.b.spec.charge_a

        self.seg_left -= dt
        if self.seg_left <= 0:
            rates, weights = zip(*self.LEVELS)
            c = self.rng.choices(rates, weights)[0]
            self.seg_left = self.rng.uniform(15, 40) if c >= 0.6 else self.rng.uniform(60, 900)
            self.watts = c * self.b.spec.capacity_ah * self.b.spec.nominal_v
        return ("power", self.watts) if self.watts else ("idle", 0.0)


PROFILES = {p.name: p for p in (Idle, Charge, Discharge, Cycle, Drive)}
