#!/usr/bin/env python3
"""Try autoswitch.py with no phone at all (Linux and macOS).

A pretend phone answers the adb commands, and this computer's network counters
are replaced by a pretend connection: 20 MB/s for a while after the phone joins
5G, then 3 MB/s, and 5 MB/s while it is held on 4G. So you can watch the script
notice the slowdown, switch, and recover, in about a minute:

    python tests/sim/run_sim.py                  quick settings, Ctrl+C to stop
    python tests/sim/run_sim.py --test           the one-off switch test
    python tests/sim/run_sim.py --below 10       any autoswitch option works

Set SIM_GOOD_FOR=seconds to change how long the fast spell lasts (default 25).
"""
import json
import os
import stat
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import autoswitch  # noqa: E402

GOOD_FOR = float(os.environ.get("SIM_GOOD_FOR", "25"))
NR_BIT = 1 << 19


def main():
    work = tempfile.mkdtemp(prefix="autoswitch-sim-")
    state = os.path.join(work, "phone.json")
    with open(state, "w") as f:      # a phone set to 5G/4G/3G/2G, data on SIM 1
        json.dump({"mask": int("11001101001110000111", 2), "changed": time.time() - 3, "slot": 0}, f)
    os.environ["FAKE_ADB_STATE"] = state
    adb = os.path.join(work, "adb")
    with open(adb, "w") as f:
        f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, os.path.join(HERE, "fake_adb.py")))
    os.chmod(adb, os.stat(adb).st_mode | stat.S_IXUSR)

    def phone():
        for _ in range(50):
            try:
                with open(state) as f:
                    return json.load(f)
            except (ValueError, OSError):
                time.sleep(0.01)
        return {"mask": NR_BIT, "changed": 0}

    def speed():
        p = phone()
        since = time.time() - p.get("changed", 0)
        if since < 1.5:
            return 0.0               # the connection drops for a moment at every switch
        if not p["mask"] & NR_BIT:
            return 5e6
        return 20e6 if since < GOOD_FOR else 3e6

    counters = {"pretend-wifi": 0}

    def pump():
        last = time.time()
        while True:
            time.sleep(0.05)
            now = time.time()
            counters["pretend-wifi"] += int(speed() * (now - last))
            last = now

    threading.Thread(target=pump, daemon=True).start()
    autoswitch.make_reader = lambda: ((lambda: dict(counters)), None, "pretend")
    autoswitch.internet_works = lambda wait=8: True
    args = sys.argv[1:] or ["--for", "5", "--wait", "3", "--settle", "8"]
    return autoswitch.main(["--adb", adb] + args)


if __name__ == "__main__":
    sys.exit(main())
