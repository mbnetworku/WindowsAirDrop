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


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Send to %(name)s</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;max-width:480px;margin:24px auto;padding:0 16px}
button,input{font-size:18px;margin:8px 0;width:100%%;padding:12px;box-sizing:border-box}
button{background:#1e88e5;color:#fff;border:0;border-radius:10px}progress{width:100%%}
.f{margin:10px 0;font-size:14px;word-break:break-all}</style></head><body>
<h2>Send files to %(name)s</h2>
<input type="file" id="files" multiple>
%(pin)s
<button id="go">Send</button><div id="out"></div>
<script>
document.getElementById('go').onclick=()=>{
 const out=document.getElementById('out');out.innerHTML='';
 const pin=document.getElementById('pin');
 for(const f of document.getElementById('files').files){
  const d=document.createElement('div');d.className='f';d.textContent=f.name;
  const p=document.createElement('progress');p.max=100;p.value=0;d.appendChild(p);out.appendChild(d);
  const x=new XMLHttpRequest();
  x.open('PUT','/send?name='+encodeURIComponent(f.name));
  if(pin&&pin.value)x.setRequestHeader('X-Pin',pin.value);
  x.setRequestHeader('X-Device',encodeURIComponent('Browser'));
  x.upload.onprogress=e=>{p.value=100*e.loaded/e.total};
  x.onload=()=>{d.append(x.status==200?' \u2705 sent':' \u274c '+x.responseText)};
  x.onerror=()=>{d.append(' \u274c failed')};
  x.send(f);
 }};
</script></body></html>"""


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

            def do_GET(self):
                if urllib.parse.urlparse(self.path).path != "/":
                    return self._reply(404, "not found")
                pin = '<input id="pin" placeholder="PIN" inputmode="numeric">' if node.pin else ""
                body = (PAGE % {"name": node.name, "pin": pin}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
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


class AppleBridge:
    """Client for airdrop-bridge (Linux VM with USB Wi-Fi running OWL + OpenDrop)."""

    def __init__(self, url, token):
        u = urllib.parse.urlparse(url if "://" in url else "http://" + url)
        self.host, self.port = u.hostname, u.port or 8765
        self.token = token

    def _req(self, method, path, timeout=10):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request(method, path, headers={"X-Token": self.token})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def status(self):
        code, body = self._req("GET", "/status", timeout=4)
        if code != 200:
            raise RuntimeError(f"bridge answered {code}: {body.decode(errors='replace')}")
        return json.loads(body)

    def peers(self):
        code, body = self._req("GET", "/peers", timeout=4)
        if code != 200:
            return []
        return [{"id": p["id"], "name": p["name"], "ip": "AirDrop", "os": "apple",
                 "pin": False, "via": "bridge"} for p in json.loads(body)]

    def send_file(self, peer, path, progress=None):
        size = os.path.getsize(path)
        q = urllib.parse.urlencode({"to": peer["id"], "name": os.path.basename(path)})
        conn = http.client.HTTPConnection(self.host, self.port, timeout=600)
        try:
            conn.putrequest("PUT", "/send?" + q)
            conn.putheader("Content-Length", str(size))
            conn.putheader("X-Token", self.token)
            conn.endheaders()
            sent = 0
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, size)
            r = conn.getresponse()  # returns after the AirDrop transfer finished
            body = r.read().decode(errors="replace")
            if r.status != 200:
                try:
                    body = json.loads(body).get("error", body)
                except ValueError:
                    pass
                raise RuntimeError(body or f"bridge answered {r.status}")
        finally:
            conn.close()

    def pull_received(self, save_dir):
        """Download files the bridge received from Apple devices; returns saved paths."""
        code, body = self._req("GET", "/received", timeout=5)
        if code != 200:
            return []
        saved = []
        os.makedirs(save_dir, exist_ok=True)
        for item in json.loads(body):
            name = urllib.parse.quote(item["name"])
            conn = http.client.HTTPConnection(self.host, self.port, timeout=60)
            try:
                conn.request("GET", f"/received/{name}", headers={"X-Token": self.token})
                r = conn.getresponse()
                if r.status != 200:
                    r.read()
                    continue
                final = unique_path(save_dir, sanitize(item["name"]))
                with open(final + ".part", "wb") as f:
                    while True:
                        d = r.read(CHUNK)
                        if not d:
                            break
                        f.write(d)
                os.replace(final + ".part", final)
            finally:
                conn.close()
            self._req("DELETE", f"/received/{name}")
            saved.append(final)
        return saved
