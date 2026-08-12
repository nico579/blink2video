***English** | [Français](README.fr.md)*

# blink2video

**Manage your Blink cameras from a computer, and keep what they record.**

Blink is built for the phone: one clip at a time, no archive, no desktop
counterpart. Clips live on a USB stick plugged into the Sync Module, erased as it
fills up.

blink2video is the missing side. A local interface to watch cameras live, arm
detection and follow the state of the installation. And what the phone cannot do
at all: fetch clips before rotation erases them, burn the time into the picture,
and assemble them into one video per day, per week and per month.

Everything runs on your machine. Nothing is sent anywhere.

## Features

- Live view of any camera in the browser, arming the system or a single camera.
- Battery, temperature, signal, model and firmware for each camera.
- Incremental download from the module's USB stick and from the subscription
  cloud, never fetching the same recording twice.
- Time burned into the picture, so any player keeps it.
- One video per day, per ISO week and per month, for each camera.
- Uninteresting clips discarded in one click, moved aside rather than deleted,
  and never downloaded again.
- Continuous monitoring: camera offline, low battery, detection switched off, or
  nothing recorded for two days. Alerts to acknowledge, per-camera muting.
- Standalone bundle for Windows, Linux and macOS, ffmpeg included.

## Screenshots

![The Live tab](Screenshots/serve_direct.PNG)

Live view: one tile per camera, its latest thumbnail, arming for the system and
for each camera, battery, temperature, signal and the time of the reading. An
offline camera keeps reporting its last known values, and the interface says how
old they are.

![The Clips tab](Screenshots/serve0.PNG)

Clips newest first, filtered by camera and by day. Each card gives the camera,
the duration, the date and the model, and the "Écarter" button removes the clip
from every assembled video.

## Installation

Download the archive for your system from the
[latest release](https://github.com/nico579/blink2video/releases/latest) and
unpack it. ffmpeg travels inside the bundle; nothing is installed system-wide.

| System | Archive | First run |
|---|---|---|
| Windows 10/11, x86-64 | `blink2video-windows-x86_64.zip` | `blink2video.exe` from a terminal |
| Linux x86-64, glibc 2.35+ (Ubuntu 22.04+, Debian 12+) | `blink2video-linux-x86_64.tar.gz` | `chmod +x blink2video`, then `./blink2video` |
| macOS 12+, Apple Silicon | `blink2video-macos-arm64.zip` | `xattr -dr com.apple.quarantine blink2video`, then `./blink2video` |

The binary is neither signed nor notarized: on macOS, Gatekeeper refuses the
first run of an archive downloaded through a browser, hence the command above.
Right-click then "Open" does the same.

From source, with Python 3.11 or newer:

```bash
git clone https://github.com/nico579/blink2video
cd blink2video
python blink2video.py login
```

The first run creates an isolated environment in `~/.blink2video/venv` and restarts
there. `blink2video smoketest` then checks that everything works on your machine.

## Usage

<!-- verbes:début -->
One command, one verb per action. `blink2video <verb> --help` gives each one's options.

```bash
blink2video login       # sign in to the Blink account, two-step verification handled
blink2video list        # what the Sync Module currently holds
blink2video download    # fetch new clips before rotation erases them
blink2video merge       # normalize, stamp and assemble day, week and month
blink2video watch       # check the installation and alert when it degrades
blink2video all         # everything, that is watch then download then merge
blink2video serve       # serve the web interface, to watch, discard, see live
blink2video open        # open the web interface in the browser
blink2video stop        # stop the instance running in the background
blink2video autostart   # register the command that follows with your session
blink2video smoketest   # check that the installation works on this machine
```
<!-- verbes:fin -->
Options follow the verb: `blink2video serve --port 8899`. Several verbs can be named in
a row, each with its own options, and run together:
`blink2video serve all --loop 10`.

### The web interface

`blink2video serve` serves a page on `127.0.0.1:8765` and opens it. Four views:

- **Live**: one tile per camera, with its state and arming.
- **Clips**: newest first, with a preview and an "Écarter" (discard) button.
- **Daily, Weekly, Monthly**: the assembled videos.

The Refresh button downloads new clips and rebuilds the videos, showing progress.

### Discarding a clip

Detection fires on a shadow, a bird, a cloud. Discarding removes the clip from
every assembled video:

```bash
blink2video merge --exclude Blink_Clips/jardin/2026-08/2026-08-10_14-05-04Z_jardin.mp4
```

The raw file moves to `Blink_Excluded/`, it will never be downloaded again, and
the day, week and month are rebuilt without it. `--include` undoes all of it.

### Monitoring

```bash
blink2video watch --loop     # check and alert, every ten minutes
blink2video all --loop       # also fetch clips and rebuild the videos
```

A camera going offline, a fading battery, detection switched off or a camera
silent for two days opens a dialog you must acknowledge. Alerts fire on change
only, so a camera you knowingly leave offline warns you once, and
`--ignore "Portail"` silences it.

## Start the watcher with your session

```bash
blink2video autostart on                    # the default, see below
blink2video autostart status                # what is installed
blink2video autostart off                   # remove
```

`autostart` runs nothing: it registers the command that follows it, exactly as
you would have typed it without the prefix. So `blink2video autostart on watch --loop 30`
automates the alerts only. No administrator rights are needed, and `--dry-run`
shows what would happen.

With no verb, the entry is the interface plus two loops running at different
rates:

```
serve all --loop 10 download --from cloud --loop 1
```

Ten minutes for the USB stick, whose manifest wakes the Sync Module, and one
minute for the cloud, whose inventory costs a tenth of a second once the session
is open. A cloud clip therefore shows up within a minute instead of ten, without
asking more of the hardware.

<details>
<summary>Doing it yourself, without <code>autostart</code></summary>

**Windows**, a shortcut in the Startup folder:

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut(
  "$([Environment]::GetFolderPath('Startup'))\blink2video.lnk")
$s.TargetPath = "C:\path\to\blink2video.exe"; $s.Arguments = "serve all --loop 10 download --from cloud --loop 1"
$s.WorkingDirectory = "C:\path\to"; $s.Save()
```

Task Scheduler would also do, but its root folder requires elevation.

**macOS**, a launch agent in
`~/Library/LaunchAgents/com.nico579.blink2video.plist`:

```xml
<plist version="1.0"><dict>
  <key>Label</key><string>com.nico579.blink2video</string>
  <key>ProgramArguments</key>
  <array><string>/path/to/blink2video</string><string>serve</string><string>all</string><string>--loop</string><string>10</string><string>download</string><string>--from</string><string>cloud</string><string>--loop</string><string>1</string></array>
  <key>WorkingDirectory</key><string>/path/to</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

Loaded with `launchctl load`, stopped with `launchctl unload`.

**Linux**, a systemd user service in
`~/.config/systemd/user/blink2video.service`:

```ini
[Service]
ExecStart=/path/to/blink2video serve all --loop 10 download --from cloud --loop 1
WorkingDirectory=/path/to
Restart=on-failure

[Install]
WantedBy=default.target
```

Enabled with `systemctl --user enable --now blink2video`, followed with
`journalctl --user -u blink2video -f`.

One launcher at a time: two give two watchers, and every notification twice.

</details>

## Updating

The instance started with your session holds the interface port and talks to the
Sync Module: stop it before replacing any file.

```bash
blink2video stop                 # stops the instance and all its verbs
```

Then replace the folder with the new archive, or `git pull` from source, and
start it again: the Startup shortcut on Windows, `launchctl load` on macOS,
`systemctl --user start blink2video` on Linux. The next logon does it anyway.

`blink2video autostart status` says what is installed and whether an instance is
running. In a terminal, Ctrl+C is enough: `stop` exists for instances without a
console, which Ctrl+C cannot reach and whose verbs were left orphaned when only
the parent process was killed.

## Where files go

```
Blink_Clips/       raw clips, as the module recorded them
Blink_Normalized/  the same with the time burned in
Blink_Excluded/    discarded clips, kept rather than deleted
Blink_Daily/       one video per camera per day
Blink_Weekly/      one per ISO week
Blink_Monthly/     one per month
```

Next to the executable, or in the folder named by `BLINK_HOME`.

<details>
<summary>All options</summary>

`blink2video <verb> --help` is always authoritative; this table summarizes.

**Root**: `login`, `list`, `download`. With no verb, the help is shown.

| Option | Effect |
|---|---|
| `--hub NAME` | Sync Module to use |
| `--camera NAME` | keep only this camera |
| `--since DAYS` | keep only clips from the last N days |
| `--output FOLDER` | destination of raw clips (default `Blink_Clips`) |
| `--overwrite` | replace existing files of a different size |
| `--from usb\|cloud\|all` | where to look for clips: the module's stick, the subscription cloud, or both (default) |
| `--loop [MINUTES]` | repeat instead of acting once (default 10) |

**`blink2video merge`**: normalization and assembly.

| Option | Effect |
|---|---|
| `--exclude CLIP…` | discard clips: the raw file moves to `Blink_Excluded`, the segment is deleted, the clip is never downloaded again |
| `--include CLIP…` | undo a discard: the raw file comes back and the clip is re-normalized |
| `--date YYYY-MM-DD` | limit to one day |
| `--camera NAME` | limit to one camera |
| `--force` | rebuild everything even if nothing changed |
| `--no-periods` | do not rebuild the weekly and monthly aggregates |
| `--preset NAME` | libx264 preset, from `ultrafast` to `veryslow` (default `veryfast`) |
| `--crf N` | quality, 0 to 51, lower is better (default 21) |
| `--font FILE` | .ttf font for the timestamp |
| `--timezone ZONE` | time zone of the timestamp (default `Europe/Paris`) |
| `--input`, `--output`, `--normalized-output`, `--excluded-output`, `--weekly-output`, `--monthly-output` | location of each folder |

**`blink2video watch`**: check the state, alert when it degrades.

| Option | Effect |
|---|---|
| `--loop [MINUTES]` | repeat instead of acting once (default 10) |
| `--ignore CAMERA…` | mute a camera, then carry on checking |
| `--unignore CAMERA…` | unmute |
| `--test` | fire a verification notification |
| `--dry-run` | show without notifying or saving state |

**`blink2video all`**: watch, then download, then merge.

| Option | Effect |
|---|---|
| `--loop [MINUTES]` | repeat instead of acting once (default 10) |
| `--hub`, `--camera`, `--since` | as for `download` |
| `--dry-run`, `--timezone` | as for `watch` |

**`blink2video serve`**: serve the web interface.

| Option | Effect |
|---|---|
| `--port N` | listening port (default 8765) |
| `--open-browser` | open the page in the browser on startup |
| `--hub NAME` | Sync Module |
| `--thumbs FOLDER` | thumbnail cache, disposable |
| `--timezone ZONE` | display time zone |
| the same folder options as `merge` | |

**`blink2video open`**: open the interface in the browser, and say so when
nobody is listening. `--port` if you moved it.

**`blink2video stop`**: stop the running instance and all its verbs. No options.

**`blink2video autostart on\|off\|status [verb…]`**: register the command that
follows it with your session. Without a verb, `serve all --loop 10 download --from cloud --loop 1`.
`--dry-run` shows without acting.

**`blink2video smoketest`**: installation check. `--keep` keeps the working folder,
`--timezone` picks the time zone of the demonstration video.

**Environment variables**

| Variable | Effect |
|---|---|
| `BLINK_HOME` | data folder, defaulting to the executable's own |
| `BLINK_BOOTSTRAP` | `auto`, `pip` or `none`: how the Python environment is handled |

</details>

<details>
<summary>How it works</summary>

The normalized clip is the pivot. Each clip is re-encoded once, with the time
written into the picture, then kept for good. The daily, weekly and monthly
videos are only stream copies of those segments: no re-encoding, no generation
loss, a few seconds each.

That is what makes adding a clip cheap. A new arrival re-encodes only itself, at
roughly real time: a one minute clip costs about one minute, once.

The time is burned into the picture rather than added as a subtitle track, which
phones and messaging apps ignore. Time zone conversion happens on the Python
side, with ffmpeg running under `TZ=UTC0`: the chain depends on no system time
zone database.

A clip is identified by its camera and its recording instant, never by the
module's own identifier: restarting it renumbers everything, and clips already
downloaded would come back as new.

</details>

## Limits

- Blink Mini cameras do not write to the module's USB stick: unless a
  subscription covers them, their detections leave only a notification, with no
  recording to archive.
- A camera out of range of the module accepts the live request but never sends a
  picture; the interface says so.
- On Linux, the imageio-ffmpeg binary is built without libfreetype and cannot
  burn the timestamp: `sudo apt install ffmpeg`. The tool tries every ffmpeg it
  finds and keeps the first that can write text. Windows and macOS need nothing.
- Notifications borrow each system's own tools: the native dialog on Windows,
  osascript on macOS, notify-send and zenity on Linux. Failing that, the watcher
  writes to `watch.log` and carries on.
- Nothing runs while the computer is off: a camera failing at night is reported
  at the next logon.
- Blink refuses some API endpoints depending on the client version announced,
  with an "An app update is required" message. Live view and downloading work
  today; nothing guarantees Blink will not widen that refusal.
- Blink exposes no way to restart a stuck Sync Module: you have to unplug it.

## Neighbours

Other projects touch the same hardware, and complement each other more than they
compete:

- [blinkpy](https://github.com/fronzbot/blinkpy) is the API library everything
  else builds on, including this.
- [BlinkCamWindowsDashboard](https://github.com/mikeoverbay/BlinkCamWindowsDashboard)
  offers a web dashboard and clip downloading, from the cloud only, so a
  subscription is required, and without live view.
- [blinkbridge](https://github.com/roger-/blinkbridge) exposes a camera over
  RTSP, to plug into an existing surveillance system.
- [blink-live-view](https://github.com/andreiele/blink-live-view) focuses on
  desktop live view.

blink2video differs on three points: it reads **both** sources, the module's USB
stick and the subscription cloud, never fetching the same recording twice; it
burns the time into the picture and assembles one video per day, per week and
per month; and it runs on all three systems as a standalone bundle, ffmpeg
included.

## Building

```bash
python build.py
```

Produces `dist/blink2video/`, about 110 MB, most of it ffmpeg. PyInstaller is not a
cross-compiler and the ffmpeg binary is platform-specific: each system needs its
own build. The release workflow handles that on GitHub runners.

## Project status and responsible use

An independent project, used daily on Windows 10 against a real installation:
one module, four cameras, live view, arming and the archive. The Linux and macOS
executables are built and their video chain verified automatically, but they have
never run against real Blink hardware. Reproducible reports are welcome in the
issues.

Film responsibly. In France, the CNIL accepts watching your own home, but not
the public street nor a neighbour's doorway. This tool only keeps what your
cameras already record, but it makes the question more concrete by building a
lasting archive where the mobile app kept a few days.

That archive is personal data: images of your home, the presence patterns they
reveal, and a session file that grants access to your Amazon account. It all
stays on your machine, and the repository's `.gitignore` excludes those files.

## Licence, author and credits

Distributed under the GNU General Public License v3.0; see [LICENSE](LICENSE).
Consistency demands it too: the Linux bundle embeds a GPL build of ffmpeg, the
only one with libfreetype.

Designed and architected by Nicolas Martin ([@nico579](https://github.com/nico579)).
Code developed with the assistance of Claude (Anthropic) as a development tool.

[blinkpy](https://github.com/fronzbot/blinkpy) provides access to the Blink API,
including the `immis` protocol without which live view would be out of reach, and
[ffmpeg](https://ffmpeg.org/) does all the video work. The notes in
[BlinkMonitorProtocol](https://github.com/MattTW/BlinkMonitorProtocol) helped to
understand what the API offers, and above all what it does not.

Not affiliated with Blink or Amazon. Blink is an Amazon trademark.
