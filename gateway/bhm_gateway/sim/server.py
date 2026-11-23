"""Serve simulated Daly BMSes on PTYs, real serial ports or TCP, plus a control socket.

Single-threaded select() loop: Daly request frames in, response frames out after a
realistic delay (BMS latency + 9600-baud wire time), physics advanced on a timer.

Runs on Linux and Windows. Windows has no PTYs, termios or AF_UNIX sockets, so there a
pack without a port is served on TCP (7001, 7002, ...), COM ports go through pyserial,
and the control channel is TCP (127.0.0.1:7000).
"""

from __future__ import annotations

import heapq
import json
import os
import random
import select
import signal
import socket
import sys
import time
from dataclasses import dataclass, field

try:                                   # POSIX only
    import grp
    import termios
    import tty
except ImportError:                    # Windows
    grp = termios = tty = None

from .. import daly
from .engine import FAULT_HELP, SimBattery
from .model import parse_spec

HAVE_PTY = termios is not None and hasattr(os, "openpty")
HAVE_UNIX_SOCKETS = hasattr(socket, "AF_UNIX")
DEFAULT_CONTROL = "/run/bhm-sim/control.sock" if HAVE_UNIX_SOCKETS else "tcp:127.0.0.1:7000"
FIRST_AUTO_TCP_PORT = 7001             # packs without a port when there are no PTYs
POLL_S = 0.01                          # pyserial ports (Windows) are polled, not select()ed

TICK_S = 0.25          # physics update period (wall clock)
MAX_STEP_S = 1.0       # largest physics step (sim time)
BAUD_S_PER_BYTE = 10 / 9600

CONTROL_HELP = f"""commands (NAME may be "all"):
  status [NAME]
  set NAME key=value ...      profile=cycle|drive|charge|discharge|idle soc=% bms_soc=%
                              ambient=°C temp=°C load_a= charge_a= max_charge_a=
                              max_discharge_a= cycles=
  fault NAME FAULT            {FAULT_HELP}
  clear NAME [FAULT|all]
  comm NAME key=value ...     online=0|1 drop=0..1 corrupt=0..1 latency=seconds
  speed X                     simulation speed multiplier"""


@dataclass
class Conn:
    battery: SimBattery
    fd: int
    kind: str                       # pty | serial | tcp
    sock: socket.socket | None = None
    ser: object | None = None       # pyserial port (Windows COM ports)
    buf: bytearray = field(default_factory=bytearray)

    def read(self) -> bytes:
        if self.sock is not None:
            return self.sock.recv(1024)
        if self.ser is not None:
            n = self.ser.in_waiting
            return self.ser.read(n) if n else b""
        return os.read(self.fd, 1024)

    def write(self, data: bytes) -> None:
        if self.sock is not None:
            self.sock.sendall(data)
        elif self.ser is not None:
            self.ser.write(data)
        else:
            try:
                os.write(self.fd, data)
            except BlockingIOError:     # nobody reading the PTY: drop, like a real wire
                pass


class Server:
    def __init__(self, batteries: list[SimBattery], *, speed: float = 1.0,
                 link_dir: str | None = None, group: str | None = None,
                 control: str | None = None, state_file: str | None = None,
                 print_every: float = 10.0, bind: str = "0.0.0.0", rng=None, out=sys.stdout):
        self.batteries = {b.name: b for b in batteries}
        self.speed = speed
        self.link_dir = link_dir
        if group and grp is None:
            raise ValueError("--group needs a POSIX system (not available on Windows)")
        self.gid = grp.getgrnam(group).gr_gid if group else None
        self.control_path = control
        self.state_file = state_file
        self.print_every = print_every
        self.bind = bind
        self.rng = rng or random.Random()
        self.out = out
        self.conns: dict[int, Conn] = {}
        self.listeners: dict[int, tuple[socket.socket, SimBattery]] = {}
        self.control_sock: socket.socket | None = None
        self.pending: list = []
        self._seq = 0
        self._keep: list = []          # PTY slave fds held open
        self.polled: set[int] = set()  # conns read by polling (pyserial), not select()
        self._next_key = -1            # dict keys for polled conns (no fd to select on)
        self.stopping = False

    def log(self, msg: str) -> None:
        print(msg, file=self.out, flush=True)

    # --- endpoints ----------------------------------------------------------------
    def open_all(self) -> None:
        if self.link_dir and HAVE_PTY:
            os.makedirs(self.link_dir, exist_ok=True)
        auto_port = FIRST_AUTO_TCP_PORT
        used = {b.spec.port for b in self.batteries.values()}
        for b in self.batteries.values():
            port = b.spec.port
            if port is None and not HAVE_PTY:          # Windows: no PTYs, serve on TCP
                while f"tcp:{auto_port}" in used:
                    auto_port += 1
                port = f"tcp:{auto_port}"
                auto_port += 1
            if port is None:
                where = self._open_pty(b)
            elif port.startswith("tcp:"):
                where = self._open_tcp(b, port[4:])
            else:
                where = self._open_serial(b, port)
            self.log(f"{b.name}: {b.spec.cells}S LiFePO4 {b.spec.nominal_v} V "
                     f"{b.spec.capacity_ah:g} Ah, profile {b.spec.profile}, on {where}")
        if self.control_path:
            self._open_control()

    def _open_pty(self, b: SimBattery) -> str:
        master, slave = os.openpty()
        tty.setraw(slave, termios.TCSANOW)
        os.set_blocking(master, False)
        path = os.ttyname(slave)
        self._keep.append(slave)        # keep the slave open so the master never sees EIO
        if self.gid is not None:        # let the gateway's service user open it
            try:
                os.chown(path, -1, self.gid)
                os.chmod(path, 0o660)
            except OSError as e:
                self.log(f"warning: cannot give {path} to group {self.gid}: {e}")
        self.conns[master] = Conn(b, master, "pty")
        if self.link_dir:
            link = os.path.join(self.link_dir, b.name)
            tmp = f"{link}.tmp{os.getpid()}"
            os.symlink(path, tmp)
            os.replace(tmp, link)
            return f"{link} -> {path}"
        return path

    def _open_serial(self, b: SimBattery, path: str) -> str:
        if termios is None:
            return self._open_pyserial(b, path)
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(fd, termios.TCSANOW)
        attrs = termios.tcgetattr(fd)
        attrs[2] = (attrs[2] & ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
                    | termios.CS8 | termios.CLOCAL | termios.CREAD)
        attrs[4] = attrs[5] = termios.B9600
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        self.conns[fd] = Conn(b, fd, "serial")
        return f"{path} (9600 8N1)"

    def _open_pyserial(self, b: SimBattery, path: str) -> str:
        try:
            import serial  # pyserial
        except ImportError:
            raise SystemExit(f"sim: serial port {path} needs pyserial on this OS "
                             "(pip install pyserial)") from None
        ser = serial.Serial(path, 9600, bytesize=8, parity="N", stopbits=1, timeout=0,
                            write_timeout=1)
        ser.reset_input_buffer()
        key, self._next_key = self._next_key, self._next_key - 1
        self.conns[key] = Conn(b, key, "serial", ser=ser)
        self.polled.add(key)
        return f"{path} (9600 8N1)"

    def _open_tcp(self, b: SimBattery, addr: str) -> str:
        host, port = _split_tcp(addr, self.bind)
        s = _tcp_listener(host, port)
        s.setblocking(False)
        self.listeners[s.fileno()] = (s, b)
        local = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return (f"tcp {host}:{port} (gateway/probe port = socket://{local}:{port}, "
                f"or socket://<this-pc-ip>:{port} from the board)")

    def _open_control(self) -> None:
        path = self.control_path
        if is_tcp_addr(path):
            host, port = _split_tcp(path, "127.0.0.1")
            s = _tcp_listener(host, port)
        else:
            if not HAVE_UNIX_SOCKETS:
                raise SystemExit(f"sim: control socket {path!r} needs AF_UNIX, which this OS "
                                 f"lacks; use --control {DEFAULT_CONTROL}")
            if os.path.exists(path):
                os.unlink(path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(path)
            os.chmod(path, 0o660)
            s.listen(4)
        self.control_sock = s
        self.log(f"control: {path}  (python -m bhm_gateway simctl --socket {path} help)")

    # --- Daly traffic -------------------------------------------------------------
    def _on_readable(self, conn: Conn) -> None:
        try:
            data = conn.read()
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            if conn.kind == "tcp":
                self._close(conn)
            return
        buf = conn.buf
        buf += data
        while len(buf) >= daly.FRAME_LEN:
            if buf[0] != daly.START:
                del buf[0]
                continue
            frame = bytes(buf[:daly.FRAME_LEN])
            if daly.checksum(frame[:12]) != frame[12]:
                del buf[0]
                continue
            del buf[:daly.FRAME_LEN]
            if frame[1] in (0x40, 0x80):     # requests only; ignores our own RS485 echo
                self._on_request(conn, frame[2])

    def _on_request(self, conn: Conn, cmd: int) -> None:
        b = conn.battery
        b.requests += 1
        c = b.comm
        if not c["online"] or self.rng.random() < c["drop"]:
            return
        reply = b.bms.answer(cmd)
        if not reply:
            return
        if self.rng.random() < c["corrupt"]:
            reply = bytearray(reply)
            reply[self.rng.randrange(4, len(reply))] ^= 1 << self.rng.randrange(8)
            reply = bytes(reply)
        delay = c["latency"]
        if conn.kind != "serial":            # a real UART paces itself
            delay += len(reply) * BAUD_S_PER_BYTE
        self._seq += 1
        heapq.heappush(self.pending, (time.monotonic() + delay, self._seq, conn.fd, reply))

    def _flush_pending(self, now: float) -> None:
        while self.pending and self.pending[0][0] <= now:
            _, _, fd, data = heapq.heappop(self.pending)
            conn = self.conns.get(fd)
            if conn:
                try:
                    conn.write(data)
                except OSError:
                    self._close(conn)

    def _close(self, conn: Conn) -> None:
        self.conns.pop(conn.fd, None)
        self.polled.discard(conn.fd)
        if conn.sock:
            conn.sock.close()
        if conn.ser is not None:
            conn.ser.close()

    # --- control ------------------------------------------------------------------
    def _targets(self, name: str) -> list[SimBattery]:
        if name == "all":
            return list(self.batteries.values())
        if name not in self.batteries:
            raise ValueError(f"no pack {name!r} (have {', '.join(self.batteries)})")
        return [self.batteries[name]]

    def command(self, line: str) -> dict:
        words = line.split()
        if not words or words[0] == "help":
            return {"ok": True, "help": CONTROL_HELP}
        cmd, args = words[0], words[1:]
        try:
            if cmd == "status":
                return {"ok": True, "speed": self.speed,
                        "packs": [b.status() for b in self._targets(args[0] if args else "all")]}
            if cmd == "speed":
                self.speed = max(0.0, float(args[0]))
                return {"ok": True, "speed": self.speed}
            if cmd not in ("set", "fault", "clear", "comm"):
                raise ValueError(f"unknown command {cmd!r}; try help")
            if not args:
                raise ValueError(f"{cmd}: missing pack name")
            targets = self._targets(args[0])
            for b in targets:
                if cmd == "set":
                    for kv in args[1:]:
                        k, _, v = kv.partition("=")
                        b.set(k, v)
                elif cmd == "fault":
                    for f in args[1:] or [""]:
                        b.fault(f)
                elif cmd == "clear":
                    b.clear(args[1] if len(args) > 1 else "all")
                elif cmd == "comm":
                    for kv in args[1:]:
                        k, _, v = kv.partition("=")
                        if k not in b.comm:
                            raise ValueError(f"comm keys: {', '.join(b.comm)}")
                        b.comm[k] = bool(int(v)) if k == "online" else float(v)
            self.log(f"control: {line.strip()}")
            return {"ok": True, "packs": [b.status() for b in targets]}
        except (ValueError, IndexError) as e:
            return {"ok": False, "error": str(e) or "missing argument"}

    def _on_control(self) -> None:
        client, _ = self.control_sock.accept()
        with client:
            client.settimeout(2)
            data = b""
            try:
                while not data.endswith(b"\n") and len(data) < 4096:
                    chunk = client.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                reply = self.command(data.decode(errors="replace"))
                client.sendall(json.dumps(reply).encode() + b"\n")
            except OSError:
                pass

    # --- state ----------------------------------------------------------------------
    def load_state(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
        except (OSError, ValueError) as e:
            self.log(f"warning: ignoring state file {self.state_file}: {e}")
            return
        for name, d in saved.items():
            if name in self.batteries and self.batteries[name].restore(d):
                self.log(f"{name}: restored SOC {d['bms_soc']:.1f} %, {d['cycles']} cycles")

    def save_state(self) -> None:
        if not self.state_file:
            return
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({n: b.save() for n, b in self.batteries.items()}, f)
        os.replace(tmp, self.state_file)

    # --- main loop -------------------------------------------------------------------
    def advance(self, wall_dt: float) -> None:
        sim_dt = wall_dt * self.speed
        while sim_dt > 1e-9:
            step = min(sim_dt, MAX_STEP_S)
            for b in self.batteries.values():
                b.step(step)
            sim_dt -= step

    def run(self) -> None:
        self.load_state()
        self.open_all()
        last = next_print = next_save = time.monotonic()
        next_tick = last + TICK_S
        while not self.stopping:
            now = time.monotonic()
            wait = next_tick - now
            if self.pending:
                wait = min(wait, self.pending[0][0] - now)
            if self.polled:
                wait = min(wait, POLL_S)
            fds = [k for k in self.conns if k not in self.polled] + list(self.listeners)
            if self.control_sock:
                fds.append(self.control_sock.fileno())
            if fds:
                try:
                    ready, _, _ = select.select(fds, [], [], max(0.0, wait))
                except InterruptedError:
                    continue
            else:                       # Windows select() refuses empty lists
                time.sleep(max(0.0, wait))
                ready = []
            ready += [k for k in self.polled if k in self.conns]
            for fd in ready:
                if fd in self.conns:
                    self._on_readable(self.conns[fd])
                elif fd in self.listeners:
                    lsock, b = self.listeners[fd]
                    try:
                        s, addr = lsock.accept()
                    except BlockingIOError:
                        continue
                    s.setblocking(False)
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.conns[s.fileno()] = Conn(b, s.fileno(), "tcp", sock=s)
                    self.log(f"{b.name}: tcp client {addr[0]}:{addr[1]} connected")
                elif self.control_sock and fd == self.control_sock.fileno():
                    self._on_control()
            now = time.monotonic()
            self._flush_pending(now)
            if now >= next_tick:
                self.advance(now - last)
                last, next_tick = now, now + TICK_S
            if self.print_every and now >= next_print:
                for b in self.batteries.values():
                    self.log(b.line())
                next_print = now + self.print_every
            if now >= next_save:
                self.save_state()
                next_save = now + 60

    def close(self) -> None:
        try:
            self.save_state()
        except OSError as e:
            self.log(f"warning: cannot save state: {e}")
        for conn in list(self.conns.values()):
            if conn.ser is not None:
                conn.ser.close()
        if self.control_sock:
            self.control_sock.close()
            if not is_tcp_addr(self.control_path) and os.path.exists(self.control_path):
                os.unlink(self.control_path)


def is_tcp_addr(addr: str) -> bool:
    return addr.startswith("tcp:")


def _split_tcp(addr: str, default_host: str) -> tuple[str, int]:
    """'tcp:7000', 'tcp:HOST:7000' or 'HOST:7000' -> (host, port)."""
    if addr.startswith("tcp:"):
        addr = addr[4:]
    host, _, port = addr.rpartition(":")
    return host or default_host, int(port)


def _tcp_listener(host: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":   # on Windows SO_REUSEADDR lets a second sim steal the port silently
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except OSError as e:
        s.close()
        raise SystemExit(f"sim: cannot listen on {host}:{port}: {e} "
                         "(already running, or port in use?)") from None
    s.listen(4)
    return s


def connect_control(addr: str, timeout: float | None = None) -> socket.socket:
    """Connect to a simulator control channel: a Unix socket path or tcp:[HOST:]PORT."""
    if is_tcp_addr(addr):
        return socket.create_connection(_split_tcp(addr, "127.0.0.1"), timeout=timeout)
    if not HAVE_UNIX_SOCKETS:
        raise OSError(f"{addr!r} is a Unix socket path, which this OS lacks; "
                      f"use --socket {DEFAULT_CONTROL}")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(addr)
    except OSError:
        s.close()
        raise
    return s


def main(args) -> int:
    rng = random.Random(args.seed)
    try:
        batteries = [SimBattery(parse_spec(s), rng) for s in args.packs]
    except ValueError as e:
        print(f"sim: {e}", file=sys.stderr)
        return 2
    if len({b.name for b in batteries}) != len(batteries):
        print("sim: pack names must be unique", file=sys.stderr)
        return 2
    control = args.control
    if not HAVE_PTY and args.link_dir:
        print(f"sim: no PTYs on this OS, ignoring --link-dir; packs without a port are "
              f"served on TCP from {FIRST_AUTO_TCP_PORT}", file=sys.stderr)
    if control is None:
        if not HAVE_UNIX_SOCKETS:
            control = DEFAULT_CONTROL          # Windows: always on, so simctl/simviz just work
        elif args.link_dir:
            control = os.path.join(args.link_dir, "control.sock")
    try:
        srv = Server(batteries, speed=args.speed, link_dir=args.link_dir, group=args.group,
                     control=control, state_file=args.state_file,
                     print_every=args.print_every, bind=args.bind, rng=rng)
    except (ValueError, KeyError) as e:
        print(f"sim: {e}", file=sys.stderr)
        return 2

    def stop(*_):
        srv.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        srv.run()
    finally:
        srv.close()
    return 0


def ctl_main(args) -> int:
    try:
        s = connect_control(args.socket)
    except OSError as e:
        print(f"simctl: cannot connect to {args.socket}: {e}", file=sys.stderr)
        return 1
    with s:
        s.sendall((" ".join(args.words) + "\n").encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    reply = json.loads(data)
    if "help" in reply:
        print(reply["help"])
    elif not reply["ok"]:
        print(f"error: {reply['error']}", file=sys.stderr)
        return 1
    elif args.json:
        print(json.dumps(reply, indent=2))
    else:
        for p in reply.get("packs", []):
            mv = [v for v in p["cells_mv"] if v] or [0]
            print(f"{p['name']:<8} {p['voltage_v']:6.2f}V {p['current_a']:+7.1f}A "
                  f"SOC {p['bms_soc']:5.1f}% (true {p['true_soc']:5.1f}) "
                  f"{min(mv)}-{max(mv)}mV T{p['temps_c']} {p['profile']}/{p['phase']} "
                  f"chg={'on' if p['chg_mos'] else 'OFF'} dis={'on' if p['dis_mos'] else 'OFF'} "
                  f"faults={','.join(p['faults']) or '-'}")
        if "speed" in reply and "packs" not in reply:
            print(f"speed {reply['speed']}x")
    return 0
