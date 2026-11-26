"""Live 3D view of a running simulator, in the browser.

    python3 -m bhm_gateway simviz --socket /tmp/bms/control.sock      # then open http://127.0.0.1:8765
    python -m bhm_gateway simviz                                       # Windows (tcp:127.0.0.1:7000)

A small stdlib HTTP server: it serves the three.js page in ``web/`` and relays the page's
requests to the simulator's control socket (``status`` for the view, plus the same
``set``/``fault``/``clear``/``comm``/``speed`` commands as simctl for the buttons).
The page loads three.js from a CDN, so the browser needs internet; the simulator does not.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .server import CONTROL_TIMEOUT_S, connect_control, control_error

WEB_DIR = Path(__file__).with_name("web")
ALLOWED = {"status", "speed", "set", "fault", "clear", "comm"}


def control(path: str, line: str, timeout: float = CONTROL_TIMEOUT_S) -> dict:
    """Send one command line to the simulator's control socket and return its JSON reply."""
    with connect_control(path, timeout) as s:
        s.sendall(line.strip().encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def make_handler(sock_path: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "bhm-simviz"

        def log_message(self, fmt, *args):   # quiet: the page polls twice a second
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: dict, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _relay(self, line: str) -> None:
            words = line.split()
            if not words or words[0] not in ALLOWED:
                self._json({"ok": False, "error": f"command must be one of {sorted(ALLOWED)}"}, 400)
                return
            try:
                self._json(control(sock_path, line))
            except OSError as e:
                self._json({"ok": False, "error": control_error(sock_path, e)}, 502)
            except ValueError as e:
                self._json({"ok": False, "error": f"bad reply from {sock_path}: {e}"}, 502)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                self._relay("status")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/cmd":
                self._send(404, b"not found", "text/plain")
                return
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            self._relay(self.rfile.read(n).decode(errors="replace"))

    return Handler


def main(args) -> int:
    httpd = ThreadingHTTPServer((args.bind, args.port), make_handler(args.socket))
    host = "127.0.0.1" if args.bind in ("0.0.0.0", "") else args.bind
    print(f"simviz: http://{host}:{args.port}/  (simulator control: {args.socket})",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
