"""WindowsAirDrop - Windows desktop app. Runs in the system tray and receives files in the background."""
import json
import os
import socket
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from airdrop_core import HTTP_PORT, AppleBridge, Node, local_ipv4s

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # tray is optional
    pystray = None

APP = "WindowsAirDrop"
CONF_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CONF = os.path.join(CONF_DIR, "config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def load_conf():
    d = {"name": socket.gethostname(), "pin": "",
         "save_dir": os.path.join(os.path.expanduser("~"), "Downloads", APP), "autostart": True,
         "bridge_url": "", "bridge_token": ""}
    try:
        with open(CONF) as f:
            d.update(json.load(f))
    except (OSError, ValueError):
        pass
    return d


def save_conf(c):
    os.makedirs(CONF_DIR, exist_ok=True)
    with open(CONF, "w") as f:
        json.dump(c, f, indent=2)


def set_autostart(enabled):
    if sys.platform != "win32":
        return
    import winreg
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}" --background'
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        cmd = f'"{exe}" "{os.path.abspath(__file__)}" --background'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, APP)
            except FileNotFoundError:
                pass


class App:
    def __init__(self, background):
        self.conf = load_conf()
        self.tray = None
        self.root = tk.Tk()
        self.root.title(APP)
        self.root.geometry("480x640")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.peers = []
        self.apple_peers = []
        self.bridge = None
        self.bridge_state = "Apple AirDrop: not configured"
        self.node = Node(self.conf["name"], self.conf["save_dir"], self.conf["pin"],
                         on_receive=self.received)
        try:
            self.node.start()
        except OSError as e:
            messagebox.showerror(APP, f"Cannot start (is another copy running?)\n{e}")
            sys.exit(1)
        try:
            set_autostart(self.conf["autostart"])
        except OSError:
            pass
        self.make_bridge()
        threading.Thread(target=self.bridge_loop, daemon=True).start()
        self.build_ui()
        self.start_tray()
        self.refresh()
        if background and self.tray:
            self.root.withdraw()

    # ---- UI ----
    def build_ui(self):
        r = self.root
        ttk.Label(r, text=f"This PC: {self.conf['name']}", font=("Segoe UI", 11, "bold")).pack(pady=(10, 0))
        urls = "   or   ".join(f"http://{ip}:{HTTP_PORT}" for ip in sorted(local_ipv4s())) or "(no network)"
        box = ttk.LabelFrame(r, text="iPhone / iPad / Mac: open this address in Safari to send files here")
        box.pack(fill="x", padx=10, pady=(10, 0))
        e = ttk.Entry(box); e.insert(0, urls); e.config(state="readonly"); e.pack(fill="x", padx=6, pady=6)
        ttk.Label(r, text="Nearby devices (Android phones running the app):").pack(anchor="w", padx=10, pady=(10, 0))
        self.lb = tk.Listbox(r, height=8, exportselection=False)
        self.lb.pack(fill="both", expand=True, padx=10, pady=5)
        row = ttk.Frame(r); row.pack(fill="x", padx=10)
        ttk.Button(row, text="Send files...", command=self.send_dialog).pack(side="left")
        ttk.Button(row, text="Open received folder", command=self.open_folder).pack(side="left", padx=6)
        self.bar = ttk.Progressbar(r, maximum=100); self.bar.pack(fill="x", padx=10, pady=8)
        self.status = ttk.Label(r, text="Listening for incoming files..."); self.status.pack(anchor="w", padx=10)
        ttk.Separator(r).pack(fill="x", pady=8)
        cf = ttk.Frame(r); cf.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(cf, text="PIN (optional, same on both devices):").grid(row=0, column=0, sticky="w")
        self.pin = tk.StringVar(value=self.conf["pin"])
        ttk.Entry(cf, textvariable=self.pin, width=10).grid(row=0, column=1, padx=6)
        ttk.Button(cf, text="Save", command=self.save_pin).grid(row=0, column=2)
        self.auto = tk.BooleanVar(value=self.conf["autostart"])
        ttk.Checkbutton(cf, text="Start with Windows", variable=self.auto, command=self.toggle_auto).grid(row=1, column=0, sticky="w", pady=4)

        af = ttk.LabelFrame(r, text="Apple AirDrop (needs the airdrop-bridge VM + USB Wi-Fi, see README)")
        af.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(af, text="Bridge address:").grid(row=0, column=0, sticky="w", padx=6)
        self.burl = tk.StringVar(value=self.conf["bridge_url"])
        ttk.Entry(af, textvariable=self.burl, width=28).grid(row=0, column=1, padx=6, pady=2)
        ttk.Label(af, text="Bridge token:").grid(row=1, column=0, sticky="w", padx=6)
        self.btok = tk.StringVar(value=self.conf["bridge_token"])
        ttk.Entry(af, textvariable=self.btok, width=28, show="*").grid(row=1, column=1, padx=6, pady=2)
        ttk.Button(af, text="Save", command=self.save_bridge).grid(row=0, column=2, rowspan=2, padx=6)
        self.bstat = ttk.Label(af, text=self.bridge_state)
        self.bstat.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(2, 6))

    def refresh(self):
        self.peers = self.node.peers() + list(self.apple_peers)
        self.bstat.config(text=self.bridge_state)
        sel = self.lb.curselection()
        self.lb.delete(0, "end")
        for p in self.peers:
            if p.get("via") == "bridge":
                self.lb.insert("end", f"\U0001F34E {p['name']}  (AirDrop)")
            else:
                self.lb.insert("end", f"{p['name']}  ({p['ip']}){'  🔒' if p['pin'] else ''}")
        if sel and sel[0] < len(self.peers):
            self.lb.selection_set(sel[0])
        self.root.after(2000, self.refresh)

    def ui(self, fn):
        self.root.after(0, fn)

    def save_pin(self):
        self.conf["pin"] = self.node.pin = self.pin.get().strip()
        save_conf(self.conf)
        self.status.config(text="PIN saved." if self.node.pin else "PIN cleared.")

    def toggle_auto(self):
        self.conf["autostart"] = self.auto.get()
        save_conf(self.conf)
        try:
            set_autostart(self.conf["autostart"])
        except OSError as e:
            messagebox.showerror(APP, str(e))

    def open_folder(self):
        os.makedirs(self.conf["save_dir"], exist_ok=True)
        if sys.platform == "win32":
            os.startfile(self.conf["save_dir"])

    # ---- send / receive ----
    def send_dialog(self):
        sel = self.lb.curselection()
        if not sel:
            return messagebox.showinfo(APP, "Select a device first.")
        files = filedialog.askopenfilenames(title="Choose files to send")
        if not files:
            return
        peer = self.peers[sel[0]]
        threading.Thread(target=self.send_worker, args=(peer, files), daemon=True).start()

    def send_worker(self, peer, files):
        try:
            for i, path in enumerate(files, 1):
                label = f"Sending {os.path.basename(path)} ({i}/{len(files)}) to {peer['name']}"
                self.ui(lambda l=label: self.status.config(text=l))
                prog = lambda s, t: self.ui(lambda: self.bar.config(value=100 * s / max(t, 1)))
                if peer.get("via") == "bridge":
                    if not self.bridge:
                        raise RuntimeError("Apple AirDrop bridge is not configured")
                    self.ui(lambda l=label: self.status.config(text=l + " - accept it on the Apple device..."))
                    self.bridge.send_file(peer, path, progress=prog)
                else:
                    self.node.send_file(peer, path, pin=self.pin.get().strip(), progress=prog)
            self.ui(lambda: self.status.config(text=f"Sent {len(files)} file(s) to {peer['name']}."))
        except Exception as e:
            self.ui(lambda: messagebox.showerror(APP, f"Send failed: {e}"))
        self.ui(lambda: self.bar.config(value=0))

    # ---- Apple AirDrop bridge ----
    def make_bridge(self):
        url, tok = self.conf["bridge_url"].strip(), self.conf["bridge_token"].strip()
        self.bridge = AppleBridge(url, tok) if url and tok else None
        if not self.bridge:
            self.bridge_state = "Apple AirDrop: not configured"
            self.apple_peers = []

    def save_bridge(self):
        self.conf["bridge_url"] = self.burl.get().strip()
        self.conf["bridge_token"] = self.btok.get().strip()
        save_conf(self.conf)
        self.make_bridge()
        self.bridge_state = "Apple AirDrop: connecting..." if self.bridge else "Apple AirDrop: not configured"

    def bridge_loop(self):
        import time
        while True:
            b = self.bridge
            if b:
                try:
                    st = b.status()
                    if not st.get("owl"):
                        self.bridge_state = "Apple AirDrop: bridge reached, but OWL/awdl0 is not running in the VM"
                    else:
                        self.bridge_state = "Apple AirDrop: ready" + ("" if st.get("ble") else " (no Bluetooth wake-up)")
                    self.apple_peers = b.peers()
                    for path in b.pull_received(self.conf["save_dir"]):
                        self.received(path, "Apple device (AirDrop)")
                except Exception as e:
                    self.apple_peers = []
                    self.bridge_state = f"Apple AirDrop: cannot reach bridge ({e.__class__.__name__})"
            time.sleep(3)

    def received(self, path, sender):
        msg = f"Received {os.path.basename(path)} from {sender}"
        self.ui(lambda: self.status.config(text=msg))
        if self.tray:
            try:
                self.tray.notify(msg, APP)
            except Exception:
                pass

    # ---- tray ----
    def start_tray(self):
        if not pystray:
            return
        img = Image.new("RGB", (64, 64), "#1e88e5")
        d = ImageDraw.Draw(img)
        d.polygon([(32, 12), (50, 40), (14, 40)], fill="white")
        d.rectangle([26, 40, 38, 52], fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda: self.ui(self.show), default=True),
            pystray.MenuItem("Received files", lambda: self.ui(self.open_folder)),
            pystray.MenuItem("Quit", lambda: self.ui(self.quit)))
        self.tray = pystray.Icon(APP, img, APP, menu)
        self.tray.run_detached()

    def show(self):
        self.root.deiconify(); self.root.lift()

    def hide(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.root.iconify()

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.node.stop()
        self.root.destroy()


if __name__ == "__main__":
    App("--background" in sys.argv).root.mainloop()
