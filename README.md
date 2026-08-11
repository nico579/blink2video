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

## Features

**Watch and control**
- Live view of any camera in the browser, arm or disarm the whole system or a
  single camera, from a real screen.
- Battery, temperature, Wi-Fi and link signal for each camera, each reading
  dated: a camera out of range keeps reporting its last known values, and the
  interface says when they were taken.
- Camera model, firmware and serial number.

**Keep**
- Incremental download of the Sync Module's local storage, before rotation
  removes the clips.
- Recording time burned into every frame, so it survives any player, any phone,
  any messaging application.
- One video per day, per ISO week and per month, per camera.
- Discarded clips moved aside rather than deleted, and never downloaded again.

**Be told**
- Continuous monitoring: camera or module offline, battery no longer good,
  detection switched off, system disarmed, or a camera that has recorded nothing
  for two days.
- Alerts on change only, acknowledged with a dialog. Per-camera muting.
- New clips downloaded and assembled automatically, then a notification that
  opens the interface when clicked.

**Run anywhere**
- Standalone bundle for Windows, Linux and macOS, ffmpeg included.
- From source, the first run builds its own isolated environment.

## Screenshots

*(to be added)*

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



<!-- verbes:début -->
One command, one verb per action. `blink <verb> --help` gives each one's options.

```bash
blink login       # se connecter au compte Blink, vérification en deux étapes gérée
blink list        # ce que contient le module de synchronisation en ce moment
blink download    # récupérer les nouveaux clips avant que la rotation ne les efface
blink merge       # normaliser, horodater et assembler jour, semaine et mois
blink all         # tout : contrôler l'état, télécharger, assembler
blink serve       # servir seulement l'interface web : visionnage, tri, direct, armement
blink watch       # contrôler l'état de l'installation et alerter s'il se dégrade
blink autostart   # lancer un verbe à l'ouverture de session, « all --serve --loop 10 » par défaut
blink smoketest   # vérifier que l'installation fonctionne sur cette machine
```
<!-- verbes:fin -->
Arguments after the verb go to the matching program, so `blink serve --port 8899`
works as expected, and `blink serve --help` shows that program's own options.

### The web interface

`blink serve` serves a page on `127.0.0.1:8765` and opens it. Four views:

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
blink serve                                   # click "Écarter" on the card
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

To have it start with your session, see
[Start the watcher with your session](#start-the-watcher-with-your-session).

## Check your installation

```bash
blink smoketest
```

Produces a real timestamped video you can open, raises a real notification, and
reports on ffmpeg, the font, the Blink session and the autostart state. The
timestamp check does not merely confirm the filter exists: it normalizes a black
clip and counts lit pixels, the only proof that a time was actually drawn.

## Start the watcher with your session

One command, using whichever mechanism your system provides:

```bash
blink autostart on        # install
blink autostart           # where things stand
blink autostart off       # remove
```

Add `--dry-run` to see what would happen without changing anything. No
administrator rights are needed.

If you would rather do it yourself, here is what the command sets up.

### Windows

A shortcut in the Startup folder. Task Scheduler would also do, but its root
folder requires elevation:

```powershell
Register-ScheduledTask -TaskName "blink2video" -Action (New-ScheduledTaskAction `
  -Execute "C:\path	olink.exe" -Argument "watch --loop") `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn)
```

If that returns "access denied", use the Startup folder instead, which needs no
rights at all:

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut(
  "$([Environment]::GetFolderPath('Startup'))link2video.lnk")
$s.TargetPath = "C:\path	olink.exe"; $s.Arguments = "watch --loop"
$s.WorkingDirectory = "C:\path	o"; $s.Save()
```

Use one or the other, never both: two launchers means two watchers and every
notification twice.

### macOS

A launch agent, loaded at login:

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.nico579.blink2video.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.nico579.blink2video</string>
  <key>ProgramArguments</key>
  <array><string>/path/to/blink</string><string>watch</string><string>--loop</string></array>
  <key>WorkingDirectory</key><string>/path/to</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/com.nico579.blink2video.plist
```

`launchctl unload` on the same file stops it. `KeepAlive` restarts the watcher
if it ever exits.

### Linux

A systemd user service:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/blink2video.service <<'UNIT'
[Unit]
Description=blink2video watcher

[Service]
ExecStart=/path/to/blink watch --loop
WorkingDirectory=/path/to
Restart=on-failure

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now blink2video
```

`journalctl --user -u blink2video -f` follows it. Desktop notifications need
`notify-send`, and the acknowledgement dialog needs `zenity`; without them the
watcher writes to `watch.log` and keeps working.

## All options

`blink <verb> --help` is always authoritative; this table summarizes.

Verbs and options follow one rule: a **verb** is what the program does, an
**option** is how it does it. An option that would divert a command from its
purpose, such as installing an autostart entry instead of watching, is a verb.

**Racine** : `login`, `list`, `download`. Sans verbe, l'aide s'affiche.

| Option | Effet |
|---|---|
| `--hub NOM` | module de synchronisation à utiliser |
| `--camera NOM` | ne garder que cette caméra |
| `--since JOURS` | ne garder que les clips des N derniers jours |
| `--output DOSSIER` | destination des clips bruts (défaut `Blink_Clips`) |
| `--overwrite` | remplacer les fichiers existants de taille différente |

**`blink merge`** : normalisation et assemblage.

| Option | Effet |
|---|---|
| `--exclude CLIP…` | écarter des clips : le brut part dans `Blink_Excluded`, le segment est effacé, le clip n'est plus retéléchargé |
| `--include CLIP…` | annuler une exclusion : le brut revient et le clip est re-normalisé |
| `--date AAAA-MM-JJ` | limiter à une journée |
| `--camera NOM` | limiter à une caméra |
| `--force` | tout reconstruire même si rien n'a changé |
| `--no-periods` | ne pas reconstruire les agrégats hebdomadaires et mensuels |
| `--preset NOM` | preset libx264, d'`ultrafast` à `veryslow` (défaut `veryfast`) |
| `--crf N` | qualité, 0 à 51, plus bas est meilleur (défaut 21) |
| `--font FICHIER` | police .ttf pour l'horodatage |
| `--timezone ZONE` | fuseau de l'horodatage (défaut `Europe/Paris`) |
| `--input`, `--output`, `--normalized-output`, `--excluded-output`, `--weekly-output`, `--monthly-output` | emplacements de chaque dossier |

**`blink serve`** : interface web.

| Option | Effet |
|---|---|
| `--port N` | port d'écoute (défaut 8765) |
| `--no-browser` | ne pas ouvrir le navigateur |
| `--hub NOM` | module de synchronisation |
| `--thumbs DOSSIER` | cache des vignettes, jetable |
| `--timezone ZONE` | fuseau d'affichage |
| les mêmes options de dossiers que `merge` | |

**`blink watch`**: check the state, alert when it degrades.

| Option | Effect |
|---|---|
| `--loop [MINUTES]` | repeat instead of acting once (default 10) |
| `--ignore CAMERA…` | mute a camera, no alerts at all |
| `--unignore CAMERA…` | unmute |
| `--test` | raise a test notification |
| `--notify popup\|mail\|both` | alert channel (default: dialog) |
| `--dry-run` | show without acting |

**`blink all`**: check, download, assemble. The everyday verb.

| Option | Effect |
|---|---|
| `--loop [MINUTES]` | repeat instead of acting once (default 10) |
| `--serve` | raise the web interface first, and leave it running |
| `--port N` | interface port (default 8765) |
| `--no-watch`, `--no-download`, `--no-merge` | drop a step |
| `--hub`, `--camera`, `--since` | as for `download` |
| `--notify`, `--dry-run`, `--timezone` | as for `watch` |

**`blink autostart on\|off\|status [verb…]`**: start a verb with your session,
using the system's own mechanism. Without a verb, `all --serve --loop 10`.
One entry per verb, `--dry-run` shows without acting.

**`blink smoketest`** : contrôle de l'installation. `--keep` conserve le dossier
de travail, `--timezone` choisit le fuseau de la vidéo de démonstration.

**Variables d'environnement**

| Variable | Effet |
|---|---|
| `BLINK_HOME` | dossier des données, à défaut celui de l'exécutable |
| `BLINK_BOOTSTRAP` | `auto`, `pip` ou `none` : gestion de l'environnement Python |

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

On Linux, the ffmpeg binary shipped by imageio-ffmpeg is built without
libfreetype, so it cannot draw the timestamp. Install your distribution's own
build, which can: `sudo apt install ffmpeg`. The tool tries each ffmpeg it can
find and keeps the first one able to draw text. Windows and macOS need nothing.

Desktop notifications use the tools of each system: the native dialog on
Windows, osascript on macOS, notify-send and zenity on Linux. Where they are
missing, the watcher writes to `watch.log` and keeps working.

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

## Project status and responsible use

blink2video is an independent project, used daily on Windows 10 against a real
installation: one Sync Module, four cameras, live view, arming and the archive.
The Linux and macOS executables are built and their video pipeline is verified
automatically on GitHub runners, but they have never run against real Blink
hardware, and their desktop notifications have not been seen on a screen.
Feedback and reproducible reports are welcome in the GitHub issues.

Film responsibly. Rules differ by country, but the common principle is that you
may watch your own property and not the public road, a neighbour's doorway, or a
shared space without informing the people concerned. This tool only keeps what
your cameras already record: it changes nothing about what you are allowed to
film, but it makes the question more concrete by building a lasting archive
where the mobile application kept only a few days.

The archive is personal data. It holds images of your home, the presence
patterns that can be inferred from them, and the session file grants access to
your Amazon account. These files stay on your machine and are never uploaded,
but they deserve the same care as your other sensitive data. The repository's
`.gitignore` excludes all of them.

## Licence, author and credits

The code is distributed under the GNU General Public License v3.0; see
[LICENSE](LICENSE). Any modified redistribution must provide the corresponding
source under the same licence. Consistency also demands it: the Linux bundle
embeds a GPL build of ffmpeg, the only one carrying libfreetype and therefore
able to draw the timestamp.

Designed and architected by Nicolas Martin
([@nico579](https://github.com/nico579)). Code developed with the assistance of
Claude (Anthropic) as a development tool.

[blinkpy](https://github.com/fronzbot/blinkpy) provides access to the Blink API,
including the `immis` protocol implementation without which live view would be
out of reach, and [ffmpeg](https://ffmpeg.org/) does all the video work. The
reverse engineering notes in
[BlinkMonitorProtocol](https://github.com/MattTW/BlinkMonitorProtocol) helped in
understanding what the API offers, and above all what it does not.

Not affiliated with Blink or Amazon. Blink is an Amazon trademark.
