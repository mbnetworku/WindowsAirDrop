"""Core of WindowsAirDrop: LAN discovery (UDP broadcast) + file transfer (HTTP PUT).

Protocol (shared with the Android app, see ../PROTOCOL.md):
  * Every node broadcasts a JSON beacon on UDP port 53318 every few seconds.
  * Every node runs an HTTP server on TCP port 53317 and accepts
    PUT /send?name=<urlencoded filename>   (body = raw file bytes)
"""
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HTTP_PORT = 53317
DISC_PORT = 53318
BEACON_INTERVAL = 3.0
PEER_TTL = 12.0
CHUNK = 256 * 1024


def sanitize(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "file"


def unique_path(directory: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    path = os.path.join(directory, name)
    i = 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{base} ({i}){ext}")
        i += 1
    return path


def local_ipv4s():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return {ip for ip in ips if not ip.startswith("127.")}


class Node:
    def __init__(self, name, save_dir, pin="", http_port=HTTP_PORT, disc_port=DISC_PORT,
                 extra_targets=(), platform="windows", on_receive=None):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.save_dir = save_dir
        self.pin = pin
        self.http_port = http_port
        self.disc_port = disc_port
        self.extra_targets = list(extra_targets)  # [(host, port)] - mainly for tests
        self.platform = platform
        self.on_receive = on_receive or (lambda path, sender: None)
        self._peers = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._httpd = None

    # ---- lifecycle ----
    def start(self):
        os.makedirs(self.save_dir, exist_ok=True)
        node = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, code, text):
                body = text.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_PUT(self):
                u = urllib.parse.urlparse(self.path)
                if u.path != "/send":
                    return self._reply(404, "not found")
                if node.pin and self.headers.get("X-Pin", "") != node.pin:
                    return self._reply(403, "bad pin")
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self._reply(411, "length required")
                q = urllib.parse.parse_qs(u.query)
                name = sanitize(q.get("name", ["file"])[0])
                sender = urllib.parse.unquote(self.headers.get("X-Device", "unknown"))
                final = unique_path(node.save_dir, name)
                part = final + ".part"
                try:
                    with open(part, "wb") as f:
                        left = length
                        while left > 0:
                            data = self.rfile.read(min(CHUNK, left))
                            if not data:
                                raise ConnectionError("truncated upload")
                            f.write(data)
                            left -= len(data)
                    os.replace(part, final)
                except Exception:
                    if os.path.exists(part):
                        os.remove(part)
                    return
                self._reply(200, "ok")
                try:
                    node.on_receive(final, sender)
                except Exception:
                    pass

        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.http_port), Handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._beacon_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    # ---- discovery ----
    def _beacon(self):
        return json.dumps({"app": "wad", "id": self.id, "name": self.name,
                           "port": self.http_port, "os": self.platform,
                           "pin": bool(self.pin)}).encode()

    def _beacon_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not self._stop.is_set():
            targets = [("255.255.255.255", self.disc_port)]
            for ip in local_ipv4s():
                targets.append((ip.rsplit(".", 1)[0] + ".255", self.disc_port))
            targets += self.extra_targets
            for t in targets:
                try:
                    s.sendto(self._beacon(), t)
                except OSError:
                    pass
            self._stop.wait(BEACON_INTERVAL)
        s.close()

    def _listen_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", self.disc_port))
        s.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(2048)
                msg = json.loads(data.decode())
            except (socket.timeout, ValueError, UnicodeDecodeError):
                continue
            except OSError:
                break
            if msg.get("app") != "wad" or msg.get("id") == self.id:
                continue
            with self._lock:
                self._peers[msg["id"]] = {"id": msg["id"], "name": str(msg.get("name", "?")),
                                          "ip": addr[0], "port": int(msg.get("port", HTTP_PORT)),
                                          "os": msg.get("os", "?"), "pin": bool(msg.get("pin")),
                                          "seen": time.time()}
        s.close()

    def peers(self):
        now = time.time()
        with self._lock:
            self._peers = {k: v for k, v in self._peers.items() if now - v["seen"] < PEER_TTL}
            return sorted(self._peers.values(), key=lambda p: p["name"].lower())

    # ---- sending ----
    def send_file(self, peer, path, pin="", progress=None):
        size = os.path.getsize(path)
        name = urllib.parse.quote(os.path.basename(path))
        conn = http.client.HTTPConnection(peer["ip"], peer["port"], timeout=30)
        try:
            conn.putrequest("PUT", f"/send?name={name}")
            conn.putheader("Content-Length", str(size))
            conn.putheader("X-Device", urllib.parse.quote(self.name))
            if pin:
                conn.putheader("X-Pin", pin)
            conn.endheaders()
            sent = 0
            try:
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break
                        conn.send(chunk)
                        sent += len(chunk)
                        if progress:
                            progress(sent, size)
            except (BrokenPipeError, ConnectionResetError):
                pass  # receiver rejected early; its response explains why
            resp = conn.getresponse()
            body = resp.read().decode(errors="replace")
            if resp.status != 200:
                raise RuntimeError(f"{resp.status}: {body}")
        finally:
            conn.close()
