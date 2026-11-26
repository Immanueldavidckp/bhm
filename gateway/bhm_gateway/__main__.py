"""Entry point.

  python3 -m bhm_gateway --config /etc/bhm/bhm.toml            run the service
  python3 -m bhm_gateway probe /dev/ttyUSB0                     read one BMS and print JSON
  python3 -m bhm_gateway sim kit12v kit36v kit48v --link-dir /tmp/bms   simulated Daly BMSes
  python3 -m bhm_gateway simctl --socket /tmp/bms/control.sock status   drive the simulator
  python3 -m bhm_gateway simviz --socket /tmp/bms/control.sock          live 3D view (browser)
  python3 -m bhm_gateway uartlink host --start       PC sim -> board over the console UART
  python3 -m bhm_gateway --config laptop.toml chargetest   simulated 4 h charge test -> broker
  python3 -m bhm_gateway status                      MQTT / network / battery check (text)
  python3 -m bhm_gateway display                     the same on the board's LCD (GTK)

On Windows (no PTYs): packs are served on TCP 7001, 7002, ... and simctl/simviz talk to the
simulator on tcp:127.0.0.1:7000 by default, e.g.
  python -m bhm_gateway sim kit12v kit36v kit48v
  python -m bhm_gateway probe socket://127.0.0.1:7003
  python -m bhm_gateway simviz
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def _run(args) -> int:
    from .config import ConfigError, load
    from .service import Gateway
    try:
        cfg = load(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(level=args.log_level or cfg.gateway.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Gateway(cfg).run()
    return 0


def _probe(args) -> int:
    from .daly import DalyBMS, DalyError
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    addrs = [args.address] if args.address is not None else [0x40, 0x80]
    for addr in addrs:
        try:
            with DalyBMS(args.port, address=addr, baudrate=args.baudrate) as bms:
                data = bms.read_all()
        except DalyError as e:
            print(f"address 0x{addr:02X}: {e}", file=sys.stderr)
            continue
        print(f"# Daly BMS answered on {args.port} with request address 0x{addr:02X}",
              file=sys.stderr)
        print(json.dumps(data, indent=2))
        return 0
    return 1


def _sim(args) -> int:
    from .sim import main as sim_main
    return sim_main(args)


def _simctl(args) -> int:
    from .sim import ctl_main
    return ctl_main(args)


def _no_quickedit_pause() -> None:
    """Windows pauses a console program while text is selected in its window (QuickEdit),
    so one stray click froze the simulator and every client timed out. Turn that off."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)                     # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)   # -QUICK_EDIT +EXTENDED
    except Exception:                                 # not a console (redirected): nothing to do
        pass


def main(argv=None) -> int:
    from .sim.server import DEFAULT_CONTROL
    # Windows consoles and redirected output default to cp1252, which cannot print the
    # status lines' Δ/°; never crash on a character the console cannot show.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    _no_quickedit_pause()
    p = argparse.ArgumentParser(prog="bhm_gateway", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="/etc/bhm/bhm.toml")
    p.add_argument("--log-level", default=None)
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("probe", help="read one Daly BMS and print the decoded snapshot")
    pr.add_argument("port")
    pr.add_argument("--address", type=lambda s: int(s, 0), default=None,
                    help="request address (default: try 0x40 RS485, then 0x80 UART)")
    pr.add_argument("--baudrate", type=int, default=9600)
    pr.add_argument("-v", "--verbose", action="store_true")

    sm = sub.add_parser(
        "sim", help="simulate Daly BMSes (LiFePO4 12/24/36/48 V) on PTYs, serial or TCP",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""\
pack spec: NAME[:PRESET|CELLS][@PORT][,key=value...]
  PRESET   12v (4S) 24v (8S) 36v (12S) 48v (16S); guessed from NAME if omitted
  PORT     none = PTY (Windows: TCP 7001, 7002, ...), /dev/ttyUSBx or COMx = real serial
           (RS485 adapter), tcp:[HOST:]PORT
  keys     ah soh soc cycles temps ambient profile charge_a load_a max_charge_a
           max_discharge_a     profiles: cycle drive charge discharge idle
examples
  sim kit12v kit36v kit48v --link-dir /tmp/bms --speed 60
  sim kit48v:48v@/dev/ttyUSB0,ah=100,profile=drive
  sim kit12v@tcp:7001 kit36v@tcp:7002 kit48v@tcp:7003
  sim kit12v@COM3 kit36v@COM4 kit48v@COM5             (Windows, needs pyserial)""")
    sm.add_argument("packs", nargs="+", help="pack specs, see below")
    sm.add_argument("--link-dir", help="create <dir>/<name> symlinks to the PTYs "
                    "(and <dir>/control.sock)")
    sm.add_argument("--speed", type=float, default=1.0,
                    help="simulated seconds per real second (60 = an hour per minute)")
    sm.add_argument("--seed", type=int, default=None, help="random seed (repeatable runs)")
    sm.add_argument("--control", default=None,
                    help="control socket: a Unix socket path or tcp:[HOST:]PORT "
                    "(Windows default: tcp:127.0.0.1:7000)")
    sm.add_argument("--group", default=None,
                    help="give the PTYs to this group, mode 660 (e.g. dialout on the board)")
    sm.add_argument("--state-file", default=None,
                    help="keep SOC/cycles across restarts in this JSON file")
    sm.add_argument("--print-every", type=float, default=10.0,
                    help="status line interval in seconds, 0 = quiet")
    sm.add_argument("--bind", default="0.0.0.0", help="address for tcp: ports")

    sc = sub.add_parser("simctl", help="send a command to a running simulator")
    sc.add_argument("--socket", default=DEFAULT_CONTROL,
                    help="simulator control: Unix socket path or tcp:[HOST:]PORT")
    sc.add_argument("--json", action="store_true", help="print the raw JSON reply")
    sc.add_argument("words", nargs="*", help="command, e.g. status | fault kit48v short")

    sv = sub.add_parser("simviz", help="live 3D view of a running simulator, in the browser")
    sv.add_argument("--socket", default=DEFAULT_CONTROL,
                    help="simulator control: Unix socket path or tcp:[HOST:]PORT")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--bind", default="127.0.0.1",
                    help="listen address (0.0.0.0 to open it from another machine)")

    ul = sub.add_parser(
        "uartlink", help="carry the packs' Daly traffic over the console UART (sim on a PC)",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""\
PC (simulator first, then the link; close minicom while the link runs):
  python3 -m bhm_gateway sim kit12v kit36v kit48v --link-dir /tmp/bms
  python3 -m bhm_gateway uartlink host --port /dev/ttyACM0 --start
board (bhm-uartlink.service; --start on the PC starts it from the board's shell):
  python3 -m bhm_gateway uartlink board kit12v kit36v kit48v   -> /run/bhm-uart/<pack>
console back: press Enter 3x in minicom, or  uartlink release --port /dev/ttyACM0""")
    us = ul.add_subparsers(dest="side", required=True)
    ub = us.add_parser("board", help="on the MP135: console UART <-> one PTY per pack")
    ub.add_argument("packs", nargs="+", help="pack names, in channel order")
    ub.add_argument("--tty", default="/dev/ttySTM0")
    ub.add_argument("--baud", type=int, default=115200)
    ub.add_argument("--link-dir", default="/run/bhm-uart", help="<dir>/<pack> PTY symlinks")
    ub.add_argument("--group", default=None, help="give the PTYs to this group, mode 660")
    ub.add_argument("--keep-kernel-messages", action="store_true",
                    help="leave the kernel console log level alone")
    uh = us.add_parser("host", help="on the PC: console UART <-> the simulator's packs")
    uh.add_argument("--port", default="/dev/ttyACM0" if os.name != "nt" else "COM3",
                    help="the ST-LINK virtual COM port")
    uh.add_argument("--baud", type=int, default=115200)
    uh.add_argument("--sim-dir", default=None,
                    help="simulator --link-dir, packs are <dir>/<name> (default /tmp/bms)")
    uh.add_argument("--sim-tcp", default=None, metavar="[HOST:]PORT",
                    help="simulator on TCP: the board's 1st pack on PORT, 2nd on PORT+1 ... "
                    "(default on Windows 127.0.0.1:7001)")
    uh.add_argument("--start", action="store_true",
                    help="if the board is at its shell, start bhm-uartlink there")
    uh.add_argument("--hide-console", action="store_true",
                    help="do not print board console text seen between frames")
    ur = us.add_parser("release", help="stop the board side and get the board's shell back")
    ur.add_argument("--port", default="/dev/ttyACM0" if os.name != "nt" else "COM3")
    ur.add_argument("--baud", type=int, default=115200)

    from .display import DEFAULT_STATUS
    st = sub.add_parser("status", help="is MQTT connected, are all batteries answering and "
                        "their data reaching the broker? (exit 0 = all OK)")
    st.add_argument("--status", default=DEFAULT_STATUS, help="gateway status file")
    st.add_argument("--json", action="store_true", help="print the raw status file")
    dp = sub.add_parser("display", help="status screen for the board's LCD (GTK under Weston)")
    dp.add_argument("--status", default=DEFAULT_STATUS, help="gateway status file")
    dp.add_argument("--windowed", action="store_true", help="normal window, not fullscreen")
    dp.add_argument("--png", default=None, help="render one frame to this PNG and exit")
    dp.add_argument("--width", type=int, default=480)
    dp.add_argument("--height", type=int, default=272)

    ct = sub.add_parser(
        "chargetest", help="simulated 4 h charge test of a 12 V 100 Ah kit, published like the gateway",
        description="Simulate one bench charge (12 V 100 Ah LiFePO4, 25 A, 24 °C, a weak cell and "
                    "a high-resistance cell) and publish it through the gateway code with "
                    "timestamps over the hours before --end. MQTT settings come from --config.")
    ct.add_argument("--battery", default="chargetest12v", help="battery id (default chargetest12v)")
    ct.add_argument("--site", default="lab1", help="site id for --dry-run (else from --config)")
    ct.add_argument("--spec", default=None, help="sim pack spec (default: the scenario above)")
    ct.add_argument("--seed", type=int, default=7)
    ct.add_argument("--end", default=None, help="UTC time the test ends, ISO 8601 (default now)")
    ct.add_argument("--out", default=None, help="also write the messages to this JSONL file")
    ct.add_argument("--truth", default=None, help="write the simulator's true values (JSON)")
    ct.add_argument("--dry-run", action="store_true", help="do not publish")

    args = p.parse_args(argv)
    if args.cmd == "chargetest":
        from .chargetest import main as chargetest_main
        return chargetest_main(args)
    if args.cmd == "status":
        from .display import status_main
        return status_main(args)
    if args.cmd == "display":
        from .display import display_main
        return display_main(args)
    if args.cmd == "uartlink":
        from .uartlink import main as uartlink_main
        return uartlink_main(args)
    if args.cmd == "simviz":
        from .sim.viz import main as viz_main
        return viz_main(args)
    if args.cmd == "probe":
        return _probe(args)
    if args.cmd == "sim":
        return _sim(args)
    if args.cmd == "simctl":
        return _simctl(args)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
