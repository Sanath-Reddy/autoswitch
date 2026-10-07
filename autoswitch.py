#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
autoswitch.py - keep a phone-hotspot download fast.

It watches this computer's download speed. When the speed stays low, it tells
your Android phone (over a USB cable, using Google's "adb" tool) to drop from
5G to 4G for a few seconds and then go back to 5G - the same toggle you would
do by hand in Settings.

    python autoswitch.py --test     one 5G -> 4G -> 5G switch, to check the phone obeys
    python autoswitch.py            watch the speed and switch automatically
    python autoswitch.py --help     all options

Needs: Android 12 or newer with USB debugging turned on, the phone plugged
into this computer, and Python 3.9 or newer. No root, nothing to install on
the phone. Run it while a download is going; stop it with Ctrl+C.
"""

import argparse
import collections
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile

__version__ = "1.0.0"

# ----------------------------------------------------------------- settings
# Sensible defaults. The ones marked (option) can also be set when you start
# the script, e.g.  python autoswitch.py --below 5 --wait 6

SLOW_FRACTION = 0.5    # "slow" means under this share of the best speed seen lately...
ALWAYS_SLOW_MB = 1.0   # ...and anything under this many MB/s is always slow
SLOW_SECONDS = 15      # (option --for)    speed must stay slow this long before a switch
WAIT_ON_4G = 8         # (option --wait)   seconds to sit on 4G before going back to 5G
SETTLE_SECONDS = 30    # (option --settle) after a switch, leave the network alone this long
MAX_SETTLE = 300       # if switching is not helping, wait up to this long between tries
STALL_SECONDS = 45     # (option --stall)  see "stall" in the Decider class below
BEST_MEMORY = 600      # how far back "best speed seen lately" looks, in seconds
IDLE_KB = 30           # under this many KB/s, nothing is really downloading
MIN_DOWNLOAD_KB = 200  # under this it is not treated as a download at all: it cannot be
#                        told apart from an online game or a call, so nothing is switched

MB = 1000 * 1000
KB = 1000

# Tiny pages that operating systems themselves use to ask "is the internet
# there?". Only fetched when a download has stopped dead, to tell "finished"
# from "connection died".
PROBE_URLS = ("http://connectivitycheck.gstatic.com/generate_204",
              "http://www.msftconnecttest.com/connecttest.txt")

# ------------------------------------------------- Android network-type bits
# From Android's TelephonyManager: bit number = NETWORK_TYPE_x - 1.
TYPE_BITS = {
    "GPRS": 0, "EDGE": 1, "UMTS": 2, "CDMA": 3, "CDMA - EvDo rev. 0": 4,
    "CDMA - EvDo rev. A": 5, "CDMA - 1xRTT": 6, "HSDPA": 7, "HSUPA": 8,
    "HSPA": 9, "iDEN": 10, "CDMA - EvDo rev. B": 11, "LTE": 12,
    "CDMA - eHRPD": 13, "HSPA+": 14, "GSM": 15, "TD_SCDMA": 16, "IWLAN": 17,
    "LTE_CA": 18, "NR": 19,
}
NR_BIT = 1 << TYPE_BITS["NR"]
LTE_BITS = (1 << TYPE_BITS["LTE"]) | (1 << TYPE_BITS["LTE_CA"])
# Used only when the phone's current setting cannot be read. These are the two
# examples Android's own help text gives for this command:
# NR|LTE|CDMA|EVDO|GSM|WCDMA and LTE|CDMA|EVDO|GSM|WCDMA.
FALLBACK_5G = int("11001111101111111111", 2)
FALLBACK_4G = int("01001111101111111111", 2)


class PhoneError(Exception):
    """The phone could not be reached, or it refused a command."""


# ------------------------------------------------------------------- screen
class Screen:
    """One status line that updates in place, plus normal lines for events."""

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.width = 0
        self.last_plain = 0.0

    def status(self, text):
        if self.tty:
            text = text[:78]
            sys.stdout.write("\r" + text.ljust(self.width))
            sys.stdout.flush()
            self.width = len(text)
        elif time.time() - self.last_plain >= 30:
            self.last_plain = time.time()
            print(text, flush=True)

    def line(self, text=""):
        if self.width:
            sys.stdout.write("\r" + " " * self.width + "\r")
            self.width = 0
        print(text, flush=True)

    def event(self, text):
        self.line("[%s] %s" % (time.strftime("%H:%M:%S"), text))


def fmt(speed):
    """Bytes per second as text."""
    if speed >= MB:
        return "%.1f MB/s" % (speed / MB)
    return "%d KB/s" % round(speed / KB)


def fatal(message):
    print()
    print(message)
    if os.name == "nt":
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(1)


# ---------------------------------------------------- download-speed meter
def _is_loopback(name):
    low = name.lower()
    return low == "lo" or low.startswith("lo0") or "loopback" in low


def _psutil_reader():
    import psutil

    def read():
        return dict((name, c.bytes_recv)
                    for name, c in psutil.net_io_counters(pernic=True).items()
                    if not _is_loopback(name))
    return read


def _linux_reader():
    def read():
        out = {}
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, _, rest = line.partition(":")
                fields = rest.split()
                if fields and not _is_loopback(name.strip()):
                    out[name.strip()] = int(fields[0])
        return out
    return read


def _mac_reader():
    def read():
        text = subprocess.run(["netstat", "-ibn"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=10).stdout.decode("utf-8", "replace")
        return parse_mac_netstat(text)
    return read


def parse_mac_netstat(text):
    out = {}
    for line in text.splitlines()[1:]:
        p = line.split()
        # the "<Link#n>" row carries the adapter totals; counting from the
        # right: ... Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
        if len(p) >= 10 and p[2].startswith("<Link#") and not _is_loopback(p[0]):
            try:
                out[p[0]] = int(p[-5])
            except ValueError:
                pass
    return out


def _windows_reader64():
    """Windows GetIfTable2: 64-bit byte counters for every adapter."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD),
                    ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    class ROW(ctypes.Structure):      # MIB_IF_ROW2 from netioapi.h
        _fields_ = [
            ("InterfaceLuid", ctypes.c_uint64),
            ("InterfaceIndex", wintypes.ULONG),
            ("InterfaceGuid", GUID),
            ("Alias", wintypes.WCHAR * 257),
            ("Description", wintypes.WCHAR * 257),
            ("PhysicalAddressLength", wintypes.ULONG),
            ("PhysicalAddress", ctypes.c_ubyte * 32),
            ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
            ("Mtu", wintypes.ULONG),
            ("Type", wintypes.ULONG),
            ("TunnelType", wintypes.ULONG),
            ("MediaType", wintypes.ULONG),
            ("PhysicalMediumType", wintypes.ULONG),
            ("AccessType", wintypes.ULONG),
            ("DirectionType", wintypes.ULONG),
            ("Flags", ctypes.c_ubyte),
            ("OperStatus", wintypes.ULONG),
            ("AdminStatus", wintypes.ULONG),
            ("MediaConnectState", wintypes.ULONG),
            ("NetworkGuid", GUID),
            ("ConnectionType", wintypes.ULONG),
            ("TransmitLinkSpeed", ctypes.c_uint64),
            ("ReceiveLinkSpeed", ctypes.c_uint64),
            ("InOctets", ctypes.c_uint64),
            ("OtherCounters", ctypes.c_uint64 * 17),
        ]

    if ctypes.sizeof(ROW) != 1352:
        raise OSError("unexpected MIB_IF_ROW2 size")

    class TABLE(ctypes.Structure):    # MIB_IF_TABLE2
        _fields_ = [("NumEntries", wintypes.ULONG), ("Table", ROW * 1)]

    lib = ctypes.WinDLL("iphlpapi")
    lib.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.POINTER(TABLE))]
    lib.GetIfTable2.restype = wintypes.ULONG
    lib.FreeMibTable.argtypes = [ctypes.c_void_p]
    lib.FreeMibTable.restype = None

    def read():
        table = ctypes.POINTER(TABLE)()
        rc = lib.GetIfTable2(ctypes.byref(table))
        if rc:
            raise OSError("GetIfTable2 failed with code %d" % rc)
        try:
            rows = ctypes.cast(ctypes.addressof(table.contents) + TABLE.Table.offset,
                               ctypes.POINTER(ROW))
            out = {}
            for i in range(table.contents.NumEntries):
                row = rows[i]
                if row.Type != 24:    # 24 = software loopback
                    out["%d:%s" % (row.InterfaceIndex, row.Alias)] = int(row.InOctets)
            return out
        finally:
            lib.FreeMibTable(table)
    return read


def _windows_reader32():
    """Windows GetIfTable: the older call, 32-bit counters that wrap at 4 GB."""
    import ctypes
    from ctypes import wintypes

    class ROW(ctypes.Structure):      # MIB_IFROW from ifmib.h
        _fields_ = [("wszName", wintypes.WCHAR * 256)]
        _fields_ += [(n, wintypes.DWORD) for n in
                     ("dwIndex", "dwType", "dwMtu", "dwSpeed", "dwPhysAddrLen")]
        _fields_ += [("bPhysAddr", ctypes.c_ubyte * 8)]
        _fields_ += [(n, wintypes.DWORD) for n in
                     ("dwAdminStatus", "dwOperStatus", "dwLastChange", "dwInOctets",
                      "dwInUcastPkts", "dwInNUcastPkts", "dwInDiscards", "dwInErrors",
                      "dwInUnknownProtos", "dwOutOctets", "dwOutUcastPkts",
                      "dwOutNUcastPkts", "dwOutDiscards", "dwOutErrors", "dwOutQLen",
                      "dwDescrLen")]
        _fields_ += [("bDescr", ctypes.c_ubyte * 256)]

    if ctypes.sizeof(ROW) != 860:
        raise OSError("unexpected MIB_IFROW size")

    lib = ctypes.WinDLL("iphlpapi")
    lib.GetIfTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.BOOL]
    lib.GetIfTable.restype = wintypes.DWORD

    def read():
        size = wintypes.ULONG(0)
        lib.GetIfTable(None, ctypes.byref(size), False)
        for _ in range(4):
            buf = ctypes.create_string_buffer(max(size.value, 4))
            rc = lib.GetIfTable(buf, ctypes.byref(size), False)
            if rc == 0:
                break
            if rc != 122:             # 122 = buffer too small, try again
                raise OSError("GetIfTable failed with code %d" % rc)
        else:
            raise OSError("GetIfTable kept asking for a bigger buffer")
        count = wintypes.DWORD.from_buffer(buf).value
        rows = (ROW * count).from_buffer(buf, ctypes.sizeof(wintypes.DWORD))
        out = {}
        for row in rows:
            if row.dwType != 24:
                descr = bytes(row.bDescr[:row.dwDescrLen]).split(b"\0")[0]
                out["%d:%s" % (row.dwIndex, descr.decode("mbcs", "replace"))] = int(row.dwInOctets)
        return out
    return read


def make_reader():
    """Find a working way to read 'bytes received' per network adapter.
    Returns (read_function, counter_wrap, name)."""
    tries = [("psutil", _psutil_reader, None)]
    if sys.platform.startswith("linux"):
        tries.append(("/proc/net/dev", _linux_reader, None))
    elif sys.platform == "win32":
        tries.append(("GetIfTable2", _windows_reader64, None))
        tries.append(("GetIfTable", _windows_reader32, 2 ** 32))
    elif sys.platform == "darwin":
        tries.append(("netstat", _mac_reader, None))
    for name, factory, wrap in tries:
        try:
            read = factory()
            if read():                # must return at least one adapter
                return read, wrap, name
        except Exception:
            continue
    raise RuntimeError(
        "Could not read this computer's network counters.\n"
        "Fix: run   %s -m pip install psutil   and start again."
        % os.path.basename(sys.executable or "python"))


class Meter:
    """Download speed of the busiest network adapter, in bytes per second."""

    MAX_SANE = 1.5e9      # faster than 12 Gbit/s means a counter glitch, not a download

    def __init__(self, only=None):
        self.read, self.wrap, self.backend = make_reader()
        self.only = only.lower() if only else None
        self.prev = None
        self.prev_time = None

    def all_adapters(self):
        return sorted(self.read())

    def _counters(self):
        counters = self.read()
        if self.only:
            counters = dict((k, v) for k, v in counters.items() if self.only in k.lower())
        return counters

    def sample(self):
        """Speed since the previous call (None the first time)."""
        now = time.monotonic()
        current = self._counters()
        speed = None
        if self.prev is not None and now > self.prev_time:
            elapsed = now - self.prev_time
            speed = 0.0
            for name, value in current.items():
                old = self.prev.get(name)
                if old is None:
                    continue
                delta = value - old
                if delta < 0:         # counter wrapped (32-bit) or was reset
                    delta = delta + self.wrap if self.wrap else 0
                rate = delta / elapsed
                if speed < rate <= self.MAX_SANE:
                    speed = rate
        self.prev, self.prev_time = current, now
        return speed


def internet_works(wait=8):
    """Can this computer reach the internet at all right now?"""
    result = []

    def probe():
        import urllib.request
        for url in PROBE_URLS:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    response.read(64)
                result.append(True)
                return
            except Exception:
                continue
        result.append(False)

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(wait)
    return bool(result and result[0])


# ------------------------------------------------------------ adb plumbing
def run_cmd(argv, timeout):
    """Run a command and return (exit_code, output_text). Never waits longer
    than `timeout`, even if a background process (adb's own server) keeps our
    output pipe open after the command itself has finished."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        raise PhoneError("could not run %s: %s" % (argv[0], e)) from None
    chunks = []

    def pump():
        try:
            while True:
                data = proc.stdout.read1(65536)
                if not data:
                    break
                chunks.append(data)
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise PhoneError("adb did not answer within %d seconds" % timeout) from None
    reader.join(1.0)
    return code, b"".join(list(chunks)).decode("utf-8", "replace").replace("\r", "")


def find_adb(explicit=None):
    exe = "adb.exe" if os.name == "nt" else "adb"
    here = os.path.dirname(os.path.abspath(__file__))
    home = os.path.expanduser("~")
    candidates = [explicit] if explicit else [
        os.path.join(here, exe),
        os.path.join(here, "platform-tools", exe),
        shutil.which("adb"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", exe),
        os.path.join(home, "Library", "Android", "sdk", "platform-tools", exe),
        os.path.join(home, "Android", "Sdk", "platform-tools", exe),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    if explicit:
        fatal("There is no adb at %s" % explicit)
    return None


def download_adb():
    """Fetch Google's official platform-tools (which contain adb) next to this script."""
    import urllib.request
    system = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    url = "https://dl.google.com/android/repository/platform-tools-latest-%s.zip" % system
    here = os.path.dirname(os.path.abspath(__file__))
    print("Downloading %s ..." % url)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(here)
    except Exception as e:
        fatal("The download did not work (%s).\nGet the zip yourself from\n  %s\n"
              "and unzip it next to this script, so there is a 'platform-tools' folder here."
              % (e, url))
    folder = os.path.join(here, "platform-tools")
    if os.name != "nt":
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and "." not in name:
                os.chmod(path, 0o755)
    print("Saved to %s" % folder)
    return find_adb()


def list_devices(adb):
    code, out = run_cmd([adb, "devices"], 30)
    devices = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and not line.startswith(("List of", "*", "adb")):
            devices.append((parts[0], parts[1].strip()))
    return devices


def wait_for_phone(adb, wanted_serial, screen):
    """Wait until a phone is plugged in and has allowed USB debugging."""
    try:
        run_cmd([adb, "start-server"], 60)
    except PhoneError:
        pass
    hinted = None
    deadline = time.monotonic() + 600
    while True:
        devices = list_devices(adb)
        if wanted_serial:
            devices = [d for d in devices if d[0] == wanted_serial]
        ready = [d for d in devices if d[1] == "device"]
        if ready:
            if len(ready) > 1:
                screen.line("More than one phone is connected; using %s. "
                            "Pick another with --serial." % ready[0][0])
            return ready[0][0]
        if any(d[1] == "unauthorized" for d in devices):
            hint = ("Look at your phone: unlock it and tap Allow on the "
                    "'Allow USB debugging?' box\n(tick 'Always allow from this computer').")
        elif devices:
            hint = ("The phone is connected but not ready (%s). Unlock it; if this "
                    "stays, unplug the\ncable and plug it in again." % devices[0][1])
        else:
            hint = ("Waiting for your phone. Check these:\n"
                    "  - it is plugged into this computer with a USB cable that carries data\n"
                    "    (some cables only charge)\n"
                    "  - on the phone: Settings > Developer options > USB debugging is on\n"
                    "  - the phone is unlocked, and you tapped Allow if it asked\n"
                    "  - on Windows the phone may need its maker's USB driver")
        if hint != hinted:
            screen.line(hint)
            hinted = hint
        if time.monotonic() > deadline:
            fatal("Gave up waiting for the phone.")
        time.sleep(2)


# ---------------------------------------------------------------- the phone
def parse_types(text):
    """'GPRS|EDGE|...|LTE|NR' (what Android prints) -> bitmask, or None."""
    mask = 0
    for token in text.strip().split("|"):
        token = token.strip()
        if token in TYPE_BITS:
            mask |= 1 << TYPE_BITS[token]
            continue
        unknown = re.match(r"^UNKNOWN\((\d+)\)$", token)
        if not unknown or not 1 <= int(unknown.group(1)) <= 62:
            return None
        mask |= 1 << (int(unknown.group(1)) - 1)
    return mask or None


def plan_masks(current):
    """Work out the '5G allowed' and '4G only' settings from the current one,
    changing nothing but the 5G bit. Returns (mask_5g, mask_4g, derived)."""
    if current and current & LTE_BITS:
        return current | NR_BIT, current & ~NR_BIT, True
    return FALLBACK_5G, FALLBACK_4G, False


def find_data_slot(dump):
    """Which SIM slot (0 or 1) carries mobile data, from 'dumpsys isub'."""
    direct = re.search(r"defaultDataPhoneId=(\d+)", dump)            # Android 12, 13
    if direct and int(direct.group(1)) < 4:
        return int(direct.group(1))
    slot_of = {}
    for slot, sub in re.findall(r"Logical SIM slot (\d+): subId=(-?\d+)", dump):  # 14+
        if int(sub) >= 0:
            slot_of[int(sub)] = int(slot)
    for slot, subs in re.findall(r"sSlotIndexToSubId\[(\d+)\]: subIds=\d+=\[([\d, ]*)\]", dump):
        for sub in re.findall(r"\d+", subs):
            slot_of.setdefault(int(sub), int(slot))
    for key in ("defaultDataSubId", "activeDataSubId"):
        found = re.search(key + r"=(-?\d+)", dump)
        if found and int(found.group(1)) in slot_of:
            return slot_of[int(found.group(1))]
    if len(set(slot_of.values())) == 1:
        return list(slot_of.values())[0]
    return None


def network_label(network, override):
    if network == "NR" or override.startswith("NR"):
        return "5G"
    if network in ("LTE", "LTE_CA"):
        return "4G"
    if network in ("UMTS", "HSDPA", "HSUPA", "HSPA", "HSPA+", "TD_SCDMA"):
        return "3G"
    if network in ("GPRS", "EDGE", "GSM"):
        return "2G"
    if network == "UNKNOWN":
        return "no signal"
    return "?"


class Phone:
    def __init__(self, adb, serial):
        self.adb = adb
        self.serial = serial
        self.slot = 0
        self.mask_5g = FALLBACK_5G
        self.mask_4g = FALLBACK_4G
        self.on_4g = False        # True while we are holding the phone on 4G
        self.name = "phone"
        self.sim_name = ""

    def shell(self, command, timeout=30):
        code, out = run_cmd([self.adb, "-s", self.serial, "shell", command], timeout)
        # adb's own complaints ("error: device offline", "adb: device 'x' not found" ...)
        if code != 0 and re.search(
                r"^\s*(adb(\.exe)?|error): .*(not found|offline|unauthorized|no devices|closed)",
                out, re.I | re.M):
            raise PhoneError("the phone is not connected (%s)" % " ".join(out.split())[:160])
        return code, out

    def setup(self, sim_choice, screen):
        """Identify the phone, find the data SIM, and work out the two settings."""
        code, out = self.shell(
            "getprop ro.product.manufacturer; getprop ro.product.model; "
            "getprop ro.build.version.release; getprop ro.build.version.sdk; "
            "getprop gsm.sim.operator.alpha")
        info = (out.split("\n") + [""] * 5)[:5]
        maker, model, release, sdk, sims = [x.strip() for x in info]
        self.name = ("%s %s" % (maker[:1].upper() + maker[1:], model)).strip() or "phone"
        screen.line("Phone:     %s, Android %s" % (self.name, release or "?"))
        if sdk.isdigit() and int(sdk) < 31:
            fatal("This phone runs Android %s. The switch command this script relies on\n"
                  "only exists on Android 12 and newer, so it cannot work here." % release)

        sim_names = [s.strip() for s in sims.split(",")]
        if sim_choice:
            self.slot = sim_choice - 1
            how = "you chose it"
        else:
            code, dump = self.shell("dumpsys isub")
            slot = find_data_slot(dump)
            if slot is None:
                self.slot = 0
                how = "could not tell which SIM has mobile data, so guessing; " \
                      "use --sim 2 if that is wrong"
            else:
                self.slot = slot
                how = "the one set for mobile data"
        self.sim_name = sim_names[self.slot] if self.slot < len(sim_names) else ""
        screen.line("SIM:       SIM %d%s (%s)" % (
            self.slot + 1, " " + self.sim_name if self.sim_name else "", how))

        current = self.read_types()
        self.mask_5g, self.mask_4g, derived = plan_masks(current)
        if not derived:
            screen.line("Note:      could not read the phone's current network setting, "
                        "so using standard\n           5G-preferred / 4G-preferred settings.")
        # Setting 5G now also proves, before anything else happens, that this
        # phone accepts the command at all.
        self.set_mode(True)
        if current is not None and not current & NR_BIT:
            screen.line("Note:      the phone was set to 4G; 5G has been turned back on.")

    def read_types(self):
        try:
            code, out = self.shell("cmd phone get-allowed-network-types-for-users -s %d" % self.slot)
        except PhoneError:
            return None
        lines = [x for x in out.splitlines() if x.strip()]
        if code != 0 or len(lines) != 1:
            return None
        return parse_types(lines[0])

    def set_mode(self, five_g):
        """Allow 5G (True) or hold the phone on 4G (False)."""
        mask = self.mask_5g if five_g else self.mask_4g
        code, out = self.shell("cmd phone set-allowed-network-types-for-users -s %d %s"
                               % (self.slot, format(mask, "b")))
        if "completed" not in out:
            raise PhoneError(self._explain(out, code))
        self.on_4g = not five_g

    def restore_5g(self):
        if self.on_4g:
            self.set_mode(True)

    @staticmethod
    def _explain(out, code):
        text = " ".join(out.split())[:300] or "no reply, exit code %d" % code
        if "SecurityException" in out or "ermission" in out:
            return ("the phone refused the command (%s).\n"
                    "Some brands need one more switch in Developer options: on "
                    "Xiaomi/Redmi/Poco turn on\n'USB debugging (Security settings)'; on "
                    "Oppo/Realme/OnePlus turn on 'Disable permission monitoring'." % text)
        if "No valid subscription" in out or "No subscription found" in out:
            return "that SIM slot has no working SIM (%s). Try --sim 1 or --sim 2." % text
        if "failed" in out:
            return "the phone tried but could not change the network setting (%s)." % text
        return ("the phone did not understand the command (%s). This needs Android 12 "
                "or newer." % text)

    def network_now(self):
        """What the phone itself says it is on: '5G', '4G', '3G', '2G', ... or '?'."""
        try:
            code, out = self.shell("dumpsys telephony.registry | "
                                   "grep -E 'Phone Id=|mTelephonyDisplayInfo='", 8)
            slot = None
            for line in out.splitlines():
                phone_id = re.search(r"Phone Id=(\d+)", line)
                if phone_id:
                    slot = int(phone_id.group(1))
                    continue
                info = re.search(r"network=([^,}]+), override(?:Network)?=([^,}]+)", line)
                if info and slot == self.slot:
                    return network_label(info.group(1).strip(), info.group(2).strip())
            code, out = self.shell("getprop gsm.network.type", 8)
            parts = [p.strip() for p in out.strip().split(",")]
            if self.slot < len(parts) and parts[self.slot]:
                return network_label(parts[self.slot], "NONE")
        except PhoneError:
            pass
        return "?"


# ------------------------------------------------------ deciding to switch
class Config:
    def __init__(self, below=None, slow_seconds=SLOW_SECONDS, wait=WAIT_ON_4G,
                 settle=SETTLE_SECONDS, stall=STALL_SECONDS):
        self.below = below * MB if below else None
        self.fraction = SLOW_FRACTION
        self.always_slow = ALWAYS_SLOW_MB * MB
        self.slow_seconds = slow_seconds
        self.wait = wait
        self.settle = settle
        self.max_settle = max(MAX_SETTLE, settle)
        self.stall = stall
        self.stall_gap = 300      # at most one "stopped dead" switch per this many seconds
        self.best_memory = BEST_MEMORY
        self.idle = IDLE_KB * KB
        self.floor = MIN_DOWNLOAD_KB * KB
        self.low_tries = 2        # switches allowed while the speed stays under 1 MB/s
        self.keep = max(self.slow_seconds, 10) + 6


class Decider:
    """Looks at one speed reading per second and says when to switch.

    The rules:
      slow  - data has been coming in steadily, at 200 KB/s or more, but for
              `slow_seconds` it never got up to the threshold. Threshold:
              `below` if you set one, otherwise half of the best 10-second
              average of the last ten minutes, and never less than 1 MB/s.
      stall - a download was running and then nothing at all came in for
              `stall` seconds. The caller checks whether the internet can be
              reached, and switches only if it cannot.
    After each switch it waits `settle` seconds before judging again, and it
    doubles that wait (up to `max_settle`) while switching is not helping.
    While the speed is under 1 MB/s it tries only `low_tries` times; the count
    starts again once a real download speed has been seen.
    """

    def __init__(self, cfg, now):
        self.cfg = cfg
        self.samples = collections.deque()    # (time, bytes per second)
        self.best_hist = collections.deque()  # (time, 10-second average), biggest first
        self.low_tries = 0        # switches made while under 1 MB/s
        self.last_active = now    # last moment anything at all was coming in
        self.last_flow = None     # last moment a download was coming in
        self.last_rescue = None   # last "stopped dead" switch
        self.hold_until = now     # no decisions before this moment
        self.last_bounce = None
        self.in_bounce = False    # the phone is being held on 4G right now
        self.recovered = True     # speed has been good since the last switch
        self.fails = 0            # switches in a row that did not bring the speed back

    # -- helpers
    def _values(self, now, seconds, after=None):
        start = now - seconds
        if after is not None and after > start:
            start = after
        return [v for t, v in self.samples if t > start]

    def _mean(self, now, seconds, after=None):
        values = self._values(now, seconds, after)
        if len(values) < max(2, int(0.6 * seconds)):
            return None
        return sum(values) / len(values)

    def now_speed(self, now):
        values = self._values(now, 3)
        return sum(values) / len(values) if values else 0.0

    def best(self):
        return self.best_hist[0][1] if self.best_hist else None

    def threshold(self):
        if self.cfg.below:
            return self.cfg.below
        best = self.best()
        if best is None:
            return self.cfg.always_slow
        return max(self.cfg.always_slow, self.cfg.fraction * best)

    def real_speed(self):
        """At or above this, it is clearly a working download."""
        return min(self.cfg.always_slow, self.threshold())

    def _tries_used_up(self, mean):
        return mean < self.real_speed() and self.low_tries >= self.cfg.low_tries

    def doing(self, now):
        """A few words for the status line."""
        if now < self.hold_until:
            return "settling %d s" % (self.hold_until - now)
        mean = self._mean(now, 10)
        if mean is None:
            return "measuring"
        if mean < self.cfg.idle:
            return "no download running"
        if mean < self.cfg.floor:
            return "under %s, ignoring" % fmt(self.cfg.floor)
        if self._tries_used_up(mean):
            return "slow, but switching did not help"
        return "switches below %s" % fmt(self.threshold())

    # -- one reading per second goes in here
    def add(self, now, speed):
        cfg = self.cfg
        self.samples.append((now, speed))
        while self.samples and self.samples[0][0] < now - cfg.keep:
            self.samples.popleft()

        avg10 = self._mean(now, 10)
        if avg10 is not None:
            while self.best_hist and self.best_hist[-1][1] <= avg10:
                self.best_hist.pop()
            self.best_hist.append((now, avg10))
        while self.best_hist and self.best_hist[0][0] < now - cfg.best_memory:
            self.best_hist.popleft()

        threshold = self.threshold()
        if speed >= cfg.idle:
            self.last_active = now
        if avg10 is not None and avg10 >= cfg.floor:
            self.last_flow = now
        if avg10 is not None and avg10 >= self.real_speed():
            self.low_tries = 0

        if not self.recovered and not self.in_bounce and self.last_bounce is not None:
            since = self._mean(now, 3, after=self.last_bounce)
            if since is not None and since >= threshold:
                self.recovered = True
                self.fails = 0
                self.hold_until = min(self.hold_until, self.last_bounce + cfg.settle)

    # -- ask this once per second while on 5G
    def check(self, now):
        """Returns 'slow', 'stall' or None. Changes nothing."""
        cfg = self.cfg
        if now < self.hold_until:
            return None
        if (cfg.stall and self.last_flow is not None
                and now - self.last_active >= cfg.stall          # dead silence...
                and self.last_active - self.last_flow <= 20      # ...straight after a download
                and (self.last_rescue is None or now - self.last_rescue >= cfg.stall_gap)):
            return "stall"
        if now - self.hold_until < cfg.slow_seconds:
            return None
        values = self._values(now, cfg.slow_seconds)
        if len(values) < max(3, 0.7 * cfg.slow_seconds):
            return None
        mean = sum(values) / len(values)
        if mean < cfg.floor:
            return None               # too little to be a download
        if sum(1 for v in values if v >= cfg.idle) < 0.6 * len(values):
            return None               # comes in bursts: browsing, not a download
        # 3-second averages, so a single good second does not hide a slow spell
        # and a single bad second does not create one
        wider = self._values(now, cfg.slow_seconds + 2)
        peak = max(sum(wider[i:i + 3]) / 3.0 for i in range(len(wider) - 2))
        if peak >= self.threshold() or self._tries_used_up(mean):
            return None
        return "slow"

    def stall_dismissed(self):
        """The download stopped but the internet is fine: it finished or paused."""
        self.last_flow = None

    def started(self, now, kind):
        """Call when the phone has just been moved to 4G."""
        self.in_bounce = True
        if kind == "stall":
            self.last_rescue = now
            return
        mean = self._mean(now, self.cfg.slow_seconds)
        if mean is not None and mean < self.real_speed():
            self.low_tries += 1
        if self.last_bounce is not None and not self.recovered:
            self.fails += 1           # the previous switch did not help

    def bounced(self, now):
        """Call when the phone is back on 5G."""
        self.in_bounce = False
        self.last_bounce = now
        self.recovered = False
        self.hold_until = now + min(self.cfg.settle * (2 ** min(self.fails, 10)),
                                    self.cfg.max_settle)

    def postpone(self, now, seconds):
        self.hold_until = max(self.hold_until, now + seconds)


# ------------------------------------------------------------ the two modes
def install_close_handlers(phone):
    """If the window is closed or the script is killed while the phone is being
    held on 4G, put 5G back first."""
    def stop(signum, frame):
        raise KeyboardInterrupt()

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), stop)
            except (OSError, ValueError):
                pass
    if os.name == "nt":
        try:
            import ctypes
            handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

            def on_console_event(event):
                if event in (2, 5, 6):        # window closed, log-off, shutdown
                    try:
                        phone.restore_5g()
                    except Exception:
                        pass
                return 0                      # then let Windows carry on

            callback = handler_type(on_console_event)
            if ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, 1):
                install_close_handlers.keep = callback    # must stay referenced
        except Exception:
            pass


def run_test(phone, cfg, screen):
    """One 5G -> 4G -> 5G switch, reporting what the phone says at each step."""
    before = phone.network_now()
    screen.line("")
    screen.line("Test: the phone shows %s now. Holding it on 4G for %d seconds - "
                "watch the icon\nat the top of the phone's screen." % (before, cfg.wait))
    during = "?"
    try:
        phone.set_mode(False)
        end = time.monotonic() + cfg.wait
        while time.monotonic() < end:
            time.sleep(1)
            during = phone.network_now()
            screen.status("  on 4G, phone shows: %s" % during)
        screen.line("  The phone took the 4G setting and shows: %s" % during)
        phone.set_mode(True)
    finally:
        try:
            phone.restore_5g()
        except PhoneError as e:
            screen.line("  COULD NOT PUT 5G BACK: %s" % e)
    after = "?"
    started = time.monotonic()
    while time.monotonic() - started < 45:
        time.sleep(1)
        after = phone.network_now()
        screen.status("  5G allowed again, phone shows: %s" % after)
        if after == "5G":
            break
    took = time.monotonic() - started
    screen.line("  5G is allowed again and the phone shows: %s (after %d s)" % (after, took))
    screen.line("")
    if during == "5G":
        screen.line("Result: the phone accepted the setting but stayed on 5G, so on this "
                    "phone the\ncommand does not do the job. Please open an issue with the phone model.")
        return 1
    if before == "5G" and after == "5G":
        screen.line("Result: it works - 5G -> %s -> 5G. Run it without --test to "
                    "switch automatically." % during)
    elif before == "5G":
        screen.line("Result: the phone went to %s on command, but had not found 5G again "
                    "after 45 s.\nIt usually does a little later. If it is often this slow, "
                    "use a longer --settle." % during)
    else:
        screen.line("Result: the phone accepted both commands. It was not showing 5G when "
                    "the test\nbegan (no 5G signal right now?), so judge by the icon you "
                    "saw on the phone.")
    return 0


def watch(phone, cfg, screen, interface=None):
    meter = Meter(interface)
    if interface and not meter._counters():
        fatal("No network adapter has '%s' in its name. The adapters are:\n  %s"
              % (interface, "\n  ".join(meter.all_adapters())))
    decider = Decider(cfg, time.monotonic())
    if cfg.below:
        rule = "under %s" % fmt(cfg.below)
    else:
        rule = "under %d%% of its recent best (or under %s)" % (
            round(cfg.fraction * 100), fmt(cfg.always_slow))
    screen.line("Rule:      switch when the download stays %s for %d s." % (rule, cfg.slow_seconds))
    if not cfg.below:
        screen.line("Tip:       it learns your normal speed as it watches. If the download is "
                    "already slow\n           right now, set the bar yourself, for example:  "
                    "--below 10")
    screen.line("Watching this computer's download speed. Press Ctrl+C to stop.")
    screen.line("")

    started = time.monotonic()
    switches = 0
    back_at = 0.0
    warned_at = -1000.0
    network = phone.network_now()
    network_checked = started
    next_tick = started
    meter.sample()
    try:
        while True:
            next_tick += 1.0
            pause = next_tick - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            else:
                next_tick = time.monotonic()
            speed = meter.sample()
            now = time.monotonic()
            if speed is None:
                continue
            decider.add(now, speed)
            shown = fmt(decider.now_speed(now))

            if phone.on_4g:
                if now >= back_at:
                    try:
                        phone.set_mode(True)
                        decider.bounced(now)
                        network_checked = now - 12      # look again in a few seconds
                        screen.event("Back to 5G. Leaving the network alone for %d s."
                                     % (decider.hold_until - now))
                    except PhoneError as e:
                        back_at = now + 3
                        if now - warned_at > 30:
                            warned_at = now
                            screen.event("Could not put 5G back yet: %s\n           Still "
                                         "trying. You can also turn 5G on in the phone's "
                                         "Settings." % e)
                status = "on 4G, back to 5G in %d s" % max(0, back_at - now)
            else:
                verdict = decider.check(now)
                if verdict == "stall" and internet_works():
                    decider.stall_dismissed()
                    screen.event("Nothing has come in for %d s, but the internet is "
                                 "reachable, so the download\n           finished or "
                                 "paused. Not switching." % cfg.stall)
                    verdict = None
                if verdict:
                    if verdict == "slow":
                        why = "Stayed under %s for %d s (now %s)" % (
                            fmt(decider.threshold()), cfg.slow_seconds, shown)
                    else:
                        why = "The download stopped and the internet is unreachable"
                    try:
                        phone.set_mode(False)
                        decider.started(now, verdict)
                        switches += 1
                        back_at = time.monotonic() + cfg.wait
                        screen.event("%s -> 4G for %d s." % (why, cfg.wait))
                    except PhoneError as e:
                        decider.postpone(now, 60)
                        screen.event("%s, but could not switch: %s\n           "
                                     "Trying again in a minute." % (why, e))
                if now - network_checked >= 15 and not phone.on_4g:
                    network_checked = now
                    network = phone.network_now()
                if phone.on_4g:
                    status = "on 4G, back to 5G in %d s" % cfg.wait
                else:
                    status = "%s | %s" % (decider.doing(now), network)
            screen.status("%s | %s | %s | %d switch%s" % (
                time.strftime("%H:%M:%S"), shown, status,
                switches, "" if switches == 1 else "es"))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if phone.on_4g:
                try:
                    phone.restore_5g()
                    ending = "Put the phone back on 5G."
                except PhoneError as e:
                    ending = ("COULD NOT PUT 5G BACK (%s).\nTurn 5G on in the phone's "
                              "Settings, or just start this again." % e)
            else:
                ending = "The phone is left on 5G."
            screen.line("")
            screen.line("Stopped after %d min, %d switch%s. %s" % (
                (time.monotonic() - started) / 60.0, switches,
                "" if switches == 1 else "es", ending))
        except Exception:
            pass
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Keep a phone-hotspot download fast: when the speed drops, switch the "
                    "phone 5G -> 4G -> 5G over USB.")
    parser.add_argument("--version", action="version", version="autoswitch " + __version__)
    parser.add_argument("--test", action="store_true",
                        help="do one switch now and report what the phone does, then exit")
    parser.add_argument("--below", type=float, metavar="MB",
                        help="switch when the speed stays under this many MB/s (megabytes per "
                             "second, the number Steam and browsers show); default: automatic")
    parser.add_argument("--for", dest="slow_for", type=int, default=SLOW_SECONDS, metavar="SEC",
                        help="how long the speed must stay low first (default %d)" % SLOW_SECONDS)
    parser.add_argument("--wait", type=int, default=WAIT_ON_4G, metavar="SEC",
                        help="seconds to stay on 4G before going back (default %d)" % WAIT_ON_4G)
    parser.add_argument("--settle", type=int, default=SETTLE_SECONDS, metavar="SEC",
                        help="seconds to leave the network alone after a switch (default %d)"
                             % SETTLE_SECONDS)
    parser.add_argument("--stall", type=int, default=STALL_SECONDS, metavar="SEC",
                        help="also switch when a download stops dead for this long and the "
                             "internet is unreachable; 0 turns that off (default %d)"
                             % STALL_SECONDS)
    parser.add_argument("--sim", type=int, choices=(1, 2),
                        help="which SIM has mobile data (default: find out from the phone)")
    parser.add_argument("--interface", metavar="TEXT",
                        help="only measure the network adapter whose name contains TEXT")
    parser.add_argument("--serial", help="which phone, if several are plugged in")
    parser.add_argument("--adb", help="where adb is, if it is not found automatically")
    args = parser.parse_args(argv)
    if min(args.slow_for, args.wait, args.settle) < 1:
        parser.error("--for, --wait and --settle must be at least 1")
    if args.stall < 0:
        parser.error("--stall cannot be negative")
    if args.below is not None and args.below <= 0:
        parser.error("--below must be more than 0")

    screen = Screen()
    cfg = Config(args.below, args.slow_for, args.wait, args.settle, args.stall)
    adb = find_adb(args.adb)
    if not adb:
        print("adb, Google's tool for talking to the phone over USB, is not on this computer.")
        try:
            answer = input("Download it from Google now (about 15 MB)? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            fatal("Then install 'Android SDK Platform-Tools' yourself and start this again.")
        adb = download_adb()
        if not adb:
            fatal("The download finished but adb is not where it was expected.")

    try:
        serial = wait_for_phone(adb, args.serial, screen)
        phone = Phone(adb, serial)
        install_close_handlers(phone)
        phone.setup(args.sim, screen)
        if args.test:
            return run_test(phone, cfg, screen)
        return watch(phone, cfg, screen, args.interface)
    except PhoneError as e:
        fatal("Stopped: %s" % e)
    except RuntimeError as e:
        fatal(str(e))
    except KeyboardInterrupt:
        screen.line("")
        return 0


if __name__ == "__main__":
    sys.exit(main())
