"""Entry point.

  python3 -m bhm_gateway --config /etc/bhm/bhm.toml            run the service
  python3 -m bhm_gateway probe /dev/ttyUSB0                     read one BMS and print JSON
  python3 -m bhm_gateway sim kit12v:4 kit36v:12 kit48v:16       fake Daly BMSes on PTYs
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
    return sim_main(args.packs, args.link_dir)


def main(argv=None) -> int:
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

    sm = sub.add_parser("sim", help="simulate Daly BMSes on pseudo-terminals")
    sm.add_argument("packs", nargs="+", help="name:cells, e.g. kit48v:16")
    sm.add_argument("--link-dir", help="also create <dir>/<name> symlinks to the PTYs")

    args = p.parse_args(argv)
    if args.cmd == "probe":
        return _probe(args)
    if args.cmd == "sim":
        return _sim(args)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
