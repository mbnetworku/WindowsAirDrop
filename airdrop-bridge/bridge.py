#!/usr/bin/env python3
"""AirDrop bridge: runs inside a Linux VM that owns a USB Wi-Fi adapter (via OWL + OpenDrop)
and exposes a small HTTP API to the WindowsAirDrop app on the Windows host.

API (all require header  X-Token: <token>):
  GET    /peers                     -> [{"id","name"}]   Apple devices currently visible
  PUT    /send?to=<id>&name=<file>  body = file bytes  -> sends via AirDrop
  GET    /received                  -> [{"name","size"}] files received from Apple devices
  GET    /received/<name>           -> file bytes
  DELETE /received/<name>           -> remove after the Windows app has downloaded it
  GET    /status                    -> {"owl":bool,"receiver":bool,"ble":bool}
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FOUND = re.compile(r"Found\s+index\s+(\d+)\s+ID\s+(\S+)\s+name\s+(.+)$")
PEER_TTL = 60


def sanitize(n):
    n = os.path.basename(n.replace("\\", "/"))
    return re.sub(r'[<>:"|?*\x00-\x1f]', "_", n).strip(" .") or "file"


class Bridge:
    def __init__(self, opendrop, recv_dir, ble_cmd):
        self.opendrop = opendrop
        self.recv_dir = recv_dir
        self.ble_cmd = ble_cmd
        self.peers = {}
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()  # opendrop sends one file at a time
        self.receiver = None
        os.makedirs(recv_dir, exist_ok=True)

    # Apple devices only start AWDL/AirDrop after a Bluetooth LE "AirDrop" advertisement.
    def ble_wake(self):
        if self.ble_cmd:
            subprocess.run(self.ble_cmd, shell=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)

    def run_receiver(self):
        while True:
            self.receiver = subprocess.Popen([self.opendrop, "receive"], cwd=self.recv_dir,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            self.receiver.wait()
            time.sleep(3)  # restart if it crashes (e.g. awdl0 went away)

    def run_finder(self):
        while True:
            try:
                self.ble_wake()
            except Exception:
                pass
            p = subprocess.Popen([self.opendrop, "find"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
            timer = threading.Timer(20, p.terminate)  # restart the scan regularly
            timer.start()
            for line in p.stdout:
                m = FOUND.search(line.strip())
                if m:
                    with self.lock:
                        self.peers[m.group(2)] = {"id": m.group(2), "name": m.group(3).strip(),
                                                  "seen": time.time()}
            p.wait()
            timer.cancel()
            time.sleep(1)

    def list_peers(self):
        now = time.time()
        with self.lock:
            self.peers = {k: v for k, v in self.peers.items() if now - v["seen"] < PEER_TTL}
            return [{"id": v["id"], "name": v["name"]} for v in self.peers.values()]

    def send(self, peer_id, path):
        with self.send_lock:
            try:
                self.ble_wake()
            except Exception:
                pass
            r = subprocess.run([self.opendrop, "send", "-r", peer_id, "-f", path],
                               capture_output=True, text=True, timeout=600)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 or "successful" not in out.lower():
            raise RuntimeError(out.splitlines()[-1] if out else f"opendrop exit {r.returncode}")

    def received(self):
        out = []
        for n in sorted(os.listdir(self.recv_dir)):
            p = os.path.join(self.recv_dir, n)
            # only report files that have stopped growing (transfer finished)
            if os.path.isfile(p) and time.time() - os.path.getmtime(p) > 3:
                out.append({"name": n, "size": os.path.getsize(p)})
        return out


def make_handler(bridge, token):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def reply(self, code, obj, ctype="application/json"):
            body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def auth(self):
            if self.headers.get("X-Token", "") != token:
                self.reply(403, {"error": "bad token"})
                return False
            return True

        def route(self):
            u = urllib.parse.urlparse(self.path)
            return u.path, urllib.parse.parse_qs(u.query)

        def do_GET(self):
            if not self.auth():
                return
            path, _ = self.route()
            if path == "/peers":
                return self.reply(200, bridge.list_peers())
            if path == "/status":
                return self.reply(200, {"owl": os.path.exists("/sys/class/net/awdl0"),
                                        "receiver": bool(bridge.receiver and bridge.receiver.poll() is None),
                                        "ble": bool(bridge.ble_cmd)})
            if path == "/received":
                return self.reply(200, bridge.received())
            if path.startswith("/received/"):
                p = os.path.join(bridge.recv_dir, sanitize(urllib.parse.unquote(path[10:])))
                if not os.path.isfile(p):
                    return self.reply(404, {"error": "not found"})
                size = os.path.getsize(p)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Connection", "close")
                self.end_headers()
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 256 * 1024)
                self.close_connection = True
                return
            self.reply(404, {"error": "not found"})

        def do_DELETE(self):
            if not self.auth():
                return
            path, _ = self.route()
            if not path.startswith("/received/"):
                return self.reply(404, {"error": "not found"})
            p = os.path.join(bridge.recv_dir, sanitize(urllib.parse.unquote(path[10:])))
            if os.path.isfile(p):
                os.remove(p)
            self.reply(200, {"ok": True})

        def do_PUT(self):
            if not self.auth():
                return
            path, q = self.route()
            if path != "/send" or "to" not in q:
                return self.reply(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.reply(411, {"error": "length required"})
            tmpdir = tempfile.mkdtemp()
            fpath = os.path.join(tmpdir, sanitize(q.get("name", ["file"])[0]))
            try:
                with open(fpath, "wb") as f:
                    left = length
                    while left > 0:
                        d = self.rfile.read(min(256 * 1024, left))
                        if not d:
                            return
                        f.write(d)
                        left -= len(d)
                bridge.send(q["to"][0], fpath)
                self.reply(200, {"ok": True})
            except Exception as e:
                self.reply(502, {"error": str(e)})
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=os.environ.get("BRIDGE_TOKEN", ""))
    ap.add_argument("--recv-dir", default="/var/lib/airdrop-bridge/received")
    ap.add_argument("--opendrop", default=shutil.which("opendrop") or "opendrop")
    ap.add_argument("--ble-cmd", default=os.environ.get("BLE_CMD", ""),
                    help="shell command that sends the AirDrop BLE wake-up advertisement")
    a = ap.parse_args()
    if not a.token:
        sys.exit("A --token (or BRIDGE_TOKEN) is required")
    b = Bridge(a.opendrop, a.recv_dir, a.ble_cmd)
    threading.Thread(target=b.run_receiver, daemon=True).start()
    threading.Thread(target=b.run_finder, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(b, a.token))
    print(f"AirDrop bridge listening on :{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
