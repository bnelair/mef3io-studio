"""Synthetic recordings for the explorer tests, written with mef3io.

Everything is made up: sine waves and zeros, a fixed start in 2000, and an
invented patient. Two helpers edit bytes, which only tests may do (the app
never touches MEF bytes): giving every part a recording-time offset, the
way meflib's deprecated date masking does, and damaging a details file.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import mef3io
import numpy as np

T0 = 946_684_800_000_000  # 2000-01-01 00:00:00 UTC
SECOND = 1_000_000
FIRST, LAST, PATIENT_ID, SITE = "Quentin", "Zebedee", "MRN-77001234", "Ward 9"
PASSWORD_BASIC, PASSWORD_FULL = "basic-pw-1", "full-pw-2"

UH_BYTES = 1024
METADATA_FILE_BYTES = 16384
S3_RTO_OFFSET = 13312  # universal header 1024 + section 1 1536 + section 2 10752


def _koopman_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEB31D82E if c & 1 else c >> 1
        table.append(c)
    return table


_TABLE = _koopman_table()


def crc32(data: bytes, crc: int = 0xFFFFFFFF) -> int:
    """meflib's CRC (Koopman-32, reflected, start value 0xFFFFFFFF)."""
    for b in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ b) & 0xFF]
    return crc


def metadata():
    return mef3io.Metadata(
        subject=mef3io.Subject(name_1=FIRST, name_2=LAST, id=PATIENT_ID, recording_location=SITE)
    )


def write_plain(path: Path, channels=("a1", "a2", "a3"), notes=True) -> Path:
    with mef3io.Writer(str(path), overwrite=True, units="uV", metadata=metadata()) as w:
        for name in channels:
            w.write(name, np.sin(np.arange(2560) / 10.0), T0, 256.0)
        if notes:
            w.write_annotations([{"time": T0 + SECOND, "text": f"{LAST} moved"}])
            w.write_annotations(
                [{"time": T0 + 2 * SECOND, "type": "EDFA", "text": "eyes", "duration": 500_000}],
                channels[0],
            )
    if notes:
        # A part-level notes file, laid out as meflib names it.
        channel = path / f"{channels[0]}.timd"
        part = channel / f"{channels[0]}-000000.segd"
        for suffix in ("rdat", "ridx"):
            shutil.copy(
                channel / f"{channels[0]}.{suffix}", part / f"{channels[0]}-000000.{suffix}"
            )
    return path


def write_gappy(path: Path) -> Path:
    """Two channels: one with a gap inside its only part, one in two parts
    with a gap between them."""
    with mef3io.Writer(str(path), overwrite=True) as w:
        w.write("x", np.sin(np.arange(2560) / 10.0), T0, 256.0)
        w.write("x", np.sin(np.arange(2560) / 10.0), T0 + 20 * SECOND, 256.0)
        w.write("y", np.zeros(5120), T0, 512.0)
        w.write("y", np.zeros(5120), T0 + 60 * SECOND, 512.0, new_segment=True)
    return path


def write_encrypted(path: Path) -> Path:
    with mef3io.Writer(
        str(path),
        overwrite=True,
        password1=PASSWORD_BASIC,
        password2=PASSWORD_FULL,
        metadata=metadata(),
    ) as w:
        w.write("c", np.zeros(2560), T0, 256.0)
    return path


def set_time_offset(path: Path, rto: int) -> Path:
    """Give section 3 of every part this recording-time offset (meflib's
    deprecated date masking), resealing the CRCs over the fixed record."""
    for tmet in sorted(Path(path).glob("*.timd/*.segd/*.tmet")):
        raw = bytearray(tmet.read_bytes())
        struct.pack_into("<q", raw, S3_RTO_OFFSET, rto)
        struct.pack_into("<I", raw, 4, crc32(bytes(raw[UH_BYTES:METADATA_FILE_BYTES])))
        struct.pack_into("<I", raw, 0, crc32(bytes(raw[4:UH_BYTES])))
        tmet.write_bytes(bytes(raw))
    return path


def damage_details(path: Path, every_part: bool = False) -> Path:
    """Flip one byte inside the first part's details file (or every part's:
    then nothing of the recording can be read)."""
    tmets = sorted(Path(path).glob("*.timd/*.segd/*.tmet"))
    for tmet in tmets if every_part else tmets[:1]:
        raw = bytearray(tmet.read_bytes())
        raw[5000] ^= 0xFF
        tmet.write_bytes(bytes(raw))
    return path


def damage_index(path: Path) -> Path:
    """Damage the block table only: the recording opens, the check finds it."""
    tidx = sorted(Path(path).glob("*.timd/*.segd/*.tidx"))[0]
    raw = bytearray(tidx.read_bytes())
    raw[-1] ^= 0xFF
    tidx.write_bytes(bytes(raw))
    return path


def write_large(path: Path, channels: int = 2, parts: int = 2, seconds: int = 1800) -> Path:
    """Noise that compresses badly: about 10 MB per part, like real data."""
    rng = np.random.default_rng(7)
    fs = 2000.0
    with mef3io.Writer(str(path), overwrite=True) as w:
        for part in range(parts):
            for ch in range(channels):
                w.write(
                    f"n{ch}",
                    rng.normal(0, 1000, int(fs * seconds)).round(),
                    T0 + part * (seconds + 60) * SECOND,
                    fs,
                    new_segment=True,
                )
    return path


def write_legacy_password(path: Path) -> tuple[Path, str]:
    """A recording whose password opens it only through the rule mef3io used
    up to 1.2.0 (bnelair/mef3io#84). That rule took the last UTF-8 byte of
    each character: for "é" (C3 A9) that is A9, the byte meflib's rule gives
    "©" (U+00A9). So a recording protected with "©" opens with "é" only
    through the old rule. Returns the path and that legacy password."""
    with mef3io.Writer(
        str(path), overwrite=True, password1="b©1", password2="f©2", metadata=metadata()
    ) as w:
        w.write("c", np.zeros(2560), T0, 256.0)
    return path, "fé2"


def write_encrypted_with_notes(path: Path) -> Path:
    with mef3io.Writer(
        str(path),
        overwrite=True,
        password1=PASSWORD_BASIC,
        password2=PASSWORD_FULL,
        metadata=metadata(),
    ) as w:
        w.write("c", np.zeros(2560), T0, 256.0)
        w.write_annotations([{"time": T0 + SECOND, "text": f"{LAST} moved"}])
    return path
