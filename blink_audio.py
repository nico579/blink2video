"""Audio selection and AAC codec signaling for the Blink MPEG-TS -> MP4 relay."""
from __future__ import annotations

def stream_options(settings: dict) -> list[str]:
    if not settings.get("camera_audio_enabled"):
        return ["-an"]
    track = settings.get("camera_audio_track", 0)
    return ["-map", f"0:a:{track}?", "-c:a", "copy", "-bsf:a", "aac_adtstoasc"]


def fragment_flags(settings: dict) -> str:
    flags = "frag_keyframe+empty_moov+default_base_moof"
    # AAC from MPEG-TS gains its AudioSpecificConfig only after the first
    # packet is filtered. Delay moov until then so browsers get a usable esds.
    return flags + ("+delay_moov" if settings.get("camera_audio_enabled") else "")


def _boxes(data: bytes):
    offset = 0
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                raise ValueError("Incomplete MP4 box")
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise ValueError("Incomplete MP4 box")
        yield kind, data[offset + header:offset + size]
        offset += size


def _child(data: bytes, kind: bytes):
    return next((payload for box, payload in _boxes(data) if box == kind), None)


def _descriptor(data: bytes, offset: int):
    if offset >= len(data):
        raise ValueError("Missing AAC descriptor")
    tag = data[offset]
    offset += 1
    size = 0
    for _ in range(4):
        if offset >= len(data):
            raise ValueError("Incomplete AAC descriptor")
        byte = data[offset]
        offset += 1
        size = (size << 7) | (byte & 0x7f)
        if not byte & 0x80:
            break
    else:
        raise ValueError("Invalid AAC descriptor size")
    if offset + size > len(data):
        raise ValueError("Incomplete AAC descriptor")
    return tag, data[offset:offset + size]


def _codec_from_esds(esds: bytes) -> str:
    tag, es = _descriptor(esds, 4)  # FullBox version/flags.
    if tag != 3 or len(es) < 3:
        raise ValueError("Invalid ES descriptor")
    offset = 3
    if es[2] & 0x80:
        offset += 2
    if es[2] & 0x40:
        if offset >= len(es):
            raise ValueError("Missing ES URL length")
        offset += 1 + es[offset]
    if es[2] & 0x20:
        offset += 2
    tag, decoder = _descriptor(es, offset)
    if tag != 4 or len(decoder) < 13:
        raise ValueError("Invalid audio decoder descriptor")
    tag, config = _descriptor(decoder, 13)
    if tag != 5 or len(config) < 2 or decoder[0] != 0x40:
        raise ValueError("Unsupported MP4 audio codec")
    audio_type = config[0] >> 3
    if audio_type == 31:
        audio_type = 32 + ((config[0] & 7) << 3) + (config[1] >> 5)
    if not audio_type:
        raise ValueError("Invalid AAC audio object type")
    return f"mp4a.40.{audio_type}"


def aac_mime_codec_from_moov(segment: bytes) -> str | None:
    moov = _child(segment, b"moov")
    if moov is None:
        return None
    for kind, track in _boxes(moov):
        if kind != b"trak":
            continue
        data = track
        for child in (b"mdia", b"minf", b"stbl", b"stsd"):
            data = _child(data, child)
            if data is None:
                break
        if data is None:
            continue
        for kind, entry in _boxes(data[8:]):  # stsd version/flags + count.
            if kind != b"mp4a":
                continue
            if len(entry) < 28:
                raise ValueError("Incomplete audio sample entry")
            version = int.from_bytes(entry[8:10], "big")
            if version not in (0, 1):
                raise ValueError("Unsupported audio sample entry")
            esds = _child(entry[28 if version == 0 else 44:], b"esds")
            if esds is None:
                raise ValueError("Missing AAC configuration")
            return _codec_from_esds(esds)
    return None
