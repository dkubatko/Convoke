"""Tiny synthetic media used by capability probes. Built deterministically in
code (no binary blobs in the repo): a 16×16 red PNG and 0.2s of silent WAV —
just enough for an endpoint to prove it accepts image/audio input."""

import struct
import zlib


def _png(width: int = 16, height: int = 16, rgb: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _silent_wav(seconds: float = 0.2, rate: int = 16000) -> bytes:
    data = b"\x00\x00" * int(seconds * rate)  # mono 16-bit PCM
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


TEST_PNG = _png()
TEST_WAV = _silent_wav()
