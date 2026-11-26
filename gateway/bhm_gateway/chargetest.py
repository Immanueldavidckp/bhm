"""Charge test with simulated data: one 4-hour bench charge of a 12 V 100 Ah LiFePO4 kit.

The scenario runs in the simulator (sim/) much faster than real time, is read with the real
Daly decoders every 10 s of simulated time (the gateway's poll interval), and goes through
the real gateway code (envelope, seq, online/offline, alarms, store-and-forward buffer), so
the messages are exactly what the board would publish. Their timestamps are spread over
the hours before --end (default: now), so the dashboard shows a real 4-hour charge.

  python3 -m bhm_gateway --config laptop.toml chargetest            publish to the broker
  python3 -m bhm_gateway chargetest --out msgs.jsonl --dry-run       only write the messages

Scenario (default spec): 4S 100 Ah, starts at 10 % after a discharge, 24 °C ambient,
10 min rest, 25 A CC/CV charger (14.4 V), a 2 min charger pause at 50 % SOC, the charger
stops at 5 A, 15 min rest. Cell 3 is aged (92 % of the others' capacity) and cell 2 has
1.6x the internal resistance, so the health analysis has something to find.
"""

from __future__ import annotations

import json
import logging
import math
import random
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import daly, service
from .config import BatteryConfig, Config, GatewayConfig, MqttConfig
from .daly import DalyBMS, DalyError
from .publisher import Publisher
from .sim import SimBattery, parse_spec

log = logging.getLogger(__name__)

BATTERY_ID = "chargetest12v"
SPEC = ("{id}:4,ah=100,soc=10.5,cycles=42,ambient=24,charge_a=25,profile=chargetest,"
        "weak=3:92,hir=2:1.6")
POLL_S = 10
PHYSICS_DT = 1.0
MAX_HOURS = 8


class SimLink(DalyBMS):
    """A DalyBMS whose 'serial port' is the simulated BMS firmware (same frames, same decoders)."""

    def __init__(self, sim: SimBattery):
        super().__init__(port=f"sim:{sim.name}")
        self.sim = sim

    def request(self, cmd: int, frames: int = 1) -> list[bytes]:
        got = daly.parse_frames(self.sim.bms.answer(cmd), cmd)
        if len(got) < frames:
            raise DalyError(f"sim answered {len(got)} of {frames} frames for 0x{cmd:02X}")
        return got[:frames]


class Replay:
    """Hands one recorded snapshot to Gateway.poll_once."""

    def __init__(self, data: dict):
        self.data = data

    def read_all(self) -> dict:
        return self.data


def dcir_truth_mohm(sim: SimBattery, seconds: float = POLL_S) -> list[float]:
    """What a step measured `seconds` after the step should give: r0 + r1 * (1 - e^-t/tau)."""
    return [1000 * (c.r0 + c.r1 * (1 - math.exp(-seconds / c.tau_s))) for c in sim.pack.cells]


def simulate(spec: str, seed: int = 7) -> tuple[SimBattery, list[tuple[float, dict]], dict]:
    """Run the scenario; return the battery, [(sim seconds, decoded snapshot)] and the truth."""
    sim = SimBattery(parse_spec(spec), random.Random(seed))
    link = SimLink(sim)
    truth = {"cell_capacity_ah": [round(c.capacity_ah, 2) for c in sim.pack.cells],
             "cell_r0_mohm": [round(c.r0 * 1000, 3) for c in sim.pack.cells],
             "cell_dcir_10s_mohm": [round(x, 3) for x in dcir_truth_mohm(sim)],
             "start_cell_soc_pct": [round(c.soc * 100, 1) for c in sim.pack.cells],
             "rated_ah": sim.spec.capacity_ah}
    snaps = [(0.0, link.read_all())]
    t, next_poll = 0.0, POLL_S
    while not getattr(sim.profile, "done", False) and t < MAX_HOURS * 3600:
        sim.step(PHYSICS_DT)
        t += PHYSICS_DT
        if t >= next_poll - 1e-9:
            snaps.append((t, link.read_all()))
            next_poll += POLL_S
    truth["duration_s"] = t
    return sim, snaps, truth


def build_messages(cfg: Config, snaps, end: float, buffer_path: str) -> service.Gateway:
    """Feed the snapshots through the real gateway with its clock set to the simulated time."""
    start = end - snaps[-1][0]
    clock = {"t": start}
    real_now = service._now
    service._now = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock["t"]))
    try:
        cfg = replace(cfg, gateway=replace(cfg.gateway, buffer_path=buffer_path,
                                           buffer_max_rows=100_000, status_path=""))
        gw = service.Gateway(cfg)
        bat = cfg.batteries[0]
        for t, data in snaps:
            clock["t"] = start + t
            gw.poll_once(bat, Replay(data))
    finally:
        service._now = real_now
    return gw


def publish(gw: service.Gateway, cfg: Config, bat_id: str, timeout_s: float = 30) -> int:
    pub = Publisher(cfg.mqtt, gw.buffer, gw.topics.status(bat_id))
    pub.start()
    try:
        if not pub.connected.wait(timeout_s):
            raise RuntimeError(f"cannot connect to MQTT broker {cfg.mqtt.host}:{cfg.mqtt.port}"
                               f" ({pub.last_error or 'timeout'})")
        total = len(gw.buffer)
        while len(gw.buffer):
            if not pub.drain(batch=100) and not pub.connected.is_set():
                raise RuntimeError(f"broker connection lost, {len(gw.buffer)} not sent")
            print(f"\r  sent {pub.sent}/{total}", end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)
        return pub.sent
    finally:
        pub.stop()


def main(args) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    spec = args.spec or SPEC.format(id=args.battery)
    if not spec.startswith(args.battery + ":"):
        print("the spec must start with the battery id (--battery)", file=sys.stderr)
        return 2
    end = (datetime.fromisoformat(args.end.replace("Z", "+00:00")).timestamp()
           if args.end else time.time())

    sim, snaps, truth = simulate(spec, args.seed)
    t_end = truth["duration_s"]
    print(f"simulated {t_end / 3600:.2f} h ({t_end / 60:.0f} min): {len(snaps)} readings, "
          f"one per {POLL_S} s", file=sys.stderr)

    if args.dry_run:
        mq = MqttConfig(host="dry-run", client_id="bhm-chargetest")
        cfg = Config(GatewayConfig(site_id=args.site), mq, [])
    else:
        from .config import load
        cfg = load(args.config)
        cfg = replace(cfg, mqtt=replace(cfg.mqtt, client_id=f"{cfg.mqtt.client_id}-chargetest"))
    bat = BatteryConfig(id=args.battery, port="sim", nominal_voltage=sim.spec.nominal_v)
    cfg = replace(cfg, batteries=[bat])

    with tempfile.TemporaryDirectory() as tmp:
        gw = build_messages(cfg, snaps, end, str(Path(tmp) / "buffer.db"))
        rows = gw.buffer.peek(len(gw.buffer) + 1)
        size = sum(len(p) for _, _, p, _, _ in rows)
        kinds: dict[str, int] = {}
        for _, topic, _, _, _ in rows:
            kinds[topic.rsplit("/", 1)[1]] = kinds.get(topic.rsplit("/", 1)[1], 0) + 1
        first = json.loads(rows[0][2])["ts"] if rows else "-"
        print(f"{len(rows)} messages ({', '.join(f'{v} {k}' for k, v in kinds.items())}), "
              f"{size / 1024:.0f} KB of JSON, {first} .. "
              f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(end))}", file=sys.stderr)
        if args.out:
            with open(args.out, "w") as f:
                for _, topic, payload, _, retain in rows:
                    f.write(json.dumps({"topic": topic, "retain": retain,
                                        "msg": json.loads(payload)}) + "\n")
            print(f"wrote {args.out}", file=sys.stderr)
        if args.truth:
            Path(args.truth).write_text(json.dumps(truth, indent=1))
        if not args.dry_run:
            sent = publish(gw, cfg, args.battery)
            print(f"published {sent} messages to {cfg.mqtt.host}:{cfg.mqtt.port} as site "
                  f"{cfg.gateway.site_id}", file=sys.stderr)
        gw.buffer.close()
    print("truth (what the analysis should find): " + json.dumps(truth), file=sys.stderr)
    return 0
