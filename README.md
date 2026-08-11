***English** | [Français](README.fr.md)*

# blink2video

**Manage your Blink cameras from a computer, and keep what they record.**

Blink is built for the phone. Its application shows one clip at a time, keeps no
archive, and has no desktop counterpart. Clips live on a USB stick plugged into
the Sync Module, and are rotated away as it fills up.

blink2video is that missing desktop side. A local interface on your own machine
to watch the cameras live, arm or disarm detection, follow the state of the
installation, and review what was recorded on a real screen with a real
keyboard. What the phone cannot do at all, it adds: the clips are downloaded
before rotation removes them, stamped with the time they were recorded, and
assembled into one video per day, per ISO week and per month.

Everything runs on your machine. Nothing is uploaded anywhere.

## What you get

| Downloaded | Assembled | Reviewed |
|---|---|---|
| Every clip from the Sync Module's local storage, kept before rotation removes it. | One video per day, per week and per month, with the recording time burned into the image. | A local page to watch, discard, and see the cameras live. |

## Install

Download the archive for your system from the
[latest release](https://github.com/nico579/blink2video/releases/latest),
unpack it, and run `blink` from a terminal. Nothing else is required: ffmpeg
travels inside the bundle.

From source, Python 3.11 or later:

```bash
git clone https://github.com/nico579/blink2video
cd blink2video
python blink.py login
```

The first run creates an isolated environment in `~/.blink/venv`, installs the
three dependencies there, and restarts itself inside it. Use `--bootstrap=pip`
to install into the current environment instead, or `--bootstrap=none` to manage
dependencies yourself.

## Use

One command, one verb per action.

```bash
blink login          # sign in to your Blink account, two-factor supported
blink list           # what the Sync Module holds right now
blink download       # fetch new clips into Blink_Clips/
blink merge          # normalize and assemble the videos
blink all            # download then assemble
blink review         # open the web interface
blink watch --loop   # continuous monitoring, notifications, auto-assembly
```

Arguments after the verb go to the matching program, so `blink review --port 8899`
works as expected, and `blink review --help` shows that program's own options.

### The web interface

`blink review` serves a page on `127.0.0.1:8765` and opens it. Four views:

- **Live**: one tile per camera with its latest thumbnail, arm and disarm at
  system and camera level, battery, temperature, signal, and the date each
  reading was taken.
- **Clips**: every clip newest first, with a preview frame, and one button to
  discard an uninteresting one.
- **Daily, Weekly, Monthly**: the assembled videos, with durations.

The refresh button downloads new clips and rebuilds the videos, showing progress
as it goes.

### Discarding a clip

Motion detection fires on a shadow, a bird, a passing cloud. Discarding removes
a clip from every assembled video:

```bash
blink review                                   # click "Écarter" on the card
blink merge --exclude Blink_Clips/jardin/2026-08/2026-08-10_14-05-04Z_jardin.mp4
```

The original is moved to `Blink_Excluded/` rather than deleted, a tombstone is
recorded so it is never downloaded again, and the day, week and month videos are
rebuilt without it. `--include` undoes all of that.

### Monitoring

```bash
blink watch --loop
```

Checks every ten minutes. A camera going offline, a battery that is no longer
good, detection switched off, or a camera that has recorded nothing for two days
opens a dialog you must acknowledge. New clips are downloaded, the videos
rebuilt, and a notification tells you, with a click that opens the interface.

Alerts fire on change only, so a camera you knowingly leave offline warns you
once. `--ignore "Portail"` silences one permanently.

To start it with your session on Windows:

```powershell
Register-ScheduledTask -TaskName "Blink" -Action (New-ScheduledTaskAction `
  -Execute "C:\path\to\blink.exe" -Argument "watch --loop") `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn)
```

## Where files go

```
Blink_Clips/       raw clips, exactly as the Sync Module recorded them
Blink_Normalized/  same clips with the time burned in, mirroring the tree above
Blink_Excluded/    clips you discarded, kept rather than deleted
Blink_Daily/       one video per camera and per day
Blink_Weekly/      one per ISO week
Blink_Monthly/     one per month
```

Next to the executable, or in the directory named by the `BLINK_HOME`
environment variable.

## How it works

The normalized clip is the pivot. Each clip is re-encoded once, with the time
stamped into the image, and stored permanently. Daily, weekly and monthly videos
are then stream copies of those segments: no re-encoding, no generation loss, a
few seconds each.

That is what makes adding a clip cheap. A new arrival re-encodes only itself,
then the day, week and month are reassembled by copy. Encoding runs at about
real time, so a one minute clip costs about one minute, once.

The time is burned into the image rather than added as a subtitle track, because
subtitle tracks are ignored by phones and messaging applications, and the video
is meant to be watched anywhere.

Time zone conversion is done in Python and ffmpeg runs with `TZ=UTC0`, so no
part of the pipeline depends on the operating system having a time zone
database.

A clip is identified by its camera and its recording instant, never by the
identifier the Sync Module assigns: rebooting the module renumbers everything,
and clips already downloaded would come back as new.

## Limits

The tool only sees clips on the Sync Module's local storage. Cloud-only
recordings are outside its reach.

Live view works on cameras that Blink serves over its `immis` protocol. A camera
out of range of the Sync Module accepts the request but never sends an image,
and the interface says so.

Desktop notifications are Windows only. Elsewhere the watcher prints to its log
and to the terminal.

Nothing runs while the computer is off. A camera failing overnight is reported
when the session next opens.

Blink exposes no way to restart a Sync Module. When it locks up, unplug it.

## Build

```bash
python build.py
```

Creates an isolated build environment, installs the dependencies and
PyInstaller, and produces `dist/blink/`. About 110 MB, most of it ffmpeg.

PyInstaller is not a cross compiler, and the ffmpeg binary is platform specific:
each system needs its own build. The release workflow does this on GitHub
runners for Windows, Linux and macOS.

## Built on

[blinkpy](https://github.com/fronzbot/blinkpy) for the Blink API, including the
`immis` live stream implementation, and
[ffmpeg](https://ffmpeg.org/) for everything video. The protocol notes in
[BlinkMonitorProtocol](https://github.com/MattTW/BlinkMonitorProtocol) were
useful for understanding what the API does and does not offer.

Not affiliated with Blink or Amazon.
