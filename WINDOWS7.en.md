# Experimental Windows 7 edition

The regular Windows bundle embeds Python 3.12, which cannot run on Windows 7
and may report that `api-ms-win-core-path-l1-1-0.dll` is missing. This separate
edition uses the last official compatible runtime, **CPython 3.8.10 x64**.

It contains the current `blinkpy 0.25.9` code. Only the wheel metadata is
backported to Python 3.8, with the latest dependencies still available for that
runtime. The original PyPI wheel is checked against its SHA-256 before it is
modified.

## Build the artifact

Open **Actions → Build Windows 7 (experimental) → Run workflow** on GitHub.
Download the resulting `blink2video-windows7-x86_64-experimental` artifact. It
is deliberately kept out of releases until it starts successfully on a real
Windows 7 system.

A local build requires 64-bit Windows and the official python.org **CPython
3.8.10** interpreter:

```powershell
python build.py --win7 --propre
```

The legacy venv and outputs are isolated in `build_venv_win7`, `build-win7`
and `dist-win7`.

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
blink2video.exe login
blink2video.exe list
blink2video.exe download --from usb
blink2video.exe merge
blink2video.exe serve
```

`--version` must include `Windows 7` (the current label is
`Windows 7 expérimental`). Then check 2FA, Gen2 USB
and cloud downloads, live view, and finally `start`, `stop` and `autostart`.
Use Firefox ESR 115 or Chromium 109 for the web UI; IE11 is not a target.

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
