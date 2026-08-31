Blink Sync Module XR test r2 (Windows 10/11 x64)
===================================================

1. Close blink2video.
2. Put Tester-XR.exe in the same folder as blink2video.exe. It replaces nothing.
3. Double-click Tester-XR.exe and allow a few minutes for the manifest request.
4. The anonymized report opens automatically.
5. Paste the complete report into your Reddit reply; no separate upload is needed.

The tester uses the Blink session already saved by blink2video. It reads the
account inventory and directly requests the local-storage manifest. It does not
download or delete clips, start Live View, or change any camera setting.

The report contains only software versions, yes/no states, counts, error classes
and a short one-way error reference. It contains no password, authentication
token, serial number, camera/network name, raw identifier, URL or raw API reply.

If the report says LOGIN REQUIRED, open blink2video normally, sign in, close it,
then run Tester-XR.exe again from the same folder. Do not rerun the test more
than once per minute because the Sync Module needs time to build its manifest.
