"""Minimal ISO 9660 reader for DVD ISO images (stdlib only).

Extracts the VIDEO_TS folder (IFO/BUP files only) from an
already-decrypted DVD ISO into a temp directory so the IFO parser
can work on it exactly as it would on a VIDEO_TS folder.

What it does:
  * Reads the Primary Volume Descriptor at sector 16, takes the
    volume label and the root directory record.
  * Walks the ISO 9660 directory tree (both-endian extent/size
    fields are read little-endian; Joliet/UDF names are not needed
    because DVD-Video filenames are plain ASCII).
  * Finds VIDEO_TS/ and extracts *.IFO / *.BUP into
    <tmpdir>/VIDEO_TS/.

What it does NOT do (documented v0.1 limits, see README.md):
  * UDF-only images: a DVD ISO is normally an ISO 9660 + UDF 1.02
    bridge, so the ISO 9660 side is present.  A pure-UDF image
    (no Primary Volume Descriptor) is rejected with a clear error
    instead of a half-extracted result.
  * Multi-extent files, Rock Ridge / Joliet long names, and
    interleaved files: not needed for VIDEO_TS IFO/BUP files,
    which are small, single-extent, 8.3-named files.
  * VOB extraction: the v0.1 parser is IFO-only, so VOBs are
    deliberately not extracted (they are hundreds of MB per file
    and the parser does not read them yet).
"""

from __future__ import annotations

import os
import shutil
import tempfile

SECTOR = 2048


class IsoError(Exception):
    pass


def _u32le(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "little")


def _read_sector(fh, sector: int) -> bytes:
    fh.seek(sector * SECTOR)
    data = fh.read(SECTOR)
    if len(data) != SECTOR:
        raise IsoError(f"short read at sector {sector} (truncated ISO?)")
    return data


def _read_extent(fh, sector: int, size: int) -> bytes:
    fh.seek(sector * SECTOR)
    data = fh.read(size)
    if len(data) != size:
        raise IsoError(f"short read: extent sector {sector} size {size}")
    return data


def _parse_dir_records(data: bytes):
    """Yield (name, extent_sector, size, is_dir) for one directory's data."""
    pos = 0
    while pos < len(data):
        rec_len = data[pos]
        if rec_len == 0:
            # Padding to the next sector boundary.
            pos = ((pos // SECTOR) + 1) * SECTOR
            continue
        if pos + rec_len > len(data) or rec_len < 34:
            break
        extent = _u32le(data, pos + 2)
        size = _u32le(data, pos + 10)
        flags = data[pos + 25]
        name_len = data[pos + 32]
        raw_name = data[pos + 33:pos + 33 + name_len]
        if name_len == 1 and raw_name in (b"\x00", b"\x01"):
            name = "." if raw_name == b"\x00" else ".."
        else:
            name = raw_name.decode("ascii", "replace")
            # ISO 9660 file versions: "VIDEO_TS.IFO;1"
            if ";" in name:
                name = name.split(";", 1)[0]
        yield name, extent, size, bool(flags & 0x02)
        pos += rec_len


def _find_dir(fh, root_extent: int, root_size: int, wanted: str):
    data = _read_extent(fh, root_extent, root_size)
    for name, extent, size, is_dir in _parse_dir_records(data):
        if is_dir and name.upper() == wanted.upper():
            return extent, size
    return None


def extract_video_ts(iso_path: str, tmpdir: str | None = None):
    """Extract VIDEO_TS/*.IFO and *.BUP from ``iso_path``.

    Returns (video_ts_dir, volume_label).  Raises IsoError with a
    human-readable reason when the image cannot be used.
    """
    if not os.path.isfile(iso_path):
        raise IsoError(f"not a file: {iso_path}")
    own_tmp = tmpdir is None
    tmpdir = tmpdir or tempfile.mkdtemp(prefix="odvd-iso-")
    out_dir = os.path.join(tmpdir, "VIDEO_TS")
    try:
        with open(iso_path, "rb") as fh:
            pvd = _read_sector(fh, 16)
            if pvd[0] != 1 or pvd[1:6] != b"CD001":
                raise IsoError(
                    "no ISO 9660 Primary Volume Descriptor at sector 16 "
                    "(this image may be UDF-only; extract its VIDEO_TS "
                    "folder with another tool and pass the folder instead)")
            label = pvd[40:72].decode("ascii", "replace").strip()
            root = pvd[156:156 + 34]
            root_extent = _u32le(root, 2)
            root_size = _u32le(root, 10)
            found = _find_dir(fh, root_extent, root_size, "VIDEO_TS")
            if found is None:
                raise IsoError("ISO has no VIDEO_TS directory at its root")
            vts_extent, vts_size = found
            vts_data = _read_extent(fh, vts_extent, vts_size)
            entries = [(n, e, s, d) for n, e, s, d in
                       _parse_dir_records(vts_data) if not d]
            ifo_names = [n for n, _e, _s, _d in entries
                         if n.upper().endswith((".IFO", ".BUP"))]
            if not ifo_names:
                raise IsoError("VIDEO_TS directory in the ISO contains "
                               "no .IFO files")
            os.makedirs(out_dir, exist_ok=True)
            for name, extent, size, _d in entries:
                if not name.upper().endswith((".IFO", ".BUP")):
                    continue
                try:
                    blob = _read_extent(fh, extent, size)
                except IsoError:
                    if name.upper().endswith(".BUP"):
                        # A .BUP is only a fallback copy.  If it is
                        # not readable (e.g. the ISO is still
                        # downloading and the BUP extent is past the
                        # bytes on disk so far), skip it: the IFO
                        # parser warns "BUP is missing" downstream
                        # instead of failing the whole parse.  A
                        # short .IFO read stays fatal.
                        continue
                    raise
                with open(os.path.join(out_dir, name.upper()), "wb") as out:
                    out.write(blob)
    except Exception:
        if own_tmp:
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return out_dir, (label or None)
