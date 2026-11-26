import json
import time

import pytest

from bhm_gateway import display
from bhm_gateway.config import BatteryConfig, Config, GatewayConfig, MqttConfig
from bhm_gateway.display import AMBER, GREEN, RED, assess
from bhm_gateway.service import Gateway

NET = {"addrs": [("wlan0", "192.168.137.12")], "default_route": True, "ssid": "bvs"}


def bat(id_, nominal, **kw):
    b = {"id": id_, "port": f"/run/bhm-uart/{id_}", "nominal_voltage": nominal,
         "state": "online", "reads": 5, "fails": 0, "last_ok": 99.0, "last_try": 99.0,
         "error": None, "last_upload": 98.0, "soc_pct": 80.0, "voltage_v": 13.2,
         "current_a": 1.5, "power_w": 19.8, "pack_state": "charging", "cycles": 3,
         "cell_count": round(nominal / 3.2), "cell_min_mv": 3300, "cell_max_mv": 3310,
         "cell_delta_mv": 10, "temp_max_c": 25, "temp_min_c": 24, "faults": []}
    b.update(kw)
    return b


def status(batteries, **mqtt):
    m = {"host": "129.225.86.126", "port": 8883, "client_id": "gw", "connected": True,
         "since": 10.0, "last_error": None, "sent": 100, "last_sent": 99.0, "queued": 0,
         "dropped": 0}
    m.update(mqtt)
    return {"schema": 1, "mono": 100.0, "time": 0, "version": "0.1.0", "site": "lab1",
            "uptime_s": 90, "poll_interval_s": 10, "clock_ok": True,
            "waiting_for_clock": False, "mqtt": m, "batteries": batteries}


def test_all_ok():
    v = assess(status([bat("kit12v", 12.8), bat("kit48v", 51.2)]), 100.0, NET)
    assert v.ok and v.wifi is True and v.mqtt is True
    assert [c.verdict for c in v.cards] == ["LIVE", "LIVE"]
    assert v.footer.startswith("2 of 2 batteries sharing")


def test_gateway_missing_or_stale():
    for v in (assess(None, 100.0), assess(status([]), 100.0 + display.STALE_S + 1)):
        assert not v.ok and v.mqtt is False and v.rows[0].label == "Gateway"
    assert assess(None, 100.0, NET).wifi is True
    assert assess(None, 100.0, {"addrs": [], "default_route": False, "ssid": None}).wifi is False


def test_broker_down_readings_queued():
    st = status([bat("kit12v", 12.8, last_upload=None)], connected=False,
                last_error="broker unreachable", queued=40)
    v = assess(st, 100.0, NET)
    assert not v.ok and v.mqtt is False
    assert v.rows[0].level == RED and "unreachable" in v.rows[0].detail
    c = v.cards[0]
    assert c.level == AMBER and c.verdict == "READING" and "broker offline" in c.note
    assert v.footer.startswith("0 of 1")


def test_battery_not_answering():
    st = status([bat("kit12v", 12.8), bat("kit36v", 38.4, state="offline",
                 error="no valid reply to 0x94 (0 bytes received)")])
    c = assess(st, 100.0, NET).cards[1]
    assert (c.level, c.verdict, c.note) == (RED, "NO RESPONSE", "no reply from BMS")


def test_wrong_pack_on_port():
    st = status([bat("kit48v", 51.2, cell_count=4)])   # a 4S pack where a 16S one belongs
    c = assess(st, 100.0, NET).cards[0]
    assert c.level == RED and c.verdict == "WRONG PACK?" and "expected 16S" in c.note


def test_stale_reading_and_fault():
    v = assess(status([bat("a", 12.8, last_ok=40.0),
                       bat("b", 12.8, faults=["cell_volt_high_l1", "temp_diff_l1"])]),
               100.0, NET)
    assert v.cards[0].verdict == "STALE"
    assert v.cards[1].verdict == "LIVE" and v.cards[1].fault == "cell volt high l1 +1"
    assert not v.ok


def test_text_and_png(tmp_path):
    st = status([bat("kit12v", 12.8), bat("kit36v", 38.4, state="offline", error="x")])
    p = tmp_path / "status.json"
    p.write_text(json.dumps(st))
    txt = display.format_text(assess(st, 100.0, NET), st)
    assert "Wi-Fi connected, MQTT connected" in txt and "[ OK ] MQTT" in txt and "[FAIL] kit36v" in txt
    pytest.importorskip("cairo")
    out = tmp_path / "s.png"
    display.render_png(str(out), str(p))
    assert out.read_bytes()[:4] == b"\x89PNG"


def test_gateway_writes_status_file(tmp_path):
    cfg = Config(GatewayConfig(site_id="lab1", buffer_path=str(tmp_path / "b.db"),
                               status_path=str(tmp_path / "run" / "status.json")),
                 MqttConfig(host="127.0.0.1", username="u", password="p", client_id="t"),
                 [BatteryConfig(id="kit12v", port="/nonexistent", nominal_voltage=12.8)])
    gw = Gateway(cfg)
    gw._live["kit12v"].update(reads=1, last_ok=time.monotonic(), data={
        "pack": {"soc_pct": 70.0, "voltage_v": 13.0, "current_a": -2.0, "power_w": -26.0,
                 "state": "discharging", "cycles": 1},
        "cells": {"count": 4, "min_mv": 3240, "max_mv": 3260, "delta_mv": 20},
        "temps": {"max_c": 26, "min_c": 25}, "faults": []})
    gw._online["kit12v"] = True
    gw.write_status()
    st = json.loads((tmp_path / "run" / "status.json").read_text())
    assert st["mqtt"]["connected"] is False and st["mqtt"]["queued"] == 0
    b = st["batteries"][0]
    assert b["state"] == "online" and b["soc_pct"] == 70.0 and b["cell_count"] == 4
    v = assess(st, st["mono"], NET)
    assert v.cards[0].verdict == "READING" and v.rows[0].level == RED
    gw.buffer.close()
