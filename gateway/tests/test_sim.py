"""Daly BMS simulator: wire protocol, battery physics, BMS protections, transports."""

import json
import math
import os
import random
import socket
import subprocess
import sys
import time

import pytest

from bhm_gateway import daly
from bhm_gateway.sim import SimBattery, parse_spec

ENV = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(__file__))}


def make(spec, seed=1):
    return SimBattery(parse_spec(spec), random.Random(seed))


def run(b, seconds, dt=1.0):
    for _ in range(int(seconds / dt)):
        b.step(dt)


def payloads(b, cmd):
    return daly.parse_frames(b.bms.answer(cmd), cmd)


def snapshot(b):
    """Decode every answer with the gateway's own decoders."""
    st = daly.decode_status(payloads(b, daly.CMD_STATUS)[0])
    n, nt = st["cell_count"], st["temp_sensor_count"]
    return {
        "status": st,
        "soc": daly.decode_soc(payloads(b, daly.CMD_SOC)[0]),
        "mos": daly.decode_mos(payloads(b, daly.CMD_MOS)[0]),
        "cmm": daly.decode_cell_minmax(payloads(b, daly.CMD_CELL_MINMAX)[0]),
        "tmm": daly.decode_temp_minmax(payloads(b, daly.CMD_TEMP_MINMAX)[0]),
        "cells": daly.decode_cell_voltages(payloads(b, daly.CMD_CELL_VOLTAGES), n),
        "temps": daly.decode_temperatures(payloads(b, daly.CMD_TEMPERATURES), nt),
        "balance": daly.decode_balance(payloads(b, daly.CMD_BALANCE)[0], n),
        "faults": daly.decode_faults(payloads(b, daly.CMD_FAULTS)[0])[0],
    }


def test_parse_spec_presets_and_options():
    assert parse_spec("kit12v").cells == 4
    assert parse_spec("kit36v").cells == 12
    assert parse_spec("kit48v").cells == 16
    assert parse_spec("rack:24v").cells == 8
    assert parse_spec("old:16").cells == 16          # NAME:CELLS still works
    s = parse_spec("kit48v:48v@tcp:7001,ah=50,soc=80,profile=drive")
    assert (s.port, s.capacity_ah, s.soc, s.profile, s.nominal_v) == ("tcp:7001", 50, 80, "drive", 51.2)
    assert s.charge_a == 10 and s.max_discharge_a == 50   # defaults scale with capacity
    assert parse_spec("k@/dev/ttyUSB0").port == "/dev/ttyUSB0"
    for bad in ("kit:99v", "kit,foo=1", "bad name"):
        with pytest.raises(ValueError):
            parse_spec(bad)


@pytest.mark.parametrize("spec,cells,lo,hi", [("kit12v", 4, 10, 15), ("kit36v", 12, 30, 44),
                                              ("kit48v", 16, 40, 59)])
def test_protocol_decodes_consistently(spec, cells, lo, hi):
    b = make(spec + ",profile=discharge,soc=60")
    run(b, 120)
    s = snapshot(b)
    assert s["status"]["cell_count"] == cells and s["status"]["temp_sensor_count"] == 2
    assert len(s["cells"]) == cells and all(3000 < c < 3500 for c in s["cells"])
    assert lo < s["soc"]["voltage_v"] < hi
    assert abs(s["soc"]["voltage_v"] - sum(s["cells"]) / 1000) < 0.15
    assert s["soc"]["current_a"] == pytest.approx(-0.3 * 100, abs=0.5)   # 0.3 C load
    assert s["mos"]["state"] == "discharging" and s["status"]["load_connected"]
    assert s["cmm"]["max_mv"] == max(s["cells"]) and s["cmm"]["min_mv"] == min(s["cells"])
    assert s["cells"][s["cmm"]["max_cell"] - 1] == max(s["cells"])
    assert s["tmm"]["max_c"] == max(s["temps"]) and 20 < s["tmm"]["max_c"] < 40
    assert s["mos"]["remaining_ah"] == pytest.approx(s["soc"]["soc_pct"], abs=0.1)
    assert s["faults"] == [] and s["balance"] == []


def test_multi_frame_counts():
    b = make("big:48v,cells=20,temps=9")
    assert len(payloads(b, daly.CMD_CELL_VOLTAGES)) == math.ceil(20 / 3)
    assert len(payloads(b, daly.CMD_TEMPERATURES)) == 2
    assert b.bms.answer(0x55) == b""     # unknown commands are ignored, like the real BMS


def test_coulomb_counting_and_cycles():
    b = make("kit48v,profile=discharge,soc=100,soh=1.1,load_a=50,cycles=10")
    run(b, 3600)
    s = snapshot(b)
    assert s["soc"]["soc_pct"] == pytest.approx(50, abs=1.0)   # 50 Ah out of 100 Ah rated
    assert s["mos"]["remaining_ah"] == pytest.approx(50, abs=1.0)
    run(b, 3700)                           # another 50+ Ah -> one full rated capacity
    assert snapshot(b)["status"]["cycles"] == 11


def test_cc_cv_charge_ends_full_and_balances():
    b = make("kit48v,profile=charge,soc=40")
    saw_balancing, peak = False, 0
    for _ in range(7 * 3600):
        b.step(1.0)
        saw_balancing |= bool(b.bms.balancing)
        peak = max(peak, b.bms.meas.max_v)
    s = snapshot(b)
    assert s["soc"]["soc_pct"] == 100.0
    assert 0 <= s["soc"]["current_a"] < 5           # tapered in CV
    assert s["soc"]["voltage_v"] == pytest.approx(57.6, abs=0.3)
    assert saw_balancing and peak < 3.75 and s["mos"]["charge_mos"]


def test_undervoltage_trips_discharge_mos_and_charging_releases_it():
    b = make("kit12v,profile=discharge,soc=3,load_a=30")
    run(b, 1800)
    s = snapshot(b)
    assert "cell_volt_low_l2" in s["faults"] and not s["mos"]["discharge_mos"]
    assert s["soc"]["current_a"] == 0 and s["soc"]["soc_pct"] == 0
    b.set("profile", "charge")
    run(b, 60)
    s = snapshot(b)
    assert s["mos"]["discharge_mos"] and s["soc"]["current_a"] > 10


def test_weak_cell_is_the_one_that_trips():
    b = make("kit48v,profile=discharge,soc=30")
    b.fault("weak:5:60")
    run(b, 3 * 3600)
    s = snapshot(b)
    assert s["cmm"]["min_cell"] == 5
    assert "cell_volt_low_l2" in s["faults"] and not s["mos"]["discharge_mos"]
    assert s["cmm"]["max_mv"] > 3000          # the other cells still have charge


def test_temperature_protection_with_hysteresis():
    b = make("kit36v,profile=idle")
    b.set("temp", "67")
    b.set("ambient", "67")
    run(b, 10)
    s = snapshot(b)
    assert {"dischg_temp_high_l2", "chg_temp_high_l2"} <= set(s["faults"])
    assert not s["mos"]["charge_mos"] and not s["mos"]["discharge_mos"]
    b.set("ambient", "25")
    b.set("temp", "45")
    run(b, 10)
    s = snapshot(b)
    assert s["mos"]["charge_mos"] and s["mos"]["discharge_mos"]
    assert not any("temp" in f for f in s["faults"])


def test_overcurrent_retries_after_32s():
    b = make("kit48v,profile=discharge,load_a=150")    # 1.5 C, L2 at 1.2 C
    run(b, 5)
    assert not b.bms.dis_mos and "dischg_overcurrent_l2" in b.bms.faults()
    b.set("load_a", "30")
    run(b, 35)
    assert b.bms.dis_mos


def test_injected_faults_show_on_the_wire():
    b = make("kit48v,profile=idle")
    b.fault("open_wire:3")
    b.fault("ntc_open:2")
    b.fault("short")
    b.fault("eeprom_err")
    run(b, 2)
    s = snapshot(b)
    assert s["cells"][2] == 0 and s["temps"][1] == -40
    assert {"cell_collect_dropped", "cell_temp_sensor_err", "short_circuit_fault",
            "eeprom_err"} <= set(s["faults"])
    assert not s["mos"]["discharge_mos"]
    b.clear()
    run(b, 2)
    s = snapshot(b)
    assert s["faults"] == [] and s["cells"][2] > 0 and s["mos"]["discharge_mos"]


def test_state_round_trip():
    a = make("kit12v,profile=discharge,soc=70")
    run(a, 600)
    b = make("kit12v,soc=20", seed=9)
    assert b.restore(json.loads(json.dumps(a.save())))
    assert b.bms.soc == a.bms.soc and b.bms.cycles == a.bms.cycles
    assert not make("kit48v").restore(a.save())       # different cell count is refused


# --- transports, against the real gateway reader --------------------------------

def start_sim(*args):
    p = subprocess.Popen([sys.executable, "-m", "bhm_gateway", "sim", "--seed", "1",
                          "--print-every", "0", *args],
                         stdout=subprocess.PIPE, text=True, env=ENV)
    return p


@pytest.mark.skipif(sys.platform != "linux", reason="needs PTYs")
def test_gateway_reads_three_kits_over_pty_and_simctl(tmp_path):
    pytest.importorskip("serial")
    from bhm_gateway.daly import DalyBMS
    sim = start_sim("kit12v", "kit36v", "kit48v", "--link-dir", str(tmp_path))
    try:
        for _ in range(4):
            sim.stdout.readline()
        for name, cells in (("kit12v", 4), ("kit36v", 12), ("kit48v", 16)):
            with DalyBMS(str(tmp_path / name)) as bms:
                snap = bms.read_all()
            assert snap["cells"]["count"] == cells
            assert all(snap["cells"]["voltages_mv"])
            assert snap["pack"]["voltage_v"] == pytest.approx(cells * 3.25, rel=0.12)

        ctl = [sys.executable, "-m", "bhm_gateway", "simctl", "--socket",
               str(tmp_path / "control.sock")]
        out = subprocess.run(ctl + ["fault", "kit48v", "short"], capture_output=True,
                             text=True, env=ENV, timeout=10)
        assert out.returncode == 0, out.stderr
        time.sleep(0.6)                      # one physics tick
        with DalyBMS(str(tmp_path / "kit48v")) as bms:
            snap = bms.read_all()
        assert "short_circuit_fault" in snap["faults"] and not snap["pack"]["discharge_mos"]

        subprocess.run(ctl + ["comm", "kit12v", "online=0"], env=ENV, timeout=10, check=True,
                       capture_output=True)
        with DalyBMS(str(tmp_path / "kit12v"), retries=1, timeout=0.3) as bms:
            with pytest.raises(daly.DalyError):
                bms.read_all()

        out = subprocess.run(ctl + ["--json", "status"], capture_output=True, text=True,
                             env=ENV, timeout=10)
        packs = {p["name"]: p for p in json.loads(out.stdout)["packs"]}
        assert packs["kit48v"]["faults"] == ["short_circuit_fault"]
        assert packs["kit48v"]["requests"] >= 9
    finally:
        sim.terminate()
        sim.wait(5)


def test_gateway_reads_over_tcp_socket_url():
    pytest.importorskip("serial")
    from bhm_gateway.daly import DalyBMS
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    sim = start_sim(f"kit48v@tcp:127.0.0.1:{port}")
    try:
        sim.stdout.readline()
        with DalyBMS(f"socket://127.0.0.1:{port}") as bms:
            snap = bms.read_all()
        assert snap["cells"]["count"] == 16 and len(snap["temps"]["sensors_c"]) == 2
    finally:
        sim.terminate()
        sim.wait(5)


@pytest.mark.skipif(sys.platform != "linux", reason="needs PTYs")
def test_simviz_serves_page_status_and_commands(tmp_path):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from bhm_gateway.sim.viz import make_handler
    sim = start_sim("kit12v", "kit48v", "--link-dir", str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(str(tmp_path / "control.sock")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        for _ in range(3):
            sim.stdout.readline()
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "three" in page and "/api/status" in page
        st = json.load(urllib.request.urlopen(base + "/api/status"))
        packs = {p["name"]: p for p in st["packs"]}
        assert len(packs["kit48v"]["cell_soc"]) == 16 and len(packs["kit12v"]["cell_ah"]) == 4
        req = urllib.request.Request(base + "/api/cmd", method="POST",
                                     data=b"set kit12v profile=charge charge_a=35")
        assert json.load(urllib.request.urlopen(req))["ok"]
        k12 = {p["name"]: p for p in json.load(urllib.request.urlopen(base + "/api/status"))["packs"]}["kit12v"]
        assert (k12["profile"], k12["charge_a"], k12["max_charge_a"]) == ("charge", 35.0, 50.0)
        req = urllib.request.Request(base + "/api/cmd", data=b"fault kit48v short", method="POST")
        assert json.load(urllib.request.urlopen(req))["ok"]
        st = json.load(urllib.request.urlopen(base + "/api/status"))
        assert "short_circuit_fault" in {p["name"]: p for p in st["packs"]}["kit48v"]["faults"]
        bad = urllib.request.Request(base + "/api/cmd", data=b"quit", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 400
    finally:
        httpd.shutdown()
        sim.terminate()
        sim.wait(5)


def _free_ports(n):
    """n consecutive free TCP ports on 127.0.0.1."""
    for _ in range(50):
        base = random.randint(20000, 60000)
        socks = []
        try:
            for i in range(n):
                s = socket.socket()
                socks.append(s)
                s.bind(("127.0.0.1", base + i))
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise RuntimeError("no free ports")


def test_windows_mode_tcp_packs_tcp_control_and_pyserial(monkeypatch, tmp_path):
    """No PTYs, termios or AF_UNIX (as on Windows): packs fall back to TCP 7001.., the
    control channel is TCP, and serial ports go through polled pyserial."""
    pytest.importorskip("serial")
    import threading
    from types import SimpleNamespace
    from bhm_gateway.daly import DalyBMS
    from bhm_gateway.sim import server as srvmod
    from bhm_gateway.sim.viz import control

    base = _free_ports(3)
    monkeypatch.setattr(srvmod, "HAVE_PTY", False)
    monkeypatch.setattr(srvmod, "HAVE_UNIX_SOCKETS", False)
    monkeypatch.setattr(srvmod, "termios", None)
    monkeypatch.setattr(srvmod, "FIRST_AUTO_TCP_PORT", base)
    ctl_addr = f"tcp:127.0.0.1:{base + 2}"

    serial_port = None
    if hasattr(os, "openpty"):                   # a PTY stands in for a COM port
        master, slave = os.openpty()
        serial_port = os.ttyname(slave)
        os.set_blocking(master, False)
    packs = [make("kit12v"), make("kit48v")]
    if serial_port:
        packs.append(make(f"kit36v@{serial_port}"))
    srv = srvmod.Server(packs, control=ctl_addr, print_every=0, bind="127.0.0.1",
                        out=open(os.devnull, "w"))
    srv.open_all()
    srv.load_state = srv.open_all = lambda: None
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    try:
        for port, cells in ((base, 4), (base + 1, 16)):
            with DalyBMS(f"socket://127.0.0.1:{port}") as bms:
                assert bms.read_all()["cells"]["count"] == cells

        if serial_port:                          # pyserial (polled) path
            os.write(master, daly.build_request(daly.CMD_STATUS))
            buf, deadline = b"", time.monotonic() + 3
            while len(buf) < daly.FRAME_LEN and time.monotonic() < deadline:
                try:
                    buf += os.read(master, 64)
                except BlockingIOError:
                    time.sleep(0.01)
            st = daly.decode_status(daly.parse_frames(buf, daly.CMD_STATUS)[0])
            assert st["cell_count"] == 12

        assert control(ctl_addr, "fault kit48v short")["ok"]
        rc = srvmod.ctl_main(SimpleNamespace(socket=ctl_addr, words=["status"], json=False))
        assert rc == 0
        st = control(ctl_addr, "status")
        packs = {p["name"]: p for p in st["packs"]}
        assert packs["kit48v"]["faults"] == ["short_circuit_fault"]
    finally:
        srv.stopping = True
        t.join(3)
        srv.close()
        if serial_port:
            os.close(master)
            os.close(slave)
