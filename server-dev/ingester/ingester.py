"""BHM ingester: subscribe to bhm/# over MQTT/TLS and write everything into MySQL.

Uses a persistent MQTT session (clean_session=False, QoS 1) and acknowledges a message
only after its database transaction commits, so nothing is lost while the ingester or the
database is down; the broker keeps the backlog. Duplicates are ignored by unique keys.

Configuration (environment):
  MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASSWORD MQTT_CA MQTT_CERT MQTT_KEY MQTT_CLIENT_ID
  MQTT_TOPIC_PREFIX  MYSQL_HOST MYSQL_PORT MYSQL_USER MYSQL_PASSWORD MYSQL_DATABASE
"""

from __future__ import annotations

import json
import logging
import os
import signal
import ssl
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import pymysql

log = logging.getLogger("ingester")
env = os.environ.get
PREFIX = env("MQTT_TOPIC_PREFIX", "bhm")


def ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc).replace(tzinfo=None)


def j(value) -> str:
    return json.dumps(value if value is not None else [])


class Store:
    def __init__(self):
        self.conn = None

    def connect(self):
        while True:
            try:
                self.conn = pymysql.connect(
                    host=env("MYSQL_HOST", "mysql"), port=int(env("MYSQL_PORT", "3306")),
                    user=env("MYSQL_USER", "bhm"), password=env("MYSQL_PASSWORD", ""),
                    database=env("MYSQL_DATABASE", "bhm"), autocommit=False, charset="utf8mb4")
                log.info("connected to MySQL")
                return
            except pymysql.MySQLError as e:
                log.warning("MySQL not ready (%s), retrying in 3 s", e)
                time.sleep(3)

    def write(self, topic: str, msg: dict) -> None:
        """Insert one message in one transaction; reconnects and retries on DB errors."""
        for attempt in range(5):
            try:
                if self.conn is None or not self.conn.open:
                    self.connect()
                with self.conn.cursor() as cur:
                    self._dispatch(cur, topic, msg)
                self.conn.commit()
                return
            except pymysql.err.OperationalError as e:
                log.warning("database error (%s), reconnecting", e)
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError("database unavailable")

    def _dispatch(self, cur, topic: str, m: dict) -> None:
        parts = topic.split("/")          # bhm/<site>/<source>/<kind>
        if len(parts) != 4 or parts[0] != PREFIX:
            log.debug("ignoring %s", topic)
            return
        _, site, source, kind = parts
        if source == "gateway":
            if kind == "status":
                self._status(cur, site, "gateway", m)
            elif kind == "health":
                cur.execute(
                    "INSERT INTO gateway_health (site, ts, boot_id, version, uptime_s, buffered,"
                    " sent, dropped, batteries) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (site, ts(m.get("ts")), m.get("boot_id"), m.get("version"), m.get("uptime_s"),
                     m.get("buffered"), m.get("sent"), m.get("dropped"),
                     json.dumps(m.get("batteries", {}))))
        elif kind == "telemetry":
            self._telemetry(cur, site, source, m)
        elif kind == "alarm":
            cur.execute(
                "INSERT IGNORE INTO alarm (site, battery_id, ts, active, raised, cleared,"
                " fault_code, boot_id, seq) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (site, source, ts(m["ts"]), j(m.get("active")), j(m.get("raised")),
                 j(m.get("cleared")), m.get("fault_code"), m["boot_id"], m["seq"]))
        elif kind == "status":
            self._status(cur, site, source, m)

    def _status(self, cur, site, source, m):
        cur.execute("INSERT INTO status_event (site, source, state, reason, ts)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (site, source, m.get("state", "?"), m.get("error") or m.get("reason"),
                     ts(m.get("ts"))))

    def _telemetry(self, cur, site, battery, m):
        p, c, t = m.get("pack", {}), m.get("cells", {}), m.get("temps", {})
        when = ts(m["ts"])
        cur.execute(
            "INSERT IGNORE INTO telemetry (site, battery_id, ts, nominal_voltage, chemistry,"
            " voltage_v, current_a, power_w, soc_pct, remaining_ah, cycles, state, charge_mos,"
            " discharge_mos, charger_connected, load_connected, cell_count, cell_max_mv,"
            " cell_max_no, cell_min_mv, cell_min_no, cell_delta_mv, temp_max_c, temp_min_c,"
            " temps_c, faults, fault_code, boot_id, seq) VALUES (" + ",".join(["%s"] * 29) + ")",
            (site, battery, when, m.get("nominal_voltage"), m.get("chemistry"),
             p.get("voltage_v"), p.get("current_a"), p.get("power_w"), p.get("soc_pct"),
             p.get("remaining_ah"), p.get("cycles"), p.get("state"), p.get("charge_mos"),
             p.get("discharge_mos"), p.get("charger_connected"), p.get("load_connected"),
             c.get("count"), c.get("max_mv"), c.get("max_cell"), c.get("min_mv"),
             c.get("min_cell"), c.get("delta_mv"), t.get("max_c"), t.get("min_c"),
             j(t.get("sensors_c")), j(m.get("faults")), m.get("fault_code"),
             m["boot_id"], m["seq"]))
        if cur.rowcount == 0:
            log.debug("duplicate %s seq %s ignored", battery, m["seq"])
            return
        tid = cur.lastrowid
        balancing = set(c.get("balancing") or [])
        rows = [(tid, battery, when, n, mv, n in balancing)
                for n, mv in enumerate(c.get("voltages_mv") or [], start=1)]
        if rows:
            cur.executemany("INSERT INTO cell_voltage (telemetry_id, battery_id, ts, cell_no, mv,"
                            " balancing) VALUES (%s,%s,%s,%s,%s,%s)", rows)


def main() -> int:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    store = Store()
    store.connect()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=env("MQTT_CLIENT_ID", "bhm-ingest"),
                         clean_session=False, manual_ack=True)
    client.username_pw_set(env("MQTT_USER", "bhm-ingest"), env("MQTT_PASSWORD"))
    ctx = ssl.create_default_context(cafile=env("MQTT_CA", "/certs/ca.crt"))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(env("MQTT_CERT", "/certs/client.crt"), env("MQTT_KEY", "/certs/client.key"))
    client.tls_set_context(ctx)
    counts = {"ok": 0}

    def on_connect(c, userdata, flags, reason, props):
        if reason.is_failure:
            log.error("MQTT connect refused: %s", reason)
            return
        log.info("MQTT connected (session present: %s)", flags.session_present)
        c.subscribe(f"{PREFIX}/#", qos=1)

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload)
            store.write(msg.topic, payload)
        except (ValueError, KeyError, TypeError) as e:     # malformed: log and drop
            log.error("bad message on %s: %s", msg.topic, e)
        except RuntimeError as e:                         # DB down: don't ack, drop the link
            log.error("%s; disconnecting so the broker redelivers", e)
            c.disconnect()
            return
        c.ack(msg.mid, msg.qos)
        counts["ok"] += 1
        if counts["ok"] % 100 == 0:
            log.info("%d messages stored", counts["ok"])

    def on_disconnect(c, userdata, flags, reason, props):
        log.warning("MQTT disconnected: %s", reason)

    client.on_connect, client.on_message, client.on_disconnect = (
        on_connect, on_message, on_disconnect)
    signal.signal(signal.SIGTERM, lambda *_: client.disconnect())
    client.reconnect_delay_set(1, 30)
    client.connect_async(env("MQTT_HOST", "mosquitto"), int(env("MQTT_PORT", "8883")), 60)
    client.loop_forever(retry_first_connection=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
