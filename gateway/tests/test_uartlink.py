"""UART link: framing, console noise, release, and gateway <-> sim through the link."""

import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from bhm_gateway import uartlink as ul

ENV = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(__file__))}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_round_trip_and_split_reads():
    data = ul.encode(1, b"\xa5\x40\x90" + bytes(10)) + ul.encode(0, b"A") + ul.frames(3, bytes(600))
    p = ul.Parser()
    got = []
    for i in range(0, len(data), 7):                # arrives in small pieces
        f, junk = p.feed(data[i:i + 7])
        got += f
        assert junk == b""
    assert [c for c, _ in got] == [1, 0, 3, 3, 3]
    assert b"".join(d for c, d in got if c == 3) == bytes(600)


def test_console_noise_and_corruption_are_skipped():
    good = ul.encode(2, b"reply")
    bad = bytearray(ul.encode(2, b"xxxxx"))
    bad[6] ^= 0x01
    noise = b"[  12.3] usb 1-1: new device\r\nroot@stm32mp1:~# "
    p = ul.Parser()
    frames, junk = p.feed(noise + bytes(bad) + good + b"\x7e")
    assert frames == [(2, b"reply")]
    assert junk.startswith(noise) and p.bad == 1
    frames, junk = p.feed(b"\xb5" + ul.encode(1, b"z")[2:])   # SYNC split across reads
    assert frames == [(1, b"z")]


def test_half_frame_expires_as_noise():
    clk = Clock()
    p = ul.Parser(clock=clk)
    assert p.feed(ul.encode(1, b"abc")[:5]) == ([], b"")
    clk.t = 1.0
    frames, junk = p.feed(b"")
    assert frames == [] and junk                     # the stuck start was dropped
    assert p.feed(ul.encode(1, b"ok"))[0] == [(1, b"ok")]


def test_three_enters_release_but_one_does_not():
    clk = Clock()
    e = ul.EnterDetector(clock=clk)
    assert not e.feed(b"\x03\r")                     # the host's --start poke
    assert not e.feed(b"systemctl start x\r")
    clk.t = 10
    assert not e.feed(b"\r\n")                       # CR LF is one Enter
    assert not e.feed(b"\r")
    assert e.feed(b"\r")
    clk.t = 20
    assert not e.feed(b"\r")
    clk.t = 30                                       # too slow: starts over
    assert not e.feed(b"\r\r")


@pytest.mark.skipif(not hasattr(os, "openpty") or not shutil.which("socat"),
                    reason="needs PTYs and socat")
def test_gateway_reads_sim_through_the_link(tmp_path):
    pytest.importorskip("serial")
    from bhm_gateway.daly import DalyBMS

    # a null-modem cable: two PTYs joined by socat (laptop end, board end)
    pc, brd = tmp_path / "ttyPC", tmp_path / "ttyBOARD"
    cable = subprocess.Popen(["socat", f"pty,raw,echo=0,link={pc}",
                              f"pty,raw,echo=0,link={brd}"])
    sim = subprocess.Popen([sys.executable, "-m", "bhm_gateway", "sim", "kit12v", "kit48v",
                            "--link-dir", str(tmp_path / "sim"), "--print-every", "0"],
                           env=ENV, stdout=subprocess.DEVNULL)
    board = host = None
    try:
        for _ in range(100):
            if pc.exists() and brd.exists() and (tmp_path / "sim" / "kit48v").exists():
                break
            time.sleep(0.05)
        log = open(tmp_path / "log", "w")
        board = ul.Board(["kit12v", "kit48v"], tty_path=str(brd), link_dir=str(tmp_path / "gw"),
                         quiet_kernel=False, out=log)
        host = ul.Host(str(pc), sim_dir=str(tmp_path / "sim"), out=log)
        threads = [threading.Thread(target=x.run, daemon=True) for x in (board, host)]
        for t in threads:
            t.start()
        for _ in range(100):
            if host.board_up and board.host_up:
                break
            time.sleep(0.05)
        assert host.board_up and board.host_up and host.names == ["kit12v", "kit48v"]

        for name, cells in (("kit12v", 4), ("kit48v", 16)):
            with DalyBMS(str(tmp_path / "gw" / name)) as bms:
                snap = bms.read_all()
            assert snap["cells"]["count"] == cells and all(snap["cells"]["voltages_mv"])

        os.write(host.link.fd, b"kernel noise\r\n")       # junk on the wire is harmless
        with DalyBMS(str(tmp_path / "gw" / "kit48v")) as bms:
            assert bms.read_all()["cells"]["count"] == 16

        assert ul.release(str(pc), wait=0.5, out=log) == 1   # no shell on this fake cable...
        for _ in range(40):
            if not threads[0].is_alive():
                break
            time.sleep(0.05)
        assert not threads[0].is_alive()                       # ...but the board side stopped
        assert not (tmp_path / "gw" / "kit48v").exists()
    finally:
        for x in (board, host):
            if x:
                x.stopping = True
        sim.terminate()
        cable.terminate()
        sim.wait(5)
        cable.wait(5)
