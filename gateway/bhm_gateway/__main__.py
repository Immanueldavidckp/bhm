"""Entry point.

  python3 -m bhm_gateway --config /etc/bhm/bhm.toml            run the service
  python3 -m bhm_gateway probe /dev/ttyUSB0                     read one BMS and print JSON
  python3 -m bhm_gateway sim kit12v kit36v kit48v --link-dir /tmp/bms   simulated Daly BMSes
  python3 -m bhm_gateway simctl --socket /tmp/bms/control.sock status   drive the simulator
  python3 -m bhm_gateway simviz --socket /tmp/bms/control.sock          live 3D view (browser)

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


def main(argv=None) -> int:
    from .sim.server import DEFAULT_CONTROL
    # Windows consoles and redirected output default to cp1252, which cannot print the
    # status lines' Δ/°; never crash on a character the console cannot show.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
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

    args = p.parse_args(argv)
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
