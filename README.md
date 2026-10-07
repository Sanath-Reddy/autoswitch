# autoswitch

Keeps a download over your phone's hotspot fast by switching the phone
**5G → 4G → 5G** whenever the speed drops.

On some mobile networks a 5G hotspot starts fast, slows down after a while, and
picks up again as soon as you flip the phone to 4G and back. This script does
that flip for you. It runs on the computer that is downloading, watches the
download speed there, and tells the phone to switch over a USB cable.

- No root, and nothing to install on the phone.
- One Python file, standard library only.
- If switching is not helping, it backs off instead of hammering the network.

What it looks like while running:

```
Phone:     OnePlus CPH2661, Android 16
SIM:       SIM 1 Jio (the one set for mobile data)
Rule:      switch when the download stays under 50% of its recent best (or under 1.0 MB/s) for 15 s.
Watching this computer's download speed. Press Ctrl+C to stop.

[13:02:11] Stayed under 18.2 MB/s for 15 s (now 3.0 MB/s) -> 4G for 8 s.
[13:02:20] Back to 5G. Leaving the network alone for 30 s.
13:02:49 | 24.2 MB/s | switches below 18.2 MB/s | 5G | 1 switch
```

Whether the flip helps depends on your network. It does on the one this was
written for; try it by hand on yours first.

## What you need

- An Android phone on **Android 12 or newer**, with **USB debugging** turned on.
- A USB cable that carries data, from the phone to the computer. The phone has
  to stay plugged in while the script runs.
- **Python 3.9 or newer** on the computer.
- `adb`, Google's tool for talking to the phone. If it is not on the computer,
  the script offers to download it from Google.

Tested on Windows 11 with a OnePlus Nord 4 (Android 16). On Linux it has only
been run against the simulated phone in `tests/sim`. macOS is untested.

## Setup

1. On the phone, open **Settings → About phone** and tap **Build number**
   seven times. Then open **Developer options** and turn on **USB debugging**.
2. Plug the phone into the computer and turn on its hotspot as usual.
3. Download this repository (**Code → Download ZIP**, then unzip) or clone it.
4. Run the test. It makes the phone go to 4G for 8 seconds and back, so you can
   see that your phone obeys:
   - Windows: double-click `Test switch - run this first.bat`
   - Anywhere: `python autoswitch.py --test`
5. The first time, the phone asks **Allow USB debugging?** Tick *Always allow
   from this computer* and tap **Allow**.

If the test ends with `Result: it works`, you are set.

## Use

Start it when you start a download, and stop it when the download is done.

- Windows: double-click `Start autoswitch.bat`. Close the window to stop.
- Anywhere: `python autoswitch.py`. Press Ctrl+C to stop.

| Option | What it does | Default |
| --- | --- | --- |
| `--below MB` | Switch when the speed stays under this many MB/s | automatic |
| `--for SEC` | How long the speed must stay low first | 15 |
| `--wait SEC` | How long to stay on 4G before going back | 8 |
| `--settle SEC` | How long to leave the network alone after a switch | 30 |
| `--stall SEC` | Also switch when a download stops dead this long and the internet is unreachable (0 = off) | 45 |
| `--sim 1` or `--sim 2` | Which SIM has mobile data | read from the phone |
| `--interface TEXT` | Only measure the network adapter whose name contains TEXT | busiest adapter |
| `--serial SERIAL` | Which phone, if several are plugged in | first one |
| `--adb PATH` | Where `adb` is | found automatically |
| `--test` | Do one switch, report what the phone did, and exit | |

On Windows you can pass options through the launcher:
`"Start autoswitch.bat" --below 8`

The remaining settings are constants at the top of `autoswitch.py`.

## When it switches

**Slow download.** Data is coming in steadily but for 15 seconds it never
reaches the bar. With `--below` the bar is the number you gave. Without it, the
bar is half of the best speed seen in the last ten minutes, and never under
1 MB/s. Because the automatic bar is learned while it runs, a download that is
already slow when you start needs `--below`.

**Dead connection.** A download was running, nothing at all has arrived for
45 seconds, and a check shows the internet cannot be reached. If the internet
can be reached, the download simply finished or paused, and nothing happens.

After every switch it waits 30 seconds before judging again. Each switch in a
row that does not bring the speed back doubles that wait, up to 5 minutes.

## When it does not switch

- **Under 200 KB/s.** That little traffic cannot be told apart from an online
  game or a voice call, so the network is left alone. A download that crawls
  below 200 KB/s is therefore ignored too.
- **Traffic that comes in bursts**, such as browsing.
- **Under 1 MB/s, after two tries.** If two switches have not lifted the speed
  above 1 MB/s it stops trying until a real download speed is seen again.

## Things to know

- **Each switch briefly interrupts the connection.** Game launchers and most
  browsers usually carry on by themselves. A download that cannot resume may
  fail.
- **Time on 4G can cost data.** If your plan's unlimited data is 5G-only, the
  seconds spent on 4G come out of your 4G allowance. `--wait 5` shortens them.
- **If the phone is ever left on 4G** (cable pulled mid-switch, computer
  crashed), start the script again or turn 5G back on in the phone's settings.
  On start it always turns 5G on.
- **USB debugging gives a computer deep access to your phone.** Only allow
  computers you trust, and turn it off when you no longer need it.
- It changes one setting: the phone's allowed network types for the SIM that
  carries mobile data, and in it only whether 5G is allowed.

## If something goes wrong

- **"Waiting for your phone"**: check the cable carries data, the phone is
  unlocked, and USB debugging is on. On Windows the phone may need its maker's
  USB driver.
- **"The phone refused the command"**: some brands are reported to need one
  more switch in Developer options, such as *USB debugging (Security settings)*
  on Xiaomi phones or *Disable permission monitoring* on Oppo, Realme and
  OnePlus phones. This has not been needed on the phone it was tested with.
- **Wrong SIM**: pass `--sim 1` or `--sim 2`.
- **The speed shown is wrong**: pass `--interface` with part of your Wi-Fi
  adapter's name.
- **"The phone accepted the setting but stayed on 5G"**: the command does not
  work on that phone. Please open an issue with the model and Android version.

## How it works

The speed is read from the computer's own network adapter counters once a
second (`psutil` if it is installed, otherwise the operating system directly).

The switch uses a command built into Android, which the `adb` shell user is
allowed to run:

```
adb shell cmd phone get-allowed-network-types-for-users -s <slot>
adb shell cmd phone set-allowed-network-types-for-users -s <slot> <bitmask>
```

The script reads the current setting, removes the 5G (NR) bit to hold the phone
on 4G, and puts the bit back afterwards.

## Development

```
python tests/test_autoswitch.py     # the tests; no phone needed
python tests/sim/run_sim.py         # watch it work against a pretend phone (Linux, macOS)
```

The tests cover the parsing, the speed meter, and the switching rules run
through simulated hours of traffic: slow downloads, finished downloads, games,
browsing, dead connections.

## Credits

- The command and the network-type names and numbers come from the Android
  Open Source Project (`TelephonyShellCommand.java`, `TelephonyManager.java`).
- [NetToggle](https://github.com/Dhangofa/NetToggle) showed that this command
  works without root. No code from it is used here.

## Disclaimer

This is an independent project. It is not affiliated with or endorsed by
Google, OnePlus, Jio or any carrier; their names are used only to describe what
it was tested with. Android is a trademark of Google LLC. Use it at your own
risk, on your own phone, and within your carrier's terms.

## License

[MIT](LICENSE)
