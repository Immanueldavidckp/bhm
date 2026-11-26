"""Gateway service: poll every Daly BMS, buffer readings, publish over MQTT/TLS."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import uuid
from collections import defaultdict

from . import __version__
from .buffer import MessageBuffer
from .config import BatteryConfig, Config
from .daly import DalyBMS, DalyError
from .publisher import Publisher, _now

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
HEALTH_INTERVAL_S = 60
NTP_SYNCED_FLAG = "/run/systemd/timesync/synchronized"   # created by systemd-timesyncd


def clock_floor() -> float:
    """The clock cannot be earlier than when this code was installed."""
    pkg = os.path.dirname(__file__)
    return max(os.path.getmtime(os.path.join(pkg, f)) for f in os.listdir(pkg)
               if f.endswith(".py"))


def clock_ok(floor: float | None = None, flag: str = NTP_SYNCED_FLAG) -> bool:
    """True once NTP has synced, or the clock is at least past the install time.

    Boards without an RTC battery boot with a clock months or years off; readings stamped
    then are wrong forever and TLS rejects the broker certificate, so wait instead.
    """
    if os.path.exists(flag):
        return True
    return time.time() >= (clock_floor() if floor is None else floor)


class Topics:
    def __init__(self, prefix: str, site: str):
        self.base = f"{prefix}/{site}"

    def telemetry(self, bat: str) -> str:
        return f"{self.base}/{bat}/telemetry"

    def status(self, bat: str) -> str:
        return f"{self.base}/{bat}/status"

    def alarm(self, bat: str) -> str:
        return f"{self.base}/{bat}/alarm"

    @property
    def gateway_status(self) -> str:
        return f"{self.base}/gateway/status"

    @property
    def gateway_health(self) -> str:
        return f"{self.base}/gateway/health"


class Gateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.boot_id = uuid.uuid4().hex[:12]
        self.started = time.monotonic()
        self.topics = Topics(cfg.mqtt.topic_prefix, cfg.gateway.site_id)
        self.buffer = MessageBuffer(cfg.gateway.buffer_path, cfg.gateway.buffer_max_rows)
        self.publisher = Publisher(cfg.mqtt, self.buffer, self.topics.gateway_status)
        self._seq: dict[str, int] = defaultdict(int)
        self._online: dict[str, bool | None] = {b.id: None for b in cfg.batteries}
        self._faults: dict[str, frozenset] = {}
        # per battery, for the status screen: last good reading and the latest error
        self._live: dict[str, dict] = {b.id: {"reads": 0, "fails": 0, "last_ok": None,
                                              "last_try": None, "error": None, "data": None}
                                       for b in cfg.batteries}
        self.waiting_for_clock = False
        self._status_err_logged = False

    # ---------------------------------------------------------------- polling
    def _enqueue(self, topic: str, obj: dict, retain: bool = False) -> None:
        payload = json.dumps(obj, separators=(",", ":")).encode()
        self.buffer.put(topic, payload, qos=self.cfg.mqtt.qos, retain=retain)

    def _envelope(self, bat: BatteryConfig) -> dict:
        self._seq[bat.id] += 1
        return {
            "schema": SCHEMA_VERSION,
            "site": self.cfg.gateway.site_id,
            "battery_id": bat.id,
            "nominal_voltage": bat.nominal_voltage,
            "chemistry": bat.chemistry,
            "ts": _now(),
            "boot_id": self.boot_id,
            "seq": self._seq[bat.id],
        }

    def _set_online(self, bat: BatteryConfig, online: bool, error: str | None = None) -> None:
        if self._online[bat.id] == online:
            return
        self._online[bat.id] = online
        msg = {"state": "online" if online else "offline", "ts": _now()}
        if error:
            msg["error"] = error
        (log.info if online else log.warning)("battery %s %s%s", bat.id, msg["state"],
                                               f": {error}" if error else "")
        self._enqueue(self.topics.status(bat.id), msg, retain=True)

    def poll_once(self, bat: BatteryConfig, bms: DalyBMS) -> None:
        live = self._live[bat.id]
        live["last_try"] = time.monotonic()
        try:
            data = bms.read_all()
        except DalyError as e:
            live["fails"] += 1
            live["error"] = str(e)
            self._set_online(bat, False, str(e))
            return
        live.update(reads=live["reads"] + 1, last_ok=time.monotonic(), error=None, data=data)
        self._set_online(bat, True)
        msg = self._envelope(bat) | data
        self._enqueue(self.topics.telemetry(bat.id), msg)

        faults = frozenset(data["faults"])
        prev = self._faults.get(bat.id, frozenset())
        if faults != prev:
            self._faults[bat.id] = faults
            alarm = self._envelope(bat) | {
                "active": sorted(faults), "raised": sorted(faults - prev),
                "cleared": sorted(prev - faults), "fault_code": data["fault_code"]}
            self._enqueue(self.topics.alarm(bat.id), alarm)
            log.warning("battery %s faults: raised=%s cleared=%s",
                        bat.id, alarm["raised"], alarm["cleared"])

    def _port_worker(self, bats: list[BatteryConfig]) -> None:
        """One thread per serial port; batteries sharing a port are polled in turn."""
        bmss = [(b, DalyBMS(b.port, address=b.address, baudrate=b.baudrate,
                            timeout=b.timeout_s, current_sign=b.current_sign)) for b in bats]
        interval = self.cfg.gateway.poll_interval_s
        next_t = time.monotonic()
        while not self.stop_event.is_set():
            for bat, bms in bmss:
                if self.stop_event.is_set():
                    break
                try:
                    self.poll_once(bat, bms)
                except Exception:
                    log.exception("unexpected error polling %s", bat.id)
            next_t += interval
            delay = next_t - time.monotonic()
            if delay < 0:  # overran; don't try to catch up in a burst
                next_t = time.monotonic()
                delay = 0
            self.stop_event.wait(delay)
        for _, bms in bmss:
            bms.close()

    # ---------------------------------------------------------------- main
    def _health(self) -> dict:
        return {
            "ts": _now(), "version": __version__, "boot_id": self.boot_id,
            "uptime_s": int(time.monotonic() - self.started),
            "buffered": len(self.buffer), "sent": self.publisher.sent,
            "dropped": self.buffer.dropped,
            "batteries": {k: ("online" if v else "offline" if v is False else "unknown")
                          for k, v in self._online.items()},
        }

    # ---------------------------------------------------------------- status file
    def status(self) -> dict:
        """Live state for the LCD screen and `bhm_gateway status`.

        Times are time.monotonic() values: CLOCK_MONOTONIC is system-wide on Linux, so a
        reader on the same board computes ages that do not jump when NTP sets the clock.
        """
        pub = self.publisher
        bats = []
        for b in self.cfg.batteries:
            live = self._live[b.id]
            d = live["data"]
            ent = {
                "id": b.id, "port": b.port, "nominal_voltage": b.nominal_voltage,
                "state": {True: "online", False: "offline", None: "unknown"}[self._online[b.id]],
                "reads": live["reads"], "fails": live["fails"],
                "last_ok": live["last_ok"], "last_try": live["last_try"],
                "error": live["error"],
                "last_upload": pub.last_ack_by_topic.get(self.topics.telemetry(b.id)),
            }
            if d:
                pk, cl, tp = d["pack"], d["cells"], d["temps"]
                ent.update({
                    "soc_pct": pk.get("soc_pct"), "voltage_v": pk.get("voltage_v"),
                    "current_a": pk.get("current_a"), "power_w": pk.get("power_w"),
                    "pack_state": pk.get("state"), "cycles": pk.get("cycles"),
                    "cell_count": cl["count"], "cell_min_mv": cl["min_mv"],
                    "cell_max_mv": cl["max_mv"], "cell_delta_mv": cl["delta_mv"],
                    "temp_max_c": tp["max_c"], "temp_min_c": tp["min_c"],
                    "faults": list(d["faults"]),
                })
            bats.append(ent)
        return {
            "schema": 1, "mono": time.monotonic(), "time": time.time(),
            "version": __version__, "site": self.cfg.gateway.site_id, "boot_id": self.boot_id,
            "pid": os.getpid(), "uptime_s": int(time.monotonic() - self.started),
            "poll_interval_s": self.cfg.gateway.poll_interval_s,
            "clock_ok": clock_ok(), "waiting_for_clock": self.waiting_for_clock,
            "mqtt": {
                "host": self.cfg.mqtt.host, "port": self.cfg.mqtt.port,
                "client_id": self.cfg.mqtt.client_id,
                "connected": pub.connected.is_set(), "since": pub.connected_since,
                "last_error": pub.last_error, "sent": pub.sent, "last_sent": pub.last_sent,
                "queued": len(self.buffer), "dropped": self.buffer.dropped,
            },
            "batteries": bats,
        }

    def write_status(self) -> None:
        path = self.cfg.gateway.status_path
        if not path:
            return
        try:
            tmp = f"{path}.tmp"
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self.status(), f, separators=(",", ":"))
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)       # readers never see a half-written file
        except Exception as e:
            if not self._status_err_logged:
                self._status_err_logged = True
                log.warning("cannot write status file %s: %s", path, e)

    def _status_loop(self) -> None:
        while not self.stop_event.is_set():
            self.write_status()
            self.stop_event.wait(1.0)

    def wait_for_clock(self) -> None:
        if clock_ok():
            return
        self.waiting_for_clock = True
        log.warning("system clock is wrong (%s, before this software was installed): waiting "
                    "for NTP before reading batteries. Check network and systemd-timesyncd.",
                    _now())
        waited = 0
        while not self.stop_event.wait(5):
            waited += 5
            if clock_ok():
                log.info("clock set by NTP (%s) after %d s", _now(), waited)
                self.waiting_for_clock = False
                return
            if waited % 300 == 0:
                log.warning("still waiting for a correct clock (%s)", _now())

    def run(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop_event.set())
        status_thread = threading.Thread(target=self._status_loop, name="status", daemon=True)
        status_thread.start()
        self.wait_for_clock()
        if self.stop_event.is_set():
            return
        log.info("bhm-gateway %s starting: site=%s batteries=%s broker=%s:%d buffered=%d",
                 __version__, self.cfg.gateway.site_id, [b.id for b in self.cfg.batteries],
                 self.cfg.mqtt.host, self.cfg.mqtt.port, len(self.buffer))

        self.publisher.start()
        by_port: dict[str, list[BatteryConfig]] = defaultdict(list)
        for b in self.cfg.batteries:
            by_port[b.port].append(b)
        workers = [threading.Thread(target=self._port_worker, args=(bats,),
                                    name=f"poll:{port}", daemon=True)
                   for port, bats in by_port.items()]
        for w in workers:
            w.start()

        next_health = time.monotonic() + 5
        try:
            while not self.stop_event.is_set():
                if time.monotonic() >= next_health:
                    self._enqueue(self.topics.gateway_health, self._health())
                    next_health = time.monotonic() + HEALTH_INTERVAL_S
                if self.publisher.drain() == 0:
                    self.stop_event.wait(0.5)
        finally:
            log.info("stopping")
            self.stop_event.set()
            for w in workers:
                w.join(timeout=5)
            self.publisher.drain(ack_timeout=3)
            self.publisher.stop()
            status_thread.join(timeout=2)
            if self.cfg.gateway.status_path:
                try:
                    os.unlink(self.cfg.gateway.status_path)   # screen shows "not running"
                except OSError:
                    pass
            self.buffer.close()
