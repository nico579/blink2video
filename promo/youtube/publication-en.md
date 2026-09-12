# Blink cameras on your computer: live view, archives and blink2video setup

## Description

Watch your Blink cameras on your computer and keep an archive of their recordings with blink2video, a free, open-source application.

Download: https://github.com/nico579/blink2video/releases/latest
English guide: https://github.com/nico579/blink2video/blob/main/README.md
Source code and feedback: https://github.com/nico579/blink2video
Community discussion and feedback: https://www.reddit.com/r/blinkcameras/comments/1vsm4bc/blink2video_local_dashboard_for_blink_cameras_no/

WHY THIS TOOL?
Find your clips on your computer, download them before they are deleted, and combine them into videos you can browse by camera, day, week and month.

WHAT THE VIDEO SHOWS
• Live view in your browser and recording on demand.
• Clips filtered by camera and date range.
• Incremental downloads from module storage: Sync Module 2 USB or Sync Module XR microSD, and from your Blink subscription's cloud storage.
• Burned-in timestamps, automatic archives and a way to set unwanted clips aside.
• Installation, first sign-in and settings.

INSTALL AND GET STARTED
1. Download the archive for your operating system and extract all its files into a folder. Published bundles include ffmpeg; no Python installation is needed.
2. On Windows, launch blink2video.exe. On Linux or macOS, run ./blink2video after completing the preparation steps in the guide.
3. Sign in to Blink in your browser and enter the verification code. On the first run, choose the data folder and time zone, then click Apply. Downloads begin after this confirmation.

SETTINGS
The gear icon opens the options: start at login, page refresh, data folder, port, automatic downloads and separate local/cloud polling intervals, timestamps, time zone, WebRTC/MSE live view, daily/weekly/monthly archives, per-camera alerts and stopping.
Automatic deletion from the source after download is optional, per camera. It removes the source clip; this differs from Discard, which keeps the original aside.

CHAPTERS
00:00 Why blink2video?
00:24 Cameras, clips, filters and live view
00:58 Module storage and Blink cloud
01:08 Archives and timestamps
01:28 Download and install
01:43 Launch and sign in
01:58 Complete the first setup
02:12 Everyday settings
02:28 Video and archive settings
02:44 Alerts, optional deletion and stopping
03:00 Explore the project

PRACTICAL NOTES
Your computer must stay on and blink2video must be running to download, assemble videos and monitor cameras. Archives are kept on your computer; account and camera access use Blink's services. The tool does not replace a subscription for accessing its cloud clips.
Bundles are available for Windows, Linux and Apple Silicon macOS. The project documents real-camera use on Windows; Linux and macOS have automated checks.

This presentation uses animated interface screenshots and original instrumental music, with no narration or AI voice.
An independent project by Nicolas Martin, not affiliated with Blink or Amazon. Software licensed under GNU GPL v3.0.

#Blink #blink2video #OpenSource

## Comment to publish

Getting started with blink2video: a practical guide.

WHAT IT DOES
Watch your Blink cameras on your computer and keep a clip archive. The application can download new recordings from your module's local storage (Sync Module 2 USB or Sync Module XR microSD) and your Blink subscription's cloud storage, then combine them into daily, weekly and monthly videos for each camera. Previously downloaded clips are not downloaded again.

INSTALL AND LAUNCH
• Get the latest release: https://github.com/nico579/blink2video/releases/latest
• Choose your operating system and extract the complete archive. Bundles include ffmpeg; you do not need to install Python.
• Windows 10/11 x64: open blink2video.exe.
• Linux x64 or Apple Silicon macOS: complete the preparation steps in the guide, then run ./blink2video in the extracted folder.
• Sign in to your Blink account in the browser and enter the verification code Blink sends. The application stores a session token, never your password.
• On the very first run, check the data folder and time zone, then click Apply. No clips are downloaded before this step.
• To reopen the interface, use blink2video open or the system tray menu where available.

WHAT NEXT?
The Live tab lets you view a camera, arm motion detection and record a live view on demand. Browse clips and live recordings with camera and date filters. The Daily, Weekly and Monthly views contain the assembled videos.

OPTIONS UNDER THE GEAR ICON
• Everyday use: start at login, automatic page refresh, data folder and server port.
• Downloads: enable automatic retrieval and choose separate polling intervals for local storage and the cloud.
• Video: burned-in timestamps, time zone, WebRTC or MSE live playback, and independent daily, weekly and monthly archive switches.
• Control: per-camera alerts and muting, optional deletion from the source after download, and a stop button.
Save changes with Apply.

Discard sets a clip aside while keeping its original. Automatic deletion from the source is a separate option: it removes clips from the module or cloud after downloading them. Enable it only if that is the behavior you want.

Your computer must stay on and the application must be running for these tasks. Files remain on your computer; signing in and accessing cameras depend on Blink's services. blink2video does not grant access to cloud clips without the necessary account permissions.

Full guide, including Linux/macOS commands:
https://github.com/nico579/blink2video/blob/main/README.md
Source code and issue reports:
https://github.com/nico579/blink2video
Community discussion and feedback: https://www.reddit.com/r/blinkcameras/comments/1vsm4bc/blink2video_local_dashboard_for_blink_cameras_no/

An independent, free project under GNU GPL v3.0, with no affiliation with Blink or Amazon. This presentation uses animated screenshots and original music, with no AI voice. Use the chapters in the description to jump to installation or settings.

