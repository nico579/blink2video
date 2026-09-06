# Legacy Windows 7 edition

The regular Windows bundle embeds Python 3.12, which cannot run on Windows 7
and may report that `api-ms-win-core-path-l1-1-0.dll` is missing. This separate
edition uses the last official compatible runtime, **CPython 3.8.10 x64**.

It contains the current `blinkpy 0.25.9` code. Only the wheel metadata is
backported to Python 3.8, with the latest dependencies still available for that
runtime. The original PyPI wheel is checked against its SHA-256 before it is
modified.

Live view offers **WebRTC and MSE**, selectable in settings. The legacy
profile packages a stack pinned for Python 3.8 and Windows 7 DLLs:
`aiortc 1.9.0`, `PyAV 12.3.0`, `cryptography 42.0.8`, `pyOpenSSL 24.1.0`,
and `pylibsrtp 0.10.0`. These versions do not replace the modern bundle's
dependencies. MSE remains available; this port does not change the Blink
live relay's TLS behavior.

The Mozilla CA store supplied by `certifi` supplements the Windows 7 store, so
Blink connections remain strictly verified even when that legacy installation
no longer receives newer root authorities.

## Build the artifact

The `build-win7.yml` workflow runs automatically on every push to `main`,
alongside the regular edition checks. It can also be triggered by hand on
GitHub via **Actions → Build Windows 7 (legacy) → Run workflow**; the
downloadable artifact is then named
`blink2video-windows7-x86_64-legacy`.

The same recipe is reused (`workflow_call`) by `release.yml`: on every stable
release tag `vX.Y.Z`, the `blink2video-windows7-x86_64-legacy.zip`
archive is published as an extra asset on the
[latest release](https://github.com/nico579/blink2video/releases/latest),
under the same tag as the three regular editions, rather than on a separate
tag or `experimental.N` numbering. Each build is automatically verified
before publishing (startup, ffmpeg, Blink TLS, the full test suite), but
unlike the other three archives, no `.sha256` is published alongside it. A
failure in this job never blocks publishing the three stable editions: this
is a best-effort edition, outside Microsoft support.

The build also requires working native WebRTC imports and runs a local
H.264 High exchange inside the bundle: MPEG-TS, ICE/DTLS/SRTP, three decoded
frames, and connection cleanup. Normal and delay-loaded PE imports are
checked against a narrow list of known post-Win7 APIs. This static check
does not replace execution on Windows 7.

Manual validation on a real Windows 7 SP1 VM (next section) is still
recommended before trusting a build for real use: the automated checks above
do not replace it.

A local build requires 64-bit Windows and the official python.org **CPython
3.8.10** interpreter:

```powershell
python build.py --win7 --propre
```

The legacy venv and outputs are isolated in `build_venv_win7`, `build-win7`
and `dist-win7`. Both editions share the same application sources in `main`;
only this legacy build envelope is separate.

## Prepare the VM

1. Install 64-bit Windows 7 SP1 with 2 CPUs, 4 GB RAM and NAT networking, then
   take a clean snapshot.
2. Install the required Microsoft updates, at least KB2533623. If an UCRT error
   remains, install KB2999226 and the official Visual C++ 2015–2019 x64
   redistributable, then reboot.
3. Do not install Python in the VM: the test must prove that the bundle is
   self-contained.
4. Copy and extract the archive into `C:\blink7`. Do not run it from inside the
   ZIP or from a VirtualBox shared folder.
5. Never download individual DLL files from third-party sites.

## Test progressively

Run these commands from `C:\blink7\blink2video` in `cmd.exe`:

```bat
blink2video.exe --version
blink2video.exe --help
blink2video.exe smoketest
blink2video.exe smoketest --webrtc --report webrtc.json
blink2video.exe login
blink2video.exe list
blink2video.exe download --from usb
blink2video.exe merge
blink2video.exe serve
```

`--version` must include `Windows 7 legacy`. Then check 2FA, Gen2 USB
and cloud downloads, live view, and finally `start`, `stop` and `autostart`.
IE11 is not a target.

The browser tested for WebRTC live view on Windows 7 is the x64 edition of
[Supermium 144.0.7559.256 R5](https://github.com/win32ss/supermium/releases/tag/v144-r5).
On September 6, 2026, in a VirtualBox Windows 7 SP1 VM, cameras Salon,
Terrasse1, and jardin produced decoded frames and advancing playback over
WebRTC, without re-encoding. The MSE check on Salon also passed, with the
module released after every stop. A second test in a normal browser window
confirmed continuous WebRTC frame presentation on all three cameras using
`requestVideoFrameCallback` and advancing playback time.

The server preserves Blink's H.264 High stream. When the browser does not
offer High directly but advertises a **High 4:4:4 Predictive** decoder,
negotiation can select that decoder: this neither converts the video to
4:4:4 nor installs a codec. The server also preserves negotiated RTP
payload type identifiers in the 35–63 range (41 in this test), instead of
replacing them with 112. The 64–95 range remains excluded to avoid RTCP
conflicts, as required by
[RFC 5761, section 4](https://www.rfc-editor.org/rfc/rfc5761.html#section-4).

This validation applies to this browser and these cameras, not all H.264
levels: in particular, the 1080p sources advertise `640028` (High, level 4.0),
whereas Supermium's offer contains `f4001f` (High 4:4:4 Predictive, level 3.1).
Decoding was observed here; an offer limited to level 3.1 does not by itself
guarantee reception of a level 4.0 stream on another browser or hardware.

On the same VM, Firefox ESR 115 with OpenH264 2.6 offers Baseline only over
WebRTC: **keep MSE with Firefox**, even though the native High diagnostic
passes. A browser extension or system codec pack does not replace the
profiles advertised by its WebRTC stack. If the offer contains no H.264 at
all, the UI rejects startup before waking the camera and suggests checking
OpenH264 or selecting MSE. MSE remains available in settings, including
with Supermium.

The `--webrtc` diagnostic does not contact cameras, read Blink credentials,
or send notifications. Its JSON report works even with a console-free bundle
and must contain `ok: true`, `frames_decoded: 3`, and `closed: true`. Then
test a real camera in the browser and select MSE in settings to verify both
transports.

Desktop notifications currently use the Windows 10 toast API. Their absence
on Windows 7 does not affect downloads or video generation.

## Security and maintenance limits

Windows 7, Python 3.8 and several of the last Python-3.8-compatible libraries
are no longer maintained. This edition is therefore **legacy / best effort**
and must not be exposed directly to the Internet. Automatic updates are
disabled because the regular archive would reinstall Python 3.12 and make the
program unstartable on Windows 7.

After `login`, the folder contains Blink account tokens. Do not publish the VM
or its files; revert to the clean snapshot after testing.
