"""BHM status screen for the STM32MP135F-DK's 480x272 LCD (and a text version for the console).

Reads the gateway's live status file (/run/bhm/status.json, written every second) and shows:
  * MQTT: connected to the broker or not, and why
  * network: interface, IP address, Wi-Fi name
  * upload: messages sent / still queued on the board, time of the last broker ACK
  * one card per battery: answering the gateway, wired as configured (cell count), its data
    reaching the broker, SOC / voltage / current / cells / temperature / faults

  python3 -m bhm_gateway display              fullscreen GTK window (Weston, as user weston)
  python3 -m bhm_gateway display --png F.png  render one frame to a PNG (no GTK needed)
  python3 -m bhm_gateway status               the same information as text
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

DEFAULT_STATUS = "/run/bhm/status.json"
STALE_S = 10            # status file older than this: the gateway is not running

GREEN, AMBER, RED, GREY = "ok", "warn", "bad", "idle"
COLORS = {GREEN: (0.18, 0.80, 0.44), AMBER: (0.98, 0.70, 0.12),
          RED: (0.94, 0.27, 0.27), GREY: (0.50, 0.55, 0.62)}
BG = (0.06, 0.09, 0.13)
CARD = (0.11, 0.15, 0.21)
FG = (0.93, 0.95, 0.97)
DIM = (0.60, 0.66, 0.73)
FONT = "Liberation Sans"   # the OpenSTLinux image ships only Liberation; "Sans" elsewhere


# ------------------------------------------------------------------ reading the state
def load_status(path: str = DEFAULT_STATUS) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def network_info() -> dict:
    """Interfaces with an IPv4 address, the default route and the Wi-Fi name (best effort)."""
    info = {"addrs": [], "default_route": False, "ssid": None}
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            m = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)", line)
            if m:
                info["addrs"].append((m.group(1), m.group(2)))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open("/proc/net/route") as f:
            info["default_route"] = any(l.split()[1] == "00000000" for l in f.readlines()[1:]
                                        if len(l.split()) > 2)
    except OSError:
        pass
    wl = next((i for i, _ in info["addrs"] if i.startswith("wl")), None)
    if wl and shutil.which("iw"):
        try:
            out = subprocess.run(["iw", "dev", wl, "link"], capture_output=True, text=True,
                                 timeout=2).stdout
            m = re.search(r"SSID:\s*(.+)", out)
            info["ssid"] = m.group(1).strip() if m else None
        except (OSError, subprocess.SubprocessError):
            pass
    return info


# ------------------------------------------------------------------ deciding what it means
def ago(now: float, t: float | None) -> str:
    if t is None:
        return "never"
    s = max(0, int(now - t))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m ago"


def duration(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def short_error(err: str | None) -> str:
    if not err:
        return ""
    m = re.search(r"serial error on (\S+?):.*(No such file|could not open)", err)
    if m:
        return f"port missing ({os.path.basename(m.group(1))})"
    if "serial error" in err:
        return "serial port error"
    if "no valid reply" in err:
        n = re.search(r"\((\d+) bytes", err)
        return "no reply from BMS" if not n or n.group(1) == "0" else "garbled reply"
    return err[:40]


def expected_cells(nominal: float | None) -> int | None:
    return round(nominal / 3.2) if nominal else None   # LiFePO4: 3.2 V per cell


@dataclass
class Row:
    level: str
    label: str
    text: str
    detail: str = ""


@dataclass
class Card:
    id: str
    level: str
    verdict: str            # one line: LIVE / NO RESPONSE / ...
    note: str = ""          # second line: why / when
    soc: str = "--"
    vi: str = ""
    cells: str = ""
    temp: str = ""
    sharing: bool = False
    fault: str = ""


@dataclass
class View:
    ok: bool                # everything fine (exit code of `status`)
    wifi: bool | None       # header icons: True connected, False not, None unknown
    mqtt: bool
    rows: list[Row] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    footer: str = ""
    footer_level: str = GREY
    age: str = ""


def wifi_state(net: dict | None) -> bool | None:
    if net is None:
        return None
    return any(i.startswith("wl") for i, _ in net["addrs"])


def assess(st: dict | None, now: float, net: dict | None = None) -> View:
    wifi = wifi_state(net)
    if st is None:
        return View(False, wifi, False, rows=[
            Row(RED, "Gateway", "not running", "no status file"),
            Row(GREY, "Check", "systemctl status bhm-gateway")])
    stale = now - st.get("mono", 0)
    if stale > STALE_S:
        return View(False, wifi, False, rows=[
            Row(RED, "Gateway", "not responding", f"last update {duration(stale)} ago"),
            Row(GREY, "Check", "journalctl -u bhm-gateway -n 30")])

    P = float(st.get("poll_interval_s", 10))
    rows: list[Row] = []
    m = st["mqtt"]
    broker = f'{m["host"]}:{m["port"]}'
    if m["connected"]:
        since = f'for {duration(now - m["since"])}' if m.get("since") else ""
        rows.append(Row(GREEN, "MQTT", f"Connected to {broker}", since))
    else:
        why = m.get("last_error") or "connecting..."
        if st.get("waiting_for_clock"):
            why = "waiting for clock (NTP) before TLS"
        rows.append(Row(RED, "MQTT", f"Offline  {broker}", why))

    if net is not None:
        if net["addrs"]:
            iface, ip = net["addrs"][0]
            txt = f"{iface} {ip}" + (f'  "{net["ssid"]}"' if net.get("ssid") else "")
            rows.append(Row(GREEN if net["default_route"] else AMBER, "Network", txt,
                            "" if net["default_route"] else "no default route"))
        else:
            rows.append(Row(RED, "Network", "no IP address", "Wi-Fi not connected"))

    q = m["queued"]
    up_level = GREEN if m["connected"] and q < 50 else AMBER if m["connected"] else RED
    up_txt = f'{m["sent"]} sent, {q} waiting on board'
    if m.get("dropped"):
        up_txt += f', {m["dropped"]} dropped'
        up_level = RED
    rows.append(Row(up_level, "Upload", up_txt, f'last ACK {ago(now, m.get("last_sent"))}'))

    if not st.get("clock_ok", True):
        rows.append(Row(AMBER, "Clock", "not set yet", "readings wait for NTP"))

    cards = []
    fresh_read = 3 * P + 5
    fresh_up = 3 * P + 15
    for b in st["batteries"]:
        c = Card(b["id"], GREY, "WAITING", "first reading...")
        if b.get("soc_pct") is not None:
            c.soc = f'{b["soc_pct"]:.0f}%'
            c.vi = f'{b["voltage_v"]:.1f}V {b["current_a"]:+.1f}A'
            c.cells = f'{b["cell_count"]}S  Δ{b["cell_delta_mv"]}mV'
            c.temp = f'{b["temp_max_c"]}°C'
        if b["state"] == "offline":
            c.level, c.verdict, c.note = RED, "NO RESPONSE", short_error(b.get("error"))
        elif b["state"] == "online":
            read_ok = b.get("last_ok") is not None and now - b["last_ok"] < fresh_read
            up = b.get("last_upload")
            up_ok = up is not None and now - up < fresh_up
            if not read_ok:
                c.level, c.verdict, c.note = AMBER, "STALE", f'read {ago(now, b.get("last_ok"))}'
            elif up_ok:
                c.level, c.verdict, c.note = GREEN, "LIVE", f"sent {ago(now, up)}"
                c.sharing = True
            elif not m["connected"]:
                c.level, c.verdict, c.note = AMBER, "READING", "queued, broker offline"
            else:
                c.level, c.verdict, c.note = AMBER, "READING", "upload pending"
            exp = expected_cells(b.get("nominal_voltage"))
            if exp and b.get("cell_count") and b["cell_count"] != exp:
                c.level = RED if c.level != GREY else c.level
                c.verdict, c.note = "WRONG PACK?", f'{b["cell_count"]}S, expected {exp}S'
        elif st.get("waiting_for_clock"):
            c.note = "waiting for clock"
        if b.get("faults"):
            n = len(b["faults"])
            c.fault = b["faults"][0].replace("_", " ") + (f" +{n - 1}" if n > 1 else "")
        cards.append(c)

    n_share = sum(c.sharing for c in cards)
    n = len(cards)
    problems = sum(r.level in (RED, AMBER) for r in rows) + sum(
        c.level != GREEN or bool(c.fault) for c in cards)
    ok = problems == 0
    footer = f"{n_share} of {n} batteries sharing data with the server"
    flevel = GREEN if n and n_share == n else AMBER if n_share else RED
    return View(ok, wifi, bool(m["connected"]), rows, cards, footer, flevel,
                f'gateway v{st.get("version", "?")}, up {duration(st.get("uptime_s", 0))}')


# ------------------------------------------------------------------ text (console)
def format_text(v: View, st: dict | None) -> str:
    mark = {GREEN: "[ OK ]", AMBER: "[WARN]", RED: "[FAIL]", GREY: "[ .. ]"}
    onoff = {True: "connected", False: "DISCONNECTED", None: "unknown"}
    out = [f"BHM gateway {st.get('site', '') if st else ''}: Wi-Fi {onoff[v.wifi]}, "
           f"MQTT {onoff[v.mqtt]}"]
    for r in v.rows:
        out.append(f"{mark[r.level]} {r.label:<8} {r.text}" + (f"  ({r.detail})" if r.detail else ""))
    for c in v.cards:
        line = f"{mark[c.level]} {c.id:<8} {c.verdict:<12} {c.note}"
        if c.soc != "--":
            line += f" | {c.soc} {c.vi} {c.cells} {c.temp}"
        if c.fault:
            line += f" | FAULT {c.fault}"
        out.append(line)
    if v.footer:
        out.append(v.footer)
    if v.age:
        out.append(v.age)
    return "\n".join(out)


def status_main(args) -> int:
    st = load_status(args.status)
    v = assess(st, time.monotonic(), network_info())
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        print(format_text(v, st))
    return 0 if v.ok else 1


# ------------------------------------------------------------------ drawing (cairo)
def _rrect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _text(cr, x, y, s, size, color=FG, bold=False, align="left", maxw=None):
    import cairo
    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL,
                        cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    if maxw:
        while s and cr.text_extents(s).x_advance > maxw:
            s = s[:-2] + "…" if len(s) > 2 else ""
    w = cr.text_extents(s).x_advance
    if align == "right":
        x -= w
    elif align == "center":
        x -= w / 2
    cr.set_source_rgb(*color)
    cr.move_to(x, y)
    cr.show_text(s)
    return w


def _dot(cr, x, y, r, level):
    cr.set_source_rgb(*COLORS[level])
    cr.arc(x, y, r, 0, 2 * math.pi)
    cr.fill()


def _slash(cr, x0, y0, x1, y1):
    cr.set_line_width(2.4)
    cr.set_source_rgb(*BG)                 # gap around the slash so it reads on the icon
    cr.move_to(x0, y0)
    cr.line_to(x1, y1)
    cr.set_line_width(4.5)
    cr.stroke()
    cr.set_source_rgb(*COLORS[RED])
    cr.move_to(x0, y0)
    cr.line_to(x1, y1)
    cr.set_line_width(2.2)
    cr.stroke()


def _wifi_icon(cr, cx, cy, state):
    """Wi-Fi fan, bottom point at (cx, cy), ~22 px wide."""
    import cairo
    col = COLORS[{True: GREEN, False: RED, None: GREY}[state]]
    cr.set_source_rgb(*col)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_width(2.4)
    for r in (5, 10, 15):
        cr.new_sub_path()
        cr.arc(cx, cy, r, -math.pi * 0.77, -math.pi * 0.23)
        cr.stroke()
    cr.arc(cx, cy - 0.5, 2, 0, 2 * math.pi)
    cr.fill()
    if state is False:
        _slash(cr, cx - 10, cy - 15, cx + 10, cy + 1)


def _mqtt_icon(cr, cx, cy, state):
    """Cloud (the broker), centred at (cx, cy), ~24 px wide."""
    import cairo
    col = COLORS[GREEN if state else RED]
    cr.set_source_rgb(*col)
    cr.arc(cx - 6, cy + 1, 5, 0, 2 * math.pi)
    cr.fill()
    cr.arc(cx + 1, cy - 3, 7, 0, 2 * math.pi)
    cr.fill()
    cr.arc(cx + 7, cy + 1.5, 4.5, 0, 2 * math.pi)
    cr.fill()
    _rrect(cr, cx - 11, cy + 1, 22.5, 5, 2.5)
    cr.fill()
    # up arrow inside: data going to the server
    cr.set_source_rgb(*BG)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_width(1.8)
    cr.move_to(cx + 0.5, cy + 4)
    cr.line_to(cx + 0.5, cy - 4)
    cr.move_to(cx - 2.5, cy - 1)
    cr.line_to(cx + 0.5, cy - 4)
    cr.line_to(cx + 3.5, cy - 1)
    cr.stroke()
    if not state:
        _slash(cr, cx - 10, cy - 10, cx + 10, cy + 8)


def render(cr, width: int, height: int, v: View, clock: str, blink: bool = True) -> None:
    """Draw the screen; designed for 480x272 and scaled to any size."""
    W, H = 480, 272
    s = min(width / W, height / H)
    cr.save()
    cr.set_source_rgb(*BG)
    cr.paint()
    cr.translate((width - W * s) / 2, (height - H * s) / 2)
    cr.scale(s, s)

    # header
    _text(cr, 10, 23, "BHM", 17, COLORS[GREEN], bold=True)
    _text(cr, 56, 23, "Battery Gateway", 15, FG, bold=True)
    cw = _text(cr, W - 10, 22, clock, 12, DIM, align="right")
    # connection icons, right to left: MQTT, Wi-Fi
    x = W - 10 - cw - 14
    mcol = COLORS[GREEN if v.mqtt else RED]
    x -= _text(cr, x, 22, "MQTT", 11, mcol, bold=True, align="right")
    _mqtt_icon(cr, x - 16, 16, v.mqtt)
    x -= 34
    wcol = COLORS[{True: GREEN, False: RED, None: GREY}[v.wifi]]
    x -= _text(cr, x, 22, "Wi-Fi", 11, wcol, bold=True, align="right")
    _wifi_icon(cr, x - 15, 25, v.wifi)
    cr.set_source_rgb(0.2, 0.25, 0.32)
    cr.rectangle(0, 35, W, 1)
    cr.fill()

    # status rows
    y = 54
    for r in v.rows[:4]:
        _dot(cr, 17, y - 4, 5, r.level if (blink or r.level != RED) else GREY)
        _text(cr, 28, y, r.label, 12, DIM, bold=True)
        tw = _text(cr, 92, y, r.text, 12, FG, maxw=250)
        _text(cr, W - 10, y, r.detail, 11, COLORS[r.level] if r.level != GREEN else DIM,
              align="right", maxw=W - 10 - (92 + tw + 12))
        y += 19

    # battery cards
    cards = v.cards
    if cards:
        top = y - 4
        bottom = H - 26
        n = len(cards)
        cols = min(n, 4)
        rowsn = math.ceil(n / cols)
        gap = 6
        cw = (W - 2 * 8 - gap * (cols - 1)) / cols
        ch = (bottom - top - gap * (rowsn - 1)) / rowsn
        for i, c in enumerate(cards):
            cx = 8 + (i % cols) * (cw + gap)
            cy = top + (i // cols) * (ch + gap)
            _draw_card(cr, cx, cy, cw, ch, c, blink)

    # footer
    _dot(cr, 15, H - 12, 4, v.footer_level)
    _text(cr, 25, H - 8, v.footer, 11, FG, maxw=300)
    _text(cr, W - 10, H - 8, v.age, 10, DIM, align="right", maxw=150)
    cr.restore()


def _draw_card(cr, x, y, w, h, c: Card, blink: bool) -> None:
    col = COLORS[c.level]
    border = COLORS[AMBER] if c.fault and c.level == GREEN else col
    _rrect(cr, x, y, w, h, 7)
    cr.set_source_rgb(*CARD)
    cr.fill_preserve()
    cr.set_source_rgb(*border)
    cr.set_line_width(2)
    cr.stroke()
    pad = 8
    _text(cr, x + pad, y + 17, c.id, 12, FG, bold=True, maxw=w - 30)
    _dot(cr, x + w - 13, y + 13, 5, c.level if (blink or c.level != RED) else GREY)
    compact = h < 100
    big = 22 if compact else 26
    yy = y + 17 + big + 3
    _text(cr, x + pad, yy, c.soc, big, FG if c.soc != "--" else DIM, bold=True)
    small = 10 if w < 120 else 11
    yy += 15
    if c.vi:
        _text(cr, x + pad, yy, c.vi, small, FG, maxw=w - 2 * pad)
        yy += 13
        _text(cr, x + pad, yy, f"{c.cells}  {c.temp}", small, DIM, maxw=w - 2 * pad)
        yy += 13
    if c.fault:
        _text(cr, x + pad, yy, "FAULT " + c.fault, small, COLORS[RED], bold=True,
              maxw=w - 2 * pad)
    # verdict strip at the bottom of the card
    sh = 30 if not compact else 26
    sy = y + h - sh
    cr.save()
    _rrect(cr, x, y, w, h, 7)
    cr.clip()
    cr.set_source_rgba(*col, 0.18)
    cr.rectangle(x, sy, w, sh)
    cr.fill()
    cr.restore()
    _text(cr, x + pad, sy + 12, c.verdict, 10.5, col, bold=True, maxw=w - 2 * pad)
    _text(cr, x + pad, sy + 24, c.note, 9.5, FG, maxw=w - 2 * pad)


def clock_text(st: dict | None) -> str:
    t = time.strftime("%H:%M:%S")
    return t if st is None or st.get("clock_ok", True) else t + "?"


# ------------------------------------------------------------------ entry points
def render_png(path: str, status_path: str, width: int = 480, height: int = 272) -> None:
    import cairo
    st = load_status(status_path)
    v = assess(st, time.monotonic() if st is None else st["mono"], network_info())
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    render(cairo.Context(surf), width, height, v, clock_text(st))
    surf.write_to_png(path)


def display_main(args) -> int:
    if args.png:
        render_png(args.png, args.status, args.width, args.height)
        return 0
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    state = {"st": None, "view": None, "net": None, "net_t": 0.0, "tick": 0}

    def refresh():
        now = time.monotonic()
        if now - state["net_t"] > 5:
            state["net"], state["net_t"] = network_info(), now
        state["st"] = load_status(args.status)
        state["view"] = assess(state["st"], now, state["net"])
        state["tick"] += 1
        area.queue_draw()
        return True

    def on_draw(widget, cr):
        a = widget.get_allocation()
        render(cr, a.width, a.height, state["view"], clock_text(state["st"]),
               blink=state["tick"] % 2 == 0)
        return False

    win = Gtk.Window(title="BHM Gateway")
    win.connect("destroy", Gtk.main_quit)
    area = Gtk.DrawingArea()
    area.connect("draw", on_draw)
    win.add(area)
    if args.windowed:
        win.set_default_size(args.width, args.height)
    else:
        win.fullscreen()
    refresh()
    GLib.timeout_add(1000, refresh)
    win.show_all()
    Gtk.main()
    return 0
