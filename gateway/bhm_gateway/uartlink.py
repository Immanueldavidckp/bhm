"""Carry the Daly traffic of several packs over one UART: the board's console port.

The board's only UART to the PC is the ST-LINK virtual COM port (ttySTM0 on the board,
/dev/ttyACM0 or COMx on the PC), and the PC cannot reach the board over the network. This
link lets a simulator on the PC answer the gateway on the board through that cable:

  PC:    sim (PTYs or TCP) <-> uartlink host  ==ttyACM0 ~ ST-LINK ~ ttySTM0==  uartlink board
  board: uartlink board <-> /run/bhm-uart/<pack> PTYs <-> bhm-gateway

Each side is a byte pipe per pack, so the gateway still speaks plain Daly 9600-baud frames to
what looks like a serial port, and the simulator still sees Daly requests on its own port.

Link frame:  0x7E 0xB5 | chan | len | payload[len] | CRC-16/CCITT(chan, len, payload), big-endian
  chan 0   control: b"H" + JSON (board hello: pack names and counters, every 2 s),
           b"A" (host answers a hello), b"R" (host: give the console back)
  chan N   byte stream of the Nth pack in the board's hello (1-based)

Bytes outside valid frames are console noise (kernel messages, a shell). On the board, three
Enter presses in a row among them (someone typing in minicom) also give the console back:
the service stops and systemd restarts the login on ttySTM0.

Stdlib only (the board has no extra packages). The host side also runs on Windows, through
pyserial for the COM port and the simulator's TCP ports.
"""

from __future__ import annotations

import binascii
import json
import os
import re
import select
import socket
import sys
import time

try:                                   # POSIX only
    import grp
    import termios
    import tty
except ImportError:                    # Windows
    grp = termios = tty = None

SYNC = b"\x7e\xb5"
MAX_PAYLOAD = 255
CTRL = 0
HELLO, ACK, RELEASE = b"H", b"A", b"R"
HELLO_EVERY_S = 2.0
HOST_TIMEOUT_S = 3 * HELLO_EVERY_S + 1   # no ACK for this long: the PC side is gone
PARTIAL_TIMEOUT_S = 0.5                  # drop a frame start whose rest never arrives
RELEASE_ENTERS = 3                       # Enter presses in a row that free the console
RELEASE_WINDOW_S = 3.0
STATS_EVERY_S = 30.0
POLL_S = 0.005                           # pyserial links are polled, not select()ed
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
PROMPT = re.compile(r"(^|\n)[^\n]*[#$] ?$")


def encode(chan: int, payload: bytes = b"") -> bytes:
    """One link frame. Payloads longer than 255 bytes need several frames (see frames())."""
    if not 0 <= chan <= 255 or len(payload) > MAX_PAYLOAD:
        raise ValueError("chan 0..255, payload up to 255 bytes")
    body = bytes([chan, len(payload)]) + payload
    return SYNC + body + binascii.crc_hqx(body, 0xFFFF).to_bytes(2, "big")


def frames(chan: int, data: bytes) -> bytes:
    return b"".join(encode(chan, data[i:i + MAX_PAYLOAD])
                    for i in range(0, len(data), MAX_PAYLOAD))


class Parser:
    """Split a byte stream into link frames and everything else (console noise)."""

    def __init__(self, clock=time.monotonic):
        self.buf = bytearray()
        self.clock = clock
        self.partial_since: float | None = None
        self.bad = 0                     # frame starts with a wrong CRC

    def feed(self, data: bytes = b"") -> tuple[list[tuple[int, bytes]], bytes]:
        buf = self.buf
        buf += data
        out: list[tuple[int, bytes]] = []
        junk = bytearray()
        while buf:
            i = buf.find(SYNC)
            if i < 0:                    # keep a lone 0x7E: the next read may complete SYNC
                keep = 1 if buf[-1] == SYNC[0] else 0
                junk += buf[:len(buf) - keep]
                del buf[:len(buf) - keep]
                break
            if i:
                junk += buf[:i]
                del buf[:i]
            if len(buf) < 4 or len(buf) < 6 + buf[3]:
                now = self.clock()
                if self.partial_since is None:
                    self.partial_since = now
                elif now - self.partial_since > PARTIAL_TIMEOUT_S:
                    junk += buf[:1]      # a frame start that never completed: noise
                    del buf[:1]
                    self.partial_since = None
                    continue
                break
            self.partial_since = None
            n = buf[3]
            body = bytes(buf[2:4 + n])
            if binascii.crc_hqx(body, 0xFFFF) != int.from_bytes(buf[4 + n:6 + n], "big"):
                self.bad += 1
                junk += buf[:1]
                del buf[:1]
                continue
            out.append((body[0], body[2:]))
            del buf[:6 + n]
        if not buf:
            self.partial_since = None
        return out, bytes(junk)


class EnterDetector:
    """True once RELEASE_ENTERS Enter keys arrive in a row (only CR/LF between them)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.count = 0
        self.last = 0.0
        self.prev = 0

    def feed(self, junk: bytes) -> bool:
        now = self.clock()
        for b in junk:
            if b in (0x0D, 0x0A):
                if b == 0x0A and self.prev == 0x0D:     # CR LF is one Enter
                    self.prev = b
                    continue
                if now - self.last > RELEASE_WINDOW_S:
                    self.count = 0
                self.count += 1
                self.last = now
                if self.count >= RELEASE_ENTERS:
                    self.count = 0
                    return True
            else:
                self.count = 0
            self.prev = b
        return False


# --- POSIX tty helpers ------------------------------------------------------------------
def open_tty(path: str, baud: int) -> int:
    """Open a serial port raw, 8N1, no flow control, non-blocking."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd, termios.TCSANOW)
    a = termios.tcgetattr(fd)
    a[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
    a[2] = (a[2] & ~(termios.PARENB | termios.CSTOPB | termios.CSIZE
                     | getattr(termios, "CRTSCTS", 0))
            | termios.CS8 | termios.CLOCAL | termios.CREAD)
    a[4] = a[5] = getattr(termios, f"B{baud}")
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def write_all(fd: int, data: bytes, timeout: float = 1.0) -> bool:
    """Write everything to a non-blocking fd; give up (drop the rest) after timeout."""
    end = time.monotonic() + timeout
    while data:
        try:
            n = os.write(fd, data)
            data = data[n:]
        except BlockingIOError:
            left = end - time.monotonic()
            if left <= 0:
                return False
            select.select([], [fd], [], left)
    return True


def _log(out, msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=out, flush=True)


# --- board side -------------------------------------------------------------------------
class Board:
    """On the MP135: own the console UART and give the gateway one PTY per pack."""

    def __init__(self, names: list[str], *, tty_path: str = "/dev/ttySTM0", baud: int = 115200,
                 link_dir: str = "/run/bhm-uart", group: str | None = None,
                 quiet_kernel: bool = True, out=sys.stdout):
        if not names or len(names) > 254 or len(set(names)) != len(names):
            raise ValueError("give 1..254 unique pack names")
        self.names = names
        self.tty_path, self.baud = tty_path, baud
        self.link_dir = link_dir
        self.gid = grp.getgrnam(group).gr_gid if group else None
        self.quiet_kernel = quiet_kernel
        self.out = out
        self.uart = -1
        self.masters: dict[int, int] = {}      # master fd -> channel
        self.chan_fd: dict[int, int] = {}      # channel -> master fd
        self._keep: list[int] = []
        self.parser = Parser()
        self.enter = EnterDetector()
        self.host_seen = 0.0
        self.host_up = False
        self.tx = [0] * (len(names) + 1)       # bytes sent to the host per channel
        self.rx = [0] * (len(names) + 1)       # bytes received from the host per channel
        self.stopping = False
        self._printk: str | None = None

    def open(self) -> None:
        os.makedirs(self.link_dir, exist_ok=True)
        for chan, name in enumerate(self.names, 1):
            master, slave = os.openpty()
            tty.setraw(slave, termios.TCSANOW)
            os.set_blocking(master, False)
            path = os.ttyname(slave)
            self._keep.append(slave)            # master never sees EIO when the gateway closes
            if self.gid is not None:
                os.chown(path, -1, self.gid)
                os.chmod(path, 0o660)
            link = os.path.join(self.link_dir, name)
            tmp = f"{link}.tmp{os.getpid()}"
            os.symlink(path, tmp)
            os.replace(tmp, link)
            self.masters[master] = chan
            self.chan_fd[chan] = master
            _log(self.out, f"{name}: channel {chan}, gateway port {link} -> {path}")
        if self.quiet_kernel:                   # kernel messages would land in the stream
            try:
                with open("/proc/sys/kernel/printk") as f:
                    self._printk = f.read().split()[0]
                with open("/proc/sys/kernel/printk", "w") as f:
                    f.write("1")
            except OSError as e:
                _log(self.out, f"warning: cannot quiet kernel console messages: {e}")
        self.uart = open_tty(self.tty_path, self.baud)
        termios.tcflush(self.uart, termios.TCIOFLUSH)
        _log(self.out, f"link on {self.tty_path} {self.baud} 8N1; waiting for the PC "
             f"(uartlink host). Press Enter {RELEASE_ENTERS}x on the console to get it back.")

    def send(self, chan: int, data: bytes) -> None:
        self.tx[chan] += len(data)
        write_all(self.uart, frames(chan, data))

    def hello(self) -> None:
        info = {"v": 1, "packs": self.names, "rx": sum(self.rx[1:]), "tx": sum(self.tx[1:]),
                "bad": self.parser.bad}
        self.send(CTRL, HELLO + json.dumps(info, separators=(",", ":")).encode())

    def on_uart(self, data: bytes) -> None:
        got, junk = self.parser.feed(data)
        for chan, payload in got:
            if chan == CTRL:
                if payload[:1] == ACK:
                    self.host_seen = time.monotonic()
                    if not self.host_up:
                        self.host_up = True
                        _log(self.out, "PC connected")
                elif payload[:1] == RELEASE:
                    _log(self.out, "PC asked for the console back")
                    self.stopping = True
            elif chan in self.chan_fd:
                self.rx[chan] += len(payload)
                try:
                    os.write(self.chan_fd[chan], payload)
                except BlockingIOError:        # gateway not reading: drop, like a real wire
                    pass
        if junk and self.enter.feed(junk):
            _log(self.out, f"{RELEASE_ENTERS}x Enter on the console: giving it back")
            self.stopping = True

    def run(self) -> None:
        self.open()
        next_hello = next_stats = time.monotonic()
        try:
            while not self.stopping:
                now = time.monotonic()
                if now >= next_hello:
                    self.hello()
                    next_hello = now + HELLO_EVERY_S
                    if self.host_up and now - self.host_seen > HOST_TIMEOUT_S:
                        self.host_up = False
                        _log(self.out, "PC gone (no answer to hello); packs will read offline")
                if now >= next_stats:
                    if self.host_up:
                        _log(self.out, "traffic " + ", ".join(
                            f"{n} req {self.tx[c]} B / reply {self.rx[c]} B"
                            for c, n in enumerate(self.names, 1)))
                    next_stats = now + STATS_EVERY_S
                try:
                    ready, _, _ = select.select([self.uart, *self.masters], [], [],
                                                max(0.0, min(next_hello - now,
                                                              PARTIAL_TIMEOUT_S)))
                except InterruptedError:
                    continue
                if not ready:
                    self.on_uart(b"")           # expire half frames
                for fd in ready:
                    try:
                        data = os.read(fd, 4096)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if fd == self.uart:
                        self.on_uart(data)
                    elif data:
                        self.send(self.masters[fd], data)
        finally:
            self.close()

    def close(self) -> None:
        if self._printk is not None:
            try:
                with open("/proc/sys/kernel/printk", "w") as f:
                    f.write(self._printk)
            except OSError:
                pass
        for name in self.names:
            try:
                os.unlink(os.path.join(self.link_dir, name))
            except OSError:
                pass
        for fd in [self.uart, *self.masters, *self._keep]:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


# --- PC side ----------------------------------------------------------------------------
class LinkPort:
    """The PC end of the cable: termios fd on POSIX, pyserial elsewhere (polled)."""

    def __init__(self, path: str, baud: int):
        self.path = path
        self.ser = None
        self.fd = -1
        if termios is not None and not path.upper().startswith("COM"):
            self.fd = open_tty(path, baud)
            termios.tcflush(self.fd, termios.TCIFLUSH)
        else:
            try:
                import serial  # pyserial
            except ImportError:
                raise SystemExit(f"uartlink: {path} needs pyserial on this OS "
                                 "(pip install pyserial)") from None
            self.ser = serial.Serial(path, baud, timeout=0, write_timeout=1)
            self.ser.reset_input_buffer()

    def read(self) -> bytes:
        if self.ser is not None:
            n = self.ser.in_waiting
            return self.ser.read(n) if n else b""
        try:
            return os.read(self.fd, 4096)
        except BlockingIOError:
            return b""

    def write(self, data: bytes) -> None:
        if self.ser is not None:
            self.ser.write(data)
        else:
            write_all(self.fd, data)

    def close(self) -> None:
        if self.ser is not None:
            self.ser.close()
        elif self.fd >= 0:
            os.close(self.fd)


class SimPort:
    """One pack on the simulator: its PTY (a path) or its TCP port."""

    def __init__(self, name: str, where: str):
        self.name, self.where = name, where
        self.fd = -1
        self.sock: socket.socket | None = None
        self.req = self.rep = 0
        self.problem: str | None = None
        self.next_try = 0.0              # a failed connect blocks the loop: retry at most every 5 s

    @property
    def is_open(self) -> bool:
        return self.fd >= 0 or self.sock is not None

    def key(self):
        return self.sock.fileno() if self.sock is not None else self.fd

    def open(self) -> bool:
        if self.is_open:
            return True
        if time.monotonic() < self.next_try:
            return False
        try:
            if self.where.startswith("tcp:"):
                host, _, port = self.where[4:].rpartition(":")
                s = socket.create_connection((host or "127.0.0.1", int(port)), timeout=2)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.setblocking(False)
                self.sock = s
            else:
                self.fd = os.open(self.where, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                tty.setraw(self.fd, termios.TCSANOW)
        except OSError as e:
            self.next_try = time.monotonic() + 5.0
            if isinstance(e, (ConnectionRefusedError, FileNotFoundError)):
                self.problem = "nothing listening; is the simulator running?"
            elif isinstance(e, TimeoutError):
                self.problem = ("no answer; is the simulator running? On Windows a click inside "
                                "its window pauses it: press Esc there")
            else:
                self.problem = str(e)
            return False
        self.problem = None
        return True

    def read(self) -> bytes:
        try:
            data = self.sock.recv(4096) if self.sock is not None else os.read(self.fd, 4096)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError:
            data = b""
        if not data:                           # simulator restarted or stopped
            self.close()
        return data

    def write(self, data: bytes) -> bool:
        if not self.open():
            return False
        try:
            if self.sock is not None:
                self.sock.sendall(data)
            else:
                write_all(self.fd, data, 0.2)
            return True
        except OSError as e:
            self.problem = str(e)
            self.close()
            return False

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class Host:
    """On the PC: relay each pack's channel to the simulator."""

    START_CMD = "systemctl --no-block start bhm-uartlink.service"

    def __init__(self, port: str, *, baud: int = 115200, sim_dir: str | None = None,
                 sim_tcp: str | None = None, start: bool = False, show_console: bool = True,
                 out=sys.stdout):
        if (sim_dir is None) == (sim_tcp is None):
            raise ValueError("give the simulator as sim_dir (PTYs) or sim_tcp (HOST:FIRSTPORT)")
        self.port, self.baud = port, baud
        self.sim_dir, self.sim_tcp = sim_dir, sim_tcp
        self.start = start
        self.show_console = show_console
        self.out = out
        self.link: LinkPort | None = None
        self.parser = Parser()
        self.chans: dict[int, SimPort] = {}
        self.names: list[str] = []
        self.board_seen = 0.0
        self.board_up = False
        self.console = ""                    # recent console text, for prompt detection
        self.line = ""
        self.started_at = 0.0
        self.start_state = "wait"            # wait -> poked -> sent (or hinted)
        self.poke_at = 0.0
        self.stopping = False

    def where(self, i: int, name: str) -> str:
        if self.sim_dir is not None:
            return os.path.join(self.sim_dir, name)
        host, _, first = self.sim_tcp.rpartition(":")
        return f"tcp:{host or '127.0.0.1'}:{int(first) + i}"

    def log(self, msg: str) -> None:
        _log(self.out, msg)

    def on_hello(self, info: dict) -> None:
        names = list(info.get("packs", []))
        if names != self.names:
            for sp in self.chans.values():
                sp.close()
            self.names = names
            self.chans = {c: SimPort(n, self.where(c - 1, n)) for c, n in enumerate(names, 1)}
            self.log("board packs: " + ", ".join(f"{n} -> {sp.where}"
                                                 for n, sp in zip(names, self.chans.values())))
            for sp in self.chans.values():
                if not sp.open():
                    self.log(f"{sp.name}: simulator not reachable at {sp.where} "
                             f"({sp.problem}); will retry")
        if not self.board_up:
            self.board_up = True
            self.log("board connected; relaying Daly traffic")
        self.board_seen = time.monotonic()
        self.link.write(encode(CTRL, ACK))

    def on_console(self, junk: bytes) -> None:
        text = ANSI.sub("", junk.decode("utf-8", "replace")).replace("\r", "")
        self.console = (self.console + text)[-400:]
        if not self.show_console:
            return
        self.line += text
        *lines, self.line = self.line.split("\n")
        self.line = self.line[-300:]
        for ln in lines:
            ln = "".join(c for c in ln if c.isprintable()).strip()
            if ln:
                self.log(f"board console: {ln}")

    def on_link(self, data: bytes) -> None:
        got, junk = self.parser.feed(data)
        for chan, payload in got:
            if chan == CTRL:
                if payload[:1] == HELLO:
                    try:
                        self.on_hello(json.loads(payload[1:]))
                    except ValueError:
                        pass
            elif chan in self.chans:
                sp = self.chans[chan]
                sp.req += len(payload)
                was = sp.problem
                if not sp.write(payload) and sp.problem != was:
                    self.log(f"{sp.name}: simulator not reachable at {sp.where} "
                             f"({sp.problem}); request dropped")
        if junk:
            self.on_console(junk)

    def maybe_start(self, now: float) -> None:
        """No hello yet: the board is probably at its shell. Start the board side there."""
        if self.board_up or now - self.started_at < 3:
            return
        if self.start_state == "wait":
            if not self.start:
                self.log("no hello from the board yet. On its console run: "
                         f"{self.START_CMD}  (or rerun with --start)")
                self.start_state = "hinted"
                return
            self.console = ""
            self.link.write(b"\x03\r")            # one Enter: never a release
            self.start_state, self.poke_at = "poked", now
        elif self.start_state == "poked":
            if PROMPT.search(self.console):
                self.log(f"board shell found; running: {self.START_CMD}")
                self.link.write(self.START_CMD.encode() + b"\r")
                self.start_state = "sent"
            elif now - self.poke_at > 5:
                self.log("no hello and no shell prompt from the board; is it powered, and is "
                         "minicom closed? Retrying.")
                self.start_state = "wait"
                self.started_at = now

    def run(self) -> None:
        self.link = LinkPort(self.port, self.baud)
        self.log(f"link on {self.port} {self.baud} 8N1; simulator at "
                 f"{self.sim_dir or 'tcp:' + self.sim_tcp}")
        self.started_at = time.monotonic()
        next_stats = self.started_at + STATS_EVERY_S
        try:
            while not self.stopping:
                now = time.monotonic()
                self.maybe_start(now)
                if self.board_up and now - self.board_seen > HOST_TIMEOUT_S:
                    self.board_up = False
                    self.log("board silent (no hello); waiting for it")
                if now >= next_stats:
                    if self.board_up:
                        self.log("traffic " + ", ".join(
                            f"{sp.name} req {sp.req} B / reply {sp.rep} B"
                            for sp in self.chans.values()))
                    next_stats = now + STATS_EVERY_S
                waits = {sp.key(): c for c, sp in self.chans.items() if sp.is_open}
                rlist = list(waits)
                if self.link.fd >= 0:
                    rlist.append(self.link.fd)
                timeout = POLL_S if self.link.ser is not None else PARTIAL_TIMEOUT_S
                try:
                    if rlist:
                        ready, _, _ = select.select(rlist, [], [], timeout)
                    else:
                        time.sleep(timeout)
                        ready = []
                except InterruptedError:
                    continue
                if self.link.ser is not None or self.link.fd in ready or not ready:
                    self.on_link(self.link.read())
                for k in ready:
                    c = waits.get(k)
                    if c is None:
                        continue
                    sp = self.chans[c]
                    data = sp.read()
                    if data:
                        sp.rep += len(data)
                        self.link.write(frames(c, data))
        finally:
            for sp in self.chans.values():
                sp.close()
            self.link.close()


def release(port: str, baud: int = 115200, out=sys.stdout, wait: float = 10.0) -> int:
    """Ask the board side to stop, then wait for the board's shell to come back."""
    link = LinkPort(port, baud)
    parser = Parser()
    text = ""
    end = time.monotonic() + wait
    next_send = 0.0
    try:
        while time.monotonic() < end:
            if time.monotonic() >= next_send:
                # the frame stops the board side; the Enter makes the fresh getty print
                # its prompt (and, repeated, is itself a release if the frame got lost)
                link.write(encode(CTRL, RELEASE) + b"\r")
                next_send = time.monotonic() + 1.0
            time.sleep(0.1)
            _, junk = parser.feed(link.read())
            text = (text + ANSI.sub("", junk.decode("utf-8", "replace"))).replace("\r", "")[-400:]
            if "login:" in text or PROMPT.search(text):
                _log(out, "board console is back (open minicom on it)")
                return 0
    finally:
        link.close()
    _log(out, "no shell prompt seen; the console may still be free - try minicom")
    return 1


def main(args) -> int:
    if args.side == "board":
        if termios is None:
            print("uartlink board: needs Linux", file=sys.stderr)
            return 2
        b = Board(args.packs, tty_path=args.tty, baud=args.baud, link_dir=args.link_dir,
                  group=args.group, quiet_kernel=not args.keep_kernel_messages)
        stop = b
    elif args.side == "host":
        sim_tcp = args.sim_tcp
        sim_dir = args.sim_dir
        if sim_dir is None and sim_tcp is None:
            if hasattr(os, "openpty"):
                sim_dir = "/tmp/bms"
            else:
                sim_tcp = "127.0.0.1:7001"
        try:
            h = Host(args.port, baud=args.baud, sim_dir=sim_dir, sim_tcp=sim_tcp,
                     start=args.start, show_console=not args.hide_console)
        except ValueError as e:
            print(f"uartlink host: {e}", file=sys.stderr)
            return 2
        stop = h
    else:
        return release(args.port, args.baud)

    import signal

    def on_signal(*_):
        stop.stopping = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    stop.run()
    return 0
