"""DVD IFO file parser (VMGI + VTSI), stdlib only.

Reads VIDEO_TS.IFO and VTS_XX_0.IFO (falling back to the matching
.BUP when the .IFO is missing or has a bad magic) and returns plain
dicts describing titles, PGCs, programs, cells, chapters, video /
audio / subtitle attributes and VM commands.

All multi-byte values in IFO files are big-endian; table pointers
in the management table are sectors (2048 bytes) from the start of
the file, except the First Play PGC pointer which is a byte offset.

Field offsets verified against:
  * dvdnav.mplayerhq.hu/dvdinfo/ifo.html (VMG/VTS IFO headers)
  * freemkv.org/docs/dvd/ (IFO structures, PGC fields, cell
    playback, command tables, TT_SRPT / VTS_PTT_SRPT / VTS_PGCIT)
  * dvd.sourceforge.net/dvdinfo/ifo_vts.html (PGCI_UT layout)
  * libdvdread ifo_types.h / ifo_read.c (structure field order)

Deliberately NOT parsed in v0.1 (see README.md): button geometry
(lives in VOB PCI packets, not in the IFO), TXTDT_MG text data
(chapter/title names), time maps, C_ADT/VOBU_ADMAP beyond a
presence check.
"""

from __future__ import annotations

import os

from vm import decode_command_list

SECTOR = 2048

AUDIO_CODECS = {
    0: "ac3", 1: "unknown", 2: "mpeg1", 3: "mpeg2",
    4: "lpcm", 5: "unknown", 6: "dts", 7: "unknown",
}

AUDIO_CODE_EXT = {
    0: "unspecified", 1: "normal", 2: "visually_impaired",
    3: "directors_comments", 4: "alternate_directors_comments",
}

SUBP_CODE_EXT = {
    0: "unspecified", 1: "normal", 2: "large", 3: "children",
    5: "normal_captions", 6: "large_captions",
    7: "childrens_captions", 9: "forced",
    13: "directors_comments", 14: "large_directors_comments",
    15: "directors_comments_children",
}

MENU_ID_NAMES = {
    0: "none", 1: "unknown1", 2: "title", 3: "root",
    4: "subpicture", 5: "audio", 6: "angle", 7: "chapter",
}

RESOLUTIONS = {0: (720, None), 1: (704, None), 2: (352, None), 3: (352, None)}


class IfoError(Exception):
    pass


def u16(data: bytes, off: int) -> int:
    return (data[off] << 8) | data[off + 1]


def u32(data: bytes, off: int) -> int:
    return (data[off] << 24) | (data[off + 1] << 16) | (data[off + 2] << 8) | data[off + 3]


def _bcd(byte: int) -> int:
    hi, lo = (byte >> 4) & 0xF, byte & 0xF
    if hi > 9 or lo > 9:
        return -1
    return hi * 10 + lo


def dvd_time(raw4: bytes, fps_default: float):
    """Decode a 4-byte BCD DVD time.

    Returns (seconds, "HH:MM:SS:FF", fps).  Byte 3 top two bits
    give the frame rate: 01 = 25 fps, 11 = 30000/1001 fps.

    Conversion follows libdvdnav's dvdnav_convert_time: the
    hours/minutes/seconds fields are literal seconds and the frame
    field is added separately (frames / fps).  This is NOT the
    frame-count interpretation (which reads the whole timecode as
    a frame count and inflates every NTSC duration by 0.1%);
    freemkv's BCD-time note flags that interpretation as
    unsettled and warns against applying it globally.
    """
    h, m, s = _bcd(raw4[0]), _bcd(raw4[1]), _bcd(raw4[2])
    if h < 0 or m < 0 or s < 0:
        return 0.0, "00:00:00:00", fps_default
    fr_bits = (raw4[3] >> 6) & 0x3
    fps = {1: 25.0, 3: 30000 / 1001}.get(fr_bits, fps_default)
    frames = _bcd(raw4[3] & 0x3F)
    if frames < 0:
        frames = 0
    seconds = h * 3600 + m * 60 + s + frames / fps
    tc = f"{h:02d}:{m:02d}:{s:02d}:{frames:02d}"
    return round(seconds, 3), tc, fps


def _timecode_from_seconds(seconds: float, fps: float) -> str:
    total_frames = int(round(seconds * fps))
    rate = 25 if fps == 25.0 else 30
    frames = total_frames % rate
    total_s = total_frames // rate
    return f"{total_s // 3600:02d}:{(total_s % 3600) // 60:02d}:{total_s % 60:02d}:{frames:02d}"


def parse_video_attr(b0: int, b1: int) -> dict:
    fmt = (b0 >> 4) & 0x3
    standard = {0: "NTSC", 1: "PAL"}.get(fmt, "unknown")
    aspect = {0: "4:3", 3: "16:9"}.get((b0 >> 2) & 0x3, "unknown")
    res_code = (b1 >> 4) & 0x3
    if standard == "NTSC":
        width, height = {0: 720, 1: 704, 2: 352, 3: 352}[res_code], \
            {0: 480, 1: 480, 2: 480, 3: 240}[res_code]
        fps = 30000 / 1001
    elif standard == "PAL":
        width, height = {0: 720, 1: 704, 2: 352, 3: 352}[res_code], \
            {0: 576, 1: 576, 2: 576, 3: 288}[res_code]
        fps = 25.0
    else:
        width = height = None
        fps = None
    return {
        "mpeg_version": (b0 >> 6) & 0x3,
        "video_standard": standard,
        "aspect_ratio": aspect,
        "resolution": {"width": width, "height": height},
        "fps": fps,
        "letterboxed": bool(b1 & 0x08),
        "raw_hex": f"{b0:02x}{b1:02x}",
    }


def parse_audio_attr(raw8: bytes) -> dict:
    b0, b1 = raw8[0], raw8[1]
    codec = AUDIO_CODECS[(b0 >> 5) & 0x7]
    lang_type = (b0 >> 2) & 0x3
    lang = None
    if lang_type == 1:
        try:
            lang = raw8[2:4].decode("ascii")
        except UnicodeDecodeError:
            lang = None
        if lang in ("", "\x00\x00", "\xff\xff"):
            lang = None
    sample_rate = 96000 if ((b1 >> 4) & 0x3) == 1 else 48000
    return {
        "codec": codec,
        "channels": (b1 & 0x7) + 1,
        "sample_rate_hz": sample_rate,
        "language": lang,
        "language_type": lang_type,
        "application_mode": b0 & 0x3,
        "multichannel_extension": bool(b0 & 0x10),
        "code_extension": raw8[5],
        "code_extension_name": AUDIO_CODE_EXT.get(raw8[5], "unknown"),
        "bitrate_kbps": None,  # not carried in the IFO; needs the VOB
    }


def parse_subp_attr(raw6: bytes) -> dict:
    lang_type = raw6[0] & 0x3
    lang = None
    if lang_type == 1:
        try:
            lang = raw6[2:4].decode("ascii")
        except UnicodeDecodeError:
            lang = None
        if lang in ("", "\x00\x00", "\xff\xff"):
            lang = None
    code_ext = raw6[5]
    return {
        "language": lang,
        "language_type": lang_type,
        "code_extension": code_ext,
        "code_extension_name": SUBP_CODE_EXT.get(code_ext, "unknown"),
        "forced": code_ext == 9,
    }


def _ycbcr_to_rgb_hex(y: int, cb: int, cr: int) -> str:
    """BT.601 limited-range YCbCr -> #RRGGBB (menu palette)."""
    yy = max(0, (y - 16)) * 255 / 219
    r = yy + 1.402 * (cr - 128)
    g = yy - 0.34414 * (cb - 128) - 0.71414 * (cr - 128)
    b = yy + 1.772 * (cb - 128)
    clamp = lambda v: max(0, min(255, int(round(v))))
    return f"#{clamp(r):02x}{clamp(g):02x}{clamp(b):02x}"


# ---------------------------------------------------------------- PGC

def parse_pgc(data: bytes, base: int, fps_default: float):
    """Parse one PGC at file offset ``base``.

    Returns (pgc_dict, unsupported, partial, warnings).
    """
    warnings = []
    if base + 0xEC > len(data):
        raise IfoError(f"PGC at offset {base} runs past end of IFO "
                       f"({len(data)} bytes)")
    nr_programs = data[base + 0x02]
    nr_cells = data[base + 0x03]
    pgc_dur, _tc, _fps = dvd_time(data[base + 0x04:base + 0x08], fps_default)
    prohibited_ops = u32(data, base + 0x08)

    audio_control = []
    for i in range(8):
        entry = u16(data, base + 0x0C + i * 2)
        audio_control.append({
            "raw": entry,
            "present": bool(entry & 0x8000),
            "stream": (entry >> 8) & 0x07,
        })
    subp_control = []
    for i in range(32):
        off = base + 0x1C + i * 4
        modes = {}
        for name, byte in (("4:3", data[off]), ("wide", data[off + 1]),
                           ("letterbox", data[off + 2]),
                           ("pan_scan", data[off + 3])):
            modes[name] = {"present": bool(byte & 0x80),
                           "stream": byte & 0x1F}
        subp_control.append(modes)

    next_pgc = u16(data, base + 0x9C)
    prev_pgc = u16(data, base + 0x9E)
    goup_pgc = u16(data, base + 0xA0)
    still_time = data[base + 0xA2]
    playback_mode = data[base + 0xA3]

    palette = []
    for i in range(16):
        off = base + 0xA4 + i * 4
        y, cb, cr = data[off], data[off + 1], data[off + 2]
        palette.append(_ycbcr_to_rgb_hex(y, cb, cr))

    cmd_off = u16(data, base + 0xE4)
    pgmap_off = u16(data, base + 0xE6)
    cellpb_off = u16(data, base + 0xE8)
    cellpos_off = u16(data, base + 0xEA)

    unsupported, partial = [], []
    pre_cmds, post_cmds, cell_cmds = [], [], []
    if cmd_off:
        tbl = base + cmd_off
        if tbl + 8 > len(data):
            warnings.append(f"PGC command table at {tbl} runs past end of IFO")
        else:
            nr_pre, nr_post, nr_cell = (u16(data, tbl), u16(data, tbl + 2),
                                        u16(data, tbl + 4))
            total = nr_pre + nr_post + nr_cell
            if total > 128:
                warnings.append(
                    f"PGC declares {total} commands (spec max 128); "
                    "parsing all of them anyway")
            raw = []
            pos = tbl + 8
            for _ in range(total):
                if pos + 8 > len(data):
                    warnings.append("command table truncated by end of IFO")
                    break
                raw.append(data[pos:pos + 8])
                pos += 8
            pre_raw = raw[:nr_pre]
            post_raw = raw[nr_pre:nr_pre + nr_post]
            cell_raw = raw[nr_pre + nr_post:]
            pre_cmds, u1, p1 = decode_command_list(pre_raw, "pre")
            post_cmds, u2, p2 = decode_command_list(post_raw, "post")
            cell_cmds, u3, p3 = decode_command_list(cell_raw, "cell")
            unsupported = u1 + u2 + u3
            partial = p1 + p2 + p3

    program_map = []
    if pgmap_off and nr_programs:
        off = base + pgmap_off
        program_map = [data[off + i] for i in range(nr_programs)
                       if off + i < len(data)]
        if len(program_map) != nr_programs:
            warnings.append("program map truncated by end of IFO")

    cells = []
    if cellpb_off and nr_cells:
        for i in range(nr_cells):
            off = base + cellpb_off + i * 24
            if off + 24 > len(data):
                warnings.append(f"cell playback entry {i + 1} truncated")
                break
            cat = data[off]
            dur, _tc2, _f = dvd_time(data[off + 4:off + 8], fps_default)
            cells.append({
                "index": i + 1,
                "block_mode": (cat >> 6) & 0x3,
                "block_type": (cat >> 4) & 0x3,
                "seamless": bool(cat & 0x08),
                "interleaved": bool(cat & 0x04),
                "stc_discontinuity": bool(cat & 0x02),
                "seamless_angle": bool(cat & 0x01),
                "playback_mode_still_after_vobu": bool(data[off + 1] & 0x80),
                "restricted": bool(data[off + 1] & 0x40),
                "cell_type": data[off + 1] & 0x1F,
                "still_time": data[off + 2],
                "cell_command_nr": data[off + 3],
                "duration_seconds": dur,
                "first_sector": u32(data, off + 8),
                "ilvu_end_sector": u32(data, off + 12),
                "last_vobu_start_sector": u32(data, off + 16),
                "last_sector": u32(data, off + 20),
            })
    if cellpos_off and nr_cells:
        for i in range(nr_cells):
            off = base + cellpos_off + i * 4
            if off + 4 > len(data) or i >= len(cells):
                break
            cells[i]["vob_id"] = u16(data, off)
            cells[i]["cell_id_in_vob"] = data[off + 3]
    if nr_cells == 0 and not (pre_cmds or post_cmds or cell_cmds):
        warnings.append("PGC has zero cells and no commands")
    if nr_programs == 0 and nr_cells:
        warnings.append("PGC has cells but zero programs")
    if nr_programs > nr_cells and nr_cells:
        warnings.append(f"PGC declares {nr_programs} programs but only "
                        f"{nr_cells} cells")

    pgc = {
        "nr_programs": nr_programs,
        "nr_cells": nr_cells,
        "playback_time_seconds": pgc_dur,
        "prohibited_user_ops_raw": prohibited_ops,
        "audio_control": audio_control,
        "subp_control": subp_control,
        "next_pgc": next_pgc or None,
        "prev_pgc": prev_pgc or None,
        "goup_pgc": goup_pgc or None,
        "still_time": still_time,
        "playback_mode": playback_mode,
        "palette": palette,
        "pre_commands": pre_cmds,
        "post_commands": post_cmds,
        "cell_commands": cell_cmds,
        "program_map": program_map,
        "cells": cells,
        "raw_category_bytes": data[base:base + 2].hex(),
    }
    return pgc, unsupported, partial, warnings


def _selected_path_seconds(cells, fps):
    """Sum of cell durations on the selected playback path.

    Only the first cell of an angle/interleaved block plays, so
    later cells of a block contribute no time.
    """
    total = 0.0
    in_block = False
    for c in cells:
        if c["block_mode"] == 1:  # first cell of a block
            total += c["duration_seconds"]
            in_block = True
        elif c["block_mode"] in (2, 3):  # inside / last cell of a block
            in_block = False if c["block_mode"] == 3 else in_block
            continue
        else:
            total += c["duration_seconds"]
            in_block = False
    return round(total, 3)


def _cell_start_times(cells, fps):
    """Start time (seconds) of each cell on the selected path.

    Non-first angle-block cells share the block's start time.
    Returns a list parallel to ``cells``.
    """
    starts = []
    t = 0.0
    block_start = 0.0
    in_block = False
    for c in cells:
        if c["block_mode"] == 1:
            block_start = t
            starts.append(t)
            t += c["duration_seconds"]
            in_block = True
        elif c["block_mode"] in (2, 3):
            starts.append(block_start)
            if c["block_mode"] == 3:
                in_block = False
        else:
            starts.append(t)
            t += c["duration_seconds"]
            in_block = False
    return starts


# ---------------------------------------------------------------- tables

def parse_pgci_ut(data: bytes, sector: int, fps_default: float):
    """Parse a PGCI_UT (menu PGCs, grouped by language unit)."""
    if not sector:
        return []
    base = sector * SECTOR
    if base + 8 > len(data):
        raise IfoError("PGCI_UT sector out of range")
    nr_lus = u16(data, base)
    units = []
    for li in range(nr_lus):
        off = base + 8 + li * 8
        if off + 8 > len(data):
            break
        lang_raw = data[off:off + 2]
        # 0xFFFF / 0x0000 mean "no specific language" on real discs.
        lang = None if lang_raw in (b"\xff\xff", b"\x00\x00") else \
            lang_raw.decode("ascii", "replace")
        menu_flags = data[off + 3]
        lu_off = u32(data, off + 4)
        lu_base = base + lu_off
        if lu_base + 8 > len(data):
            continue
        nr_pgcs = u16(data, lu_base)
        pgcs = []
        for pi in range(nr_pgcs):
            srp = lu_base + 8 + pi * 8
            if srp + 8 > len(data):
                break
            cat = u32(data, srp)
            start = u32(data, srp + 4)
            pgc_base = lu_base + start
            try:
                pgc, uns, par, warns = parse_pgc(data, pgc_base, fps_default)
            except IfoError as exc:
                pgc, uns, par, warns = None, [], [], [str(exc)]
            pgcs.append({
                "pgc_number": pi + 1,
                "category_raw": f"{cat:08x}",
                "entry": bool(cat & 0x80000000),
                "menu_id": (cat >> 24) & 0x0F,
                "menu_name": MENU_ID_NAMES.get((cat >> 24) & 0x0F, "unknown"),
                "pgc": pgc,
                "unsupported": uns,
                "partial": par,
                "warnings": warns,
            })
        units.append({
            "language": lang,
            "menu_existence_flags": menu_flags,
            "menu_existence": {
                "root": bool(menu_flags & 0x80),
                "subpicture": bool(menu_flags & 0x40),
                "audio": bool(menu_flags & 0x20),
                "angle": bool(menu_flags & 0x10),
                "chapter": bool(menu_flags & 0x08),
            },
            "pgcs": pgcs,
        })
    return units


def parse_pgcit(data: bytes, sector: int, fps_default: float):
    """Parse a VTS_PGCIT (title-domain PGCs)."""
    base = sector * SECTOR
    if base + 8 > len(data):
        raise IfoError("VTS_PGCIT sector out of range")
    nr = u16(data, base)
    pgcs = []
    for i in range(nr):
        srp = base + 8 + i * 8
        if srp + 8 > len(data):
            break
        entry_id = data[srp]
        block_mode = (data[srp + 1] >> 6) & 0x3
        block_type = (data[srp + 1] >> 4) & 0x3
        ptl_mask = u16(data, srp + 2)
        start = u32(data, srp + 4)
        pgc_base = base + start
        try:
            pgc, uns, par, warns = parse_pgc(data, pgc_base, fps_default)
        except IfoError as exc:
            pgc, uns, par, warns = None, [], [], [str(exc)]
        pgcs.append({
            "pgc_number": i + 1,
            "entry_id_raw": entry_id,
            "entry": bool(entry_id & 0x80),
            "block_mode": block_mode,
            "block_type": block_type,
            "parental_mask": ptl_mask,
            "pgc": pgc,
            "unsupported": uns,
            "partial": par,
            "warnings": warns,
        })
    return pgcs


def parse_tt_srpt(data: bytes, sector: int):
    base = sector * SECTOR
    if base + 8 > len(data):
        raise IfoError("TT_SRPT sector out of range")
    nr = u16(data, base)
    titles = []
    for i in range(nr):
        off = base + 8 + i * 12
        if off + 12 > len(data):
            break
        titles.append({
            "title_number": i + 1,
            "playback_type_raw": data[off],
            "angles": data[off + 1],
            "chapters": u16(data, off + 2),
            "parental_id": u16(data, off + 4),
            "vts": data[off + 6],
            "vts_ttn": data[off + 7],
            "vts_sector": u32(data, off + 8),
        })
    return titles


def parse_vts_ptt_srpt(data: bytes, sector: int):
    """Returns {vts_ttn: [(pgcn, pgn), ...]} (1-based keys)."""
    base = sector * SECTOR
    if base + 8 > len(data):
        raise IfoError("VTS_PTT_SRPT sector out of range")
    nr = u16(data, base)
    last_byte = u32(data, base + 4)
    offsets = [u32(data, base + 8 + i * 4) for i in range(nr)
               if base + 8 + i * 4 + 4 <= len(data)]
    result = {}
    for ti, off in enumerate(offsets):
        end = offsets[ti + 1] if ti + 1 < len(offsets) else last_byte + 1
        entries = []
        pos = base + off
        while pos + 4 <= base + end and pos + 4 <= len(data):
            entries.append((u16(data, pos), u16(data, pos + 2)))
            pos += 4
        result[ti + 1] = entries
    return result


def parse_c_adt(data: bytes, sector: int):
    if not sector:
        return {"present": False, "cells": 0}
    base = sector * SECTOR
    if base + 8 > len(data):
        return {"present": True, "cells": 0, "error": "sector out of range"}
    return {"present": True, "cells": u16(data, base)}


# ---------------------------------------------------------------- IFO files

def _read_ifo(video_ts_dir: str, stem: str):
    """Read <stem>.IFO, falling back to <stem>.BUP.

    Returns (data, used_backup, warnings).
    """
    warnings = []
    ifo_path = os.path.join(video_ts_dir, stem + ".IFO")
    bup_path = os.path.join(video_ts_dir, stem + ".BUP")
    data = None
    if os.path.exists(ifo_path):
        with open(ifo_path, "rb") as fh:
            data = fh.read()
        if len(data) < 12 or data[:12] not in (b"DVDVIDEO-VMG", b"DVDVIDEO-VTS"):
            warnings.append(f"{stem}.IFO has a bad magic; trying {stem}.BUP")
            data = None
    if data is None:
        if os.path.exists(bup_path):
            with open(bup_path, "rb") as fh:
                data = fh.read()
            if len(data) >= 12 and data[:12] in (b"DVDVIDEO-VMG", b"DVDVIDEO-VTS"):
                warnings.append(f"{stem}.IFO unusable; parsed {stem}.BUP instead")
                return data, True, warnings
        raise IfoError(f"no readable {stem}.IFO or {stem}.BUP in {video_ts_dir}")
    if not os.path.exists(bup_path):
        warnings.append(f"{stem}.BUP is missing (no backup copy on disc)")
    return data, False, warnings


def _mat_pointers(data: bytes):
    return {
        "menu_vob_sector": u32(data, 0xC0),
        "title_vob_sector_or_tt_srpt": u32(data, 0xC4),
        "ptr_0xC8": u32(data, 0xC8),
        "ptr_0xCC": u32(data, 0xCC),
        "ptr_0xD0": u32(data, 0xD0),
        "ptr_0xD4": u32(data, 0xD4),
        "ptr_0xD8": u32(data, 0xD8),
        "ptr_0xDC": u32(data, 0xDC),
        "ptr_0xE0": u32(data, 0xE0),
        "ptr_0xE4": u32(data, 0xE4),
    }


def parse_vmgi(data: bytes):
    """Parse VIDEO_TS.IFO."""
    if data[:12] != b"DVDVIDEO-VMG":
        raise IfoError("VIDEO_TS.IFO does not start with DVDVIDEO-VMG")
    warnings = []
    provider = data[0x40:0x60].decode("ascii", "replace").rstrip("\x00 ").strip()
    nr_vts = u16(data, 0x3E)
    fp_off = u32(data, 0x84)
    ptrs = _mat_pointers(data)
    menu_video = parse_video_attr(data[0x100], data[0x101])
    nr_menu_audio = u16(data, 0x102)
    menu_audio = [parse_audio_attr(data[0x104 + i * 8:0x104 + (i + 1) * 8])
                  for i in range(min(nr_menu_audio, 8))]
    nr_menu_subp = u16(data, 0x154)
    menu_subp = [parse_subp_attr(data[0x156 + i * 6:0x156 + (i + 1) * 6])
                 for i in range(min(nr_menu_subp, 1))]

    fps_default = menu_video["fps"] or (30000 / 1001)
    titles = parse_tt_srpt(data, ptrs["title_vob_sector_or_tt_srpt"]) \
        if ptrs["title_vob_sector_or_tt_srpt"] else []

    first_play = None
    fp_unsupported, fp_partial, fp_warnings = [], [], []
    if fp_off:
        try:
            first_play, fp_unsupported, fp_partial, fp_warnings = parse_pgc(
                data, fp_off, fps_default)
        except IfoError as exc:
            warnings.append(f"First Play PGC unreadable: {exc}")
    else:
        warnings.append("no First Play PGC (VMGI_MAT offset 0x84 is 0)")

    menu_units = []
    if ptrs["ptr_0xC8"]:
        try:
            menu_units = parse_pgci_ut(data, ptrs["ptr_0xC8"], fps_default)
        except IfoError as exc:
            warnings.append(f"VMGM_PGCI_UT unreadable: {exc}")
    if ptrs["ptr_0xD4"]:
        warnings.append("TXTDT_MG text data present but not parsed in "
                        "v0.1 (disc/title/chapter names unavailable)")
    return {
        "provider_id": provider or None,
        "version_raw": data[0x20:0x22].hex(),
        "nr_volumes": u16(data, 0x26),
        "volume_number": u16(data, 0x28),
        "side_id": data[0x2A],
        "region_mask": data[0x23],
        "nr_vts": nr_vts,
        "titles": titles,
        "first_play_pgc": first_play,
        "first_play_unsupported": fp_unsupported,
        "first_play_partial": fp_partial,
        "first_play_warnings": fp_warnings,
        "menu_units": menu_units,
        "menu_video": menu_video,
        "menu_audio": menu_audio,
        "menu_subp": menu_subp,
        "c_adt": parse_c_adt(data, ptrs["ptr_0xD8"]),
        "txtdt_present": bool(ptrs["ptr_0xD4"]),
        "warnings": warnings,
        "pointers": ptrs,
    }


def parse_vtsi(data: bytes, vts_number: int):
    """Parse VTS_XX_0.IFO."""
    if data[:12] != b"DVDVIDEO-VTS":
        raise IfoError(f"VTS_{vts_number:02d}_0.IFO does not start with DVDVIDEO-VTS")
    warnings = []
    ptrs = _mat_pointers(data)
    menu_video = parse_video_attr(data[0x100], data[0x101])
    title_video = parse_video_attr(data[0x200], data[0x201])
    fps_default = title_video["fps"] or menu_video["fps"] or (30000 / 1001)

    nr_menu_audio = u16(data, 0x102)
    menu_audio = [parse_audio_attr(data[0x104 + i * 8:0x104 + (i + 1) * 8])
                  for i in range(min(nr_menu_audio, 8))]
    nr_menu_subp = u16(data, 0x154)
    menu_subp = [parse_subp_attr(data[0x156 + i * 6:0x156 + (i + 1) * 6])
                 for i in range(min(nr_menu_subp, 1))]
    nr_audio = u16(data, 0x202)
    audio = [parse_audio_attr(data[0x204 + i * 8:0x204 + (i + 1) * 8])
             for i in range(min(nr_audio, 8))]
    nr_subp = u16(data, 0x254)
    subp = [parse_subp_attr(data[0x256 + i * 6:0x256 + (i + 1) * 6])
            for i in range(min(nr_subp, 32))]
    if nr_audio > 8:
        warnings.append(f"VTS declares {nr_audio} audio streams (max 8); "
                        "only the first 8 were read")
    if nr_subp > 32:
        warnings.append(f"VTS declares {nr_subp} subpicture streams "
                        "(max 32); only the first 32 were read")

    ptt = {}
    if ptrs["ptr_0xC8"]:
        try:
            ptt = parse_vts_ptt_srpt(data, ptrs["ptr_0xC8"])
        except IfoError as exc:
            warnings.append(f"VTS_PTT_SRPT unreadable: {exc}")
    pgcs = []
    if ptrs["ptr_0xCC"]:
        try:
            pgcs = parse_pgcit(data, ptrs["ptr_0xCC"], fps_default)
        except IfoError as exc:
            warnings.append(f"VTS_PGCIT unreadable: {exc}")
    menu_units = []
    if ptrs["ptr_0xD0"]:
        try:
            menu_units = parse_pgci_ut(data, ptrs["ptr_0xD0"], fps_default)
        except IfoError as exc:
            warnings.append(f"VTSM_PGCI_UT unreadable: {exc}")
    return {
        "vts": vts_number,
        "menu_video": menu_video,
        "title_video": title_video,
        "menu_audio": menu_audio,
        "menu_subp": menu_subp,
        "audio": audio,
        "subp": subp,
        "nr_audio_declared": nr_audio,
        "nr_subp_declared": nr_subp,
        "ptt": ptt,
        "pgcs": pgcs,
        "menu_units": menu_units,
        "c_adt": parse_c_adt(data, ptrs["ptr_0xE0"]),
        "menu_c_adt": parse_c_adt(data, ptrs["ptr_0xD8"]),
        "warnings": warnings,
        "pointers": ptrs,
    }


def parse_video_ts(video_ts_dir: str):
    """Parse a whole VIDEO_TS folder. Returns (vmgi, {vts_no: vtsi})."""
    data, _used_bup, warnings = _read_ifo(video_ts_dir, "VIDEO_TS")
    vmgi = parse_vmgi(data)
    vmgi["warnings"].extend(warnings)
    vts_map = {}
    for n in range(1, vmgi["nr_vts"] + 1):
        stem = f"VTS_{n:02d}_0"
        try:
            vdata, _b, w = _read_ifo(video_ts_dir, stem)
        except IfoError as exc:
            vmgi["warnings"].append(f"{stem}.IFO unreadable: {exc}; "
                                    "its titles will be missing")
            continue
        vtsi = parse_vtsi(vdata, n)
        vtsi["warnings"].extend(w)
        vts_map[n] = vtsi
    # VTS files present on disk but not counted in VMGI_MAT: parse
    # them too (authoring tools sometimes disagree with the count).
    for name in sorted(os.listdir(video_ts_dir)):
        if name.upper().startswith("VTS_") and name.upper().endswith("_0.IFO"):
            try:
                n = int(name[4:6])
            except ValueError:
                continue
            if n not in vts_map and 1 <= n <= 99:
                try:
                    vdata, _b, w = _read_ifo(video_ts_dir, name[:-4])
                    vtsi = parse_vtsi(vdata, n)
                    vtsi["warnings"].extend(w)
                    vts_map[n] = vtsi
                    vmgi["warnings"].append(
                        f"VTS {n} present on disk but VMGI_MAT counts "
                        f"only {vmgi['nr_vts']} title sets; parsed it anyway")
                except IfoError:
                    pass
    return vmgi, vts_map
