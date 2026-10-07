"""Tests for autoswitch.py: parsing, the speed meter, and the switching rules
driven through simulated time. No phone needed.

    python tests/test_autoswitch.py      (or: pytest tests)
"""
import os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import autoswitch as A

MB, KB = A.MB, A.KB

# ---------------------------------------------------------------- parsing
NAMES_BY_TYPE = {1: "GPRS", 2: "EDGE", 3: "UMTS", 4: "CDMA", 5: "CDMA - EvDo rev. 0",
                 6: "CDMA - EvDo rev. A", 7: "CDMA - 1xRTT", 8: "HSDPA", 9: "HSUPA", 10: "HSPA",
                 11: "iDEN", 12: "CDMA - EvDo rev. B", 13: "LTE", 14: "CDMA - eHRPD",
                 15: "HSPA+", 16: "GSM", 17: "TD_SCDMA", 18: "IWLAN", 19: "LTE_CA", 20: "NR"}

def android_print(mask):
    """What Android's convertNetworkTypeBitmaskToString prints for a mask."""
    names = [NAMES_BY_TYPE[t] for t in range(1, 21) if mask & (1 << (t - 1))]
    return "|".join(names) or "UNKNOWN"

def test_type_table_matches_android():
    for t, name in NAMES_BY_TYPE.items():
        assert A.TYPE_BITS[name] == t - 1, name
    assert A.NR_BIT == 1 << 19 and A.LTE_BITS == (1 << 12) | (1 << 18)

def test_parse_types_roundtrip():
    # the examples from Android's own help text for the command
    examples = ["10000000000000000000", "11000001000000000000", "11001111101111111111",
                "01001111101111111111", "01000001000000000000",
                "11011111101111111111", "1011111101111111111"]
    for text in examples:
        mask = int(text, 2)
        assert A.parse_types(android_print(mask)) == mask, text
    random.seed(1)
    for _ in range(2000):
        mask = random.getrandbits(20)
        assert A.parse_types(android_print(mask)) == (mask or None)
    assert A.parse_types("UNKNOWN") is None
    assert A.parse_types("") is None
    assert A.parse_types("LTE|bogus") is None
    assert A.parse_types("LTE|NR|UNKNOWN(21)") == (1 << 12) | (1 << 19) | (1 << 20)
    assert A.parse_types("  LTE | NR \n") == (1 << 12) | (1 << 19)

def test_plan_masks():
    cur = int("11001101001110000111", 2)             # NR/LTE/GSM/WCDMA, what Settings uses
    m5, m4, derived = A.plan_masks(cur)
    assert derived and m5 == cur and m4 == cur & ~A.NR_BIT and m4 & A.LTE_BITS
    assert format(m4, "b") == "1001101001110000111"
    # phone currently on 4G-preferred: 5G mask adds only the NR bit
    m5, m4, derived = A.plan_masks(cur & ~A.NR_BIT)
    assert derived and m5 == cur and m4 == cur & ~A.NR_BIT
    # "5G only" or unreadable: use the fixed pair, which differ only in NR
    for bad in (A.NR_BIT, None, 0, 0b111):
        m5, m4, derived = A.plan_masks(bad)
        assert not derived and m5 == A.FALLBACK_5G and m4 == A.FALLBACK_4G
    assert A.FALLBACK_4G == A.FALLBACK_5G & ~A.NR_BIT and A.FALLBACK_4G & A.LTE_BITS

A13_DUAL_DATA_SIM2 = """SubscriptionController:
 mLastISubServiceRegTime=1234
 defaultSubId=2
 defaultDataSubId=2
 defaultVoiceSubId=1
 defaultSmsSubId=1
 defaultDataPhoneId=1
 defaultVoicePhoneId=0
 defaultSmsPhoneId=0
 sSlotIndexToSubId[0]: subIds=0=[1]
 sSlotIndexToSubId[1]: subIds=1=[2]
++++++++++++++++++++++++++++++++
"""
A14_DUAL_DATA_SIM2 = """SubscriptionManagerService:
  Active modem count=2
  Logical SIM slot sub id mapping:
    Logical SIM slot 0: subId=3
    Logical SIM slot 1: subId=7
  ICCID:
    slot 0: 8991[****]
    slot 1: 8991[****]

  defaultSubId=3
  defaultVoiceSubId=3
  defaultDataSubId=7
  activeDataSubId=7
  defaultSmsSubId=3
  areAllSubscriptionsLoaded=true
"""
A14_SINGLE = """SubscriptionManagerService:
  Active modem count=2
  Logical SIM slot sub id mapping:
    Logical SIM slot 0: subId=-1
    Logical SIM slot 1: subId=5
  defaultSubId=5
  defaultDataSubId=-1
  activeDataSubId=-1
"""
A12_NO_PHONEID_INVALID = """SubscriptionController:
 defaultDataSubId=1
 defaultDataPhoneId=2147483647
 sSlotIndexToSubId[0]: subIds=0=[1]
"""

def test_find_data_slot():
    assert A.find_data_slot(A13_DUAL_DATA_SIM2) == 1
    assert A.find_data_slot(A13_DUAL_DATA_SIM2.replace("defaultDataPhoneId=1", "defaultDataPhoneId=0")) == 0
    assert A.find_data_slot(A14_DUAL_DATA_SIM2) == 1
    assert A.find_data_slot(A14_DUAL_DATA_SIM2.replace("defaultDataSubId=7", "defaultDataSubId=3")) == 0
    assert A.find_data_slot(A14_SINGLE) == 1          # only one SIM present
    assert A.find_data_slot(A12_NO_PHONEID_INVALID) == 0
    assert A.find_data_slot("something else entirely") is None
    assert A.find_data_slot("") is None

def test_network_label():
    L = A.network_label
    assert L("NR", "NONE") == "5G" and L("LTE", "NR_NSA") == "5G" and L("LTE", "NR_ADVANCED") == "5G"
    assert L("LTE", "NONE") == "4G" and L("LTE", "LTE_CA") == "4G" and L("LTE_CA", "NONE") == "4G"
    assert L("HSPA+", "NONE") == "3G" and L("EDGE", "NONE") == "2G" and L("UNKNOWN", "NONE") == "no signal"

class StubPhone(A.Phone):
    def __init__(self, replies):
        A.Phone.__init__(self, "adb", "X")
        self.replies = replies
        self.sent = []
    def shell(self, command, timeout=30):
        self.sent.append(command)
        for key, value in self.replies.items():
            if key in command:
                if isinstance(value, Exception):
                    raise value
                return value
        return (1, "")

REG_A16 = """    Phone Id=0
      mTelephonyDisplayInfo=TelephonyDisplayInfo {network=LTE, overrideNetwork=NONE, isRoaming=false, isNtn=false, isSatelliteConstrainedData=false}
    Phone Id=1
      mTelephonyDisplayInfo=TelephonyDisplayInfo {network=NR, overrideNetwork=NONE, isRoaming=false, isNtn=false, isSatelliteConstrainedData=false}
"""
REG_A12 = """  Phone Id=0
    mTelephonyDisplayInfo=TelephonyDisplayInfo {network=LTE, override=NR_NSA}
"""

def test_network_now():
    p = StubPhone({"telephony.registry": (0, REG_A16)})
    p.slot = 0; assert p.network_now() == "4G"
    p.slot = 1; assert p.network_now() == "5G"
    p = StubPhone({"telephony.registry": (0, REG_A12)}); assert p.network_now() == "5G"
    # registry unreadable -> falls back to the system property
    p = StubPhone({"telephony.registry": (0, "Phone Id=0\nmTelephonyDisplayInfo=null\n"),
                   "gsm.network.type": (0, "LTE,NR\n")})
    p.slot = 1; assert p.network_now() == "5G"
    p.slot = 0; assert p.network_now() == "4G"
    p = StubPhone({"telephony.registry": A.PhoneError("gone")}); assert p.network_now() == "?"
    p = StubPhone({}); assert p.network_now() == "?"

def test_set_mode_and_errors():
    ok = "set-allowed-network-types-for-users completed\n"
    p = StubPhone({"set-allowed": (0, ok)})
    p.slot = 1
    p.set_mode(False)
    assert p.on_4g and p.sent[-1] == "cmd phone set-allowed-network-types-for-users -s 1 " + format(A.FALLBACK_4G, "b")
    p.set_mode(True)
    assert not p.on_4g and p.sent[-1].endswith(" " + format(A.FALLBACK_5G, "b"))
    for reply, word in [((0, "set-allowed-network-types-for-users failed\n"), "could not change"),
                        ((255, "java.lang.SecurityException: no MODIFY_PHONE_STATE\n"), "Developer options"),
                        ((255, "set-allowed-network-types-for-users: No valid subscription found.\n"), "--sim"),
                        ((255, "Unknown command: set-allowed-network-types-for-users\n"), "Android 12"),
                        ((1, ""), "Android 12")]:
        p = StubPhone({"set-allowed": reply})
        try:
            p.set_mode(False); assert False, "should have raised"
        except A.PhoneError as e:
            assert word in str(e), (reply, str(e))
        assert not p.on_4g                      # a refused switch does not count as "on 4G"

def test_read_types():
    p = StubPhone({"get-allowed": (0, "GPRS|EDGE|UMTS|HSDPA|HSUPA|HSPA|LTE|HSPA+|GSM|LTE_CA|NR\n")})
    assert p.read_types() == int("11001101001110000111", 2)
    for reply in [(255, ""), (0, "line one\nline two\n"), (0, "garbage\n")]:
        assert StubPhone({"get-allowed": reply}).read_types() is None

def test_mac_netstat():
    sample = """Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                         1354382     0  402983204  1354382     0  402983204     0
lo0        16384 127           127.0.0.1          1354382     -  402983204  1354382     -  402983204     -
en0        1500  <Link#6>      a4:83:e7:11:22:33 51435478     0 62038408471 23434434     0 4329834234     0
en0        1500  192.168.43    192.168.43.77     51435478     - 62038408471 23434434     - 4329834234     -
utun0      1380  <Link#17>                              0     0          0        2     0        200     0
en5*       1500  <Link#9>      ac:de:48:00:11:22        0     0          0        0     0          0     0
"""
    got = A.parse_mac_netstat(sample)
    assert got == {"en0": 62038408471, "utun0": 0, "en5*": 0}, got

def test_fmt():
    assert A.fmt(12.44 * MB) == "12.4 MB/s" and A.fmt(999 * KB) == "999 KB/s" and A.fmt(0) == "0 KB/s"
    assert A.fmt(1 * MB) == "1.0 MB/s"

# ------------------------------------------------------------------ meter
class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t

def make_meter(readings, wrap=None, only=None):
    m = A.Meter.__new__(A.Meter)
    it = iter(readings)
    m.read, m.wrap, m.backend = (lambda: next(it)), wrap, "fake"
    m.only = only; m.prev = None; m.prev_time = None
    return m

def test_meter(monkey_time=None):
    clock = FakeClock()
    real = A.time.monotonic
    A.time.monotonic = clock
    try:
        m = make_meter([{"wlan0": 1000, "eth0": 5}, {"wlan0": 1000 + 5 * MB, "eth0": 5 + MB},
                        {"wlan0": 1000 + 5 * MB, "eth0": 5 + MB, "new0": 10 ** 12},   # adapter appears
                        {"wlan0": 100, "eth0": 5 + MB},                               # counter reset
                        {"wlan0": 100 + 3 * MB},                                      # adapter vanishes
                        {"wlan0": 100 + 3 * MB + 10 ** 13}])                          # absurd jump
        assert m.sample() is None
        clock.t += 1; assert m.sample() == 5 * MB          # busiest adapter wins, not the sum
        clock.t += 1; assert m.sample() == 0               # new adapter has no baseline yet
        clock.t += 1; assert m.sample() == 0               # reset is not a download
        clock.t += 2; assert m.sample() == 1.5 * MB        # 3 MB over 2 seconds
        clock.t += 1; assert m.sample() == 0               # glitch ignored
        # 32-bit counters wrap round
        m = make_meter([{"a": 2 ** 32 - 1000}, {"a": 4000}], wrap=2 ** 32)
        m.sample(); clock.t += 1; assert m.sample() == 5000
        # --interface filter
        m = make_meter([{"Wi-Fi": 0, "Ethernet": 0}, {"Wi-Fi": 100, "Ethernet": 900}], only="wi-fi")
        m.sample(); clock.t += 1; assert m.sample() == 100
    finally:
        A.time.monotonic = real

def test_real_reader_on_this_machine():
    read, wrap, name = A.make_reader()
    counters = read()
    assert counters and all(isinstance(v, int) for v in counters.values())
    assert not any(A._is_loopback(k) for k in counters)

def test_each_native_reader_for_this_system():
    """Every way of reading the counters that this operating system offers must work
    on its own (make_reader would hide a broken one behind the next)."""
    natives = {"linux": [A._linux_reader], "win32": [A._windows_reader64, A._windows_reader32],
               "darwin": [A._mac_reader]}
    for platform, factories in natives.items():
        if sys.platform.startswith(platform):
            for factory in factories:
                counters = factory()()
                assert counters and all(isinstance(v, int) and v >= 0 for v in counters.values()), factory.__name__

def test_run_cmd():
    code, out = A.run_cmd([sys.executable, "-c", "print('hello'); raise SystemExit(3)"], 30)
    assert code == 3 and out.strip() == "hello"
    try:
        A.run_cmd([sys.executable, "-c", "import time; time.sleep(30)"], 1); assert False
    except A.PhoneError as e:
        assert "did not answer" in str(e)
    try:
        A.run_cmd(["definitely-not-a-real-program-xyz"], 5); assert False
    except A.PhoneError as e:
        assert "could not run" in str(e)

# ----------------------------------------------- the switching rules, in time
class World:
    """A simulated connection + phone, stepped one tick at a time, driving a
    Decider exactly the way watch() does."""
    def __init__(self, speed, cfg=None, internet=True, jitter=0.0, fail_switch_until=None, seed=7):
        self.cfg = cfg or A.Config()
        self.speed_fn = speed
        self.internet = internet            # bool or function(t)
        self.jitter = jitter
        self.fail_switch_until = fail_switch_until
        self.t = 0.0
        self.on_4g = False
        self.back_at = 0.0
        self.last_5g = 0.0                  # when we last (re)joined 5G
        self.last_change = -100.0
        self.d = A.Decider(self.cfg, 0.0)
        self.events = []                    # (time, kind)
        self.dismissed = []
        self.rng = random.Random(seed)

    def run(self, seconds):
        end = self.t + seconds
        while self.t < end:
            self.t += 1.0 + (self.rng.random() * self.jitter)
            t = self.t
            self.d.add(t, max(0.0, self.speed_fn(self, t)))
            if self.on_4g:
                if t >= self.back_at:
                    self.on_4g = False
                    self.last_5g = self.last_change = t
                    self.d.bounced(t)
                continue
            verdict = self.d.check(t)
            if verdict == "stall":
                works = self.internet(t) if callable(self.internet) else self.internet
                if works:
                    self.d.stall_dismissed(); self.dismissed.append(t); verdict = None
            if verdict:
                if self.fail_switch_until and t < self.fail_switch_until:
                    self.d.postpone(t, 60); self.events.append((t, "failed")); continue
                self.d.started(t, verdict)
                self.on_4g = True
                self.back_at = t + self.cfg.wait
                self.last_change = t
                self.events.append((t, verdict))
        return self

    def times(self, kind=None):
        return [t for t, k in self.events if k != "failed" and (kind is None or k == kind)]

def decaying(good, bad, good_for, on4g=5 * MB, helps=True):
    """Fast for `good_for` s after joining 5G, then slow. 2 s of nothing around a switch."""
    def f(w, t):
        if t - w.last_change < 2: return 0.0
        if w.on_4g: return on4g
        fresh = (t - w.last_5g) < good_for
        return good if (fresh and (helps or w.last_5g == 0.0)) else bad
    return f

def gaps(times):
    return [round(b - a) for a, b in zip(times, times[1:])]

def test_S1_fast_then_slow_bounces_and_keeps_going():
    w = World(decaying(20 * MB, 3 * MB, 120)).run(1800)
    t = w.times()
    assert all(k == "slow" for _, k in w.events)
    assert 120 + 15 <= t[0] <= 120 + 21, t[0]            # ~15 s after the speed dropped
    assert len(t) >= 11                                  # keeps doing it all half hour
    assert all(120 + 8 + 15 <= g <= 120 + 8 + 22 for g in gaps(t)), gaps(t)   # no back-off
    assert w.d.fails == 0 and w.d.low_tries == 0

def test_S7_switching_stops_helping_backs_off_then_accepts_new_normal():
    w = World(decaying(20 * MB, 6 * MB, 120, helps=False)).run(3600)
    t = w.times()
    g = gaps(t)
    assert 3 <= len(t) <= 6, t
    assert all(b > a for a, b in zip(g, g[1:])), g       # each wait longer than the last
    assert g[0] >= 30 + 15 + 8 and g[1] >= 60 + 15 + 8, g
    assert t[-1] < 120 + 600 + 320, t                    # once the old best is forgotten, it stops
    assert w.d.threshold() < 6 * MB

def test_S7b_fixed_threshold_backoff_reaches_cap_and_stays_there():
    cfg = A.Config(below=10)
    w = World(decaying(20 * MB, 6 * MB, 120, helps=False), cfg).run(3600)
    g = gaps(w.times())
    assert g[:4] == sorted(g[:4]) and g[0] < 70 and max(g) <= 300 + 8 + 15 + 8, g
    assert all(abs(x - g[-1]) <= 4 for x in g[-4:]), g   # settles at the 5-minute cap
    assert len(w.times()) <= 3600 / 300 + 4

def test_S2_crawling_from_the_start_gets_two_tries_only():
    w = World(lambda w, t: 300 * KB).run(3600)
    t = w.times()
    assert len(t) == 2 and 15 <= t[0] <= 21, t
    assert w.d.doing(w.t) == "slow, but switching did not help"

def test_S2b_crawling_from_the_start_and_switching_helps():
    def f(w, t):
        if t - w.last_change < 2: return 0.0
        if w.on_4g: return 2 * MB
        if w.last_5g == 0.0: return 300 * KB
        return 20 * MB if t - w.last_5g < 100 else 300 * KB
    w = World(f).run(1800)
    t = w.times()
    assert 15 <= t[0] <= 21 and len(t) >= 12, t           # never runs out of tries: each one works

def test_S2c_crawl_that_two_switches_cannot_fix_is_retried_after_a_real_download():
    def f(w, t):
        if 1000 <= t < 1200: return 20 * MB               # a proper download later on
        return 300 * KB
    w = World(f).run(2400)
    t = w.times()
    assert len([x for x in t if x < 1000]) == 2 and len([x for x in t if x > 1200]) == 2, t

def test_S3_download_finishes_no_switch():
    w = World(lambda w, t: 20 * MB if t < 200 else 0.0).run(3600)
    assert w.times() == [] and len(w.dismissed) == 1, (w.events, w.dismissed)
    assert 200 + 44 <= w.dismissed[0] <= 200 + 50, w.dismissed
    assert w.d.doing(w.t) == "no download running"

def test_S4_gaming_after_the_download_never_switches():
    rng = random.Random(3)
    def f(w, t):
        if t < 200: return 20 * MB
        if t < 260: return 0.0
        return rng.uniform(40, 150) * KB                 # online game traffic
    w = World(f).run(2 * 3600)
    assert w.times() == [], w.events

def test_S4a_gaming_while_the_download_ends_never_switches():
    rng = random.Random(4)
    def f(w, t):
        game = rng.uniform(40, 150) * KB                 # the game runs the whole time
        return game + (20 * MB if t < 200 else 0.0)      # the download just ends, no quiet gap
    w = World(f).run(2 * 3600)
    assert w.times() == [] and w.dismissed == [], (w.events, w.dismissed)
    assert w.d.doing(w.t) == "under 200 KB/s, ignoring"

def test_S4b_gaming_with_no_download_at_all_never_switches():
    w = World(lambda w, t: 60 * KB).run(2 * 3600)
    assert w.times() == [], w.events

def test_S4c_script_started_idle_then_game():
    w = World(lambda w, t: 0.0 if t < 40 else 80 * KB).run(2 * 3600)
    assert w.times() == [], w.events

def test_S4d_download_dropping_under_200k_is_left_alone():
    # the known blind spot: this cannot be told from a game, so nothing happens
    w = World(lambda w, t: 20 * MB if t < 200 else 100 * KB).run(1800)
    assert w.times() == [] and w.d.doing(w.t) == "under 200 KB/s, ignoring"

def test_S5_bursty_browsing_never_switches():
    w = World(lambda w, t: 2 * MB if int(t) % 15 < 2 else 5 * KB).run(3600)
    assert w.times() == [] and w.dismissed == [], (w.events, w.dismissed)

def test_S5b_browsing_right_after_a_download_never_switches():
    def f(w, t):
        if t < 200: return 20 * MB
        return 3 * MB if int(t) % 12 < 2 else 10 * KB    # pages and trailers, no quiet gap
    w = World(f).run(3600)
    assert w.times() == [], w.events

def test_S5c_steady_stream_after_download_two_tries_at_most():
    def f(w, t):
        if t < 200: return 20 * MB
        if t < 260: return 0.0
        return 500 * KB
    w = World(f).run(2 * 3600)
    assert len(w.times()) <= 2, w.events

def test_S6_dead_connection_gets_rescue_switches():
    dead_from, dead_until = 200, 1500
    def f(w, t):
        return 0.0 if dead_from <= t < dead_until else 20 * MB
    w = World(f, internet=lambda t: not (dead_from <= t < dead_until)).run(3000)
    t = w.times("stall")
    assert w.times("slow") == []
    assert 200 + 44 <= t[0] <= 200 + 50, t
    assert all(300 <= g <= 345 for g in gaps(t)), gaps(t)        # then one try per 5 minutes
    assert t[-1] < dead_until and len(t) == 5, t
    assert w.dismissed == []

def test_S6b_download_stops_then_only_chatter_then_silence_no_stall():
    # light traffic went on long after the download; its ending is not a stalled download
    def f(w, t):
        if t < 200: return 20 * MB
        if t < 600: return 60 * KB
        return 0.0
    w = World(f, internet=False).run(1800)
    assert w.events == [], w.events

def test_S8_disk_limited_bursts_never_switch():
    w = World(lambda w, t: 20 * MB if int(t) % 11 < 5 else 0.0).run(3600)
    assert w.times() == [] and w.dismissed == [], (w.events, w.dismissed)

def test_S9_normal_wobble_never_switches():
    rng = random.Random(5)
    w = World(lambda w, t: rng.uniform(0.6, 1.0) * 20 * MB).run(3600)
    assert w.times() == []

def test_S9b_one_second_dropouts_never_switch():
    w = World(lambda w, t: 0.0 if int(t) % 7 == 0 else 15 * MB).run(3600)
    assert w.times() == []

def test_S9c_one_good_second_does_not_hide_a_slow_spell():
    w = World(lambda w, t: (20 * MB if t < 100 else (11 * MB if int(t) % 9 == 0 else 3 * MB))).run(400)
    assert w.times() and w.times()[0] <= 100 + 21, w.events

def test_T10_fixed_threshold():
    f = lambda w, t: 8 * MB if t < 100 else 4.5 * MB
    assert World(f).run(600).times() == []                       # automatic: 4.5 is over half of 8
    t = World(f, A.Config(below=5)).run(600).times()
    assert t and 100 + 15 <= t[0] <= 100 + 21, t

def test_T11_uneven_ticks_still_trigger():
    w = World(decaying(20 * MB, 3 * MB, 120), jitter=0.6).run(900)
    t = w.times()
    assert t and 120 + 15 <= t[0] <= 120 + 30, t

def test_T12_failed_switch_is_retried_and_not_counted():
    w = World(decaying(20 * MB, 3 * MB, 120, helps=False), fail_switch_until=400)
    w.run(470)
    kinds = [k for _, k in w.events]
    assert kinds.count("failed") >= 3 and kinds[-1] == "slow" and kinds.count("slow") == 1, w.events
    failed = [t for t, k in w.events if k == "failed"]
    assert all(60 <= g <= 80 for g in gaps(failed)), gaps(failed)   # one retry a minute
    assert w.d.fails == 0                                            # refusals are not "did not help"

def test_T13_recovery_during_a_long_back_off_cancels_it():
    def f(w, t):
        if t - w.last_change < 2: return 0.0
        if w.on_4g: return 5 * MB
        if t < 120: return 20 * MB
        if t < 700: return 3 * MB
        return 20 * MB if (t - w.last_5g) < 150 or t < 900 else 3 * MB
    w = World(f, A.Config(below=10)).run(1500)
    t = w.times()
    late = [x for x in t if x > 900]
    assert late and w.d.fails == 0, t
    assert late[0] <= 900 + 15 + 22, (t, late)            # answered promptly again, not minutes later

def test_T14_slow_spell_shorter_than_the_limit_is_ignored():
    w = World(lambda w, t: 3 * MB if 100 <= t < 112 else 20 * MB).run(600)
    assert w.times() == []

def test_T15_settle_time_is_respected():
    cfg = A.Config(settle=90)
    w = World(decaying(20 * MB, 3 * MB, 20), cfg).run(900)       # goes slow 20 s after each switch
    g = gaps(w.times())
    assert g and all(x >= 90 + 15 + 8 for x in g), g

def test_T16_stall_option_off():
    dead = lambda w, t: 20 * MB if t < 200 else 0.0
    w = World(dead, A.Config(stall=0), internet=False).run(1800)
    assert w.events == []

def test_T17_for_and_wait_options():
    cfg = A.Config(slow_seconds=5, wait=3, settle=10)
    w = World(decaying(20 * MB, 3 * MB, 60), cfg).run(600)
    t = w.times()
    assert 60 + 5 <= t[0] <= 60 + 10, t
    assert all(60 + 3 + 5 <= g <= 60 + 3 + 11 for g in gaps(t)), gaps(t)

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn(); print("ok   ", name)
        except Exception as e:
            failed += 1
            import traceback; print("FAIL ", name); traceback.print_exc()
    print("\n%d tests, %d failed" % (len(tests), failed))
    sys.exit(1 if failed else 0)
