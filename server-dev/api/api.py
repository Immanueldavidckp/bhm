"""BHM dashboard API: serves dashboard.html and read-only JSON from the MySQL database.

  GET /                          the dashboard page, opened in live mode:
                                 redirects to /?mode=mqtt&url=wss://<this host>/mqtt
                                 (/?mode=sim keeps the page's built-in demo simulator)
  GET /mqtt  (WebSocket)         MQTT over WebSocket, proxied to mosquitto:9001
                                 (read-only telemetry, see mosquitto/acl-ws)
  GET /api/summary               latest reading + cells + online state per battery, gateway health
  GET /api/history?battery=ID&minutes=10   voltage, current, SOC, temperature over time
  GET /api/events?limit=30       alarm raised/cleared and online/offline changes, newest first
  GET /api/recent?limit=240[&hours=6]   stored readings in the gateway's telemetry JSON format,
                                 per battery, oldest first (the page loads these first, so it has
                                 data while the board is offline). With hours: the readings of the
                                 last `hours` before that battery's last reading, thinned evenly to
                                 at most `limit`; without: the last `limit` readings
  GET /api/health[?battery=ID][&hours=24]   calculated health per battery (health.py): SOH from
                                 the last full charge, internal resistance of every cell from
                                 current steps, cell status (Good / Watch / Replace), charges
  GET /api/frames?battery=ID[&hours=6]      every reading (frame) of one battery, compact: time,
                                 current, voltage, SOC, temperature, cell mV, balancing cells and BMS faults

HTTP basic auth when DASH_PASSWORD is set (user DASH_USER, default "bhm").
Configuration (environment): MYSQL_HOST MYSQL_PORT MYSQL_USER MYSQL_PASSWORD MYSQL_DATABASE
DASH_USER DASH_PASSWORD DASH_PAGE (path of dashboard.html) LISTEN_PORT MQTT_WS_HOST MQTT_WS_PORT

Browsers do not reliably send basic-auth credentials on a WebSocket upgrade, so the page
response sets an HttpOnly cookie (derived from DASH_PASSWORD) that also admits /mqtt.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import hashlib
import socket
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pymysql
import pymysql.cursors

import health

env = os.environ.get
log = logging.getLogger("bhm-api")
PAGE = env("DASH_PAGE", "/app/dashboard.html")
USER = env("DASH_USER", "bhm")
PASSWORD = env("DASH_PASSWORD", "")
BATTERY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_POINTS = 400
WS_HOST = env("MQTT_WS_HOST", "mosquitto")
WS_PORT = int(env("MQTT_WS_PORT", "9001"))
WS_COOKIE = "bhm_ws"
WS_TOKEN = hmac.new(PASSWORD.encode(), b"bhm-dashboard-ws", hashlib.sha256).hexdigest()[:40]
# request headers not passed on to mosquitto (hop-by-hop, our auth, the tunnel's own)
WS_DROP = {"host", "authorization", "cookie", "connection", "upgrade"}


def db():
    return pymysql.connect(
        host=env("MYSQL_HOST", "mysql"), port=int(env("MYSQL_PORT", "3306")),
        user=env("MYSQL_USER", "bhm"), password=env("MYSQL_PASSWORD", ""),
        database=env("MYSQL_DATABASE", "bhm"), charset="utf8mb4", autocommit=True,
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=5, read_timeout=15)


def jsonable(v):
    if isinstance(v, datetime):           # stored as UTC
        return v.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(v, Decimal):
        return float(v)
    return v


def row(r: dict) -> dict:
    out = {}
    for k, v in r.items():
        if k in ("temps_c", "faults", "active", "raised", "cleared", "batteries") and isinstance(v, str):
            v = json.loads(v)
        out[k] = jsonable(v)
    return out


def summary(cur) -> dict:
    cur.execute("SELECT * FROM battery_latest ORDER BY nominal_voltage, battery_id")
    packs = [row(r) for r in cur.fetchall()]
    cur.execute("""SELECT s.source, s.state, s.reason, s.received_at FROM status_event s
                   JOIN (SELECT source, MAX(id) id FROM status_event GROUP BY source) l ON l.id = s.id""")
    states = {r["source"]: row(r) for r in cur.fetchall()}
    for p in packs:
        cur.execute("SELECT cell_no, mv, balancing FROM cell_voltage WHERE telemetry_id = %s "
                    "ORDER BY cell_no", (p["id"],))
        cells = cur.fetchall()
        p["cells_mv"] = [c["mv"] for c in cells]
        p["balancing"] = [c["cell_no"] for c in cells if c["balancing"]]
        p["link"] = states.get(p["battery_id"])
        soc, rem = p.get("soc_pct"), p.get("remaining_ah")
        p["rated_ah"] = round(rem / (soc / 100)) if soc and rem and soc > 5 else None
    cur.execute("SELECT * FROM gateway_health ORDER BY id DESC LIMIT 1")
    health = cur.fetchone()
    return {"now": jsonable(datetime.now(timezone.utc).replace(tzinfo=None)),
            "gateway": {"link": states.get("gateway"), "health": row(health) if health else None},
            "packs": packs}


def history(cur, battery: str, minutes: int) -> dict:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes)
    cur.execute("SELECT COUNT(*) n FROM telemetry WHERE battery_id = %s AND ts >= %s", (battery, since))
    step = max(1, -(-cur.fetchone()["n"] // MAX_POINTS))
    cur.execute("""SELECT ts, voltage_v, current_a, soc_pct, temp_max_c, cell_delta_mv FROM
                     (SELECT ts, voltage_v, current_a, soc_pct, temp_max_c, cell_delta_mv,
                             ROW_NUMBER() OVER (ORDER BY ts) rn
                      FROM telemetry WHERE battery_id = %s AND ts >= %s) t
                   WHERE (rn - 1) %% %s = 0 ORDER BY ts""", (battery, since, step))
    return {"battery": battery, "minutes": minutes, "points": [row(r) for r in cur.fetchall()]}


def _message(r: dict, cells: list[dict]) -> dict:
    """A telemetry row as the gateway's MQTT message (what dashboard.html ingests)."""
    b = lambda v: None if v is None else bool(v)
    num = lambda v: float(v) if isinstance(v, Decimal) else v
    j = lambda v: json.loads(v) if isinstance(v, str) else (v or [])
    return {
        "schema": 1, "site": r["site"], "battery_id": r["battery_id"],
        "nominal_voltage": num(r["nominal_voltage"]), "chemistry": r["chemistry"],
        "ts": jsonable(r["ts"]), "boot_id": r["boot_id"], "seq": r["seq"], "stored": True,
        "pack": {"voltage_v": num(r["voltage_v"]), "current_a": num(r["current_a"]),
                 "power_w": num(r["power_w"]), "soc_pct": num(r["soc_pct"]),
                 "state": r["state"], "remaining_ah": num(r["remaining_ah"]),
                 "cycles": r["cycles"], "charge_mos": b(r["charge_mos"]),
                 "discharge_mos": b(r["discharge_mos"]),
                 "charger_connected": b(r["charger_connected"]),
                 "load_connected": b(r["load_connected"])},
        "cells": {"count": r["cell_count"], "voltages_mv": [c["mv"] for c in cells],
                  "max_mv": r["cell_max_mv"], "max_cell": r["cell_max_no"],
                  "min_mv": r["cell_min_mv"], "min_cell": r["cell_min_no"],
                  "delta_mv": r["cell_delta_mv"],
                  "balancing": [c["cell_no"] for c in cells if c["balancing"]]},
        "temps": {"sensors_c": j(r["temps_c"]), "max_c": r["temp_max_c"],
                  "min_c": r["temp_min_c"]},
        "faults": j(r["faults"]), "fault_code": r["fault_code"],
    }


def _batteries(cur, battery: str | None = None) -> list[str]:
    if battery:
        return [battery]
    cur.execute("SELECT DISTINCT battery_id FROM telemetry")
    return sorted(r["battery_id"] for r in cur.fetchall())


def _window(cur, bat: str, hours: float) -> tuple[datetime, datetime] | None:
    """The `hours` before this battery's last reading (its data may be days old)."""
    cur.execute("SELECT MAX(ts) last FROM telemetry WHERE battery_id = %s", (bat,))
    last = cur.fetchone()["last"]
    return (last - timedelta(hours=hours), last) if last else None


def _cells_by_id(cur, bat: str, ids: list[int], lo=None, hi=None) -> dict[int, list]:
    if not ids:
        return {}
    if lo is not None:     # whole window: use the (battery_id, cell_no, ts) index
        cur.execute("SELECT telemetry_id, cell_no, mv, balancing FROM cell_voltage "
                    "WHERE battery_id = %s AND ts BETWEEN %s AND %s", (bat, lo, hi))
    else:
        cur.execute("SELECT telemetry_id, cell_no, mv, balancing FROM cell_voltage "
                    "WHERE telemetry_id IN (" + ",".join(["%s"] * len(ids)) + ")", ids)
    cells: dict[int, list] = {}
    for c in cur.fetchall():
        cells.setdefault(c["telemetry_id"], []).append(c)
    for v in cells.values():
        v.sort(key=lambda c: c["cell_no"])
    return cells


def _thin(rows: list, limit: int) -> list:
    """At most `limit` rows, evenly spaced, always keeping the first and the last."""
    if len(rows) <= limit:
        return rows
    step = (len(rows) - 1) / (limit - 1)
    return [rows[round(k * step)] for k in range(limit)]


def recent(cur, limit: int, hours: float | None = None) -> dict:
    out = []
    for bat in _batteries(cur):
        if hours:
            w = _window(cur, bat, hours)
            if not w:
                continue
            cur.execute("SELECT * FROM telemetry WHERE battery_id = %s AND ts BETWEEN %s AND %s "
                        "ORDER BY ts", (bat, *w))
            rows = _thin(cur.fetchall(), limit)
        else:
            cur.execute("SELECT * FROM telemetry WHERE battery_id = %s ORDER BY ts DESC LIMIT %s",
                        (bat, limit))
            rows = cur.fetchall()
        if not rows:
            continue
        cells = _cells_by_id(cur, bat, [r["id"] for r in rows])
        out += [_message(r, cells.get(r["id"], [])) for r in rows]
    out.sort(key=lambda m: m["ts"])
    return {"messages": out}


def _readings(cur, bat: str, hours: float) -> list[dict]:
    """All readings of the window as health.reading() dicts, oldest first."""
    w = _window(cur, bat, hours)
    if not w:
        return []
    cur.execute("SELECT id, ts, current_a, voltage_v, soc_pct, remaining_ah, temp_max_c, "
                "temp_min_c, faults FROM telemetry WHERE battery_id = %s AND ts BETWEEN %s AND %s "
                "ORDER BY ts", (bat, *w))
    rows = cur.fetchall()
    cells = _cells_by_id(cur, bat, [r["id"] for r in rows], *w)
    f = lambda v: None if v is None else float(v)  # noqa: E731
    return [{"t": r["ts"].replace(tzinfo=timezone.utc).timestamp(), "i": f(r["current_a"]),
             "v": f(r["voltage_v"]), "soc": f(r["soc_pct"]), "rem": f(r["remaining_ah"]),
             "tmax": r["temp_max_c"], "tmin": r["temp_min_c"],
             "cells": [c["mv"] for c in cells.get(r["id"], [])],
             "bal": [c["cell_no"] for c in cells.get(r["id"], []) if c["balancing"]],
             "faults": (json.loads(r["faults"]) if isinstance(r["faults"], str) else r["faults"]) or []}
            for r in rows]


_health_cache: dict[tuple, dict] = {}
_cache_lock = threading.Lock()


def health_report(cur, battery: str | None, hours: float) -> dict:
    out = {}
    for bat in _batteries(cur, battery):
        cur.execute("SELECT MAX(id) id FROM telemetry WHERE battery_id = %s", (bat,))
        key = (bat, hours, cur.fetchone()["id"])
        with _cache_lock:
            hit = _health_cache.get(key)
        if hit is None:
            hit = health.analyse(_readings(cur, bat, hours))
            with _cache_lock:
                if len(_health_cache) > 64:
                    _health_cache.clear()
                _health_cache[key] = hit
        out[bat] = hit
    return {"hours": hours, "batteries": out}


def frames(cur, battery: str, hours: float) -> dict:
    rs = _readings(cur, battery, hours)
    return {"battery": battery, "hours": hours, "n": len(rs),
            "t": [int(r["t"] * 1000) for r in rs], "i": [r["i"] for r in rs],
            "v": [r["v"] for r in rs], "soc": [r["soc"] for r in rs],
            "temp": [r["tmax"] for r in rs], "cells": [r["cells"] for r in rs],
            "bal": [r["bal"] for r in rs], "faults": [r["faults"] for r in rs]}


def events(cur, limit: int) -> dict:
    cur.execute("""SELECT 'alarm' kind, battery_id source, ts, raised, cleared, active, NULL state,
                          NULL reason FROM alarm
                   WHERE JSON_LENGTH(raised) > 0 OR JSON_LENGTH(cleared) > 0
                   ORDER BY id DESC LIMIT %s""", (limit,))
    out = [row(r) for r in cur.fetchall()]
    cur.execute("""SELECT 'status' kind, source, COALESCE(ts, received_at) ts, state, reason
                   FROM status_event ORDER BY id DESC LIMIT %s""", (limit,))
    out += [row(r) for r in cur.fetchall()]
    out.sort(key=lambda e: e["ts"] or "", reverse=True)
    return {"events": out[:limit]}


class Handler(BaseHTTPRequestHandler):
    server_version = "bhm-api"

    def log_message(self, fmt, *args):     # the page polls every few seconds
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self, name: str) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _scheme(self) -> str:
        """https when reached through the Cloudflare tunnel (or another TLS proxy)."""
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https" \
                or '"https"' in self.headers.get("Cf-Visitor", ""):
            return "https"
        return "http"

    def _authorized(self, path: str = "") -> bool:
        if not PASSWORD:
            return True
        if path == "/mqtt" and hmac.compare_digest(self._cookie(WS_COOKIE), WS_TOKEN):
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(user, USER) & hmac.compare_digest(pw, PASSWORD)

    def _page(self, q: dict) -> None:
        if "mode" not in q:                 # open live: the page's own URL parameters
            host = self.headers.get("Host", f"localhost:{LISTEN_PORT}")
            ws = "wss" if self._scheme() == "https" else "ws"
            loc = f"/?mode=mqtt&url={ws}://{host}/mqtt"
            self._send(302, b"", "text/plain", {"Location": loc})
            return
        cookie = f"{WS_COOKIE}={WS_TOKEN}; Path=/mqtt; HttpOnly; SameSite=Strict"
        if self._scheme() == "https":
            cookie += "; Secure"
        with open(PAGE, "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8",
                       {"Set-Cookie": cookie} if PASSWORD else None)

    def _mqtt_ws(self) -> None:
        """Splice this WebSocket connection to mosquitto's WebSocket listener."""
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send(400, b"WebSocket only", "text/plain")
            return
        try:
            up = socket.create_connection((WS_HOST, WS_PORT), timeout=5)
        except OSError as e:
            log.warning("mqtt ws: cannot reach %s:%d: %s", WS_HOST, WS_PORT, e)
            self._send(502, b"broker unreachable", "text/plain")
            return
        up.settimeout(None)
        head = [f"GET /mqtt HTTP/1.1", f"Host: {WS_HOST}:{WS_PORT}",
                "Connection: Upgrade", "Upgrade: websocket"]
        head += [f"{k}: {v}" for k, v in self.headers.items()
                 if k.lower() not in WS_DROP and not k.lower().startswith(("cf-", "x-forwarded"))]
        up.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
        self.close_connection = True
        client = self.connection

        def pump_client_to_broker():
            try:
                while True:
                    data = self.rfile.read1(65536)   # rfile may hold bytes already buffered
                    if not data:
                        break
                    up.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    up.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t = threading.Thread(target=pump_client_to_broker, daemon=True)
        t.start()
        try:
            while True:
                data = up.recv(65536)
                if not data:
                    break
                client.sendall(data)
        except OSError:
            pass
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            up.close()
            t.join(timeout=2)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if not self._authorized(url.path):
            self._send(401, b"login required", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="BHM dashboard", charset="UTF-8"'})
            return
        try:
            if url.path in ("/", "/index.html", "/dashboard.html"):
                self._page(q)
                return
            if url.path == "/mqtt":
                self._mqtt_ws()
                return
            with db() as conn, conn.cursor() as cur:
                if url.path == "/api/summary":
                    data = summary(cur)
                elif url.path == "/api/history":
                    battery = q.get("battery", [""])[0]
                    if not BATTERY_RE.fullmatch(battery):
                        self._send(400, b'{"error":"bad battery"}', "application/json")
                        return
                    minutes = min(max(int(q.get("minutes", ["10"])[0]), 1), 7 * 24 * 60)
                    data = history(cur, battery, minutes)
                elif url.path == "/api/recent":
                    hours = q.get("hours", [""])[0]
                    data = recent(cur, min(max(int(q.get("limit", ["240"])[0]), 2), 1000),
                                  min(max(float(hours), 0.1), 168) if hours else None)
                elif url.path in ("/api/health", "/api/frames"):
                    battery = q.get("battery", [""])[0] or None
                    if (battery is not None or url.path == "/api/frames") and not (
                            battery and BATTERY_RE.fullmatch(battery)):
                        self._send(400, b'{"error":"bad battery"}', "application/json")
                        return
                    hours = min(max(float(q.get("hours", ["24" if url.path == "/api/health"
                                                          else "6"])[0]), 0.1), 168)
                    data = (health_report(cur, battery, hours) if url.path == "/api/health"
                            else frames(cur, battery, hours))
                elif url.path == "/api/events":
                    data = events(cur, min(max(int(q.get("limit", ["30"])[0]), 1), 200))
                else:
                    self._send(404, b"not found", "text/plain")
                    return
            self._send(200, json.dumps(data).encode(), "application/json")
        except (ValueError, pymysql.MySQLError) as e:
            log.warning("%s: %s", self.path, e)
            self._send(503, json.dumps({"error": str(e)}).encode(), "application/json")


LISTEN_PORT = int(env("LISTEN_PORT", "8090"))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = LISTEN_PORT
    log.info("dashboard on :%d, auth %s", port, "on" if PASSWORD else "OFF")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
