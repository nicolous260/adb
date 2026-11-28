#!/usr/bin/env python3
"""
ADB File Transfer - move files and folders between your PC and an Android phone
over USB or Wi-Fi, using adb.

Requirements:
  * Python 3.8+ (tkinter is included with the standard installer)
  * adb (Android platform-tools) on PATH, next to this file, or picked in the app
  * On the phone: Developer options -> USB debugging (or Wireless debugging)
  * Optional: `pip install qrcode` for QR pairing, `pip install windnd` for
    drag and drop on Windows

Run:  python adb_transfer.py
"""

import ctypes
import json
import math
import os
import platform
import posixpath
import queue
import re
import secrets
import shlex
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import tkinter as tk
from tkinter import filedialog, font as tkfont, ttk

try:  # optional: only needed for the QR pairing window
    import qrcode
except ImportError:  # pragma: no cover
    qrcode = None

try:  # optional: drag and drop from Explorer (Windows only)
    import windnd
except Exception:  # pragma: no cover
    windnd = None

APP_TITLE = "ADB File Transfer"
DEFAULT_DEST = "/sdcard/Download/"
PRESETS = [
    ("Download", "/sdcard/Download/"),
    ("DCIM", "/sdcard/DCIM/"),
    ("Pictures", "/sdcard/Pictures/"),
    ("Movies", "/sdcard/Movies/"),
    ("Music", "/sdcard/Music/"),
    ("Documents", "/sdcard/Documents/"),
]

# Hide the console window that would flash on Windows for each adb call.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Make adb use its built-in mDNS so the QR pairing can discover the phone
# (needed on Windows when Bonjour isn't installed).
ADB_ENV = dict(os.environ, ADB_MDNS_OPENSCREEN="1")

# ---------------------------------------------------------------- design tokens
# Deep ink-blue surfaces, one periwinkle accent, status colours kept for state.
BG = "#0F1012"
SURFACE = "#151619"
RAISED = "#1B1C20"
RAISED_HI = "#25272C"
LINE = "#2A2C31"
LINE_HI = "#4A4D55"
HAIR = "#1C1D21"
TEXT = "#ECEDEF"
MUTED = "#9A9DA5"
FAINT = "#5F626A"
ACCENT = "#E9A23B"  # signal amber: live transfer, progress, selection mark
ACCENT_HI = "#F2B65C"
ACCENT_PRESS = "#D18F2F"
ACCENT_INK = "#1A1204"
ACCENT_WASH = "#25272C"
LINK_IDLE = "#4A4D55"
OK = "#6FCF97"
OK_WASH = "#16261D"
WARN = "#D9C06A"
BAD = "#F07582"
BAD_WASH = "#2A1A1D"
BAD_HI = "#3A2226"
BAD_PRESS = "#4A2A2F"
PRIMARY = "#ECEDEF"
PRIMARY_HI = "#FFFFFF"
PRIMARY_PRESS = "#C5C7CC"
PRIMARY_FG = "#111214"
DISABLED = "#1A1B1F"
LOGFG = "#B4B7BE"
THEME = "dark"

SCALE = 1.0
FAMILY = "Helvetica"
MONO = "Courier"
SEMI = None  # semibold family when the system has one
_FONTS = {}


def px(n):
    return max(1, int(round(n * SCALE)))


def enable_dpi_awareness():
    """Render crisp text on high-DPI Windows displays."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def init_fonts(root):
    global FAMILY, MONO, SEMI
    avail = set(tkfont.families(root))
    for name in ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Inter",
                 "Helvetica Neue", "Cantarell", "Noto Sans", "DejaVu Sans"):
        if name in avail:
            FAMILY = name
            break
    if FAMILY.startswith("Segoe UI") and "Segoe UI Semibold" in avail:
        SEMI = "Segoe UI Semibold"
    for name in ("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono"):
        if name in avail:
            MONO = name
            break


def font(size=10, weight="normal", mono=False):
    key = (size, weight, mono)
    if key not in _FONTS:
        if weight == "bold" and SEMI and not mono:
            _FONTS[key] = tkfont.Font(family=SEMI, size=size, weight="normal")
        else:
            _FONTS[key] = tkfont.Font(family=MONO if mono else FAMILY, size=size, weight=weight)
    return _FONTS[key]


def _colorref(hex_):
    r, g, b = int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)
    return (b << 16) | (g << 8) | r


def style_titlebar(win):
    """Title bar that matches the app theme on Windows 10/11."""
    if sys.platform != "win32":
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dwm = ctypes.windll.dwmapi

        def setattr_(attr, value):
            v = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        dark = 1 if THEME == "dark" else 0
        if setattr_(20, dark) != 0:
            setattr_(19, dark)
        setattr_(35, _colorref(BG))  # caption colour
        setattr_(36, _colorref(TEXT))  # caption text colour
    except Exception:
        pass


ZIP_APPS = (".apks", ".xapk", ".apkm")  # bundles of split APKs
APK_EXTS = (".apk",) + ZIP_APPS
INSTALL_OPTIONS = [
    ("replace", "Replace if already installed", "Keeps the app's data", True),
    ("grant", "Grant all permissions", "Skips the permission prompts", False),
    ("downgrade", "Allow older versions", "Install over a newer version", False),
    ("test", "Allow test builds", "For debug and test-only APKs", False),
    ("combine", "Combine into one app", "For split APKs added together", False),
]
INSTALL_ERRORS = {
    "INSTALL_FAILED_ALREADY_EXISTS": "Already installed. Turn on Replace if already installed",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "Signed differently from the installed app. Uninstall that app first",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "A newer version is installed. Turn on Allow older versions",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "Not enough storage on the phone",
    "INSTALL_FAILED_OLDER_SDK": "This app needs a newer version of Android",
    "INSTALL_FAILED_TEST_ONLY": "Test build. Turn on Allow test builds",
    "INSTALL_FAILED_USER_RESTRICTED": "Blocked by the phone. Allow installs over USB in Developer options",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "This app doesn't support the phone's processor",
    "INSTALL_FAILED_MISSING_SPLIT": "Split APKs are missing. Add them all and turn on Combine into one app",
    "INSTALL_FAILED_VERIFICATION_FAILURE": "Blocked by Play Protect or app verification",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "The APK isn't signed",
    "INSTALL_PARSE_FAILED_NOT_APK": "Not a valid APK file",
    "INSTALL_FAILED_INVALID_APK": "Not a valid APK file",
}


def explain_install_error(text):
    m = re.search(r"(INSTALL_[A-Z_]+)", text)
    if m:
        return INSTALL_ERRORS.get(m.group(1), m.group(1).replace("_", " ").capitalize())
    last = [ln for ln in text.splitlines() if ln.strip()]
    return last[-1][:120] if last else "Install failed"


def mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(a, b))


# ------------------------------------------------------------------ app icon
# 64px PNGs of assets/icon-dark.png and icon-light.png (see assets/make_icons.py).
ICON_DARK = (
    "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"
)
ICON_LIGHT = (
    "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"
    "RU5ErkJggg=="
)


# ------------------------------------------------------------------- helpers
def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def path_size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def ellipsize(text, fnt, maxw, keep="start"):
    """Shorten text to fit maxw pixels. keep='end' keeps the tail (paths)."""
    if maxw <= 0:
        return ""
    if fnt.measure(text) <= maxw:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        cand = text[:mid] + "…" if keep == "start" else "…" + text[len(text) - mid:]
        if fnt.measure(cand) <= maxw:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…" if keep == "start" else "…" + text[len(text) - lo:]


def rrect(c, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


LS_RE = re.compile(
    r"^([\-dlcbps])[rwxsStT\-]{9}\S*\s+(?:\d+\s+)?\S+\s+\S+\s+(?:(\d+)\s+)?"
    r"(\d{4}-\d\d-\d\d)\s+(\d\d:\d\d)\s+(.*)$")


def parse_ls(text):
    """Parse `adb shell ls -lA` output into (entries, error lines)."""
    entries, errors = [], []
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip() or line.startswith("total "):
            continue
        m = LS_RE.match(line)
        if not m:
            errors.append(line.strip())
            continue
        kind, size, date, tm, name = m.groups()
        if kind == "l" and " -> " in name:
            name = name.split(" -> ")[0]
        entries.append({"name": name, "dir": kind in ("d", "l"),
                        "size": int(size or 0), "meta": f"{date} {tm}"})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return entries, errors


def pc_places():
    home = os.path.expanduser("~")
    out = [(n, os.path.join(home, n)) for n in ("Downloads", "Desktop", "Documents", "Pictures")
           if os.path.isdir(os.path.join(home, n))]
    return out or [("Home", home)]


def find_adb():
    exe = "adb.exe" if sys.platform == "win32" else "adb"
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                           else os.path.abspath(__file__))
    for cand in (os.path.join(base, exe), os.path.join(base, "platform-tools", exe)):
        if os.path.isfile(cand):
            return cand
    return shutil.which("adb") or ""


# file badge colours: (tint, foreground)
BADGES = {
    "video": ("#2A2447", "#B7A6FF"),
    "image": ("#133A33", "#6FE0B8"),
    "audio": ("#3A2A14", "#F2C27A"),
    "archive": ("#3A1F2B", "#FF9AAE"),
    "doc": ("#14303F", "#7FCBF2"),
    "apk": ("#1E3A22", "#9BE58A"),
    "folder": ("#26282C", "#A9ACB3"),
    "other": ("#222428", "#A9ACB3"),
}
EXT_KIND = {}
for _kind, _exts in {
    "video": "mp4 mkv mov avi webm m4v 3gp flv wmv",
    "image": "jpg jpeg png gif webp heic bmp svg raw dng",
    "audio": "mp3 flac wav m4a aac ogg opus wma",
    "archive": "zip rar 7z tar gz xz iso",
    "doc": "pdf doc docx txt md xls xlsx ppt pptx csv epub",
    "apk": "apk xapk apks apkm aab obb",
}.items():
    for _e in _exts.split():
        EXT_KIND[_e] = _kind


PALETTE_KEYS = (
    "BG SURFACE RAISED RAISED_HI LINE LINE_HI HAIR TEXT MUTED FAINT ACCENT ACCENT_HI ACCENT_PRESS "
    "ACCENT_INK ACCENT_WASH LINK_IDLE OK OK_WASH WARN BAD BAD_WASH BAD_HI BAD_PRESS PRIMARY "
    "PRIMARY_HI PRIMARY_PRESS PRIMARY_FG DISABLED LOGFG BADGES"
).split()

PALETTES = {"dark": {k: globals()[k] for k in PALETTE_KEYS}}
PALETTES["light"] = dict(
    BG="#F6F6F7", SURFACE="#FFFFFF", RAISED="#EDEEF0", RAISED_HI="#E2E3E7", LINE="#DCDDE1",
    LINE_HI="#B9BBC2", HAIR="#E9EAEC", TEXT="#17181A", MUTED="#5F626A", FAINT="#9A9DA5",
    ACCENT="#D98A1E", ACCENT_HI="#E59A33", ACCENT_PRESS="#C27813", ACCENT_INK="#1A1204",
    ACCENT_WASH="#E2E3E7", LINK_IDLE="#B9BBC2", OK="#2E8B5B", OK_WASH="#DDF1E6", WARN="#8A6D10",
    BAD="#C93C4D", BAD_WASH="#FBE4E7", BAD_HI="#F6D3D8", BAD_PRESS="#EFC0C7",
    PRIMARY="#17181A", PRIMARY_HI="#000000", PRIMARY_PRESS="#3A3B3F", PRIMARY_FG="#FFFFFF",
    DISABLED="#E9EAEC", LOGFG="#44474E",
    BADGES={
        "video": ("#E8E4FB", "#5B47B8"),
        "image": ("#DDF3EA", "#1F7A56"),
        "audio": ("#F8EBD4", "#9A6410"),
        "archive": ("#F9E2E7", "#B23A55"),
        "doc": ("#DCEEF8", "#1F6A94"),
        "apk": ("#E3F3DC", "#3E7E2C"),
        "folder": ("#E9EAEC", "#5F626A"),
        "other": ("#E9EAEC", "#5F626A"),
    },
)


def set_palette(name):
    globals().update(PALETTES[name])
    globals()["THEME"] = name


PREFS_PATH = os.path.join(os.path.expanduser("~"), ".adb_file_transfer.json")


def load_prefs():
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs):
    try:
        with open(PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
    except Exception:
        pass


def system_theme():
    """Windows app light/dark setting; dark elsewhere."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        except Exception:
            pass
    return "dark"


def badge_for(path, is_dir):
    if is_dir:
        return BADGES["folder"], ""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return BADGES[EXT_KIND.get(ext, "other")], (ext[:4] or "file")


# ------------------------------------------------------------------- widgets
class RButton(tk.Canvas):
    """Pill button drawn on a canvas: hover, press, focus ring, disabled."""

    @staticmethod
    def _kinds():
        return {
            "primary": dict(fill=PRIMARY, hover=PRIMARY_HI, press=PRIMARY_PRESS, fg=PRIMARY_FG, line=None),
            "ghost": dict(fill=RAISED, hover=RAISED_HI, press=LINE, fg=TEXT, line=None),
            "quiet": dict(fill=None, hover=RAISED, press=LINE, fg=MUTED, fgh=TEXT, line=None),
            "danger": dict(fill=BAD_WASH, hover=BAD_HI, press=BAD_PRESS, fg=BAD, line=None),
            "chip": dict(fill=None, hover=RAISED, press=LINE, fg=MUTED, fgh=TEXT, line=LINE),
            "chip_on": dict(fill=RAISED_HI, hover=RAISED_HI, press=RAISED_HI, fg=TEXT, line=LINE_HI),
        }

    def __init__(self, master, text, command=None, kind="ghost", height=36, pad=16,
                 size=10, weight="normal", bg=None, width=None, anchor="center"):
        bg = BG if bg is None else bg
        self.fnt = font(size, weight)
        self.text = text
        self.pad = pad
        self.anchor_ = anchor
        w = width or (self.fnt.measure(text) + 2 * px(pad))
        super().__init__(master, width=w, height=px(height), bg=bg, highlightthickness=0,
                         bd=0, cursor="hand2", takefocus=1)
        self.command = command
        self.kind = kind
        self._bw, self._bh = w, px(height)
        self._hover = self._down = self._focus = False
        self._enabled = True
        self.bind("<Configure>", self._on_conf)
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, down=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(down=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda e: self._set(focus=True))
        self.bind("<FocusOut>", lambda e: self._set(focus=False))
        self.bind("<Return>", lambda e: self._invoke())
        self.bind("<space>", lambda e: self._invoke())
        self._draw()

    def _on_conf(self, e):
        self._bw, self._bh = e.width, e.height
        self._draw()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, "_" + k, v)
        self._draw()

    def _release(self, e):
        was = self._down
        self._set(down=False)
        if was and 0 <= e.x <= self._bw and 0 <= e.y <= self._bh:
            self._invoke()

    def _invoke(self):
        if self._enabled and self.command:
            self.command()

    def set_text(self, text):
        self.text = text
        self.configure(width=self.fnt.measure(text) + 2 * px(self.pad))
        self._draw()

    def set_kind(self, kind):
        if kind != self.kind:
            self.kind = kind
            self._draw()

    def set_enabled(self, on):
        self._enabled = on
        self.configure(cursor="hand2" if on else "arrow")
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h, k = self._bw, self._bh, self._kinds()[self.kind]
        if self._enabled:
            fill = k["press"] if self._down else (k["hover"] if self._hover else k["fill"])
            fg = k.get("fgh", k["fg"]) if self._hover else k["fg"]
            line = k["line"]
            if self.kind == "chip" and self._hover:
                line = FAINT
        else:
            fill = DISABLED if k["fill"] else None
            fg, line = FAINT, (HAIR if k["line"] else None)
        if fill or line:
            rrect(self, 1, 1, w - 1, h - 1, min(h / 2, px(10)), fill=fill or "", outline=line or "", width=1)
        if self._focus:
            rrect(self, 2, 2, w - 2, h - 2, min(h / 2, px(10)), fill="", outline=ACCENT_HI, width=2)
        if self.anchor_ == "w":
            self.create_text(px(self.pad), h / 2, text=self.text, font=self.fnt, fill=fg, anchor="w")
        else:
            self.create_text(w / 2, h / 2, text=self.text, font=self.fnt, fill=fg)


class Field(tk.Canvas):
    """Rounded text field with a focus outline and optional placeholder."""

    def __init__(self, master, textvariable=None, placeholder=None, bg=None, fill=None, mono=False):
        bg = BG if bg is None else bg
        fill = RAISED if fill is None else fill
        super().__init__(master, height=px(40), bg=bg, highlightthickness=0, bd=0)
        self._fill, self._focus = fill, False
        self._ph_text, self._ph = placeholder, False
        self.entry = tk.Entry(
            self, relief="flat", bd=0, highlightthickness=0, bg=fill, fg=TEXT,
            insertbackground=TEXT, selectbackground=ACCENT, selectforeground=ACCENT_INK,
            font=font(10, mono=mono), textvariable=textvariable,
        )
        self._win = self.create_window(px(14), px(20), window=self.entry, anchor="w")
        self.bind("<Configure>", self._layout)
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        if placeholder and textvariable is None:
            self._show_ph()

    def _layout(self, e):
        self.itemconfigure(self._win, width=max(10, e.width - px(28)))
        self.coords(self._win, px(14), e.height / 2)
        self._paint(e.width, e.height)

    def _paint(self, w=None, h=None):
        w = w or self.winfo_width()
        h = h or self.winfo_height()
        self.delete("bg")
        rrect(self, 1, 1, w - 1, h - 1, px(11), fill=self._fill,
              outline=ACCENT if self._focus else LINE, width=1, tags="bg")
        self.tag_lower("bg")

    def _show_ph(self):
        self._ph = True
        self.entry.delete(0, "end")
        self.entry.insert(0, self._ph_text)
        self.entry.configure(fg=FAINT)

    def _focus_in(self, _e):
        self._focus = True
        if self._ph:
            self._ph = False
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT)
        self._paint()

    def _focus_out(self, _e):
        self._focus = False
        if self._ph_text and not self.entry.get():
            self._show_ph()
        self._paint()

    def value(self):
        return "" if self._ph else self.entry.get().strip()

    def set_value(self, text):
        self._ph = False
        self.entry.configure(fg=TEXT)
        self.entry.delete(0, "end")
        self.entry.insert(0, text)

    def focus_field(self):
        self.entry.focus_set()


class ProgressBar(tk.Canvas):
    def __init__(self, master, bg=None):
        bg = SURFACE if bg is None else bg
        super().__init__(master, height=px(4), bg=bg, highlightthickness=0, bd=0)
        self.frac, self.color = 0.0, ACCENT
        self.phase = None  # set to a 0..1 value for an indeterminate sweep (installs)
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, frac, color=None):
        self.frac = min(max(frac, 0.0), 1.0)
        if color:
            self.color = color
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        rrect(self, 0, 0, w, h, h / 2, fill=RAISED, outline="")
        if self.frac > 0:
            rrect(self, 0, 0, max(w * self.frac, h), h, h / 2, fill=self.color, outline="")
        if self.phase is not None:
            fw = w * self.frac
            span = w - fw
            if span > h * 2:
                seg = span * 0.3
                start = fw + self.phase * (span + seg) - seg
                x0, x1 = max(start, fw), min(start + seg, w)
                if x1 - x0 > 2:
                    rrect(self, x0, 0, x1, h, h / 2, fill=mix(RAISED, ACCENT, 0.6), outline="")


class Route(tk.Canvas):
    """Header: this PC -> phone. The link is the one animated element; it flows
    while a transfer is running."""

    def __init__(self, master, hostname):
        super().__init__(master, height=px(136), bg=BG, highlightthickness=0, bd=0)
        self.host = hostname or "Computer"
        self.model = None
        self.sub = ""
        self.mode = "idle"  # idle | ready | sending | done | error
        self.center = "Not connected"
        self.offset = 0
        self.reverse = False  # True when data flows phone -> PC
        self.bind("<Configure>", lambda e: self.draw())

    def set_device(self, model, sub=""):
        self.model, self.sub = model, sub
        if self.mode in ("idle", "ready"):
            self.mode = "ready" if model else "idle"
            self.center = "Ready to send" if model else "Not connected"
        self.draw()

    def set_mode(self, mode, center):
        self.mode, self.center = mode, center
        self.draw()

    def set_center(self, text):
        self.center = text
        self.draw()

    def advance(self):
        self.offset += px(2) if self.reverse else -px(2)
        try:
            self.itemconfigure("flow", dashoffset=self.offset)
        except tk.TclError:
            pass

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 200:
            return
        pad, cy = px(32), (h - 1) // 2
        connected = self.model is not None
        name_f, sub_f = font(12, "bold"), font(9)

        # this PC
        mw, mh = px(46), px(30)
        mx, my = pad, cy - mh // 2 - px(2)
        rrect(self, mx, my, mx + mw, my + mh, px(6), fill="", outline=MUTED, width=2)
        self.create_line(mx + mw / 2, my + mh, mx + mw / 2, my + mh + px(5), fill=MUTED, width=2)
        self.create_line(mx + mw * 0.3, my + mh + px(5), mx + mw * 0.7, my + mh + px(5),
                         fill=MUTED, width=2, capstyle="round")
        lx = pad + mw + px(16)
        host = ellipsize(self.host, sub_f, px(150))
        self.create_text(lx, cy - px(9), text="This PC", anchor="w", font=name_f, fill=TEXT)
        self.create_text(lx, cy + px(11), text=host, anchor="w", font=sub_f, fill=MUTED)

        # phone
        pw, ph = px(26), px(46)
        phx = w - pad - pw
        rrect(self, phx, cy - ph // 2, phx + pw, cy + ph // 2, px(7),
              fill=RAISED if connected else "", outline=TEXT if connected else FAINT, width=2)
        self.create_line(phx + pw * 0.34, cy - ph // 2 + px(7), phx + pw * 0.66, cy - ph // 2 + px(7),
                         fill=TEXT if connected else FAINT, width=2, capstyle="round")
        rx = phx - px(16)
        title = ellipsize(self.model or "No phone connected", name_f, px(240))
        sub = ellipsize(self.sub if connected else "Connect by USB or Wi-Fi", sub_f, px(240))
        self.create_text(rx, cy - px(9), text=title, anchor="e", font=name_f,
                         fill=TEXT if connected else MUTED)
        self.create_text(rx, cy + px(11), text=sub, anchor="e", font=sub_f, fill=MUTED)

        # link
        x1 = lx + max(name_f.measure("This PC"), sub_f.measure(host)) + px(26)
        x2 = rx - max(name_f.measure(title), sub_f.measure(sub)) - px(26)
        if x2 - x1 > px(90):
            if self.mode == "sending":
                self.create_line(x1, cy, x2, cy, fill=ACCENT, width=3, dash=(12, 9),
                                 dashoffset=self.offset, tags="flow")
                col = ACCENT
            elif self.mode == "done":
                self.create_line(x1, cy, x2, cy, fill=OK, width=2)
                col = OK
            elif self.mode == "error":
                self.create_line(x1, cy, x2, cy, fill=BAD, width=2)
                col = BAD
            elif connected:
                self.create_line(x1, cy, x2, cy, fill=LINK_IDLE, width=2)
                col = MUTED
            else:
                self.create_line(x1, cy, x2, cy, fill=FAINT, width=2, dash=(2, 7), capstyle="round")
                col = FAINT
            r = px(4)
            for x in (x1, x2):
                self.create_oval(x - r, cy - r, x + r, cy + r, fill=col, outline="")
            tone = {"sending": TEXT, "done": OK, "error": BAD}.get(self.mode, MUTED)
            self.create_text((x1 + x2) / 2, cy - px(16), text=self.center, font=font(10), fill=tone)
        self.create_line(0, h - 1, w, h - 1, fill=LINE)


class DropZone(tk.Canvas):
    def __init__(self, master, on_files, on_folder, can_drop, labels=None):
        super().__init__(master, bg=BG, highlightthickness=0, bd=0)
        self.can_drop = can_drop
        self.labels = dict(drop="Drop files or folders here", add="Add files or folders",
                           sub="They go to the phone folder chosen on the right",
                           b1="Add files", b2="Add folder")
        self.labels.update(labels or {})
        self.b1 = RButton(self, self.labels["b1"], on_files, "primary", height=40, pad=20, weight="bold")
        self.b2 = RButton(self, self.labels["b2"], on_folder, "ghost", height=40, pad=20)
        self.bind("<Configure>", lambda e: self.draw())

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 120 or h < 120:
            return
        rrect(self, 1, 1, w - 1, h - 1, px(16), fill=SURFACE, outline=LINE, width=1)
        x0 = px(44)
        top = max(px(26), h / 2 - px(60))
        title = self.labels["drop"] if self.can_drop else self.labels["add"]
        self.create_text(x0, top, text=title, anchor="w", font=font(18, "bold"), fill=TEXT)
        self.create_text(x0, top + px(34), anchor="w", font=font(10), fill=MUTED,
                         text=self.labels["sub"])
        by = top + px(90)
        gap = px(10)
        self.create_window(x0, by, window=self.b1, anchor="w")
        self.create_window(x0 + int(self.b1["width"]) + gap, by, window=self.b2, anchor="w")


class Row(tk.Canvas):
    """One queued file or folder (on the PC), or one entry on the phone."""

    def __init__(self, master, path, on_remove, remote=False, is_dir=False, meta="",
                 size_text="…", on_toggle=None, on_open=None):
        super().__init__(master, height=px(64), bg=BG, highlightthickness=0, bd=0)
        self.path = path
        self.on_remove, self.on_toggle, self.on_open = on_remove, on_toggle, on_open
        self.remote = remote
        if remote:
            self.is_dir = bool(is_dir)
            self.name = posixpath.basename(path.rstrip("/")) or path
            self.parent = meta
        else:
            self.is_dir = os.path.isdir(path)
            stripped = path.rstrip("/\\")
            self.name = os.path.basename(stripped) or path
            self.parent = os.path.dirname(stripped)
        self.size_text = size_text
        self.state = "queued"
        self.removable = True
        self.selected = False
        self.note = ""
        self.hover = False
        self.phase = 0
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Double-Button-1>", self._double)

    def _hover(self, on):
        self.hover = on
        if not on:
            self.configure(cursor="")
        self.redraw()

    def _in_tail(self, x):
        return x > self.winfo_width() - px(52)

    def _can_remove(self):
        return self.state == "queued" and self.removable

    def _motion(self, e):
        if self.remote:
            self.configure(cursor="hand2" if self._can_remove() else "")
        else:
            self.configure(cursor="hand2" if self._in_tail(e.x) and self._can_remove() else "")

    def _click(self, e):
        if not self._can_remove():
            return
        if self.remote:
            if self.is_dir and self._in_tail(e.x):
                if self.on_open:
                    self.on_open(self.path)
            elif self.on_toggle:
                self.on_toggle(self.path)
        elif self._in_tail(e.x):
            self.on_remove(self.path)

    def _double(self, e):
        if self.remote and self.is_dir and self._can_remove() and self.on_open:
            self.on_open(self.path)

    def set_state(self, state):
        self.state = state
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60:
            return
        if self.selected:
            rrect(self, 0, 3, w, h - 3, px(12), fill=RAISED, outline=LINE_HI, width=1)
        elif self.hover:
            rrect(self, 0, 3, w, h - 3, px(12), fill=SURFACE, outline="")
        else:
            self.create_line(px(62), h - 1, w - px(6), h - 1, fill=HAIR)
        lead = px(30) if self.remote else 0
        if self.remote:  # selection marker
            cx, cy, r = px(22), h / 2, px(8)
            if self.selected:
                self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=ACCENT, outline="")
                self.create_line(cx - px(3.5), cy, cx - px(1), cy + px(3), cx + px(4), cy - px(3),
                                 fill=ACCENT_INK, width=2, capstyle="round", joinstyle="round")
            else:
                self.create_oval(cx - r, cy - r, cx + r, cy + r, outline=FAINT if self.hover else LINE, width=2)
        # badge
        bs = px(40)
        bx, by = px(12) + lead, (h - bs) // 2
        (tint, fg), label = badge_for(self.path, self.is_dir)
        rrect(self, bx, by, bx + bs, by + bs, px(10), fill=tint, outline="")
        if self.is_dir:
            x0, x1, y0, y1 = bx + px(9), bx + bs - px(9), by + px(11), by + bs - px(10)
            self.create_polygon(x0, y1, x0, y0, x0 + (x1 - x0) * 0.42, y0, x0 + (x1 - x0) * 0.56, y0 + px(4),
                                x1, y0 + px(4), x1, y1, fill="", outline=fg, width=2, joinstyle="round")
        else:
            self.create_text(bx + bs / 2, by + bs / 2, text=label, font=font(9, "bold"), fill=fg)
        # text
        tx = bx + bs + px(14)
        size_right = w - px(58)
        maxw = size_right - px(84) - tx
        self.create_text(tx, h / 2 - px(9), text=ellipsize(self.name, font(10), maxw), anchor="w",
                         font=font(10), fill=TEXT)
        if self.remote:
            sub = self.parent
        else:
            sub = self.parent or ("Folder" if self.is_dir else "")
        if self.note and self.state == "failed":
            self.create_text(tx, h / 2 + px(11), text=ellipsize(self.note, font(9), maxw),
                             anchor="w", font=font(9), fill=BAD)
        else:
            self.create_text(tx, h / 2 + px(11), text=ellipsize(sub, font(9), maxw, keep="end"),
                             anchor="w", font=font(9), fill=MUTED)
        self.create_text(size_right, h / 2, text=self.size_text, anchor="e", font=font(9, mono=True), fill=MUTED)
        # tail
        cx, cy, r = w - px(26), h / 2, px(9)
        st = self.state
        if st == "sending":
            self.create_oval(cx - r, cy - r, cx + r, cy + r, outline=LINE, width=2)
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=-self.phase * 18, extent=110,
                            style="arc", outline=ACCENT, width=2)
        elif st == "done":
            self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=OK_WASH, outline="")
            self.create_line(cx - px(4), cy, cx - px(1), cy + px(3), cx + px(4), cy - px(3),
                             fill=OK, width=2, capstyle="round", joinstyle="round")
        elif st == "failed":
            self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=BAD_WASH, outline="")
            self.create_line(cx, cy - px(4), cx, cy + px(1), fill=BAD, width=2, capstyle="round")
            self.create_oval(cx - 1, cy + px(3) - 1, cx + 1, cy + px(3) + 1, fill=BAD, outline="")
        elif self.remote:
            if self.is_dir:  # chevron: open this folder
                d = px(4)
                self.create_line(cx - d / 2, cy - d - 1, cx + d / 2, cy, cx - d / 2, cy + d + 1,
                                 fill=TEXT if self.hover else MUTED, width=2, capstyle="round",
                                 joinstyle="round")
        elif self.hover and self.removable:
            d = px(4)
            self.create_line(cx - d, cy - d, cx + d, cy + d, fill=TEXT, width=2, capstyle="round")
            self.create_line(cx - d, cy + d, cx + d, cy - d, fill=TEXT, width=2, capstyle="round")
        else:
            self.create_oval(cx - px(3), cy - px(3), cx + px(3), cy + px(3), fill=FAINT, outline="")


class Segmented(tk.Canvas):
    """Two-way switch (To phone / From phone)."""

    def __init__(self, master, options, command, bg=None):
        bg = BG if bg is None else bg
        self.fnt = font(10, "bold")
        self.options, self.command, self.value = options, command, options[0][0]
        self.seg_w = [self.fnt.measure(lbl) + px(36) for _k, lbl in options]
        super().__init__(master, width=sum(self.seg_w) + px(8), height=px(40), bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self._focus = False
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self._click)
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.bind("<Left>", lambda e: self._step(-1))
        self.bind("<Right>", lambda e: self._step(1))

    def _set_focus(self, on):
        self._focus = on
        self.draw()

    def _step(self, d):
        keys = [k for k, _l in self.options]
        i = min(max(keys.index(self.value) + d, 0), len(keys) - 1)
        self.command(keys[i])

    def set(self, key):
        self.value = key
        self.draw()

    def _click(self, e):
        x = px(4)
        for (key, _lbl), w in zip(self.options, self.seg_w):
            if x <= e.x < x + w:
                self.command(key)
                return
            x += w

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        rrect(self, 1, 1, w - 1, h - 1, px(11), fill=SURFACE,
              outline=ACCENT_HI if self._focus else LINE, width=2 if self._focus else 1)
        x = px(4)
        for (key, lbl), sw in zip(self.options, self.seg_w):
            on = key == self.value
            if on:
                rrect(self, x, px(4), x + sw, h - px(4), px(8), fill=RAISED_HI, outline=LINE_HI, width=1)
            self.create_text(x + sw / 2, h / 2, text=lbl, font=self.fnt, fill=TEXT if on else MUTED)
            x += sw


class FileList(tk.Frame):
    def __init__(self, master, on_remove):
        super().__init__(master, bg=BG)
        self.on_remove = on_remove
        self.rows = {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", style="Dark.Vertical.TScrollbar",
                                command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)

    def _on_scroll(self, lo, hi):
        self.sb.set(lo, hi)
        if float(lo) <= 0 and float(hi) >= 1:
            self.sb.grid_remove()
        else:
            self.sb.grid(row=0, column=1, sticky="ns", padx=(px(8), 0))

    def _bind_wheel(self, _e):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))

    def _unbind_wheel(self, _e):
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(ev)

    def _wheel(self, e):
        if self.sb.winfo_ismapped():
            self.canvas.yview_scroll(int(-e.delta / 120) * 2 or (-2 if e.delta > 0 else 2), "units")

    def add(self, path, **kw):
        row = Row(self.inner, path, self.on_remove, **kw)
        row.pack(fill="x")
        self.rows[path] = row
        return row

    def remove(self, path):
        row = self.rows.pop(path, None)
        if row:
            row.destroy()

    def clear(self):
        for row in self.rows.values():
            row.destroy()
        self.rows.clear()

    def set_size(self, path, text):
        row = self.rows.get(path)
        if row:
            row.size_text = text
            row.redraw()

    def set_note(self, path, text):
        row = self.rows.get(path)
        if row:
            row.note = text
            row.redraw()

    def set_state(self, path, state):
        row = self.rows.get(path)
        if not row:
            return
        row.set_state(state)
        if state == "sending":
            self.update_idletasks()
            top = self.canvas.canvasy(0)
            bottom = top + self.canvas.winfo_height()
            y, rh = row.winfo_y(), row.winfo_height()
            total = max(self.inner.winfo_height(), 1)
            if y < top or y + rh > bottom:
                self.canvas.yview_moveto(max(0.0, (y - rh) / total))

    def set_removable(self, on):
        for row in self.rows.values():
            row.removable = on
            row.redraw()

    def reset_states(self):
        for row in self.rows.values():
            row.note = ""
            row.set_state("queued")

    def pulse(self):
        for row in self.rows.values():
            if row.state == "sending":
                row.phase = (row.phase + 1) % 20
                row.redraw()


class CheckRow(tk.Canvas):
    """Checkbox with a label and a one-line hint."""

    def __init__(self, master, text, hint, variable, command=None):
        super().__init__(master, height=px(44), bg=BG, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=1)
        self.text, self.hint, self.var, self.command = text, hint, variable, command
        self._hover = self._focus = False
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Enter>", lambda e: self._set(_hover=True))
        self.bind("<Leave>", lambda e: self._set(_hover=False))
        self.bind("<ButtonRelease-1>", lambda e: self.toggle())
        self.bind("<FocusIn>", lambda e: self._set(_focus=True))
        self.bind("<FocusOut>", lambda e: self._set(_focus=False))
        self.bind("<space>", lambda e: self.toggle())
        self.bind("<Return>", lambda e: self.toggle())

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.draw()

    def toggle(self):
        self.var.set(not self.var.get())
        if self.command:
            self.command()
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40:
            return
        if self._focus:
            rrect(self, 0, 2, w, h - 2, px(10), fill="", outline=ACCENT_HI, width=2)
        bs, bx, by = px(20), px(8), (h - px(20)) // 2
        if self.var.get():
            rrect(self, bx, by, bx + bs, by + bs, px(6), fill=PRIMARY, outline="")
            self.create_line(bx + px(5), by + px(10), bx + px(8.5), by + px(13.5), bx + px(15), by + px(6.5),
                             fill=PRIMARY_FG, width=2, capstyle="round", joinstyle="round")
        else:
            rrect(self, bx + 1, by + 1, bx + bs - 1, by + bs - 1, px(6), fill="",
                  outline=TEXT if self._hover else LINE_HI, width=2)
        tx = bx + bs + px(12)
        self.create_text(tx, h / 2 - px(7), text=ellipsize(self.text, font(10), w - tx), anchor="w",
                         font=font(10), fill=TEXT)
        self.create_text(tx, h / 2 + px(9), text=ellipsize(self.hint, font(9), w - tx), anchor="w",
                         font=font(9), fill=MUTED)


class ThemeButton(tk.Canvas):
    """Sun or moon: switches to the other theme."""

    def __init__(self, master, command):
        super().__init__(master, width=px(32), height=px(32), bg=BG, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=1)
        self.command = command
        self._hover = self._focus = False
        self.bind("<Enter>", lambda e: self._set(_hover=True))
        self.bind("<Leave>", lambda e: self._set(_hover=False))
        self.bind("<ButtonRelease-1>", lambda e: self.command())
        self.bind("<FocusIn>", lambda e: self._set(_focus=True))
        self.bind("<FocusOut>", lambda e: self._set(_focus=False))
        self.bind("<Return>", lambda e: self.command())
        self.bind("<space>", lambda e: self.command())
        self.draw()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.draw()

    def draw(self):
        self.delete("all")
        w = px(32)
        bgc = RAISED if self._hover else BG
        if self._hover:
            rrect(self, 0, 0, w, w, px(10), fill=RAISED, outline="")
        if self._focus:
            rrect(self, 1, 1, w - 1, w - 1, px(10), fill="", outline=ACCENT_HI, width=2)
        c = w / 2
        col = TEXT if self._hover else MUTED
        if THEME == "dark":  # offer light: sun
            r = px(4)
            self.create_oval(c - r, c - r, c + r, c + r, fill=col, outline="")
            for i in range(8):
                a = i * math.pi / 4
                self.create_line(c + math.cos(a) * px(7), c + math.sin(a) * px(7),
                                 c + math.cos(a) * px(10), c + math.sin(a) * px(10),
                                 fill=col, width=2, capstyle="round")
        else:  # offer dark: crescent moon
            r = px(8)
            self.create_oval(c - r, c - r, c + r, c + r, fill=col, outline="")
            r2 = px(7)
            self.create_oval(c + px(3) - r2, c - px(3) - r2, c + px(3) + r2, c - px(3) + r2,
                             fill=bgc, outline="")


class Modal(tk.Toplevel):
    def __init__(self, app, title, width=440):
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.title(title)
        self.configure(bg=BG)
        self.transient(app)
        self.resizable(False, False)
        self.min_w = px(width)
        self.body = tk.Frame(self, bg=BG, padx=px(28), pady=px(24))
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.destroy())

    def present(self):
        self.update_idletasks()
        w = max(self.winfo_reqwidth(), self.min_w)
        h = self.winfo_reqheight()
        x = self.app.winfo_rootx() + (self.app.winfo_width() - w) // 2
        y = self.app.winfo_rooty() + max(px(40), (self.app.winfo_height() - h) // 3)
        self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        style_titlebar(self)
        self.deiconify()
        self.update()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_set()


# ----------------------------------------------------------------------- app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self._prefs = load_prefs()
        set_palette(self._prefs.get("theme") if self._prefs.get("theme") in PALETTES else system_theme())
        self._status = ("Add files to get started", "normal")
        self._log_history = []
        global SCALE
        SCALE = max(1.0, self.winfo_fpixels("1i") / 96.0)
        init_fonts(self)
        self.title(APP_TITLE)
        self.configure(bg=BG)
        self._set_icon()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(px(1060), sw - 60), min(px(780), sh - 90)
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        self.minsize(min(px(960), w), min(px(640), h))

        self.adb_path = tk.StringVar(value=find_adb())
        self.device_var = tk.StringVar()
        self.dest_var = tk.StringVar(value=DEFAULT_DEST)
        self.save_var = tk.StringVar(value=pc_places()[0][1])
        self.mode = "push"  # push: PC -> phone, pull: phone -> PC
        self.remote_path = "/sdcard/"
        self.sel = []  # selected phone paths (pull)
        self.rsizes = {}
        self.rdirs = set()
        self._browse_gen = 0

        self.items = []  # absolute paths to transfer
        self.apks = []  # APK files queued for install
        saved = self._prefs.get("install_opts", {})
        self.opt_vars = {k: tk.BooleanVar(value=saved.get(k, default)) for k, _l, _h, default in INSTALL_OPTIONS}
        for var in self.opt_vars.values():
            var.trace_add("write", lambda *_: self._save_opts())
        self.sizes = {}  # path -> bytes (once known)
        self.devices = {}  # display label -> serial
        self.models = {}  # serial -> nice model name
        self.q = queue.Queue()
        self.worker = None
        self.proc = None
        self.cancel_flag = threading.Event()
        self.sending = False
        self._bytes = self._total = 0
        self._disp = 0.0
        self._t0 = self._last_t = 0.0
        self._last_bytes = 0
        self._speed = 0.0
        self._index = (0, 0)

        self.can_drop = False
        if windnd is not None:
            try:
                windnd.hook_dropfiles(self, func=self._on_drop)
                self.can_drop = True
            except Exception:
                pass

        self._style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.dest_var.trace_add("write", lambda *_: self._sync_chips())
        self.save_var.trace_add("write", lambda *_: self._sync_chips())
        self.device_var.trace_add("write", lambda *_: self._apply_device())
        self._sync_chips()
        self._update_adb_label()
        style_titlebar(self)
        self.after(50, self._drain_queue)
        self.after(300, self.refresh_devices)

    def _set_icon(self):
        """Window and taskbar icon that follows the Windows light or dark app setting."""
        dark = system_theme() == "dark"
        try:
            self._icon = tk.PhotoImage(data="".join(ICON_DARK if dark else ICON_LIGHT))
            self.iconphoto(True, self._icon)
        except tk.TclError:
            pass

    def _save_opts(self):
        self._prefs["install_opts"] = {k: v.get() for k, v in self.opt_vars.items()}
        save_prefs(self._prefs)

    def toggle_theme(self):
        if self.sending:
            self.set_status("Wait for the transfer to finish before changing the theme", "dim")
            return
        self.apply_theme("light" if THEME == "dark" else "dark")

    def apply_theme(self, name):
        """Switch palette and rebuild the window, keeping the user's state."""
        snap = dict(
            mode=self.mode,
            rows={p: r.state for p, r in self.filelist.rows.items()},
            apk_rows={p: (r.state, r.note) for p, r in self.apklist.rows.items()},
            status=self._status,
            progress=(self.progress.frac, self.progress.color),
            stats=self.stats_lbl.cget("text"),
            route=(self.route.mode, self.route.center, self.route.reverse),
            log_open=self._log_open,
            wifi=self.wifi_field.value(),
            send=self.send_btn.text,
        )
        set_palette(name)
        self._prefs["theme"] = name
        save_prefs(self._prefs)
        self.configure(bg=BG)
        for w in self.winfo_children():
            w.destroy()
        self._style()
        self._build_ui()

        self.device_combo["values"] = list(self.devices)
        self._sync_chips()
        self._update_adb_label()
        if snap["wifi"]:
            self.wifi_field.set_value(snap["wifi"])
        for p in self.items:
            row = self.filelist.add(p)
            if p in self.sizes:
                row.size_text = human_size(self.sizes[p])
            row.state = snap["rows"].get(p, "queued")
        for p in self.apks:
            row = self.apklist.add(p)
            row.size_text = human_size(self.sizes.get(p, 0))
            row.state, row.note = snap["apk_rows"].get(p, ("queued", ""))
        self._show_stage()
        self._show_apk_stage()
        for text, tag in self._log_history:
            self._log_insert(text, tag)
        if snap["log_open"]:
            self._log_open = True
            self.log_frame.grid(row=3, column=0, sticky="ew")
            self.act_btn.set_text("Hide activity")
        self._apply_device()
        if snap["mode"] != "push":
            self.mode = "push"
            self.set_direction(snap["mode"])
        self.progress.set(*snap["progress"])
        self.stats_lbl.configure(text=snap["stats"])
        self.send_btn.set_text(snap["send"])
        self.route.mode, self.route.center, self.route.reverse = snap["route"]
        self.route.draw()
        self._update_count()
        if snap["mode"] == "pull":  # the phone list reloads, so the selection starts over
            self.set_status("Click files or folders on the phone to select them", "dim")
        else:
            self.set_status(*snap["status"])
        style_titlebar(self)

    # -------------------------------------------------------------- styling
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.layout("Dark.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.configure("Dark.Vertical.TScrollbar", troughcolor=BG, background=RAISED_HI,
                    bordercolor=BG, lightcolor=RAISED_HI, darkcolor=RAISED_HI, width=px(8))
        s.map("Dark.Vertical.TScrollbar", background=[("active", FAINT)])
        s.configure("TCombobox", fieldbackground=RAISED, background=RAISED, foreground=TEXT,
                    arrowcolor=MUTED, bordercolor=LINE, lightcolor=RAISED, darkcolor=RAISED,
                    padding=px(8), arrowsize=px(14))
        s.map("TCombobox",
              fieldbackground=[("readonly", RAISED)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", RAISED)], selectforeground=[("readonly", TEXT)],
              bordercolor=[("focus", ACCENT)], arrowcolor=[("hover", TEXT)])
        self.option_add("*TCombobox*Listbox.background", RAISED)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", RAISED_HI)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.option_add("*TCombobox*Listbox.font", font(10))

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.route = Route(self, platform.node())
        self.route.grid(row=0, column=0, sticky="ew")
        self.theme_btn = ThemeButton(self.route, self.toggle_theme)
        self.theme_btn.place(relx=1.0, x=-px(12), y=px(8), anchor="ne")

        body = tk.Frame(self, bg=BG)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_files(body).grid(row=0, column=0, sticky="nsew")
        tk.Frame(body, bg=LINE, width=1).grid(row=0, column=1, sticky="ns")
        self._build_side(body).grid(row=0, column=2, sticky="ns")

        self._build_bar()
        self._build_log()

    def _build_files(self, parent):
        f = tk.Frame(parent, bg=BG, padx=px(28), pady=px(24))
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        bar = tk.Frame(f, bg=BG)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, px(12)))
        self.seg = Segmented(bar, [("push", "To phone"), ("pull", "From phone"), ("install", "Install apps")],
                             self.set_direction)
        self.seg.pack(side="left")
        self.tools = tk.Frame(bar, bg=BG)
        RButton(self.tools, "Clear", self.clear_items, "quiet", height=32, pad=12).pack(side="right")
        RButton(self.tools, "Add folder", self.add_folder, "ghost", height=32, pad=14).pack(
            side="right", padx=px(8))
        RButton(self.tools, "Add files", self.add_files, "ghost", height=32, pad=14).pack(side="right")
        self.apk_tools = tk.Frame(bar, bg=BG)
        RButton(self.apk_tools, "Clear", self.clear_apks, "quiet", height=32, pad=12).pack(side="right")
        RButton(self.apk_tools, "Add folder", self.add_apk_folder, "ghost", height=32, pad=14).pack(
            side="right", padx=px(8))
        RButton(self.apk_tools, "Add APKs", self.add_apks, "ghost", height=32, pad=14).pack(side="right")

        stage = self.push_stage = tk.Frame(f, bg=BG)
        stage.grid(row=1, column=0, sticky="nsew")
        stage.columnconfigure(0, weight=1)
        stage.rowconfigure(0, weight=1)
        self.dropzone = DropZone(stage, self.add_files, self.add_folder, self.can_drop)
        self.filelist = FileList(stage, self.remove_path)
        self.dropzone.grid(row=0, column=0, sticky="nsew")

        # install apps
        astage = self.apk_stage = tk.Frame(f, bg=BG)
        astage.columnconfigure(0, weight=1)
        astage.rowconfigure(0, weight=1)
        self.apk_drop = DropZone(astage, self.add_apks, self.add_apk_folder, self.can_drop, labels=dict(
            drop="Drop APK files here", add="Add APK files",
            sub="Also works with .apks, .xapk and .apkm bundles",
            b1="Add APKs", b2="Add folder"))
        self.apklist = FileList(astage, self.remove_apk)
        self.apk_drop.grid(row=0, column=0, sticky="nsew")

        # phone browser (From phone)
        ps = self.pull_stage = tk.Frame(f, bg=BG)
        ps.columnconfigure(0, weight=1)
        ps.rowconfigure(1, weight=1)
        pbar = tk.Frame(ps, bg=BG)
        pbar.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        self.up_btn = RButton(pbar, "Up", self.browse_up, "ghost", height=32, pad=16)
        self.up_btn.pack(side="left")
        RButton(pbar, "Reload", lambda: self.browse(self.remote_path), "quiet", height=32, pad=12).pack(side="right")
        self.path_lbl = tk.Label(pbar, text="", font=font(10, mono=True), bg=BG, fg=MUTED, anchor="w")
        self.path_lbl.pack(side="left", fill="x", expand=True, padx=px(12))
        self.phonelist = FileList(ps, None)
        self.phonelist.grid(row=1, column=0, sticky="nsew")
        self.browse_msg = tk.Label(ps, text="", font=font(10), bg=BG, fg=MUTED, justify="center")
        return f

    def _section(self, parent, title, first=False):
        lbl = tk.Label(parent, text=title, font=font(12, "bold"), bg=BG, fg=TEXT, anchor="w")
        lbl.pack(fill="x", pady=(0 if first else px(24), px(8)))
        return lbl

    def _build_side(self, parent):
        side = tk.Frame(parent, bg=BG, width=px(344))
        side.pack_propagate(False)
        inner = tk.Frame(side, bg=BG)
        inner.pack(fill="both", expand=True, padx=px(24), pady=px(24))

        # destination (phone folder when sending, PC folder when saving)
        self.side_title = self._section(inner, "Send to", first=True)
        holder = tk.Frame(inner, bg=BG)
        holder.pack(fill="x")

        self.push_dest = tk.Frame(holder, bg=BG)
        grid = tk.Frame(self.push_dest, bg=BG)
        grid.pack(fill="x")
        grid.columnconfigure((0, 1), weight=1, uniform="chips")
        self.chips = {}
        for i, (label, path) in enumerate(PRESETS):
            b = RButton(grid, label, lambda p=path: self.dest_var.set(p), "chip", height=36, pad=12)
            b.grid(row=i // 2, column=i % 2, sticky="ew",
                   padx=(0, px(4)) if i % 2 == 0 else (px(4), 0), pady=px(4))
            self.chips[path] = b
        tk.Label(self.push_dest, text="Folder on the phone", font=font(9), bg=BG, fg=MUTED,
                 anchor="w").pack(fill="x", pady=(px(16), px(8)))
        Field(self.push_dest, textvariable=self.dest_var, mono=True).pack(fill="x")
        self.push_dest.pack(fill="x")

        self.pull_dest = tk.Frame(holder, bg=BG)
        pgrid = tk.Frame(self.pull_dest, bg=BG)
        pgrid.pack(fill="x")
        pgrid.columnconfigure((0, 1), weight=1, uniform="pchips")
        self.save_chips = {}
        for i, (label, path) in enumerate(pc_places()):
            b = RButton(pgrid, label, lambda p=path: self.save_var.set(p), "chip", height=36, pad=12)
            b.grid(row=i // 2, column=i % 2, sticky="ew",
                   padx=(0, px(4)) if i % 2 == 0 else (px(4), 0), pady=px(4))
            self.save_chips[path] = b
        tk.Label(self.pull_dest, text="Folder on this PC", font=font(9), bg=BG, fg=MUTED,
                 anchor="w").pack(fill="x", pady=(px(16), px(8)))
        prow = tk.Frame(self.pull_dest, bg=BG)
        prow.pack(fill="x")
        prow.columnconfigure(0, weight=1)
        Field(prow, textvariable=self.save_var, mono=True).grid(row=0, column=0, sticky="ew")
        RButton(prow, "Browse", self.browse_save, "ghost", height=40, pad=16).grid(
            row=0, column=1, padx=(px(8), 0))

        self.apk_opts = tk.Frame(holder, bg=BG)
        for key, label, hint, _d in INSTALL_OPTIONS:
            CheckRow(self.apk_opts, label, hint, self.opt_vars[key]).pack(fill="x")

        # connection
        head = tk.Frame(inner, bg=BG)
        head.pack(fill="x", pady=(px(24), px(8)))
        tk.Label(head, text="Phone", font=font(12, "bold"), bg=BG, fg=TEXT).pack(side="left")
        RButton(head, "Refresh", self.refresh_devices, "quiet", height=28, pad=10, size=9).pack(side="right")
        self.device_combo = ttk.Combobox(inner, textvariable=self.device_var, state="readonly",
                                         font=font(10))
        self.device_combo.pack(fill="x", ipady=px(4))

        row = tk.Frame(inner, bg=BG)
        row.pack(fill="x", pady=(px(8), 0))
        row.columnconfigure((0, 1), weight=1, uniform="pair")
        RButton(row, "Scan QR", self.qr_connect, "ghost", height=36).grid(
            row=0, column=0, sticky="ew", padx=(0, px(4)))
        RButton(row, "Pair with code", self.pair_with_code, "ghost", height=36).grid(
            row=0, column=1, sticky="ew", padx=(px(4), 0))

        tk.Label(inner, text="Wi-Fi address", font=font(9), bg=BG, fg=MUTED, anchor="w").pack(
            fill="x", pady=(px(16), px(8)))
        wrow = tk.Frame(inner, bg=BG)
        wrow.pack(fill="x")
        wrow.columnconfigure(0, weight=1)
        self.wifi_field = Field(wrow, placeholder="192.168.1.5:5555", mono=True)
        self.wifi_field.grid(row=0, column=0, sticky="ew")
        RButton(wrow, "Connect", self.wifi_connect, "ghost", height=40, pad=16).grid(
            row=0, column=1, padx=(px(8), 0))

        # adb location, at the bottom
        foot = tk.Frame(inner, bg=BG)
        foot.pack(side="bottom", fill="x")
        self.adb_lbl = tk.Label(foot, text="", font=font(9), bg=BG, fg=MUTED, anchor="w")
        self.adb_lbl.pack(side="left", fill="x", expand=True)
        self.adb_btn = RButton(foot, "Change", self.browse_adb, "quiet", height=28, pad=10, size=9)
        self.adb_btn.pack(side="right")
        return side

    def _build_bar(self):
        bar = tk.Frame(self, bg=SURFACE)
        bar.grid(row=2, column=0, sticky="ew")
        tk.Frame(bar, bg=LINE, height=1).pack(fill="x")
        inner = tk.Frame(bar, bg=SURFACE, padx=px(28), pady=px(16))
        inner.pack(fill="x")
        inner.columnconfigure(0, weight=1)

        top = tk.Frame(inner, bg=SURFACE)
        top.grid(row=0, column=0, sticky="ew")
        self.status_lbl = tk.Label(top, text="Add files to get started", font=font(10),
                                   bg=SURFACE, fg=TEXT, anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.act_btn = RButton(top, "Activity", self.toggle_log, "quiet", height=26, pad=10, size=9, bg=SURFACE)
        self.act_btn.pack(side="right")
        self.stats_lbl = tk.Label(top, text="", font=font(9, mono=True), bg=SURFACE, fg=MUTED)
        self.stats_lbl.pack(side="right", padx=(0, px(8)))
        self.progress = ProgressBar(inner, bg=SURFACE)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))

        btns = tk.Frame(inner, bg=SURFACE)
        btns.grid(row=0, column=1, rowspan=2, padx=(px(24), 0))
        self.cancel_btn = RButton(btns, "Cancel", self.cancel_transfer, "danger", height=44, pad=22, bg=SURFACE)
        self.send_btn = RButton(btns, "Send to phone", self.start_transfer, "primary", height=44,
                                pad=32, size=10, weight="bold", bg=SURFACE)
        self.send_btn.pack(side="right")

    def _build_log(self):
        self.log_frame = tk.Frame(self, bg=SURFACE)
        wrap = tk.Frame(self.log_frame, bg=SURFACE, padx=px(28), pady=px(4))
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        self.log = tk.Text(wrap, height=9, state="disabled", wrap="word", bg=SURFACE, fg=LOGFG,
                           font=font(9, mono=True), relief="flat", bd=0, highlightthickness=0,
                           padx=0, pady=px(8), selectbackground=RAISED_HI, spacing1=px(2))
        sb = ttk.Scrollbar(wrap, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.grid(row=0, column=0, sticky="ew")
        sb.grid(row=0, column=1, sticky="ns", padx=(px(8), 0))
        for tag, color in (("err", BAD), ("warn", WARN), ("cmd", TEXT), ("ok", OK)):
            self.log.tag_configure(tag, foreground=color)
        self._log_open = False

    # ------------------------------------------------------------- helpers
    def toggle_log(self):
        self._log_open = not self._log_open
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        if self._log_open:
            self.log_frame.grid(row=3, column=0, sticky="ew")
            self.act_btn.set_text("Hide activity")
            self.update_idletasks()
            self._log_h = self.log_frame.winfo_reqheight()
            self.geometry(f"{w}x{h + self._log_h}")  # grow so the side panel keeps its room
            self.log.see("end")
        else:
            self.log_frame.grid_remove()
            self.act_btn.set_text("Activity")
            self.geometry(f"{w}x{max(self.minsize()[1], h - getattr(self, '_log_h', 0))}")

    def log_write(self, msg):
        msg = msg.rstrip("\n")
        low = msg.lower()
        tag = None
        if msg.startswith(("[error]", "[failed]")) or low.startswith(("pairing failed", "error")):
            tag = "err"
        elif msg.startswith("[warn]"):
            tag = "warn"
        elif msg.startswith(">"):
            tag = "cmd"
        elif low.startswith(("done", "sent", "connected")):
            tag = "ok"
        self._log_history.append((msg, tag))
        del self._log_history[:-500]
        self._log_insert(msg, tag)

    def _log_insert(self, msg, tag):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, text, tone="normal"):
        color = {"normal": TEXT, "ok": OK, "bad": BAD, "dim": MUTED}[tone]
        self._status = (text, tone)
        self.status_lbl.configure(text=text, fg=color)

    def alert(self, title, body, tone="info"):
        m = Modal(self, title, 420)
        color = {"info": MUTED, "error": BAD, "warn": WARN, "ok": OK}[tone]
        head = tk.Frame(m.body, bg=BG)
        head.pack(fill="x")
        dot = tk.Canvas(head, width=px(12), height=px(12), bg=BG, highlightthickness=0)
        dot.create_oval(1, 1, px(11), px(11), fill=color, outline="")
        dot.pack(side="left", padx=(0, px(8)))
        tk.Label(head, text=title, font=font(14, "bold"), bg=BG, fg=TEXT).pack(side="left")
        tk.Label(m.body, text=body, font=font(10), bg=BG, fg=MUTED, justify="left", anchor="w",
                 wraplength=px(360)).pack(fill="x", pady=(px(12), px(20)))
        RButton(m.body, "OK", m.destroy, "primary", height=40, pad=28, weight="bold").pack(anchor="e")
        m.bind("<Return>", lambda e: m.destroy())
        m.present()
        m.wait_window()

    def adb(self):
        path = self.adb_path.get().strip()
        if not path or not (os.path.isfile(path) or shutil.which(path)):
            raise FileNotFoundError(
                "adb not found. Install Android platform-tools and add it to PATH, "
                "or use Change to pick adb."
            )
        return path

    def run_adb(self, args, timeout=30):
        return subprocess.run(
            [self.adb()] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=ADB_ENV,
            creationflags=CREATE_NO_WINDOW,
        )

    def selected_serial(self):
        return self.devices.get(self.device_var.get())

    def _update_adb_label(self):
        path = self.adb_path.get().strip()
        if path and (os.path.isfile(path) or shutil.which(path)):
            self.adb_lbl.configure(text=ellipsize("adb: " + path, font(9), px(220), keep="end"), fg=MUTED)
            self.adb_btn.set_text("Change")
        else:
            self.adb_lbl.configure(text="adb not found", fg=BAD)
            self.adb_btn.set_text("Locate")

    def _sync_chips(self):
        cur = self.dest_var.get().strip().rstrip("/")
        for path, chip in self.chips.items():
            chip.set_kind("chip_on" if path.rstrip("/") == cur else "chip")
        save = os.path.normcase(os.path.normpath(self.save_var.get().strip() or "."))
        for path, chip in self.save_chips.items():
            chip.set_kind("chip_on" if os.path.normcase(os.path.normpath(path)) == save else "chip")

    @staticmethod
    def _conn_type(serial):
        if serial.startswith("emulator-"):
            return "Emulator"
        if ":" in serial or "_adb" in serial or "." in serial:
            return "Wi-Fi"
        return "USB"

    def _apply_device(self):
        serial = self.selected_serial()
        if not serial:
            self.route.set_device(None)
            return
        short = serial if len(serial) <= 22 else serial[:10] + "…"
        self.route.set_device(self.models.get(serial, "Android device"),
                              f"{self._conn_type(serial)} · {short}")
        if self.mode == "pull" and not self.sending:
            self.browse(self.remote_path)

    # ---------------------------------------------------------- ADB / device
    def browse_adb(self):
        p = filedialog.askopenfilename(title="Select adb executable")
        if p:
            self.adb_path.set(p)
            self._update_adb_label()
            self.refresh_devices()

    def refresh_devices(self):
        try:
            res = self.run_adb(["devices", "-l"])
        except Exception as e:
            self.device_combo["values"] = []
            self.device_var.set("")
            self.devices = {}
            self.set_status(str(e), "bad")
            self.log_write(f"[error] {e}")
            self._update_adb_label()
            self._apply_device()
            return

        self.devices, self.models = {}, {}
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
                        model = p.split(":", 1)[1].replace("_", " ")
                nice = model or "Android device"
                self.models[serial] = nice
                self.devices[f"{nice}  ({serial})"] = serial
            elif state == "unauthorized":
                unauthorized.append(serial)

        labels = list(self.devices)
        self.device_combo["values"] = labels
        if labels:
            if self.device_var.get() not in labels:
                self.device_var.set(labels[0])
            if not self.sending and not self.items:
                self.set_status(("1 phone connected" if len(labels) == 1 else f"{len(labels)} phones connected"), "dim")
        else:
            self.device_var.set("")
            msg = "No phone found. Plug it in and turn on USB debugging"
            if unauthorized:
                msg = "Phone is locked out. Accept the Allow USB debugging prompt on the phone"
            self.set_status(msg, "bad" if unauthorized else "dim")
            self.log_write(f"[info] {msg}")
        self._apply_device()

    def wifi_connect(self):
        addr = self.wifi_field.value()
        if not addr:
            self.alert("Enter an address", "Type the phone's IP and port, for example 192.168.1.5:5555.", "info")
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
            self.alert("QR needs one more package",
                       "Install it with:  pip install qrcode\n\nOr use Pair with code, which needs nothing extra.",
                       "warn")
            return
        try:
            adb = self.adb()
        except FileNotFoundError as e:
            self.alert("adb not found", str(e), "error")
            return

        # Same shape Android Studio uses for its pairing QR code.
        name = "studio-" + "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10)
        )
        password = "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(12)
        )
        qr_text = f"WIFI:T:ADB;S:{name};P:{password};;"

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
        qr.add_data(qr_text)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        n = len(matrix)
        quiet = 3
        cell = max(px(4), px(250) // (n + 2 * quiet))
        size = (n + 2 * quiet) * cell

        m = Modal(self, "Pair over Wi-Fi", 460)
        b = m.body
        tk.Label(b, text="Pair over Wi-Fi", font=font(14, "bold"), bg=BG, fg=TEXT, anchor="w").pack(fill="x")
        steps = [
            "Open Settings > Developer options > Wireless debugging on the phone.",
            "Tap Pair device with QR code.",
            "Scan the code below.",
        ]
        for i, text in enumerate(steps, 1):
            r = tk.Frame(b, bg=BG)
            r.pack(fill="x", pady=(px(12) if i == 1 else px(8), 0))
            c = tk.Canvas(r, width=px(24), height=px(24), bg=BG, highlightthickness=0)
            c.create_oval(1, 1, px(24) - 1, px(24) - 1, fill=RAISED_HI, outline="")
            c.create_text(px(12), px(12), text=str(i), font=font(9, "bold"), fill=TEXT)
            c.pack(side="left", padx=(0, px(12)), anchor="n")
            tk.Label(r, text=text, font=font(10), bg=BG, fg=TEXT, justify="left", anchor="w",
                     wraplength=px(370)).pack(side="left", fill="x")
        tk.Label(b, text="The phone and this PC must be on the same Wi-Fi.", font=font(9), bg=BG,
                 fg=MUTED, anchor="w").pack(fill="x", pady=(px(12), px(16)))

        canvas = tk.Canvas(b, width=size, height=size, bg=BG, highlightthickness=0)
        canvas.pack()
        rrect(canvas, 0, 0, size, size, px(16), fill="white", outline="")
        for r_, row in enumerate(matrix):
            for c_, filled in enumerate(row):
                if filled:
                    x, y = (c_ + quiet) * cell, (r_ + quiet) * cell
                    canvas.create_rectangle(x, y, x + cell, y + cell, fill="#0F1012", outline="")

        status = tk.StringVar(value="Waiting for the phone to scan…")
        status_lbl = tk.Label(b, textvariable=status, font=font(10), bg=BG, fg=MUTED,
                              wraplength=max(size, px(380)), justify="center")
        status_lbl.pack(pady=(px(16), px(16)))

        cancel = threading.Event()
        qq = queue.Queue()

        def close():
            cancel.set()
            try:
                m.destroy()
            except tk.TclError:
                pass

        bar = tk.Frame(b, bg=BG)
        bar.pack(fill="x")
        RButton(bar, "Pair with code instead", lambda: (close(), self.pair_with_code()), "quiet",
                height=40, pad=12).pack(side="left")
        btn = RButton(bar, "Cancel", close, "ghost", height=40, pad=22)
        btn.pack(side="right")
        m.protocol("WM_DELETE_WINDOW", close)

        def poll():
            try:
                if not m.winfo_exists():
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
                        status_lbl.configure(fg=OK if ok else BAD)
                        self.log_write(text)
                        self.refresh_devices()
                        if ok:
                            m.after(1500, close)
                        else:
                            btn.set_text("Close")
                        return
            except queue.Empty:
                pass
            except tk.TclError:
                return
            m.after(200, poll)

        m.present()
        m.after(200, poll)
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
            # An adb server that was started earlier (outside this app) may use a
            # discovery backend that can't see phones. Restart it with ours.
            chk = run(["mdns", "check"], 15)
            chk_text = (chk.stdout + chk.stderr).strip()
            if "openscreen" not in chk_text.lower():
                qq.put(("status", "Restarting adb for Wi-Fi discovery…"))
                run(["kill-server"], 15)
                run(["start-server"], 30)
                chk = run(["mdns", "check"], 15)
                chk_text = (chk.stdout + chk.stderr).strip()
            qq.put(("log", f"mDNS check: {chk_text or 'no output'}"))
            qq.put(("status", "Waiting for the phone to scan…"))

            pair_addr = None
            seen = set()
            deadline = time.time() + 180
            while time.time() < deadline and not cancel.is_set():
                for sname, stype, addr in services():
                    if (sname, stype) not in seen:
                        seen.add((sname, stype))
                        qq.put(("log", f"mDNS: {sname} {stype} {addr}"))
                    if "pairing" in stype and sname.lower() == name.lower():
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
                    "Timed out: the PC never saw the phone's pairing service. Check that both "
                    "devices are on the same Wi-Fi (not a guest network) and that Windows "
                    "Firewall allows adb.exe on private networks. You can also use "
                    "Pair with code instead.",
                ))
                return

            qq.put(("status", "Phone found. Pairing…"))
            r = run(["pair", pair_addr, password], 30)
            text = (r.stdout + r.stderr).strip()
            qq.put(("log", text))
            if "successfully paired" not in text.lower():
                qq.put(("done", False, f"Pairing failed: {text or 'no response from adb'}"))
                return

            qq.put(("status", "Paired. Connecting…"))
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
                "on the Wireless debugging screen and press Connect.",
            ))
        except Exception as e:  # noqa: BLE001
            qq.put(("done", False, f"Error: {e}"))

    def pair_with_code(self):
        """Manual pairing: works even when mDNS discovery doesn't."""
        try:
            self.adb()
        except FileNotFoundError as e:
            self.alert("adb not found", str(e), "error")
            return

        m = Modal(self, "Pair with code", 440)
        b = m.body
        tk.Label(b, text="Pair with code", font=font(14, "bold"), bg=BG, fg=TEXT, anchor="w").pack(fill="x")
        tk.Label(b, text="On the phone, open Developer options > Wireless debugging > Pair device "
                         "with pairing code, then enter what it shows.",
                 font=font(10), bg=BG, fg=MUTED, justify="left", anchor="w",
                 wraplength=px(380)).pack(fill="x", pady=(px(8), px(8)))

        def labelled(text, placeholder):
            tk.Label(b, text=text, font=font(9), bg=BG, fg=MUTED, anchor="w").pack(
                fill="x", pady=(px(16), px(8)))
            f = Field(b, placeholder=placeholder, mono=True)
            f.pack(fill="x")
            return f

        f_pair = labelled("Pairing address", "192.168.1.5:37123")
        f_code = labelled("Pairing code", "123456")
        f_conn = labelled("Connect address (optional)", "192.168.1.5:41234")
        tk.Label(b, text="The connect address is on the main Wireless debugging screen. Its port "
                         "differs from the pairing port.",
                 font=font(9), bg=BG, fg=MUTED, justify="left", anchor="w",
                 wraplength=px(380)).pack(fill="x", pady=(px(16), px(24)))

        def go():
            pair, code, conn = f_pair.value(), f_code.value(), f_conn.value()
            if not pair or not code:
                self.alert("Missing details", "Enter the pairing address and the pairing code.", "info")
                m.grab_set()
                return
            go_btn.set_text("Pairing…")
            go_btn.set_enabled(False)
            m.update_idletasks()
            try:
                r = self.run_adb(["pair", pair, code], timeout=30)
                text = (r.stdout + r.stderr).strip()
                self.log_write(text)
                if "successfully paired" not in text.lower():
                    self.alert("Pairing failed", text or "No response from adb.", "error")
                    return
                if not conn:
                    host = pair.rsplit(":", 1)[0]
                    for _ in range(4):
                        out = self.run_adb(["mdns", "services"], timeout=10).stdout
                        for line in out.splitlines():
                            parts = line.split()
                            if (
                                len(parts) >= 3
                                and "connect" in parts[1]
                                and parts[-1].startswith(host + ":")
                            ):
                                conn = parts[-1]
                        if conn:
                            break
                        time.sleep(1.0)
                if conn:
                    r = self.run_adb(["connect", conn], timeout=15)
                    out = (r.stdout + r.stderr).strip()
                    self.log_write(out)
                    if "connected" in out.lower():
                        self.refresh_devices()
                        m.destroy()
                        return
                    self.alert("Paired, but not connected", out, "warn")
                else:
                    self.alert("Paired",
                               "Now enter the connect address from the Wireless debugging screen "
                               "and press Connect in the main window", "ok")
            except Exception as e:  # noqa: BLE001
                self.alert("Something went wrong", str(e), "error")
            finally:
                try:
                    go_btn.set_text("Pair and connect")
                    go_btn.set_enabled(True)
                    m.grab_set()
                except tk.TclError:
                    pass
                self.refresh_devices()

        bar = tk.Frame(b, bg=BG)
        bar.pack(fill="x")
        go_btn = RButton(bar, "Pair and connect", go, "primary", height=40, pad=22, weight="bold")
        go_btn.pack(side="right")
        RButton(bar, "Close", m.destroy, "ghost", height=40, pad=22).pack(side="right", padx=(0, px(8)))
        m.present()
        f_pair.focus_field()

    # ----------------------------------------------------------- file list
    def _on_drop(self, files):
        paths = []
        for f in files:
            if isinstance(f, bytes):
                try:
                    f = f.decode("utf-8")
                except UnicodeDecodeError:
                    f = f.decode("mbcs", "replace")
            paths.append(f)
        self.after(0, lambda: self._add_paths(paths))

    def _add_paths(self, paths):
        if self.mode == "install":
            self._add_apks(paths)
            return
        if self.mode != "push":
            self.set_status("Switch to To phone to send files", "dim")
            return
        if self.sending:
            self.set_status("Wait for the current transfer to finish before adding more", "dim")
            return
        fresh = []
        for p in paths:
            p = os.path.abspath(p)
            if os.path.exists(p) and p not in self.items:
                self.items.append(p)
                self.filelist.add(p)
                fresh.append(p)
        if not fresh:
            return
        self._show_stage()
        self.filelist.reset_states()
        self.route.set_mode("ready" if self.selected_serial() else "idle",
                            "Ready to send" if self.selected_serial() else "Not connected")
        self.progress.set(0, ACCENT)
        self.stats_lbl.configure(text="")
        threading.Thread(target=self._measure, args=(fresh,), daemon=True).start()
        self._update_count()

    def _measure(self, paths):
        for p in paths:
            self.q.put(("size", p, path_size(p)))

    def _update_count(self):
        """Selection summary lives in the status line."""
        if self.sending:
            return
        if self.mode == "pull":
            n = len(self.sel)
            if not n:
                self.set_status("Click files or folders on the phone to select them", "dim")
                return
            known = [self.rsizes[p] for p in self.sel if p in self.rsizes]
            text = f"{n} selected"
            if len(known) == n:
                text += f" · {human_size(sum(known))}"
            self.set_status(text, "normal")
            return
        if self.mode == "install":
            lst, noun, tail, empty = self.apks, "APK", "ready to install", "Add APK files to get started"
        else:
            lst, noun, tail, empty = self.items, "item", "ready to send", "Add files to get started"
        if not lst:
            self.set_status(empty, "dim")
            return
        known = [self.sizes[p] for p in lst if p in self.sizes]
        text = f"{len(lst)} {noun}{'s' if len(lst) != 1 else ''}"
        if len(known) == len(lst):
            text += f" · {human_size(sum(known))}"
        self.set_status(f"{text} {tail}", "normal")

    def _show_stage(self):
        if self.items:
            self.dropzone.grid_remove()
            self.filelist.grid(row=0, column=0, sticky="nsew")
            if self.mode == "push":
                self.tools.pack(side="right")
        else:
            self.filelist.grid_remove()
            self.tools.pack_forget()
            self.dropzone.grid(row=0, column=0, sticky="nsew")

    def add_files(self):
        paths = filedialog.askopenfilenames(title="Choose files to send")
        if paths:
            self._add_paths(paths)

    def add_folder(self):
        p = filedialog.askdirectory(title="Choose a folder to send")
        if p:
            self._add_paths([p])

    def remove_path(self, path):
        if self.sending or path not in self.items:
            return
        self.items.remove(path)
        self.sizes.pop(path, None)
        self.filelist.remove(path)
        self._show_stage()
        self._update_count()

    def clear_items(self):
        if self.sending:
            return
        self.items.clear()
        self.sizes.clear()
        self.filelist.clear()
        self._show_stage()
        self._update_count()
        self.progress.set(0, ACCENT)
        self.stats_lbl.configure(text="")
        self.route.set_mode("ready" if self.selected_serial() else "idle",
                            "Ready to send" if self.selected_serial() else "Not connected")

    # ------------------------------------------------------------ transfer
    def start_transfer(self):
        if self.mode == "pull":
            self._start_pull()
            return
        if self.mode == "install":
            self._start_install()
            return
        if self.worker and self.worker.is_alive():
            return
        if not self.items:
            self.alert("Nothing to send", "Add at least one file or folder first.", "info")
            return
        serial = self.selected_serial()
        if not serial:
            self.refresh_devices()
            serial = self.selected_serial()
            if not serial:
                self.alert("No phone selected",
                           "Connect a phone with USB debugging on, or pair one over Wi-Fi, then try again.",
                           "error")
                return
        dest = self.dest_var.get().strip() or DEFAULT_DEST
        if not dest.endswith("/"):
            dest += "/"
        try:
            adb = self.adb()
        except FileNotFoundError as e:
            self.alert("adb not found", str(e), "error")
            return

        self.cancel_flag.clear()
        self.sending = True
        self.filelist.reset_states()
        self.filelist.set_removable(False)
        self.send_btn.set_text("Sending…")
        self.send_btn.set_enabled(False)
        self.cancel_btn.pack(side="right", padx=(0, px(8)))
        self._bytes = self._last_bytes = 0
        self._total = sum(self.sizes.get(p, 0) for p in self.items)
        self._disp, self._speed = 0.0, 0.0
        self._t0 = self._last_t = time.time()
        self._index = (0, len(self.items))
        self.progress.set(0, ACCENT)
        self.stats_lbl.configure(text="")
        self.route.set_mode("sending", f"0 of {len(self.items)}")
        items = list(self.items)
        self.worker = threading.Thread(
            target=self._transfer_worker, args=(adb, serial, dest, items), daemon=True
        )
        self.worker.start()
        self.after(33, self._tick)

    def cancel_transfer(self):
        self.cancel_flag.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _close(self):
        self.cancel_transfer()
        self.destroy()

    def _poll_remote(self, adb, serial, remote, is_dir, size, base, stop, q):
        """Read how much of the current item has reached the phone."""
        quoted = shlex.quote(remote)
        cmd = ["shell", "du", "-sk", quoted] if is_dir else ["shell", "stat", "-c", "%s", quoted]
        while not stop.wait(0.7):
            try:
                r = subprocess.run([adb, "-s", serial] + cmd, capture_output=True, text=True,
                                   timeout=6, env=ADB_ENV, creationflags=CREATE_NO_WINDOW)
                n = int(r.stdout.split()[0]) * (1024 if is_dir else 1)
            except Exception:
                continue
            q.put(("bytes", base + min(n, size)))

    def _transfer_worker(self, adb, serial, dest, items):
        q = self.q
        ok = failed = 0
        try:
            sizes = {p: self.sizes.get(p) if p in self.sizes else path_size(p) for p in items}
            q.put(("total", sum(sizes.values())))
            # Make sure destination exists on the phone.
            r = subprocess.run(
                [adb, "-s", serial, "shell", "mkdir", "-p", shlex.quote(dest)],
                capture_output=True,
                text=True,
                env=ADB_ENV,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode != 0:
                q.put(("log", f"[warn] mkdir: {(r.stdout + r.stderr).strip()}"))

            total = len(items)
            base = 0
            for i, path in enumerate(items, 1):
                if self.cancel_flag.is_set():
                    q.put(("log", "[cancelled]"))
                    break
                name = os.path.basename(path.rstrip("/\\")) or path
                size = sizes[path]
                q.put(("file", path, "sending"))
                q.put(("index", i, total))
                q.put(("status", f"Sending {name}"))
                q.put(("log", f"> {path}  ->  {dest}"))
                self.proc = subprocess.Popen(
                    [adb, "-s", serial, "push", path, dest],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=ADB_ENV,
                    creationflags=CREATE_NO_WINDOW,
                )
                stop = threading.Event()
                threading.Thread(
                    target=self._poll_remote,
                    args=(adb, serial, dest + name, os.path.isdir(path), size, base, stop, q),
                    daemon=True,
                ).start()
                last = ""
                for line in self.proc.stdout:
                    line = line.strip()
                    if line:
                        last = line
                        # Skip the noisy per-file progress lines; keep the summary.
                        if not line.startswith("[") or "pushed" in line:
                            q.put(("log", line))
                self.proc.wait()
                stop.set()
                if self.cancel_flag.is_set():
                    q.put(("file", path, "queued"))
                    q.put(("log", "[cancelled]"))
                    break
                base += size
                if self.proc.returncode == 0:
                    ok += 1
                    q.put(("file", path, "done"))
                else:
                    failed += 1
                    q.put(("file", path, "failed"))
                    q.put(("log", f"[failed] {name}: {last}"))
                q.put(("bytes", base))

            q.put(("done", ok, failed, self.cancel_flag.is_set(), dest))
        except Exception as e:
            q.put(("log", f"[error] {e}"))
            q.put(("done", ok, failed + 1, False, dest))

    def _tick(self):
        """Animation + live stats while a transfer is running."""
        if not self.sending:
            return
        now = time.time()
        self.route.advance()
        self._active_list().pulse()
        target = self._bytes / self._total if self._total else 0
        self._disp += (target - self._disp) * 0.2
        if self.mode == "install":
            self.progress.phase = ((self.progress.phase or 0.0) + 0.012) % 1.0
        self.progress.set(self._disp, ACCENT)
        if now - self._last_t >= 0.7:
            inst = (self._bytes - self._last_bytes) / (now - self._last_t)
            self._speed = inst if self._speed == 0 else self._speed * 0.6 + inst * 0.4
            self._last_t, self._last_bytes = now, self._bytes
            i, n = self._index
            speed = f" · {human_size(self._speed)}/s" if self._speed > 0 and self.mode != "install" else ""
            self.route.set_center(f"{i} of {n}{speed}")
            self.stats_lbl.configure(text=f"{human_size(self._bytes)} of {human_size(self._total)}")
        self.after(33, self._tick)

    # ------------------------------------------------------ Install apps
    def _show_apk_stage(self):
        if self.apks:
            self.apk_drop.grid_remove()
            self.apklist.grid(row=0, column=0, sticky="nsew")
            if self.mode == "install":
                self.apk_tools.pack(side="right")
        else:
            self.apklist.grid_remove()
            self.apk_tools.pack_forget()
            self.apk_drop.grid(row=0, column=0, sticky="nsew")

    def add_apks(self):
        paths = filedialog.askopenfilenames(
            title="Choose apps to install",
            filetypes=[("Android apps", "*.apk *.apks *.xapk *.apkm"), ("All files", "*.*")])
        if paths:
            self._add_apks(paths)

    def add_apk_folder(self):
        p = filedialog.askdirectory(title="Choose a folder with APK files")
        if p:
            self._add_apks([p])

    def _add_apks(self, paths):
        if self.sending:
            self.set_status("Wait for the current install to finish before adding more", "dim")
            return
        found, skipped = [], 0
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isdir(p):
                for root, _dirs, files in os.walk(p):
                    found += [os.path.join(root, f) for f in sorted(files) if f.lower().endswith(APK_EXTS)]
            elif p.lower().endswith(APK_EXTS):
                found.append(p)
            else:
                skipped += 1
        fresh = []
        for p in found:
            if p not in self.apks:
                self.apks.append(p)
                self.apklist.add(p)
                try:
                    self.sizes[p] = os.path.getsize(p)
                except OSError:
                    self.sizes[p] = 0
                self.apklist.set_size(p, human_size(self.sizes[p]))
                fresh.append(p)
        if not fresh:
            if skipped:
                self.set_status("Only .apk, .apks, .xapk and .apkm files can be installed", "dim")
            return
        self._show_apk_stage()
        self.apklist.reset_states()
        self._reset_route()
        self.progress.set(0, ACCENT)
        self.stats_lbl.configure(text="")
        self._update_count()

    def remove_apk(self, path):
        if self.sending or path not in self.apks:
            return
        self.apks.remove(path)
        self.apklist.remove(path)
        self._show_apk_stage()
        self._update_count()

    def clear_apks(self):
        if self.sending:
            return
        self.apks.clear()
        self.apklist.clear()
        self._show_apk_stage()
        self.progress.set(0, ACCENT)
        self.stats_lbl.configure(text="")
        self._reset_route()
        self._update_count()

    def _start_install(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.apks:
            self.alert("Nothing to install", "Add at least one APK file first.", "info")
            return
        serial = self.selected_serial()
        if not serial:
            self.refresh_devices()
            serial = self.selected_serial()
            if not serial:
                self.alert("No phone selected",
                           "Connect a phone with USB debugging on, or pair one over Wi-Fi, then try again.",
                           "error")
                return
        try:
            adb = self.adb()
        except FileNotFoundError as e:
            self.alert("adb not found", str(e), "error")
            return
        items = list(self.apks)
        opts = {k: v.get() for k, v in self.opt_vars.items()}
        self.cancel_flag.clear()
        self.sending = True
        self.apklist.reset_states()
        self.apklist.set_removable(False)
        self.send_btn.set_text("Installing…")
        self.send_btn.set_enabled(False)
        self.cancel_btn.pack(side="right", padx=(0, px(8)))
        self._bytes = self._last_bytes = 0
        self._total = sum(self.sizes.get(p, 0) for p in items)
        self._disp, self._speed = 0.0, 0.0
        self._t0 = self._last_t = time.time()
        self._index = (0, len(items))
        self.progress.set(0, ACCENT)
        self.progress.phase = 0.0
        self.stats_lbl.configure(text="")
        self.route.set_mode("sending", f"0 of {len(items)}")
        self.worker = threading.Thread(target=self._install_worker, args=(adb, serial, items, opts), daemon=True)
        self.worker.start()
        self.after(33, self._tick)

    @staticmethod
    def _unpack_bundle(path, tmp):
        """Extract an .apks/.xapk/.apkm; returns (apk files, [(local obb, phone path)])."""
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            z.extractall(tmp)
            apks = sorted(os.path.join(tmp, n) for n in names if n.lower().endswith(".apk"))
            obbs = []
            if "manifest.json" in names:
                try:
                    with z.open("manifest.json") as mf:
                        manifest = json.load(mf)
                    for e in manifest.get("expansions", []):
                        if e.get("file") and e.get("install_path"):
                            obbs.append((os.path.join(tmp, e["file"]), "/sdcard/" + e["install_path"].lstrip("/")))
                except Exception:
                    pass
        return apks, obbs

    def _install_worker(self, adb, serial, items, opts):
        q = self.q
        ok = failed = 0
        flags = [f for key, f in (("replace", "-r"), ("grant", "-g"), ("downgrade", "-d"), ("test", "-t"))
                 if opts.get(key)]
        plain = [p for p in items if not p.lower().endswith(ZIP_APPS)]
        combine = bool(opts.get("combine")) and len(plain) > 1
        jobs, placed = [], False
        for p in items:
            if p.lower().endswith(ZIP_APPS):
                jobs.append(("bundle", [p]))
            elif combine:
                if not placed:
                    jobs.append(("multi", plain))
                    placed = True
            else:
                jobs.append(("single", [p]))
        try:
            q.put(("total", sum(self.sizes.get(p, 0) for p in items)))
            base = 0
            for i, (kind, paths) in enumerate(jobs, 1):
                if self.cancel_flag.is_set():
                    q.put(("log", "[cancelled]"))
                    break
                name = os.path.basename(paths[0]) if kind != "multi" else f"{len(paths)} APKs"
                for p in paths:
                    q.put(("file", p, "sending"))
                q.put(("index", i, len(jobs)))
                q.put(("status", f"Installing {name}"))
                q.put(("log", f"> install {name}"))
                tmp, obbs, success, out = None, [], False, ""
                try:
                    if kind == "single":
                        cmd = [adb, "-s", serial, "install"] + flags + [paths[0]]
                    elif kind == "multi":
                        cmd = [adb, "-s", serial, "install-multiple"] + flags + paths
                    else:
                        tmp = tempfile.mkdtemp(prefix="adbx_")
                        apks, obbs = self._unpack_bundle(paths[0], tmp)
                        if not apks:
                            raise RuntimeError("No APK files found inside this bundle")
                        cmd = [adb, "-s", serial, "install-multiple"] + flags + apks
                    self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                 text=True, env=ADB_ENV, creationflags=CREATE_NO_WINDOW)
                    lines = [ln.strip() for ln in self.proc.stdout if ln.strip()]
                    self.proc.wait()
                    out = "\n".join(lines)
                    for ln in lines:
                        q.put(("log", ln))
                    success = self.proc.returncode == 0 and "success" in out.lower()
                    if success and obbs and not self.cancel_flag.is_set():
                        for local, remote in obbs:
                            q.put(("status", f"Copying game data for {name}"))
                            subprocess.run([adb, "-s", serial, "shell", "mkdir", "-p",
                                            shlex.quote(posixpath.dirname(remote))],
                                           capture_output=True, env=ADB_ENV, creationflags=CREATE_NO_WINDOW)
                            r = subprocess.run([adb, "-s", serial, "push", local, remote], capture_output=True,
                                               text=True, env=ADB_ENV, creationflags=CREATE_NO_WINDOW)
                            q.put(("log", (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else ""))
                            if r.returncode != 0:
                                q.put(("log", f"[warn] could not copy {os.path.basename(local)}"))
                except Exception as e:  # noqa: BLE001
                    out = str(e)
                finally:
                    if tmp:
                        shutil.rmtree(tmp, ignore_errors=True)
                if self.cancel_flag.is_set():
                    for p in paths:
                        q.put(("file", p, "queued"))
                    q.put(("log", "[cancelled]"))
                    break
                base += sum(self.sizes.get(p, 0) for p in paths)
                if success:
                    ok += 1
                    for p in paths:
                        q.put(("file", p, "done"))
                else:
                    failed += 1
                    reason = explain_install_error(out)
                    for p in paths:
                        q.put(("note", p, reason))
                        q.put(("file", p, "failed"))
                    q.put(("log", f"[failed] {name}: {reason}"))
                q.put(("bytes", base))
            q.put(("done", ok, failed, self.cancel_flag.is_set(), ""))
        except Exception as e:  # noqa: BLE001
            q.put(("log", f"[error] {e}"))
            q.put(("done", ok, failed + 1, False, ""))

    # --------------------------------------------------- From phone (pull)
    def _active_list(self):
        return {"pull": self.phonelist, "install": self.apklist}.get(self.mode, self.filelist)

    def _action_text(self):
        return {"pull": "Save to PC", "install": "Install on phone"}.get(self.mode, "Send to phone")

    def _reset_route(self):
        has = bool(self.selected_serial())
        self.route.set_mode("ready" if has else "idle",
                            {"pull": "Ready to save", "install": "Ready to install"}.get(self.mode, "Ready to send") if has else "Not connected")

    def set_direction(self, mode):
        if self.sending or mode == self.mode:
            self.seg.set(self.mode)
            return
        self.mode = mode
        self.seg.set(mode)
        self.progress.set(0, ACCENT)
        self.stats_lbl.configure(text="")
        self._reset_route()
        self.route.reverse = mode == "pull"
        self.send_btn.set_text(self._action_text())
        stages = {"push": self.push_stage, "pull": self.pull_stage, "install": self.apk_stage}
        dests = {"push": self.push_dest, "pull": self.pull_dest, "install": self.apk_opts}
        titles = {"push": "Send to", "pull": "Save to", "install": "Install options"}
        for key in stages:
            if key != mode:
                stages[key].grid_remove()
                dests[key].pack_forget()
        self.tools.pack_forget()
        self.apk_tools.pack_forget()
        stages[mode].grid(row=1, column=0, sticky="nsew")
        dests[mode].pack(fill="x")
        self.side_title.configure(text=titles[mode])
        if mode == "pull":
            self.browse(self.remote_path)
        elif mode == "push":
            self._show_stage()
        else:
            self._show_apk_stage()
        self._update_count()

    def browse_save(self):
        p = filedialog.askdirectory(title="Choose where to save", initialdir=self.save_var.get() or None)
        if p:
            self.save_var.set(os.path.normpath(p))

    def _browse_message(self, text):
        if text:
            self.browse_msg.configure(text=text)
            self.browse_msg.place(in_=self.phonelist, relx=0.5, rely=0.4, anchor="center")
            self.browse_msg.lift()
        else:
            self.browse_msg.place_forget()

    def browse(self, path):
        if self.sending:
            return
        self.sel = []
        self._update_count()
        serial = self.selected_serial()
        if not path.endswith("/"):
            path += "/"
        self.remote_path = path
        self.path_lbl.configure(text=ellipsize(path, font(10, mono=True), px(340), keep="end"))
        self.up_btn.set_enabled(path != "/")
        self._browse_gen += 1
        gen = self._browse_gen
        self.phonelist.clear()
        if not serial:
            self._browse_message("Connect a phone to browse its files")
            return
        self._browse_message("Loading…")
        threading.Thread(target=self._ls_worker, args=(serial, path, gen), daemon=True).start()

    def browse_up(self):
        if self.sending:
            return
        parent = posixpath.dirname(self.remote_path.rstrip("/")) or "/"
        self.browse(parent)

    def _open_remote(self, path):
        if not self.sending:
            self.browse(path + "/")

    def _ls_worker(self, serial, path, gen):
        try:
            r = subprocess.run([self.adb(), "-s", serial, "shell", "ls", "-lA", shlex.quote(path)],
                               capture_output=True, text=True, timeout=25, env=ADB_ENV,
                               creationflags=CREATE_NO_WINDOW)
            entries, errors = parse_ls(r.stdout + "\n" + (r.stderr or ""))
            self.q.put(("ls", gen, path, entries, " ".join(errors[:2])))
        except Exception as e:  # noqa: BLE001
            self.q.put(("ls", gen, path, [], str(e)))

    def _toggle_remote(self, path):
        row = self.phonelist.rows.get(path)
        if not row or self.sending:
            return
        row.selected = not row.selected
        row.redraw()
        if row.selected:
            if path not in self.sel:
                self.sel.append(path)
            if row.is_dir and path not in self.rsizes:
                threading.Thread(target=self._du_bg, args=(path,), daemon=True).start()
        elif path in self.sel:
            self.sel.remove(path)
        self._update_count()
        n = len(self.sel)
        self.set_status(f"{n} selected" if n else "Click files or folders on the phone to select them",
                        "normal" if n else "dim")

    def _remote_du(self, adb, serial, path):
        try:
            r = subprocess.run([adb, "-s", serial, "shell", "du", "-sk", shlex.quote(path)],
                               capture_output=True, text=True, timeout=120, env=ADB_ENV,
                               creationflags=CREATE_NO_WINDOW)
            return int(r.stdout.split()[0]) * 1024
        except Exception:
            return 0

    def _du_bg(self, path):
        serial = self.selected_serial()
        if serial:
            self.q.put(("rsize", path, self._remote_du(self.adb(), serial, path)))

    def _start_pull(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.sel:
            self.alert("Nothing selected",
                       "Click the files or folders you want to save. Double-click a folder to open it.", "info")
            return
        serial = self.selected_serial()
        if not serial:
            self.refresh_devices()
            serial = self.selected_serial()
            if not serial:
                self.alert("No phone selected",
                           "Connect a phone with USB debugging on, or pair one over Wi-Fi, then try again.",
                           "error")
                return
        dest = self.save_var.get().strip()
        if not dest:
            self.alert("Choose a folder", "Pick the folder on this PC where the files should be saved.", "info")
            return
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            self.alert("Can't use that folder", str(e), "error")
            return
        try:
            adb = self.adb()
        except FileNotFoundError as e:
            self.alert("adb not found", str(e), "error")
            return

        items = list(self.sel)
        self.cancel_flag.clear()
        self.sending = True
        self.phonelist.reset_states()
        self.phonelist.set_removable(False)
        self.send_btn.set_text("Saving…")
        self.send_btn.set_enabled(False)
        self.cancel_btn.pack(side="right", padx=(0, px(8)))
        self._bytes = self._last_bytes = 0
        self._total = sum(self.rsizes.get(p, 0) for p in items)
        self._disp, self._speed = 0.0, 0.0
        self._t0 = self._last_t = time.time()
        self._index = (0, len(items))
        self.progress.set(0, ACCENT)
        self.stats_lbl.configure(text="")
        self.route.set_mode("sending", f"0 of {len(items)}")
        self.worker = threading.Thread(target=self._pull_worker, args=(adb, serial, dest, items), daemon=True)
        self.worker.start()
        self.after(33, self._tick)

    def _poll_local(self, local, size, base, stop, q):
        """Watch the file or folder growing on the PC."""
        while not stop.wait(0.5):
            try:
                n = path_size(local) if os.path.exists(local) else 0
            except Exception:
                continue
            q.put(("bytes", base + min(n, size)))

    def _pull_worker(self, adb, serial, dest, items):
        q = self.q
        ok = failed = 0
        try:
            sizes = {p: (self.rsizes[p] if p in self.rsizes else self._remote_du(adb, serial, p)) for p in items}
            q.put(("total", sum(sizes.values())))
            base = 0
            total = len(items)
            for i, path in enumerate(items, 1):
                if self.cancel_flag.is_set():
                    q.put(("log", "[cancelled]"))
                    break
                name = posixpath.basename(path.rstrip("/"))
                size = sizes[path]
                q.put(("file", path, "sending"))
                q.put(("index", i, total))
                q.put(("status", f"Saving {name}"))
                q.put(("log", f"> {path}  ->  {dest}"))
                self.proc = subprocess.Popen(
                    [adb, "-s", serial, "pull", path, dest],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=ADB_ENV,
                    creationflags=CREATE_NO_WINDOW)
                stop = threading.Event()
                threading.Thread(target=self._poll_local,
                                 args=(os.path.join(dest, name), size, base, stop, q), daemon=True).start()
                last = ""
                for line in self.proc.stdout:
                    line = line.strip()
                    if line:
                        last = line
                        if not line.startswith("[") or "pulled" in line:
                            q.put(("log", line))
                self.proc.wait()
                stop.set()
                if self.cancel_flag.is_set():
                    q.put(("file", path, "queued"))
                    q.put(("log", "[cancelled]"))
                    break
                base += size
                if self.proc.returncode == 0:
                    ok += 1
                    q.put(("file", path, "done"))
                else:
                    failed += 1
                    q.put(("file", path, "failed"))
                    q.put(("log", f"[failed] {name}: {last}"))
                q.put(("bytes", base))
            q.put(("done", ok, failed, self.cancel_flag.is_set(), dest))
        except Exception as e:  # noqa: BLE001
            q.put(("log", f"[error] {e}"))
            q.put(("done", ok, failed + 1, False, dest))

    def _finish(self, ok, failed, cancelled, dest):
        word = {"pull": "Saved", "install": "Installed"}.get(self.mode, "Sent")
        self.progress.phase = None
        self.sending = False
        elapsed = max(time.time() - self._t0, 0.1)
        self._active_list().set_removable(True)
        self.send_btn.set_text(self._action_text())
        self.send_btn.set_enabled(True)
        self.cancel_btn.pack_forget()
        avg = f" · {human_size(self._total / elapsed)}/s" if self._total and ok and self.mode != "install" else ""
        summary = f"{human_size(self._total)} in {human_time(elapsed)}{avg}"
        if cancelled:
            self.route.set_mode("ready", "Cancelled")
            self.progress.set(self._disp, FAINT)
            self.set_status(f"Cancelled. {ok} {word.lower()}, {failed} failed", "dim")
        elif failed:
            self.route.set_mode("error", f"{failed} failed")
            self.progress.set(1.0, BAD)
            self.set_status(f"Finished with errors: {ok} {word.lower()}, {failed} failed", "bad")
            self.stats_lbl.configure(text=summary)
        else:
            self.route.set_mode("done", word)
            self.progress.set(1.0, OK)
            noun = ("app" if self.mode == "install" else "item") + ("" if ok == 1 else "s")
            where = f" to {dest}" if dest else ""
            self.set_status(f"{word} {ok} {noun}{where}", "ok")
            self.stats_lbl.configure(text=summary)
            self.log_write(f"{word} {ok} {noun}{where}")

    def _drain_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.log_write(msg[1])
                elif kind == "status":
                    self.set_status(msg[1])
                elif kind == "size":
                    _k, path, n = msg
                    if path in self.filelist.rows:
                        self.sizes[path] = n
                        self.filelist.set_size(path, human_size(n))
                        self._update_count()
                elif kind == "file":
                    self._active_list().set_state(msg[1], msg[2])
                elif kind == "note":
                    self._active_list().set_note(msg[1], msg[2])
                elif kind == "rsize":
                    _k, path, n = msg
                    self.rsizes[path] = n
                    self.phonelist.set_size(path, human_size(n))
                    self._update_count()
                elif kind == "ls":
                    _k, gen, path, entries, err = msg
                    if gen == self._browse_gen and self.mode == "pull":
                        self.phonelist.clear()
                        self.sel, self.rsizes, self.rdirs = [], {}, set()
                        for e in entries:
                            p = path + e["name"]
                            if e["dir"]:
                                self.rdirs.add(p)
                            else:
                                self.rsizes[p] = e["size"]
                            self.phonelist.add(p, remote=True, is_dir=e["dir"], meta=e["meta"],
                                               size_text="" if e["dir"] else human_size(e["size"]),
                                               on_toggle=self._toggle_remote, on_open=self._open_remote)
                        if entries:
                            self._browse_message("")
                        elif err:
                            self._browse_message("Can't open this folder.\n" + ellipsize(err, font(9), px(380)))
                        else:
                            self._browse_message("This folder is empty")
                        self._update_count()
                elif kind == "index":
                    self._index = (msg[1], msg[2])
                elif kind == "total":
                    self._total = msg[1]
                elif kind == "bytes":
                    self._bytes = max(self._bytes, msg[1])
                elif kind == "done":
                    _k, ok, failed, cancelled, dest = msg
                    self._finish(ok, failed, cancelled, dest)
        except queue.Empty:
            pass
        self.after(50, self._drain_queue)


def main():
    enable_dpi_awareness()
    App().mainloop()


if __name__ == "__main__":
    main()
