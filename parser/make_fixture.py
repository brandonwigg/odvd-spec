#!/usr/bin/env python3
"""Build hand-crafted minimal DVD IFO fixtures for the ODVD parser.

Writes two synthetic discs under fixtures/:

  fixtures/disc1/VIDEO_TS/  -- NTSC, 1 VTS, 1 title, 2 chapters,
      VMGM menu, First Play -> JumpTT 1, one hidden PGC, EN/ES AC-3
      audio, EN subtitles (one forced).
  fixtures/disc2/VIDEO_TS/  -- PAL, 1 VTS, 2 titles (title 1 spans
      two PGCs), angle block, still cells, VTSM menu with a nav
      timer, a type-7 unsupported VM command, a command-only PGC,
      and a deliberately missing VTS_01_0.BUP.

Every byte is built from the same offsets the parser reads
(ifo.py), so a fixture that parses proves the parser and the
fixture agree on the on-disc layout.  The fixtures are NOT real
discs: they carry no VOBs, so segment hashes stay null and button
geometry is absent by design.

Usage:  python3 make_fixture.py   (writes fixtures/ next to this file)
"""

from __future__ import annotations

import os
import struct
import sys

SECTOR = 2048


def u16(n: int) -> bytes:
    return struct.pack(">H", n)


def u32(n: int) -> bytes:
    return struct.pack(">I", n)


def _bcd(n: int) -> int:
    return ((n // 10) << 4) | (n % 10)


def bcd_time(h=0, m=0, s=0, f=0, pal=False) -> bytes:
    return bytes([_bcd(h), _bcd(m), _bcd(s),
                  ((0x40 if pal else 0xC0) | _bcd(f))])


# ---------------------------------------------------------------- VM commands

def cmd_jump_tt(ttn):      return bytes([0x30, 0x02, 0, 0, 0, ttn, 0, 0])
def cmd_jump_vts_ptt(ttn, pttn):
    return bytes([0x30, 0x05]) + u16(pttn & 0x3FF) + bytes([0, ttn, 0, 0])
def cmd_jump_ss_vmgm_menu(menu=2):
    return bytes([0x30, 0x06, 0, 0, 0, 0x40 | menu, 0, 0])
def cmd_call_ss_vts_menu(ttn, vts, menu=3, rsm=0):
    return bytes([0x30, 0x08, ttn, vts, rsm, 0x80 | menu, 0, 0])
def cmd_link_top_pgc():    return bytes([0x20, 0x01, 0, 0, 0, 0, 0, 9])
def cmd_link_next_pgc():   return bytes([0x20, 0x01, 0, 0, 0, 0, 0, 10])
def cmd_link_tail_pgc():   return bytes([0x20, 0x01, 0, 0, 0, 0, 0, 13])
def cmd_exit():             return bytes([0x30, 0x01, 0, 0, 0, 0, 0, 0])
def cmd_break():            return bytes([0x00, 0x02, 0, 0, 0, 0, 0, 0])
def cmd_nop():              return bytes(8)
def cmd_set_gprm_imm(gprm, value):
    return bytes([0x71, 0x00, 0x00, gprm]) + u16(value) + bytes([0, 0])
def cmd_set_gprm_reg(gprm, src):
    return bytes([0x61, 0x00, 0x00, gprm, 0x00, src, 0, 0])
def cmd_set_gprm_guarded(left_gprm, value_cmp, dest_gprm, value):
    # if (GPRM<left> == <value_cmp>) GPRM<dest> = <value>
    return (bytes([0x71, 0xA0, left_gprm, dest_gprm]) + u16(value)
            + u16(value_cmp))
def cmd_set_stn_audio_imm(stream):
    return bytes([0x51, 0x00, 0x00, 0x80 | stream, 0, 0, 0, 0])
def cmd_set_gprmmd_imm(gprm, value, counter=False):
    return (bytes([0x53, 0x00]) + u16(value)
            + bytes([0, (0x80 if counter else 0) | gprm, 0, 0]))
def cmd_set_nvtmr_imm(seconds, pgcn):
    return bytes([0x52, 0x00]) + u16(seconds) + bytes([0, pgcn, 0, 0])
def cmd_unsupported_type7():
    return bytes([0xE0, 0, 0, 0, 0, 0, 0, 0])


# ---------------------------------------------------------------- PGC

def audio_ctl_present(*streams):
    """PGC audio control: entry = 0x8000 | (stream << 8) when present."""
    out = []
    for i in range(8):
        out.append(0x8000 | (i << 8) if i in streams else 0)
    return out


def subp_ctl_present(*streams):
    """PGC subpicture control: each mode byte = 0x80 | stream."""
    out = []
    for i in range(32):
        if i in streams:
            out.append((0x80 | i) * 0x01010101)
        else:
            out.append(0)
    return out


def build_pgc(*, programs, cells, pre=(), post=(), cell_cmds=(),
              playback_time=(0, 0, 0, 0), pal=False, next_pgc=0,
              prev_pgc=0, goup_pgc=0, still_time=0,
              audio_ctl=None, subp_ctl=None):
    """programs: [first_cell_1based, ...]; cells: list of dicts."""
    audio_ctl = audio_ctl if audio_ctl is not None else [0] * 8
    subp_ctl = subp_ctl if subp_ctl is not None else [0] * 32
    nr_programs = len(programs)
    nr_cells = len(cells)
    cmd_tbl = bytearray()
    cmd_tbl += u16(len(pre)) + u16(len(post)) + u16(len(cell_cmds)) + u16(0)
    for c in list(pre) + list(post) + list(cell_cmds):
        assert len(c) == 8, c
        cmd_tbl += c
    last_byte = 8 + 8 * (len(pre) + len(post) + len(cell_cmds)) - 1
    cmd_tbl[6:8] = u16(last_byte)

    header_size = 0xEC
    cmd_off = header_size if cmd_tbl else 0
    pgmap_off = header_size + len(cmd_tbl) if programs else 0
    cellpb_off = pgmap_off + len(programs) if programs else 0
    if not programs:
        cellpb_off = header_size + len(cmd_tbl) if cells else 0
    cellpos_off = cellpb_off + 24 * nr_cells if cells else 0

    hdr = bytearray(header_size)
    hdr[0x02] = nr_programs
    hdr[0x03] = nr_cells
    hdr[0x04:0x08] = bcd_time(*playback_time, pal=pal)
    for i, v in enumerate(audio_ctl):
        hdr[0x0C + i * 2:0x0E + i * 2] = u16(v)
    for i, v in enumerate(subp_ctl):
        hdr[0x1C + i * 4:0x20 + i * 4] = u32(v)
    hdr[0x9C:0x9E] = u16(next_pgc)
    hdr[0x9E:0xA0] = u16(prev_pgc)
    hdr[0xA0:0xA2] = u16(goup_pgc)
    hdr[0xA2] = still_time
    # Palette: 16 YCbCr entries; entry 0 black, 1 white, 2 red-ish.
    palette = [(16, 128, 128), (235, 128, 128), (82, 90, 240)] + \
        [(16, 128, 128)] * 13
    for i, (y, cb, cr) in enumerate(palette):
        hdr[0xA4 + i * 4:0xA8 + i * 4] = bytes([y, cb, cr, 0])
    hdr[0xE4:0xE6] = u16(cmd_off)
    hdr[0xE6:0xE8] = u16(pgmap_off)
    hdr[0xE8:0xEA] = u16(cellpb_off)
    hdr[0xEA:0xEC] = u16(cellpos_off)

    out = bytes(hdr) + bytes(cmd_tbl)
    out += bytes(programs)
    for c in cells:
        dur = c.get("dur", (0, 0, 10, 0))
        cat = ((c.get("block_mode", 0) & 3) << 6) | \
              ((c.get("block_type", 0) & 3) << 4)
        out += bytes([
            cat, 0x00, c.get("still_time", 0), c.get("cell_cmd_nr", 0),
        ]) + bcd_time(*dur, pal=pal)
        out += u32(c.get("first_sector", 0))
        out += u32(c.get("ilvu_end", 0))
        out += u32(c.get("last_vobu_start", 0))
        out += u32(c.get("last_sector", 99))
    for i, c in enumerate(cells):
        out += u16(c.get("vob_id", 1)) + bytes([0, c.get("cell_id", i + 1)])
    return out


def _pad_to_sector(buf: bytearray) -> bytearray:
    rem = len(buf) % SECTOR
    if rem:
        buf += bytes(SECTOR - rem)
    return buf


def _place(buf: bytearray, sector: int, payload: bytes) -> None:
    off = sector * SECTOR
    if len(buf) < off + len(payload):
        buf.extend(bytes(off + len(payload) - len(buf)))
    buf[off:off + len(payload)] = payload


# ---------------------------------------------------------------- VMGI

def build_vmgi(*, nr_vts, titles, fp_pgc, menu_units, pal=False,
               provider=b"ODVD-FIXTURE"):
    """titles: [(angles, chapters, vts, vts_ttn)]; menu_units:
    [(lang, [(menu_id, entry, pgc_bytes), ...]), ...]."""
    tt_srpt = bytearray()
    tt_srpt += u16(len(titles)) + u16(0) + u32(0)
    for angles, chapters, vts, vts_ttn in titles:
        tt_srpt += bytes([0, angles]) + u16(chapters) + u16(0)
        tt_srpt += bytes([vts, vts_ttn]) + u32(0)
    tt_srpt[4:8] = u32(8 + 12 * len(titles) - 1)

    pgci_ut = bytearray()
    if menu_units:
        pgci_ut += u16(len(menu_units)) + u16(0) + u32(0)
        lu_blobs = []
        for lang, pgcs in menu_units:
            lu = bytearray()
            lu += u16(len(pgcs)) + u16(0) + u32(0)
            pgc_blobs = []
            off = 8 + 8 * len(pgcs)
            for menu_id, entry, pgc in pgcs:
                cat = (0x80000000 if entry else 0) | (menu_id << 24)
                lu += u32(cat) + u32(off)
                pgc_blobs.append(pgc)
                off += len(pgc)
            lu[4:8] = u32(8 + 8 * len(pgcs) + sum(len(p) for p in pgc_blobs) - 1)
            for p in pgc_blobs:
                lu += p
            lu_blobs.append((lang, bytes(lu)))
        ut_off = 8 + 8 * len(menu_units)
        for lang, blob in lu_blobs:
            flags = 0x80  # root menu present
            pgci_ut += lang.encode("ascii") + bytes([0, flags]) + u32(ut_off)
            ut_off += len(blob)
        for _lang, blob in lu_blobs:
            pgci_ut += blob
        pgci_ut[4:8] = u32(len(pgci_ut) - 1)

    mat = bytearray(0x400)
    mat[0:12] = b"DVDVIDEO-VMG"
    mat[0x20:0x22] = b"\x00\x11"
    mat[0x26:0x28] = u16(1)
    mat[0x28:0x2A] = u16(1)
    mat[0x2A] = 1
    mat[0x3E:0x40] = u16(nr_vts)
    mat[0x40:0x40 + len(provider)] = provider
    mat[0x80:0x84] = u32(0x400)
    mat[0x84:0x88] = u32(0x400 if fp_pgc else 0)
    mat[0xC4:0xC8] = u32(1)          # TT_SRPT sector
    mat[0xC8:0xCC] = u32(2 if menu_units else 0)  # VMGM_PGCI_UT sector
    mat[0x100] = 0x40 if not pal else 0x50  # MPEG-2, NTSC/PAL, 4:3
    mat[0x101] = 0x00

    buf = bytearray(mat)
    if fp_pgc:
        _place(buf, 0, b"")  # ensure sector 0 exists
        buf[0x400:0x400 + len(fp_pgc)] = fp_pgc
    _place(buf, 1, bytes(tt_srpt))
    if menu_units:
        _place(buf, 2, bytes(pgci_ut))
    _pad_to_sector(buf)
    nsec = len(buf) // SECTOR
    buf[0x0C:0x10] = u32(nsec - 1)
    buf[0x1C:0x20] = u32(nsec - 1)
    return bytes(buf)


# ---------------------------------------------------------------- VTSI

def audio_attr(codec, channels, lang, code_ext=1):
    """8-byte VTS audio attribute. codec: 0 AC-3, 4 LPCM, 6 DTS."""
    b0 = ((codec & 7) << 5) | (1 << 2)  # language code present
    b1 = ((channels - 1) & 7)
    return bytes([b0, b1]) + lang.encode("ascii") + bytes([0, code_ext, 0, 0])


def subp_attr(lang, code_ext=1):
    return bytes([0x01, 0]) + lang.encode("ascii") + bytes([0, code_ext])


def build_vtsi(*, vts_no, ptt_titles, pgcs, menu_units, audio_attrs,
               subp_attrs, pal=False):
    """ptt_titles: [[(pgcn, pgn), ...], ...] per VTS title (1-based).
    pgcs: [(entry_bool, pgc_bytes), ...].  menu_units as in VMGI."""
    ptt = bytearray()
    ptt += u16(len(ptt_titles)) + u16(0) + u32(0)
    offsets = []
    blobs = bytearray()
    for entries in ptt_titles:
        offsets.append(8 + 4 * len(ptt_titles) + len(blobs))
        for pgcn, pgn in entries:
            blobs += u16(pgcn) + u16(pgn)
    for off in offsets:
        ptt += u32(off)
    ptt += blobs
    ptt[4:8] = u32(len(ptt) - 1)

    pgcit = bytearray()
    pgcit += u16(len(pgcs)) + u16(0) + u32(0)
    off = 8 + 8 * len(pgcs)
    for entry, pgc in pgcs:
        pgcit += bytes([0x80 if entry else 0x00, 0x00]) + u16(0) + u32(off)
        off += len(pgc)
    for _entry, pgc in pgcs:
        pgcit += pgc
    pgcit[4:8] = u32(len(pgcit) - 1)

    menu_ut = bytearray()
    if menu_units:
        menu_ut += u16(len(menu_units)) + u16(0) + u32(0)
        lu_blobs = []
        for lang, m_pgcs in menu_units:
            lu = bytearray()
            lu += u16(len(m_pgcs)) + u16(0) + u32(0)
            blobs2 = []
            off2 = 8 + 8 * len(m_pgcs)
            for menu_id, entry, pgc in m_pgcs:
                cat = (0x80000000 if entry else 0) | (menu_id << 24)
                lu += u32(cat) + u32(off2)
                blobs2.append(pgc)
                off2 += len(pgc)
            lu[4:8] = u32(8 + 8 * len(m_pgcs) + sum(len(p) for p in blobs2) - 1)
            for p in blobs2:
                lu += p
            lu_blobs.append((lang, bytes(lu)))
        ut_off = 8 + 8 * len(menu_units)
        for lang, blob in lu_blobs:
            menu_ut += lang.encode("ascii") + bytes([0, 0x80]) + u32(ut_off)
            ut_off += len(blob)
        for _lang, blob in lu_blobs:
            menu_ut += blob
        menu_ut[4:8] = u32(len(menu_ut) - 1)

    mat = bytearray(0x400)
    mat[0:12] = b"DVDVIDEO-VTS"
    mat[0x20:0x22] = b"\x00\x11"
    mat[0xC8:0xCC] = u32(1)   # VTS_PTT_SRPT
    mat[0xCC:0xD0] = u32(2)   # VTS_PGCIT
    mat[0xD0:0xD4] = u32(3 if menu_units else 0)  # VTSM_PGCI_UT
    mat[0x100] = 0x40 if not pal else 0x50
    mat[0x102:0x104] = u16(0)
    mat[0x154:0x156] = u16(0)
    mat[0x200] = 0x40 if not pal else 0x50
    mat[0x202:0x204] = u16(len(audio_attrs))
    for i, a in enumerate(audio_attrs):
        mat[0x204 + i * 8:0x20C + i * 8] = a
    mat[0x254:0x256] = u16(len(subp_attrs))
    for i, s in enumerate(subp_attrs):
        mat[0x256 + i * 6:0x25C + i * 6] = s

    buf = bytearray(mat)
    _place(buf, 1, bytes(ptt))
    _place(buf, 2, bytes(pgcit))
    if menu_units:
        _place(buf, 3, bytes(menu_ut))
    _pad_to_sector(buf)
    nsec = len(buf) // SECTOR
    buf[0x0C:0x10] = u32(nsec - 1)
    buf[0x1C:0x20] = u32(nsec - 1)
    return bytes(buf)


# ---------------------------------------------------------------- discs

def build_disc1(root: str):
    vts_dir = os.path.join(root, "VIDEO_TS")
    os.makedirs(vts_dir, exist_ok=True)

    fp = build_pgc(programs=[], cells=[], pre=[cmd_jump_tt(1)],
                   playback_time=(0, 0, 0, 0))
    vmgm_menu = build_pgc(
        programs=[1], cells=[{"dur": (0, 0, 10, 0), "still_time": 255}],
        pre=[cmd_set_gprm_imm(0, 1)], post=[cmd_link_top_pgc()],
        playback_time=(0, 0, 10, 0))
    vmgi = build_vmgi(
        nr_vts=1, titles=[(1, 2, 1, 1)], fp_pgc=fp,
        menu_units=[("en", [(2, True, vmgm_menu)])])

    title_pgc = build_pgc(
        programs=[1, 3],
        cells=[{"dur": (0, 1, 0, 0), "first_sector": 0, "last_sector": 100},
               {"dur": (0, 0, 30, 0), "first_sector": 101, "last_sector": 150},
               {"dur": (0, 1, 30, 0), "first_sector": 151, "last_sector": 300}],
        pre=[cmd_set_gprm_imm(0, 1),
             cmd_set_gprm_guarded(3, 2, 0, 1)],
        post=[cmd_jump_ss_vmgm_menu(2)],
        playback_time=(0, 3, 0, 0),
        audio_ctl=audio_ctl_present(0, 1),
        subp_ctl=subp_ctl_present(0, 1))
    hidden_pgc = build_pgc(
        programs=[1], cells=[{"dur": (0, 0, 5, 0)}],
        pre=[cmd_nop()], post=[cmd_exit()], playback_time=(0, 0, 5, 0))
    vtsi = build_vtsi(
        vts_no=1, ptt_titles=[[(1, 1), (1, 2)]],
        pgcs=[(True, title_pgc), (False, hidden_pgc)],
        menu_units=[],
        audio_attrs=[audio_attr(0, 6, "en"), audio_attr(0, 2, "es")],
        subp_attrs=[subp_attr("en", 1), subp_attr("en", 9)])

    for name, blob in (("VIDEO_TS.IFO", vmgi), ("VIDEO_TS.BUP", vmgi),
                       ("VTS_01_0.IFO", vtsi), ("VTS_01_0.BUP", vtsi)):
        with open(os.path.join(vts_dir, name), "wb") as fh:
            fh.write(blob)
    return vts_dir


def build_disc2(root: str):
    vts_dir = os.path.join(root, "VIDEO_TS")
    os.makedirs(vts_dir, exist_ok=True)

    fp = build_pgc(programs=[], cells=[], pre=[cmd_jump_ss_vmgm_menu(2)],
                   playback_time=(0, 0, 0, 0))
    vmgm_menu = build_pgc(
        programs=[1], cells=[{"dur": (0, 0, 10, 0), "still_time": 255}],
        post=[cmd_link_top_pgc()], playback_time=(0, 0, 10, 0), pal=True)
    vmgi = build_vmgi(
        nr_vts=1, titles=[(1, 3, 1, 1), (1, 1, 1, 2)], fp_pgc=fp,
        menu_units=[("en", [(2, True, vmgm_menu)])], pal=True)

    pgc1 = build_pgc(
        programs=[1, 3],
        cells=[{"dur": (0, 0, 30, 0), "block_mode": 1, "block_type": 1,
                "first_sector": 0, "last_sector": 50},
               {"dur": (0, 0, 30, 0), "block_mode": 3, "block_type": 1,
                "first_sector": 51, "last_sector": 100},
               {"dur": (0, 0, 45, 0), "first_sector": 101, "last_sector": 200},
               {"dur": (0, 0, 10, 0), "still_time": 5,
                "first_sector": 201, "last_sector": 220}],
        pre=[cmd_set_stn_audio_imm(0)],
        post=[cmd_link_tail_pgc()],
        playback_time=(0, 1, 25, 0), pal=True,
        audio_ctl=audio_ctl_present(0, 1),
        subp_ctl=subp_ctl_present(0))
    pgc2 = build_pgc(
        programs=[1], cells=[{"dur": (0, 2, 0, 0)}],
        pre=[cmd_set_gprmmd_imm(2, 60, counter=True)],
        post=[cmd_call_ss_vts_menu(1, 1, menu=3)],
        playback_time=(0, 2, 0, 0), pal=True)
    pgc3 = build_pgc(
        programs=[1],
        cells=[{"dur": (0, 1, 0, 0)}, {"dur": (0, 1, 0, 0)}],
        pre=[cmd_unsupported_type7()],
        post=[cmd_link_next_pgc()],
        playback_time=(0, 2, 0, 0), pal=True)
    pgc4 = build_pgc(
        programs=[], cells=[],
        pre=[cmd_set_gprm_reg(1, 0x81)], post=[cmd_break()],
        playback_time=(0, 0, 0, 0), pal=True)
    # PGC5 is the genuinely hidden one: nothing references it, and
    # nothing links to it (PGC4's post is Break, not LinkNextPGC).
    pgc5 = build_pgc(
        programs=[1], cells=[{"dur": (0, 0, 7, 0)}],
        pre=[cmd_nop()], post=[cmd_exit()],
        playback_time=(0, 0, 7, 0), pal=True)
    vtsm_menu = build_pgc(
        programs=[1], cells=[{"dur": (0, 0, 8, 0)}],
        pre=[cmd_set_nvtmr_imm(30, 1)],
        post=[cmd_link_top_pgc()],
        playback_time=(0, 0, 8, 0), pal=True)
    vtsi = build_vtsi(
        vts_no=1,
        ptt_titles=[[(1, 1), (1, 2), (2, 1)], [(3, 1)]],
        pgcs=[(True, pgc1), (False, pgc2), (True, pgc3), (False, pgc4),
              (False, pgc5)],
        menu_units=[("en", [(3, True, vtsm_menu)])],
        audio_attrs=[audio_attr(0, 2, "en"), audio_attr(4, 2, "en")],
        subp_attrs=[subp_attr("fr", 1)],
        pal=True)

    # Deliberately NO VTS_01_0.BUP: exercises the missing-BUP warning.
    for name, blob in (("VIDEO_TS.IFO", vmgi), ("VIDEO_TS.BUP", vmgi),
                       ("VTS_01_0.IFO", vtsi)):
        with open(os.path.join(vts_dir, name), "wb") as fh:
            fh.write(blob)
    return vts_dir


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "fixtures")
    for disc, builder in (("disc1", build_disc1), ("disc2", build_disc2)):
        d = os.path.join(out, disc)
        if os.path.exists(d):
            import shutil
            shutil.rmtree(d)
        builder(d)
        print(f"wrote {d}/VIDEO_TS/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
