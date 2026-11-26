"""The charge-test scenario and the server's health analysis, checked against the simulator's truth."""

import importlib.util
import json
import os

import pytest

from bhm_gateway import chargetest
from bhm_gateway.config import BatteryConfig, Config, GatewayConfig, MqttConfig

HERE = os.path.dirname(__file__)
HEALTH_PY = os.path.join(HERE, "..", "..", "server-dev", "api", "health.py")


def load_health():
    spec = importlib.util.spec_from_file_location("bhm_health", HEALTH_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    sim, snaps, truth = chargetest.simulate(chargetest.SPEC.format(id="ct"), seed=7)
    cfg = Config(GatewayConfig(site_id="lab1"), MqttConfig(host="dry-run", client_id="t"),
                 [BatteryConfig(id="ct", port="sim", nominal_voltage=12.8)])
    gw = chargetest.build_messages(cfg, snaps, end=1_800_000_000.0,
                                   buffer_path=str(tmp_path_factory.mktemp("b") / "b.db"))
    rows = gw.buffer.peek(10_000)
    gw.buffer.close()
    msgs = [(t, json.loads(p)) for _, t, p, _, _ in rows]
    return truth, msgs


def test_scenario_is_a_four_hour_charge(run):
    truth, msgs = run
    tele = [m for t, m in msgs if t.endswith("/telemetry")]
    assert 3.8 * 3600 <= truth["duration_s"] <= 4.3 * 3600
    assert len(tele) == pytest.approx(truth["duration_s"] / 10, abs=2)
    first, last = tele[0], tele[-1]
    assert first["ts"] < last["ts"] and last["ts"] == "2027-01-15T08:00:00Z"
    assert first["pack"]["soc_pct"] == pytest.approx(10.5, abs=1)
    assert last["pack"]["soc_pct"] == 100.0 and abs(last["pack"]["current_a"]) < 0.5
    peak = max(m["pack"]["current_a"] for m in tele)
    assert peak == pytest.approx(25, abs=0.5)
    cv = [m for m in tele if m["pack"]["voltage_v"] >= 14.3 and m["pack"]["current_a"] > 1]
    assert 12 * 60 <= len(cv) * 10 <= 35 * 60          # CV taper lasts 12..35 min
    assert max(m["temps"]["max_c"] for m in tele) <= 30  # 0.25 C at 24 °C stays cool
    assert any(m["cells"]["balancing"] for m in tele)
    alarms = [m for t, m in msgs if t.endswith("/alarm")]
    assert alarms and alarms[0]["raised"] == ["soc_low_l1"]


def test_health_finds_the_weak_and_the_high_resistance_cell(run):
    truth, msgs = run
    h = load_health()
    r = h.analyse([h.reading(m) for t, m in msgs if t.endswith("/telemetry")])
    assert r["rated_ah"] == 100.0
    true_caps = truth["cell_capacity_ah"]
    for got, want in zip([c["capacity_ah"] for c in r["cell_health"]], true_caps):
        assert got == pytest.approx(want, rel=0.02)
    assert r["soh_pct"] == pytest.approx(min(true_caps), abs=2)     # weakest cell limits
    for got, r0, dcir in zip(r["ir"]["cell_mohm"], truth["cell_r0_mohm"],
                             truth["cell_dcir_10s_mohm"]):
        assert 0.85 * r0 <= got <= 1.15 * dcir
    st = {c["cell"]: c["status"] for c in r["cell_health"]}
    assert st == {1: "Good", 2: "Replace", 3: "Watch", 4: "Good"}
    assert r["abnormal_cells"] == [2, 3] and r["status"] == "Replace"
    good = [e for e in r["ir_events"] if e["quality"] == "good"]
    assert [e["kind"] for e in good] == ["charger on", "charger off", "charger on"]
    ch = r["charges"][-1]
    assert ch["full"] and ch["pauses"] and ch["pauses"][0]["s"] == pytest.approx(120, abs=10)


def test_health_without_a_usable_charge_says_why():
    h = load_health()
    rs = [{"t": i * 10.0, "i": 0.0, "v": 13.3, "soc": 60.0, "rem": 60.0,
           "cells": [3300, 3301, 3299, 3300], "bal": [], "tmax": 25, "tmin": 25}
          for i in range(30)]
    r = h.analyse(rs)
    assert r["soh_pct"] is None and r["status"] == "Unknown"
    assert all(c["status"] == "Unknown" for c in r["cell_health"])
    assert h.soc_from_ocv(3200) == pytest.approx(0.10) and h.soc_from_ocv(2400) == 0.0
