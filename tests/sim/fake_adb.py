#!/usr/bin/env python3
"""A pretend adb with a pretend phone behind it, for trying autoswitch.py without
real hardware. Started by run_sim.py; its state lives in the JSON file named by
the FAKE_ADB_STATE environment variable."""
import json, os, sys, time
STATE = os.environ["FAKE_ADB_STATE"]
NAMES = {1: "GPRS", 2: "EDGE", 3: "UMTS", 4: "CDMA", 5: "CDMA - EvDo rev. 0", 6: "CDMA - EvDo rev. A",
         7: "CDMA - 1xRTT", 8: "HSDPA", 9: "HSUPA", 10: "HSPA", 11: "iDEN", 12: "CDMA - EvDo rev. B",
         13: "LTE", 14: "CDMA - eHRPD", 15: "HSPA+", 16: "GSM", 17: "TD_SCDMA", 18: "IWLAN",
         19: "LTE_CA", 20: "NR"}
def load():
    for _ in range(50):
        try:
            with open(STATE) as f: return json.load(f)
        except (ValueError, OSError): time.sleep(0.01)
    raise SystemExit("state unreadable")
def save(s):
    tmp = STATE + ".%d" % os.getpid()
    with open(tmp, "w") as f: json.dump(s, f)
    os.replace(tmp, STATE)
s = load()
a = sys.argv[1:]
mode = s.get("mode", "ok")
if a[:1] == ["start-server"]: sys.exit(0)
if a[:1] == ["devices"]:
    print("List of devices attached")
    if mode == "unauthorized": print("ABC123\tunauthorized")
    elif mode != "none": print("ABC123\tdevice")
    print(); sys.exit(0)
if a[:1] == ["-s"] and a[2:3] == ["shell"]:
    if mode == "none":
        print("adb: device '%s' not found" % a[1]); sys.exit(1)
    if mode == "hang": time.sleep(120)
    cmd = a[3]
    s.setdefault("calls", []).append([round(time.time(), 2), cmd[:90]])
    slot = s.get("slot", 0)
    nr = bool(s["mask"] & (1 << 19))
    settled = time.time() - s.get("changed", 0) > 2.0
    net = "NR" if (nr and settled) else "LTE"
    if "ro.product.manufacturer" in cmd:
        print("Pretend\nPhone 1\n%s\n%s\n%s" % (s.get("release", "15"), s.get("sdk", "35"), "CarrierA,CarrierB"))
    elif "dumpsys isub" in cmd:
        if int(s.get("sdk", 35)) >= 34:
            print("SubscriptionManagerService:\n  Active modem count=2\n  Logical SIM slot sub id mapping:\n"
                  "    Logical SIM slot 0: subId=3\n    Logical SIM slot 1: subId=7\n"
                  "  defaultSubId=3\n  defaultVoiceSubId=3\n  defaultDataSubId=%d\n  activeDataSubId=%d"
                  % ((3, 7)[slot], (3, 7)[slot]))
        else:
            print("SubscriptionController:\n defaultDataSubId=2\n defaultDataPhoneId=%d\n" % slot)
    elif "get-allowed-network-types-for-users" in cmd:
        print("|".join(NAMES[t] for t in range(1, 21) if s["mask"] & (1 << (t - 1))) or "UNKNOWN")
    elif "set-allowed-network-types-for-users" in cmd:
        parts = cmd.split()
        if mode == "refuse":
            print("java.lang.SecurityException: Neither user 2000 nor current process has android.permission.MODIFY_PHONE_STATE."); save(s); sys.exit(255)
        if int(parts[parts.index("-s") + 1]) != slot:
            print("set-allowed-network-types-for-users: No valid subscription found."); save(s); sys.exit(255)
        new = int(parts[-1], 2)
        if new != s["mask"]:
            s["mask"] = new; s["changed"] = time.time()
        s.setdefault("sets", []).append([round(time.time(), 2), parts[-1]])
        print("set-allowed-network-types-for-users completed")
    elif "telephony.registry" in cmd:
        other = "LTE"
        nets = [net, other] if slot == 0 else [other, net]
        for i, n in enumerate(nets):
            print("    Phone Id=%d\n      mTelephonyDisplayInfo=TelephonyDisplayInfo {network=%s, overrideNetwork=NONE, isRoaming=false}" % (i, n))
    elif "gsm.network.type" in cmd:
        print("LTE,LTE")
    else:
        print("/system/bin/sh: unknown"); save(s); sys.exit(127)
    save(s); sys.exit(0)
print("fake adb: unsupported", a); sys.exit(2)
