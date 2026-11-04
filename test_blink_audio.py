"""Actual AAC/H.264 remux and audio codec metadata, without a camera."""
from __future__ import annotations

import io
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import av
import imageio_ffmpeg
import blink_audio


class AudioRemuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="blink-audio-test-")
        cls.root = Path(cls.temp.name)
        cls.ffmpeg = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
        cls.source = cls.root / "source.ts"
        cls.run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=16000:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=16000:duration=4",
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
            "-c:v", "libx264", "-g", "5", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "32k", "-ac", "1", "-f", "mpegts", str(cls.source)])
        cls.video_only = cls.root / "video.ts"
        cls.run_ffmpeg(["-i", str(cls.source), "-map", "0:v:0", "-c", "copy",
                        "-f", "mpegts", str(cls.video_only)])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def run_ffmpeg(cls, args):
        env = dict(os.environ, GCONV_PATH=os.environ.get("GCONV_PATH", "/dev/null"))
        result = subprocess.run([cls.ffmpeg, "-hide_banner", "-loglevel", "error", *args],
                                capture_output=True, timeout=20, env=env)
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace")[-2000:].strip()
            raise AssertionError(f"FFmpeg exit code {result.returncode}: {detail}" if detail
                                 else f"FFmpeg failed with exit code {result.returncode}")

    def remux(self, enabled, track=0, source=None):
        target = self.root / f"output-{enabled}-{track}-{source is not None}.mp4"
        self.run_ffmpeg(["-y", "-i", str(source or self.source), "-map", "0:v:0",
                         "-c:v", "copy", *blink_audio.stream_options({
                             "camera_audio_enabled": enabled, "camera_audio_track": track}),
                         "-f", "mp4", "-movflags", blink_audio.fragment_flags({"camera_audio_enabled": enabled}),
                         str(target)])
        return target

    def test_selected_primary_and_alternate_aac_channels_are_saved(self):
        for track, frequency in ((0, 1000), (1, 500)):
            with self.subTest(track=track):
                target = self.remux(True, track)
                self.assertEqual(blink_audio.aac_mime_codec_from_moov(target.read_bytes()),
                                 "mp4a.40.2")
                with av.open(str(target)) as container:
                    self.assertEqual([s.type for s in container.streams], ["video", "audio"])
                    frames = list(container.decode(audio=0))
                samples = [sample for frame in frames[3:8]
                           for (sample,) in struct.iter_unpack(
                               "<f", bytes(frame.planes[0])[:frame.samples * 4])][:1600]
                def power(f):
                    x = sum(v * math.cos(2 * math.pi * f * i / 16000) for i, v in enumerate(samples))
                    y = sum(v * math.sin(2 * math.pi * f * i / 16000) for i, v in enumerate(samples))
                    return x*x + y*y
                other = 500 if frequency == 1000 else 1000
                self.assertGreater(power(frequency), 10 * power(other))
                with av.open(str(target)) as container:
                    self.assertGreater(sum(1 for _ in container.decode(video=0)), 0)

    def test_disabled_audio_omits_audio_track(self):
        target = self.remux(False)
        self.assertIsNone(blink_audio.aac_mime_codec_from_moov(target.read_bytes()))
        with av.open(str(target)) as container:
            self.assertEqual([s.type for s in container.streams], ["video"])

    def test_absent_audio_falls_back_to_decodable_video(self):
        for track in (0, 1):
            target = self.remux(True, track, self.video_only)
            self.assertIsNone(blink_audio.aac_mime_codec_from_moov(target.read_bytes()))
            with av.open(str(target)) as container:
                self.assertEqual([s.type for s in container.streams], ["video"])
                self.assertGreater(sum(1 for _ in container.decode(video=0)), 0)

    def test_partial_recording_from_keyframe_fragment_retains_audio_and_video(self):
        target = self.remux(True)
        boxes = list(blink_audio._boxes(target.read_bytes()))
        def box(kind, data):
            return (len(data)+8).to_bytes(4, "big")+kind+data
        init = b"".join(box(kind, data) for kind, data in boxes if kind in (b"ftyp", b"moov"))
        groups = []
        for kind, data in boxes:
            if kind == b"moof":
                groups.append(box(kind, data))
            elif kind == b"mdat" and groups:
                groups[-1] += box(kind, data)
        self.assertGreater(len(groups), 2)
        partial = init + b"".join(groups[1:3])
        for media in ("audio", "video"):
            with av.open(io.BytesIO(partial)) as container:
                self.assertGreater(sum(1 for _ in container.decode(**{media: 0})), 0)

    def test_truncated_audio_configuration_is_rejected(self):
        target = self.remux(True)
        data = target.read_bytes()
        with self.assertRaises(ValueError):
            blink_audio.aac_mime_codec_from_moov(data[:data.index(b"esds") + 5])


if __name__ == "__main__":
    unittest.main()
