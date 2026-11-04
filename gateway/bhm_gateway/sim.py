"""Daly BMS simulator on pseudo-terminals, for testing without hardware.

Each pack gets its own PTY; point a [[battery]] port at the printed path
(or at <link_dir>/<name> when --link-dir is given).
"""

from __future__ import annotations

import math
import os
import random
import select
import sys
import termios
import time
import tty

from . import daly


class SimPack:
    def __init__(self, name: str, cells: int, temp_sensors: int = 2):
        self.name = name
        self.cells = cells
        self.temp_sensors = temp_sensors
        self.capacity_ah = 100.0
        self.soc = random.uniform(40, 90)
        self.cycles = random.randint(5, 300)
        self.t0 = time.monotonic()
        self.offsets = [random.randint(-15, 15) for _ in range(cells)]

    def state(self):
        t = time.monotonic() - self.t0
        current = 20 * math.sin(t / 60)  # +charge / -discharge, 2 min period
        self.soc = min(100.0, max(0.0, self.soc + current / self.capacity_ah / 36))
        cell_mv = int(3000 + 4 * self.soc + current * 2)  # ~3.0 V empty .. ~3.4 V full
        cells = [cell_mv + o for o in self.offsets]
        temps = [int(25 + abs(current) / 4 + i) for i in range(self.temp_sensors)]
        return current, cells, temps

    def answer(self, cmd: int) -> bytes:
        current, cells, temps = self.state()
        total_dv = round(sum(cells) / 100)
        frames = []

        def frame(data):
            frames.append(daly.build_response(cmd, bytes(data)))

        u16 = lambda v: [(v >> 8) & 0xFF, v & 0xFF]
        if cmd == daly.CMD_SOC:
            frame(u16(total_dv) + u16(total_dv) + u16(int(30000 + current * 10))
                  + u16(int(self.soc * 10)))
        elif cmd == daly.CMD_CELL_MINMAX:
            hi, lo = max(cells), min(cells)
            frame(u16(hi) + [cells.index(hi) + 1] + u16(lo) + [cells.index(lo) + 1, 0, 0])
        elif cmd == daly.CMD_TEMP_MINMAX:
            frame([max(temps) + 40, temps.index(max(temps)) + 1,
                   min(temps) + 40, temps.index(min(temps)) + 1, 0, 0, 0, 0])
        elif cmd == daly.CMD_MOS:
            st = 1 if current > 0.5 else 2 if current < -0.5 else 0
            mah = int(self.capacity_ah * self.soc * 10)
            frame([st, 1, 1, 200, (mah >> 24) & 0xFF, (mah >> 16) & 0xFF,
                   (mah >> 8) & 0xFF, mah & 0xFF])
        elif cmd == daly.CMD_STATUS:
            frame([self.cells, self.temp_sensors, int(current > 0), int(current < 0), 0]
                  + u16(self.cycles) + [0])
        elif cmd == daly.CMD_CELL_VOLTAGES:
            for n in range(math.ceil(self.cells / 3)):
                chunk = cells[n * 3:n * 3 + 3] + [0] * 3
                frame([n + 1] + u16(chunk[0]) + u16(chunk[1]) + u16(chunk[2]) + [0])
        elif cmd == daly.CMD_TEMPERATURES:
            for n in range(math.ceil(self.temp_sensors / 7)):
                chunk = [t + 40 for t in temps[n * 7:n * 7 + 7]] + [0] * 7
                frame([n + 1] + chunk[:7])
        elif cmd == daly.CMD_BALANCE:
            hi = cells.index(max(cells))
            bits = [0] * 8
            if current > 0:
                bits[hi // 8] |= 1 << (hi % 8)
            frame(bits)
        elif cmd == daly.CMD_FAULTS:
            bits = [0] * 8
            if self.soc < 15:
                bits[2] |= 1 << 6  # soc_low_l1
            frame(bits)
        return b"".join(frames)


def main(specs: list[str], link_dir: str | None = None) -> int:
    fds = {}
    for spec in specs:
        name, _, cells = spec.partition(":")
        master, slave = os.openpty()
        tty.setraw(slave, termios.TCSANOW)
        path = os.ttyname(slave)
        fds[master] = (SimPack(name, int(cells or 16)), slave, bytearray())
        if link_dir:
            os.makedirs(link_dir, exist_ok=True)
            link = os.path.join(link_dir, name)
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink(path, link)
            path = f"{link} -> {path}"
        print(f"{name}: {cells or 16} cells on {path}", flush=True)

    try:
        while True:
            ready, _, _ = select.select(list(fds), [], [], 1.0)
            for fd in ready:
                pack, _, buf = fds[fd]
                buf += os.read(fd, 1024)
                while len(buf) >= daly.FRAME_LEN:
                    if buf[0] != daly.START:
                        del buf[0]
                        continue
                    frame = bytes(buf[:daly.FRAME_LEN])
                    del buf[:daly.FRAME_LEN]
                    if daly.checksum(frame[:12]) == frame[12] and frame[1] in (0x40, 0x80):
                        os.write(fd, pack.answer(frame[2]))
    except KeyboardInterrupt:
        return 0
