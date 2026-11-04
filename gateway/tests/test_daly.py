import json
import os
import subprocess
import sys
import time

import pytest

from bhm_gateway import daly
from bhm_gateway.buffer import MessageBuffer


def test_request_frame():
    f = daly.build_request(daly.CMD_SOC)
    assert f.hex() == "a54090080000000000000000" + "7d"
    assert len(f) == 13


def test_parse_with_noise_echo_and_bad_checksum():
    good = daly.build_response(0x90, bytes.fromhex("021400007 52e03e8".replace(" ", "")))
    bad = bytearray(good)
    bad[12] ^= 0xFF
    echo = daly.build_request(0x90)  # half-duplex adapter echo of our own request
    buf = b"\x00\xff" + echo + bytes(bad) + good
    assert daly.parse_frames(buf, 0x90) == [good[4:12]]
    assert daly.parse_frames(buf, 0x91) == []


def test_decode_soc():
    d = bytes.fromhex("0214 0000 752e 03e8".replace(" ", ""))
    assert daly.decode_soc(d) == {"voltage_v": 53.2, "current_a": -0.2, "soc_pct": 100.0}
    assert daly.decode_soc(d, current_sign=-1)["current_a"] == 0.2


def test_decode_cells_multi_frame_out_of_order():
    f2 = bytes([2, 0x0C, 0xE4, 0x0C, 0xE5, 0, 0, 0])
    f1 = bytes([1, 0x0C, 0xE0, 0x0C, 0xE1, 0x0C, 0xE2, 0])
    assert daly.decode_cell_voltages([f2, f1], 5) == [3296, 3297, 3298, 3300, 3301]


def test_decode_temps_and_status_and_mos():
    assert daly.decode_temperatures([bytes([1, 65, 66, 0, 0, 0, 0, 0])], 2) == [25, 26]
    st = daly.decode_status(bytes([16, 2, 1, 0, 0, 0x01, 0x2C, 0]))
    assert st["cell_count"] == 16 and st["cycles"] == 300 and st["charger_connected"]
    mos = daly.decode_mos(bytes([2, 1, 1, 200, 0, 0x01, 0x86, 0xA0]))
    assert mos["state"] == "discharging" and mos["remaining_ah"] == 100.0


def test_decode_faults_and_balance():
    names, code = daly.decode_faults(bytes([0b00000001, 0, 0b01000000, 0, 0, 0, 0b100, 7]))
    assert names == ["cell_volt_high_l1", "soc_low_l1", "short_circuit_fault"]
    assert code == 7
    assert daly.decode_balance(bytes([0b101, 0b1, 0, 0, 0, 0, 0, 0]), 16) == [1, 3, 9]


def test_buffer_cap_and_order(tmp_path):
    b = MessageBuffer(str(tmp_path / "q.db"), max_rows=3)
    for i in range(5):
        b.put("t", str(i).encode())
    rows = b.peek(10)
    assert [r[2] for r in rows] == [b"2", b"3", b"4"] and b.dropped == 2
    b.delete([rows[0][0]])
    assert len(b) == 2


@pytest.mark.skipif(sys.platform != "linux", reason="needs PTYs")
def test_probe_against_simulator(tmp_path):
    env = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(__file__))}
    sim = subprocess.Popen([sys.executable, "-m", "bhm_gateway", "sim", "kit48v:16",
                            "--link-dir", str(tmp_path)], stdout=subprocess.PIPE, text=True,
                           env=env)
    try:
        sim.stdout.readline()
        out = subprocess.run([sys.executable, "-m", "bhm_gateway", "probe",
                              str(tmp_path / "kit48v")], capture_output=True, text=True,
                             timeout=20, env=env)
        assert out.returncode == 0, out.stderr
        snap = json.loads(out.stdout)
        assert snap["cells"]["count"] == 16
        assert len(snap["cells"]["voltages_mv"]) == 16 and all(snap["cells"]["voltages_mv"])
        assert 40 < snap["pack"]["voltage_v"] < 60
        assert len(snap["temps"]["sensors_c"]) == 2
    finally:
        sim.terminate()
        sim.wait()
