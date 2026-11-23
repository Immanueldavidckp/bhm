"""Daly BMS simulator: realistic LiFePO4 packs (12 V 4S, 24 V 8S, 36 V 12S, 48 V 16S) that
answer the Daly RS485/UART protocol on PTYs, real serial ports or TCP.

Stdlib only, so it runs on the PC or directly on the STM32MP135 (OpenSTLinux).
"""

from .engine import SimBattery
from .model import PackSpec, parse_spec
from .server import Server, ctl_main, main

__all__ = ["PackSpec", "Server", "SimBattery", "ctl_main", "main", "parse_spec"]
