"""BHM dashboard API: serves dashboard.html and read-only JSON from the MySQL database.

  GET /                          the dashboard page
  GET /api/summary               latest reading + cells + online state per battery, gateway health
  GET /api/history?battery=ID&minutes=10   voltage, current, SOC, temperature over time
  GET /api/events?limit=30       alarm raised/cleared and online/offline changes, newest first

HTTP basic auth when DASH_PASSWORD is set (user DASH_USER, default "bhm").
Configuration (environment): MYSQL_HOST MYSQL_PORT MYSQL_USER MYSQL_PASSWORD MYSQL_DATABASE
DASH_USER DASH_PASSWORD DASH_PAGE (path of dashboard.html) LISTEN_PORT
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pymysql
import pymysql.cursors

env = os.environ.get
log = logging.getLogger("bhm-api")
PAGE = env("DASH_PAGE", "/app/dashboard.html")
USER = env("DASH_USER", "bhm")
PASSWORD = env("DASH_PASSWORD", "")
BATTERY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_POINTS = 400


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

    def _authorized(self) -> bool:
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(user, USER) & hmac.compare_digest(pw, PASSWORD)

    def do_GET(self):
        if not self._authorized():
            self._send(401, b"login required", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="BHM dashboard", charset="UTF-8"'})
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html", "/dashboard.html"):
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
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
                elif url.path == "/api/events":
                    data = events(cur, min(max(int(q.get("limit", ["30"])[0]), 1), 200))
                else:
                    self._send(404, b"not found", "text/plain")
                    return
            self._send(200, json.dumps(data).encode(), "application/json")
        except (ValueError, pymysql.MySQLError) as e:
            log.warning("%s: %s", self.path, e)
            self._send(503, json.dumps({"error": str(e)}).encode(), "application/json")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(env("LISTEN_PORT", "8090"))
    log.info("dashboard on :%d, auth %s", port, "on" if PASSWORD else "OFF")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
