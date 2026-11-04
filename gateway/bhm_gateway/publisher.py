"""MQTT-over-TLS publisher that drains the persistent buffer."""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from .buffer import MessageBuffer
from .config import MqttConfig

log = logging.getLogger(__name__)


def make_tls_context(cfg: MqttConfig) -> ssl.SSLContext:
    """TLS >= 1.2, server certificate and hostname always verified.

    With no ca_file the system trust store is used (e.g. a Let's Encrypt
    certificate on the broker). With cert_file/key_file the gateway also
    authenticates itself with a client certificate (mutual TLS).
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cfg.ca_file or None)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if cfg.cert_file:
        ctx.load_cert_chain(cfg.cert_file, cfg.key_file)
    return ctx


class Publisher:
    def __init__(self, cfg: MqttConfig, buffer: MessageBuffer, status_topic: str):
        self.cfg = cfg
        self.buffer = buffer
        self.status_topic = status_topic
        self.connected = threading.Event()
        self.sent = 0

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.client_id,
                        protocol=mqtt.MQTTv311, clean_session=True)
        c.tls_set_context(make_tls_context(cfg))
        if cfg.username:
            c.username_pw_set(cfg.username, cfg.password or None)
        c.will_set(status_topic, json.dumps({"state": "offline", "reason": "connection_lost"}),
                   qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=120)
        c.max_inflight_messages_set(50)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_connect_fail = self._on_connect_fail
        self._last_fail_log = 0.0
        self.client = c

    # -- callbacks (run on paho's network thread) --
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect refused: %s", reason_code)
            return
        log.info("MQTT connected to %s:%d", self.cfg.host, self.cfg.port)
        client.publish(self.status_topic,
                       json.dumps({"state": "online", "ts": _now()}), qos=1, retain=True)
        self.connected.set()

    def _on_connect_fail(self, client, userdata):
        if time.monotonic() - self._last_fail_log > 60:
            self._last_fail_log = time.monotonic()
            log.warning("cannot reach MQTT broker %s:%d (network, DNS, TLS certificate or "
                        "clock problem); buffering locally, %d queued",
                        self.cfg.host, self.cfg.port, len(self.buffer))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        if reason_code.is_failure:
            log.warning("MQTT disconnected: %s (will retry)", reason_code)

    # -- lifecycle --
    def start(self) -> None:
        self.client.connect_async(self.cfg.host, self.cfg.port, keepalive=self.cfg.keepalive_s)
        self.client.loop_start()

    def stop(self) -> None:
        if self.connected.is_set():
            info = self.client.publish(
                self.status_topic, json.dumps({"state": "offline", "reason": "shutdown",
                                               "ts": _now()}), qos=1, retain=True)
            try:
                info.wait_for_publish(timeout=3)
            except (RuntimeError, ValueError):
                pass
        self.client.disconnect()
        self.client.loop_stop()

    def drain(self, batch: int = 50, ack_timeout: float = 15.0) -> int:
        """Publish up to `batch` buffered messages; delete the acknowledged ones."""
        if not self.connected.is_set():
            return 0
        rows = self.buffer.peek(batch)
        if not rows:
            return 0
        pending = []
        for rid, topic, payload, qos, retain in rows:
            info = self.client.publish(topic, payload, qos=qos, retain=retain)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                break
            pending.append((rid, info))
        done = []
        deadline = time.monotonic() + ack_timeout
        for rid, info in pending:
            try:
                info.wait_for_publish(timeout=max(0.0, deadline - time.monotonic()))
            except (RuntimeError, ValueError):
                break
            if not info.is_published():
                break
            done.append(rid)
        self.buffer.delete(done)
        self.sent += len(done)
        return len(done)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
