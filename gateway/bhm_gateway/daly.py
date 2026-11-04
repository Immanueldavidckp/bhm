"""Daly BMS RS485/UART protocol driver.

Frame layout (13 bytes, both directions):

    0xA5 | addr | cmd | 0x08 | d0 .. d7 | checksum

  * request addr: 0x40 = upper computer over RS485 (default), 0x80 = UART/BT port
  * response addr: 0x01 (BMS)
  * checksum: sum of the first 12 bytes & 0xFF
  * serial: 9600 baud, 8N1

Commands 0x95 (cell voltages) and 0x96 (temperatures) answer with several
frames; d0 is the 1-based frame number.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

START = 0xA5
FRAME_LEN = 13
DATA_LEN = 8

CMD_SOC = 0x90
CMD_CELL_MINMAX = 0x91
CMD_TEMP_MINMAX = 0x92
CMD_MOS = 0x93
CMD_STATUS = 0x94
CMD_CELL_VOLTAGES = 0x95
CMD_TEMPERATURES = 0x96
CMD_BALANCE = 0x97
CMD_FAULTS = 0x98

CURRENT_OFFSET = 30000
TEMP_OFFSET = 40

STATES = {0: "idle", 1: "charging", 2: "discharging"}

# 0x98 fault bitmap: FAULT_BITS[byte][bit] -> name
FAULT_BITS = [
    ["cell_volt_high_l1", "cell_volt_high_l2", "cell_volt_low_l1", "cell_volt_low_l2",
     "sum_volt_high_l1", "sum_volt_high_l2", "sum_volt_low_l1", "sum_volt_low_l2"],
    ["chg_temp_high_l1", "chg_temp_high_l2", "chg_temp_low_l1", "chg_temp_low_l2",
     "dischg_temp_high_l1", "dischg_temp_high_l2", "dischg_temp_low_l1", "dischg_temp_low_l2"],
    ["chg_overcurrent_l1", "chg_overcurrent_l2", "dischg_overcurrent_l1", "dischg_overcurrent_l2",
     "soc_high_l1", "soc_high_l2", "soc_low_l1", "soc_low_l2"],
    ["cell_volt_diff_l1", "cell_volt_diff_l2", "temp_diff_l1", "temp_diff_l2",
     None, None, None, None],
    ["chg_mos_temp_high", "dischg_mos_temp_high", "chg_mos_temp_sensor_err",
     "dischg_mos_temp_sensor_err", "chg_mos_adhesion_err", "dischg_mos_adhesion_err",
     "chg_mos_open_circuit_err", "dischg_mos_open_circuit_err"],
    ["afe_chip_err", "cell_collect_dropped", "cell_temp_sensor_err", "eeprom_err",
     "rtc_err", "precharge_failure", "comm_failure", "internal_comm_failure"],
    ["current_module_fault", "sum_volt_detect_fault", "short_circuit_fault",
     "low_volt_no_charge", None, None, None, None],
]


class DalyError(Exception):
    """Communication or protocol error talking to the BMS."""


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_request(cmd: int, address: int = 0x40, data: bytes = bytes(DATA_LEN)) -> bytes:
    if len(data) != DATA_LEN:
        raise ValueError("data must be 8 bytes")
    head = bytes([START, address, cmd, DATA_LEN]) + data
    return head + bytes([checksum(head)])


def build_response(cmd: int, data: bytes, address: int = 0x01) -> bytes:
    """Build a BMS->host frame (used by the simulator and tests)."""
    return build_request(cmd, address, data)


def parse_frames(buf: bytes, cmd: int) -> list[bytes]:
    """Extract the 8-byte payloads of every valid frame for `cmd` in `buf`.

    Resyncs on 0xA5, skips frames with bad checksum or another command.
    """
    out = []
    i = 0
    while i <= len(buf) - FRAME_LEN:
        if buf[i] != START:
            i += 1
            continue
        frame = buf[i:i + FRAME_LEN]
        if frame[3] != DATA_LEN or checksum(frame[:12]) != frame[12]:
            i += 1
            continue
        if frame[2] == cmd and frame[1] != 0x40 and frame[1] != 0x80:  # skip our own echo
            out.append(bytes(frame[4:12]))
        i += FRAME_LEN
    return out


def _u16(d: bytes, i: int) -> int:
    return (d[i] << 8) | d[i + 1]


def _u32(d: bytes, i: int) -> int:
    return (d[i] << 24) | (d[i + 1] << 16) | (d[i + 2] << 8) | d[i + 3]


# ---- payload decoders (pure functions, unit tested) -------------------------

def decode_soc(d: bytes, current_sign: int = 1) -> dict:
    return {
        "voltage_v": round(_u16(d, 0) / 10, 1),
        "current_a": round(current_sign * (_u16(d, 4) - CURRENT_OFFSET) / 10, 1),
        "soc_pct": round(_u16(d, 6) / 10, 1),
    }


def decode_cell_minmax(d: bytes) -> dict:
    return {"max_mv": _u16(d, 0), "max_cell": d[2], "min_mv": _u16(d, 3), "min_cell": d[5]}


def decode_temp_minmax(d: bytes) -> dict:
    return {"max_c": d[0] - TEMP_OFFSET, "max_sensor": d[1],
            "min_c": d[2] - TEMP_OFFSET, "min_sensor": d[3]}


def decode_mos(d: bytes) -> dict:
    return {
        "state": STATES.get(d[0], f"unknown_{d[0]}"),
        "charge_mos": bool(d[1]),
        "discharge_mos": bool(d[2]),
        "bms_life": d[3],
        "remaining_ah": round(_u32(d, 4) / 1000, 3),
    }


def decode_status(d: bytes) -> dict:
    return {
        "cell_count": d[0],
        "temp_sensor_count": d[1],
        "charger_connected": bool(d[2]),
        "load_connected": bool(d[3]),
        "dio_bits": d[4],
        "cycles": _u16(d, 5),
    }


def decode_cell_voltages(frames: list[bytes], cell_count: int) -> list[int]:
    cells: dict[int, int] = {}
    for f in frames:
        n = f[0]
        if n < 1:
            continue
        for k in range(3):
            idx = (n - 1) * 3 + k
            if idx < cell_count:
                cells[idx] = _u16(f, 1 + 2 * k)
    return [cells.get(i) for i in range(cell_count)]


def decode_temperatures(frames: list[bytes], sensor_count: int) -> list[int]:
    temps: dict[int, int] = {}
    for f in frames:
        n = f[0]
        if n < 1:
            continue
        for k in range(7):
            idx = (n - 1) * 7 + k
            if idx < sensor_count:
                temps[idx] = f[1 + k] - TEMP_OFFSET
    return [temps.get(i) for i in range(sensor_count)]


def decode_balance(d: bytes, cell_count: int) -> list[int]:
    """1-based cell numbers currently balancing."""
    return [i + 1 for i in range(min(cell_count, 48)) if d[i // 8] >> (i % 8) & 1]


def decode_faults(d: bytes) -> tuple[list[str], int]:
    names = []
    for byte_i, bits in enumerate(FAULT_BITS):
        for bit_i, name in enumerate(bits):
            if name and d[byte_i] >> bit_i & 1:
                names.append(name)
    return names, d[7]


# ---- transport --------------------------------------------------------------

@dataclass
class DalyBMS:
    port: str
    address: int = 0x40
    baudrate: int = 9600
    timeout: float = 0.6
    retries: int = 3
    current_sign: int = 1
    _ser: object = field(default=None, init=False, repr=False)

    def open(self):
        import serial  # pyserial
        if self._ser is None or not self._ser.is_open:
            self._ser = serial.Serial(self.port, self.baudrate, bytesize=8, parity="N",
                                      stopbits=1, timeout=0.05)
        return self

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def request(self, cmd: int, frames: int = 1) -> list[bytes]:
        last_err = None
        for attempt in range(self.retries):
            try:
                return self._request_once(cmd, frames)
            except DalyError as e:
                last_err = e
                log.debug("%s cmd 0x%02X attempt %d failed: %s", self.port, cmd, attempt + 1, e)
                time.sleep(0.1)
            except OSError as e:  # unplugged adapter etc. -> reopen next time
                self.close()
                raise DalyError(f"serial error on {self.port}: {e}") from e
        raise last_err

    def _request_once(self, cmd: int, frames: int) -> list[bytes]:
        self.open()
        ser = self._ser
        ser.reset_input_buffer()
        ser.write(build_request(cmd, self.address))
        ser.flush()
        want = FRAME_LEN * frames
        buf = bytearray()
        deadline = time.monotonic() + self.timeout + 0.02 * frames
        while time.monotonic() < deadline:
            chunk = ser.read(want)
            if chunk:
                buf += chunk
                got = parse_frames(bytes(buf), cmd)
                if len(got) >= frames:
                    return got
        got = parse_frames(bytes(buf), cmd)
        if got:  # partial multi-frame answers are still useful
            return got
        raise DalyError(f"no valid reply to 0x{cmd:02X} ({len(buf)} bytes received)")

    def read_all(self) -> dict:
        """Poll every data command and return one decoded snapshot."""
        status = decode_status(self.request(CMD_STATUS)[0])
        soc = decode_soc(self.request(CMD_SOC)[0], self.current_sign)
        cmm = decode_cell_minmax(self.request(CMD_CELL_MINMAX)[0])
        tmm = decode_temp_minmax(self.request(CMD_TEMP_MINMAX)[0])
        mos = decode_mos(self.request(CMD_MOS)[0])

        n_cells = status["cell_count"]
        n_temps = status["temp_sensor_count"]
        cells = []
        if n_cells:
            cells = decode_cell_voltages(
                self.request(CMD_CELL_VOLTAGES, math.ceil(n_cells / 3)), n_cells)
        temps = []
        if n_temps:
            temps = decode_temperatures(
                self.request(CMD_TEMPERATURES, math.ceil(n_temps / 7)), n_temps)
        balancing = decode_balance(self.request(CMD_BALANCE)[0], n_cells)
        faults, fault_code = decode_faults(self.request(CMD_FAULTS)[0])

        valid = [c for c in cells if c]
        return {
            "pack": {
                **soc,
                "power_w": round(soc["voltage_v"] * soc["current_a"], 1),
                "state": mos["state"],
                "remaining_ah": mos["remaining_ah"],
                "cycles": status["cycles"],
                "charge_mos": mos["charge_mos"],
                "discharge_mos": mos["discharge_mos"],
                "charger_connected": status["charger_connected"],
                "load_connected": status["load_connected"],
                "bms_life": mos["bms_life"],
            },
            "cells": {
                "count": n_cells,
                "voltages_mv": cells,
                "max_mv": cmm["max_mv"],
                "max_cell": cmm["max_cell"],
                "min_mv": cmm["min_mv"],
                "min_cell": cmm["min_cell"],
                "delta_mv": (max(valid) - min(valid)) if valid else cmm["max_mv"] - cmm["min_mv"],
                "balancing": balancing,
            },
            "temps": {
                "sensors_c": temps,
                "max_c": tmm["max_c"],
                "min_c": tmm["min_c"],
            },
            "faults": faults,
            "fault_code": fault_code,
        }
