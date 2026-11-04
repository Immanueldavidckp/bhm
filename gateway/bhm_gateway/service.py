"""Gateway service: poll every Daly BMS, buffer readings, publish over MQTT/TLS."""

from __future__ import annotations

import json
import logging
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
        try:
            data = bms.read_all()
        except DalyError as e:
            self._set_online(bat, False, str(e))
            return
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

    def run(self) -> None:
        if time.gmtime().tm_year < 2025:
            log.warning("system clock looks unset (%s): TLS certificate checks will fail and "
                        "timestamps will be wrong. Enable NTP (timedatectl set-ntp true).", _now())
        log.info("bhm-gateway %s starting: site=%s batteries=%s broker=%s:%d buffered=%d",
                 __version__, self.cfg.gateway.site_id, [b.id for b in self.cfg.batteries],
                 self.cfg.mqtt.host, self.cfg.mqtt.port, len(self.buffer))

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop_event.set())

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
            self.buffer.close()
