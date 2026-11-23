#!/usr/bin/env python3
"""
ADB File Transfer - a small desktop window app to push files/folders from
your PC to an Android phone using adb.

Requirements:
  * Python 3.8+ (tkinter is included with the standard installer)
  * adb (Android platform-tools) on PATH, or browse to adb.exe in the app
  * On the phone: Developer options -> USB debugging enabled

Run:  python adb_transfer.py
"""

import os
import queue
import secrets
import shlex
import shutil
import string
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:  # optional: only needed for the "Scan QR" pairing window
    import qrcode
except ImportError:  # pragma: no cover
    qrcode = None

APP_TITLE = "ADB File Transfer"
DEFAULT_DEST = "/sdcard/Download/"
PRESETS = [
    "/sdcard/Download/",
    "/sdcard/DCIM/",
    "/sdcard/Pictures/",
    "/sdcard/Movies/",
    "/sdcard/Music/",
    "/sdcard/Documents/",
]

# Hide the console window that would flash on Windows for each adb call.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Make adb use its built-in mDNS so the QR pairing can discover the phone
# (needed on Windows when Bonjour isn't installed).
ADB_ENV = dict(os.environ, ADB_MDNS_OPENSCREEN="1")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def path_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("880x660")
        self.minsize(820, 580)

        self.adb_path = tk.StringVar(value=shutil.which("adb") or "")
        self.device_var = tk.StringVar()
        self.dest_var = tk.StringVar(value=DEFAULT_DEST)
        self.wifi_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")

        self.items = []  # absolute paths to transfer
        self.devices = {}  # display label -> serial
        self.q = queue.Queue()
        self.worker = None
        self.proc = None
        self.cancel_flag = threading.Event()

        self._build_ui()
        self.after(100, self._drain_queue)
        self.after(300, self.refresh_devices)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)

        # ADB location
        f_adb = ttk.LabelFrame(root, text="ADB")
        f_adb.pack(fill="x", **pad)
        ttk.Entry(f_adb, textvariable=self.adb_path).pack(
            side="left", fill="x", expand=True, padx=6, pady=6
        )
        ttk.Button(f_adb, text="Browse…", command=self.browse_adb).pack(
            side="left", padx=(0, 6)
        )

        # Device
        f_dev = ttk.LabelFrame(root, text="Device")
        f_dev.pack(fill="x", **pad)
        self.device_combo = ttk.Combobox(
            f_dev, textvariable=self.device_var, state="readonly"
        )
        self.device_combo.pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f_dev, text="Refresh", command=self.refresh_devices).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(f_dev, text="Scan QR (Wi-Fi)", command=self.qr_connect).pack(
            side="left", padx=(0, 6)
        )

        f_wifi = ttk.Frame(f_dev)
        f_wifi.pack(side="left", padx=(0, 6))
        ttk.Entry(f_wifi, textvariable=self.wifi_var, width=20).pack(side="left")
        ttk.Button(f_wifi, text="Connect (Wi-Fi)", command=self.wifi_connect).pack(
            side="left", padx=(4, 0)
        )
        self._placeholder(self.wifi_var, "192.168.1.5:5555")

        # Files
        f_files = ttk.LabelFrame(root, text="Files to send")
        f_files.pack(fill="both", expand=True, **pad)

        btns = ttk.Frame(f_files)
        btns.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Button(btns, text="Add files…", command=self.add_files).pack(side="left")
        ttk.Button(btns, text="Add folder…", command=self.add_folder).pack(
            side="left", padx=4
        )
        ttk.Button(btns, text="Remove selected", command=self.remove_selected).pack(
            side="left"
        )
        ttk.Button(btns, text="Clear", command=self.clear_items).pack(
            side="left", padx=4
        )
        self.count_lbl = ttk.Label(btns, text="0 items")
        self.count_lbl.pack(side="right")

        list_wrap = ttk.Frame(f_files)
        list_wrap.pack(fill="both", expand=True, padx=6, pady=6)
        self.listbox = tk.Listbox(list_wrap, selectmode="extended", activestyle="none")
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<Delete>", lambda _e: self.remove_selected())

        # Destination
        f_dest = ttk.LabelFrame(root, text="Destination folder on phone")
        f_dest.pack(fill="x", **pad)
        self.dest_combo = ttk.Combobox(f_dest, textvariable=self.dest_var, values=PRESETS)
        self.dest_combo.pack(fill="x", padx=6, pady=6)

        # Transfer controls
        f_go = ttk.Frame(root)
        f_go.pack(fill="x", **pad)
        self.send_btn = ttk.Button(f_go, text="Send to phone", command=self.start_transfer)
        self.send_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            f_go, text="Cancel", command=self.cancel_transfer, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(f_go, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(root, textvariable=self.status_var).pack(anchor="w", padx=10)

        # Log
        f_log = ttk.LabelFrame(root, text="Log")
        f_log.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(f_log, height=8, state="disabled", wrap="word")
        lsb = ttk.Scrollbar(f_log, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        lsb.pack(side="right", fill="y", pady=6, padx=(0, 6))

    def _placeholder(self, var, text):
        # Simple hint text for the Wi-Fi entry (cleared on focus).
        var.set(text)
        self._wifi_hint = text

        def clear_hint(_e=None):
            if var.get() == text:
                var.set("")

        # Bind on the widget lazily after UI is built.
        def bind_later():
            for widget in self._iter_widgets(self):
                if isinstance(widget, ttk.Entry) and widget.cget("textvariable") == str(var):
                    widget.bind("<FocusIn>", clear_hint)
                    break

        self.after(50, bind_later)

    def _iter_widgets(self, w):
        for c in w.winfo_children():
            yield c
            yield from self._iter_widgets(c)

    # ------------------------------------------------------------- helpers
    def log_write(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def adb(self):
        path = self.adb_path.get().strip()
        if not path or not (os.path.isfile(path) or shutil.which(path)):
            raise FileNotFoundError(
                "adb not found. Install Android platform-tools and add it to PATH, "
                "or use Browse… to pick adb(.exe)."
            )
        return path

    def run_adb(self, args, timeout=30):
        cmd = [self.adb()] + args
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=ADB_ENV,
            creationflags=CREATE_NO_WINDOW,
        )

    def selected_serial(self):
        return self.devices.get(self.device_var.get())

    # ---------------------------------------------------------- ADB / device
    def browse_adb(self):
        p = filedialog.askopenfilename(title="Select adb executable")
        if p:
            self.adb_path.set(p)
            self.refresh_devices()

    def refresh_devices(self):
        try:
            res = self.run_adb(["devices", "-l"])
        except Exception as e:
            self.device_combo["values"] = []
            self.device_var.set("")
            self.devices = {}
            self.status_var.set(str(e))
            self.log_write(f"[error] {e}")
            return

        self.devices = {}
        unauthorized = []
        for line in res.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            if state == "device":
                model = ""
                for p in parts[2:]:
                    if p.startswith("model:"):
                        model = p.split(":", 1)[1]
                label = f"{model or 'Android device'}  ({serial})"
                self.devices[label] = serial
            elif state == "unauthorized":
                unauthorized.append(serial)

        labels = list(self.devices)
        self.device_combo["values"] = labels
        if labels:
            if self.device_var.get() not in labels:
                self.device_var.set(labels[0])
            self.status_var.set(f"{len(labels)} device(s) connected")
        else:
            self.device_var.set("")
            msg = "No device found. Plug in the phone and enable USB debugging."
            if unauthorized:
                msg = "Device is unauthorized - accept the 'Allow USB debugging' prompt on the phone."
            self.status_var.set(msg)
            self.log_write(f"[info] {msg}")

    def wifi_connect(self):
        addr = self.wifi_var.get().strip()
        if not addr or addr == getattr(self, "_wifi_hint", ""):
            messagebox.showinfo(APP_TITLE, "Enter the phone's IP:port first, e.g. 192.168.1.5:5555")
            return
        try:
            res = self.run_adb(["connect", addr], timeout=15)
            self.log_write((res.stdout + res.stderr).strip())
        except Exception as e:
            self.log_write(f"[error] {e}")
        self.refresh_devices()

    # --------------------------------------------------------- QR pairing
    def qr_connect(self):
        """Show a QR code; scan it from Developer options -> Wireless debugging
        -> Pair device with QR code. Then pair + connect automatically."""
        if qrcode is None:
            messagebox.showerror(
                APP_TITLE,
                "QR support needs the 'qrcode' package.\n\nInstall it with:\n    pip install qrcode",
            )
            return
        try:
            adb = self.adb()
        except FileNotFoundError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return

        name = "ADB_" + "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6)
        )
        password = "".join(secrets.choice(string.digits) for _ in range(8))
        qr_text = f"WIFI:T:ADB;S:{name};P:{password};;"

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
        qr.add_data(qr_text)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        n = len(matrix)
        quiet = 4
        cell = max(4, 300 // (n + 2 * quiet))
        size = (n + 2 * quiet) * cell

        win = tk.Toplevel(self)
        win.title("Connect by QR code")
        win.resizable(False, False)
        win.transient(self)

        ttk.Label(
            win,
            text=(
                "On your phone open:\n"
                "Settings > Developer options > Wireless debugging\n"
                "> Pair device with QR code, and scan this code.\n"
                "(Phone and PC must be on the same Wi-Fi.)"
            ),
            justify="center",
        ).pack(padx=14, pady=(14, 6))

        canvas = tk.Canvas(win, width=size, height=size, bg="white", highlightthickness=0)
        canvas.pack(padx=14, pady=6)
        for r, row in enumerate(matrix):
            for c, filled in enumerate(row):
                if filled:
                    x, y = (c + quiet) * cell, (r + quiet) * cell
                    canvas.create_rectangle(x, y, x + cell, y + cell, fill="black", outline="")

        status = tk.StringVar(value="Waiting for the phone to scan...")
        ttk.Label(win, textvariable=status, wraplength=max(size, 320), justify="center").pack(
            padx=14, pady=6
        )

        cancel = threading.Event()
        qq = queue.Queue()

        def close():
            cancel.set()
            try:
                win.destroy()
            except tk.TclError:
                pass

        btn = ttk.Button(win, text="Cancel", command=close)
        btn.pack(pady=(0, 14))
        win.protocol("WM_DELETE_WINDOW", close)

        def poll():
            try:
                if not win.winfo_exists():
                    return
                while True:
                    msg = qq.get_nowait()
                    if msg[0] == "status":
                        status.set(msg[1])
                    elif msg[0] == "log":
                        self.log_write(msg[1])
                    elif msg[0] == "done":
                        _kind, ok, text = msg
                        status.set(text)
                        self.log_write(text)
                        self.refresh_devices()
                        if ok:
                            win.after(1500, close)
                        else:
                            btn.config(text="Close")
                        return
            except queue.Empty:
                pass
            except tk.TclError:
                return
            win.after(200, poll)

        win.after(200, poll)
        threading.Thread(
            target=self._qr_worker, args=(adb, name, password, cancel, qq), daemon=True
        ).start()

    def _qr_worker(self, adb, name, password, cancel, qq):
        def run(args, timeout=20):
            return subprocess.run(
                [adb] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=ADB_ENV,
                creationflags=CREATE_NO_WINDOW,
            )

        def services():
            out = run(["mdns", "services"], 10).stdout
            found = []
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3 and "_adb" in parts[1]:
                    found.append((parts[0], parts[1], parts[-1]))
            return found

        try:
            run(["start-server"], 30)

            pair_addr = None
            deadline = time.time() + 180
            while time.time() < deadline and not cancel.is_set():
                for sname, stype, addr in services():
                    if "pairing" in stype and sname == name:
                        pair_addr = addr
                        break
                if pair_addr:
                    break
                time.sleep(1.5)
            if cancel.is_set():
                return
            if not pair_addr:
                qq.put((
                    "done", False,
                    "Timed out waiting for the phone. Make sure Wireless debugging is on, "
                    "both devices share the same Wi-Fi, then try again.",
                ))
                return

            qq.put(("status", "Phone found - pairing..."))
            r = run(["pair", pair_addr, password], 30)
            text = (r.stdout + r.stderr).strip()
            qq.put(("log", text))
            if "successfully paired" not in text.lower():
                qq.put(("done", False, f"Pairing failed: {text or 'no response from adb'}"))
                return

            qq.put(("status", "Paired - connecting..."))
            host = pair_addr.rsplit(":", 1)[0]
            for _ in range(10):
                if cancel.is_set():
                    return
                for _sname, stype, addr in services():
                    if "connect" in stype and addr.startswith(host + ":"):
                        r = run(["connect", addr], 15)
                        out = (r.stdout + r.stderr).strip()
                        qq.put(("log", out))
                        if "connected" in out.lower():
                            qq.put(("done", True, f"Connected to {addr}"))
                            return
                time.sleep(1.5)

            qq.put((
                "done", False,
                "Paired, but the phone's connect address wasn't found. Enter the IP:port shown "
                "on the Wireless debugging screen and press 'Connect (Wi-Fi)'.",
            ))
        except Exception as e:  # noqa: BLE001
            qq.put(("done", False, f"Error: {e}"))

    # ----------------------------------------------------------- file list
    def _add_paths(self, paths):
        for p in paths:
            p = os.path.abspath(p)
            if p not in self.items:
                self.items.append(p)
                kind = "📁" if os.path.isdir(p) else "📄"
                self.listbox.insert("end", f"{kind} {p}")
        self._update_count()

    def _update_count(self):
        self.count_lbl.config(text=f"{len(self.items)} item(s)")

    def add_files(self):
        paths = filedialog.askopenfilenames(title="Choose files to send")
        if paths:
            self._add_paths(paths)

    def add_folder(self):
        p = filedialog.askdirectory(title="Choose a folder to send")
        if p:
            self._add_paths([p])

    def remove_selected(self):
        for idx in reversed(self.listbox.curselection()):
            self.listbox.delete(idx)
            del self.items[idx]
        self._update_count()

    def clear_items(self):
        self.listbox.delete(0, "end")
        self.items.clear()
        self._update_count()

    # ------------------------------------------------------------ transfer
    def start_transfer(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.items:
            messagebox.showinfo(APP_TITLE, "Add at least one file or folder first.")
            return
        serial = self.selected_serial()
        if not serial:
            self.refresh_devices()
            serial = self.selected_serial()
            if not serial:
                messagebox.showerror(APP_TITLE, "No connected device selected.")
                return
        dest = self.dest_var.get().strip() or DEFAULT_DEST
        if not dest.endswith("/"):
            dest += "/"
        try:
            adb = self.adb()
        except FileNotFoundError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return

        self.cancel_flag.clear()
        self.send_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress.config(value=0, maximum=len(self.items))
        items = list(self.items)
        self.worker = threading.Thread(
            target=self._transfer_worker, args=(adb, serial, dest, items), daemon=True
        )
        self.worker.start()

    def cancel_transfer(self):
        self.cancel_flag.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _transfer_worker(self, adb, serial, dest, items):
        q = self.q
        ok = failed = 0
        try:
            # Make sure destination exists on the phone.
            r = subprocess.run(
                [adb, "-s", serial, "shell", "mkdir", "-p", shlex.quote(dest)],
                capture_output=True,
                text=True,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode != 0:
                q.put(("log", f"[warn] mkdir: {(r.stdout + r.stderr).strip()}"))

            total = len(items)
            for i, path in enumerate(items, 1):
                if self.cancel_flag.is_set():
                    q.put(("log", "[cancelled]"))
                    break
                size = human_size(path_size(path))
                q.put(("status", f"Sending {i}/{total}: {os.path.basename(path)} ({size})"))
                q.put(("log", f"> {path}  ->  {dest}"))
                self.proc = subprocess.Popen(
                    [adb, "-s", serial, "push", path, dest],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=CREATE_NO_WINDOW,
                )
                last = ""
                for line in self.proc.stdout:
                    line = line.strip()
                    if line:
                        last = line
                        # Skip the noisy per-file progress lines; keep the summary.
                        if not line.startswith("[") or "pushed" in line:
                            q.put(("log", line))
                self.proc.wait()
                if self.cancel_flag.is_set():
                    q.put(("log", "[cancelled]"))
                    break
                if self.proc.returncode == 0:
                    ok += 1
                else:
                    failed += 1
                    q.put(("log", f"[failed] {os.path.basename(path)}: {last}"))
                q.put(("progress", i))

            if self.cancel_flag.is_set():
                q.put(("done", f"Cancelled ({ok} sent, {failed} failed)"))
            elif failed:
                q.put(("done", f"Finished with errors: {ok} sent, {failed} failed"))
            else:
                q.put(("done", f"Done - {ok} item(s) sent to {dest}"))
        except Exception as e:
            q.put(("log", f"[error] {e}"))
            q.put(("done", f"Error: {e}"))

    def _drain_queue(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log_write(val)
                elif kind == "status":
                    self.status_var.set(val)
                elif kind == "progress":
                    self.progress.config(value=val)
                elif kind == "done":
                    self.status_var.set(val)
                    self.log_write(val)
                    self.send_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)


if __name__ == "__main__":
    App().mainloop()
