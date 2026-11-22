#!/usr/bin/env python3
"""Tests for the ODVD v0.1 IFO parser.

Covers, without any real disc:
  1. VM command decode vectors (vm.py).
  2. Fixture disc 1 (NTSC) end-to-end via the CLI.
  3. Fixture disc 2 (PAL, multi-title, angles, hidden PGC,
     unsupported command, missing BUP) end-to-end via the CLI.
  4. ISO input: a minimal ISO 9660 image built here from the
     disc-1 fixture, parsed via the CLI.
  5. Failure mode: a VIDEO_TS folder with no VIDEO_TS.IFO.

Usage:  python3 test_ifo.py            (builds fixtures first)
        python3 test_ifo.py -v         (verbose unittest output)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import make_fixture  # noqa: E402
from vm import decode_command  # noqa: E402

SECTOR = 2048


def run_cli(*args):
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "odvd_parse.py"), *args],
        capture_output=True, text=True, cwd=HERE)


def load_manifest(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def pgcs_of(manifest, vts=1):
    for ts in manifest["titlesets"]:
        if ts["vts"] == vts:
            return {p["id"]: p for p in ts["pgcs"]}
    return {}


# ---------------------------------------------------------------- ISO builder

def _dir_record(name: bytes, extent: int, size: int, is_dir: bool) -> bytes:
    rec_len = 33 + len(name)
    if rec_len % 2:
        rec_len += 1
    rec = bytearray(rec_len)
    rec[0] = rec_len
    struct.pack_into("<I", rec, 2, extent)
    struct.pack_into(">I", rec, 6, extent)
    struct.pack_into("<I", rec, 10, size)
    struct.pack_into(">I", rec, 14, size)
    rec[25] = 0x02 if is_dir else 0x00
    struct.pack_into("<H", rec, 28, 1)
    struct.pack_into(">H", rec, 30, 1)
    rec[32] = len(name)
    rec[33:33 + len(name)] = name
    return bytes(rec)


def _dir_data(entries):
    out = bytearray()
    for name, extent, size, is_dir in entries:
        out += _dir_record(name, extent, size, is_dir)
    out += bytes(SECTOR - (len(out) % SECTOR))
    return bytes(out)


def build_iso(video_ts_dir: str, iso_path: str, volume_label: str):
    """Minimal ISO 9660 image holding one VIDEO_TS directory."""
    files = sorted(n for n in os.listdir(video_ts_dir)
                   if n.upper().endswith((".IFO", ".BUP")))
    blobs = {}
    for name in files:
        with open(os.path.join(video_ts_dir, name), "rb") as fh:
            blobs[name] = fh.read()
    # Sector layout: 16 PVD, 17 root dir, 18 VIDEO_TS dir, 19+ files.
    sector = 19
    file_sectors = {}
    for name in files:
        file_sectors[name] = (sector, blobs[name])
        sector += (len(blobs[name]) + SECTOR - 1) // SECTOR
    root = _dir_data([
        (b"\x00", 17, SECTOR, True),
        (b"\x01", 16, SECTOR, True),
        (b"VIDEO_TS", 18, SECTOR, True),
    ])
    vts_entries = [(b"\x00", 18, SECTOR, True), (b"\x01", 17, SECTOR, True)]
    for name in files:
        ext, blob = file_sectors[name]
        vts_entries.append((f"{name};1".encode("ascii"), ext, len(blob), False))
    vts = _dir_data(vts_entries)

    pvd = bytearray(SECTOR)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    label = volume_label.encode("ascii")[:32].ljust(32, b" ")
    pvd[40:72] = label
    struct.pack_into("<I", pvd, 80, sector)   # volume space size (LE)
    struct.pack_into(">I", pvd, 84, sector)   # volume space size (BE)
    pvd[120:124] = struct.pack("<HH", 1, 1)   # volume set size / seq
    struct.pack_into("<H", pvd, 124, SECTOR)
    struct.pack_into(">H", pvd, 126, SECTOR)
    root_rec = _dir_record(b"\x00", 17, SECTOR, True)
    pvd[156:156 + len(root_rec)] = root_rec

    with open(iso_path, "wb") as fh:
        fh.write(bytes(16 * SECTOR))
        fh.write(bytes(pvd))
        fh.write(root)
        fh.write(vts)
        for name in files:
            ext, blob = file_sectors[name]
            fh.seek(ext * SECTOR)
            fh.write(blob)
            fh.write(bytes((SECTOR - len(blob) % SECTOR) % SECTOR))
    return iso_path


# ---------------------------------------------------------------- tests

class TestVmDecode(unittest.TestCase):
    def check(self, raw, expected_op, **extra):
        obj, status = decode_command(bytes(raw))
        self.assertEqual(status, "ok", obj)
        self.assertEqual(obj["op"], expected_op, obj)
        for k, v in extra.items():
            self.assertEqual(obj.get(k), v, obj)
        return obj

    def test_nop(self):
        self.check([0] * 8, "nop")

    def test_jump_tt(self):
        self.check(make_fixture.cmd_jump_tt(3), "jump_tt", title=3)

    def test_jump_vts_ptt(self):
        self.check(make_fixture.cmd_jump_vts_ptt(2, 7), "jump_vts_ptt",
                   title=2, chapter=7)

    def test_jump_ss_vmgm_menu(self):
        obj = self.check(make_fixture.cmd_jump_ss_vmgm_menu(2), "jump_ss",
                         target="vmgm_menu", menu=2)
        self.assertEqual(obj["menu_name"], "title")

    def test_call_ss_vts_menu(self):
        obj = self.check(make_fixture.cmd_call_ss_vts_menu(1, 1, menu=3),
                         "call_ss", target="vts_menu", vts=1, title=1)
        self.assertEqual(obj["menu_name"], "root")

    def test_link_next_pgc(self):
        self.check(make_fixture.cmd_link_next_pgc(), "link",
                   target="next_pgc")

    def test_link_tail_pgc(self):
        self.check(make_fixture.cmd_link_tail_pgc(), "link",
                   target="tail_pgc")

    def test_exit_break(self):
        self.check(make_fixture.cmd_exit(), "exit")
        self.check(make_fixture.cmd_break(), "break")

    def test_set_gprm_imm(self):
        self.check(make_fixture.cmd_set_gprm_imm(0, 1), "set",
                   gprm=0, value=1, operation="mov")

    def test_set_gprm_reg_sprm(self):
        obj = self.check(make_fixture.cmd_set_gprm_reg(1, 0x81), "set",
                         gprm=1, operation="mov")
        self.assertEqual(obj.get("source"), {"sprm": 1}, obj)

    def test_set_gprm_reg_gprm_keeps_dest(self):
        # Regression: GPRM7 = GPRM6 (raw 61 00 00 07 00 06 00 00,
        # from the Night of the Living Dead disc) must not collapse
        # into "GPRM6 = GPRM6": source lives under "source".
        obj, status = decode_command(bytes.fromhex("6100000700060000"))
        self.assertEqual(status, "ok")
        self.assertEqual(obj, {"op": "set", "gprm": 7, "operation": "mov",
                               "source": {"gprm": 6}})

    def test_guarded_set_becomes_compare(self):
        obj, status = decode_command(
            make_fixture.cmd_set_gprm_guarded(3, 2, 0, 1))
        self.assertEqual(status, "ok")
        self.assertEqual(obj["op"], "compare")
        self.assertEqual(obj["operator"], "eq")
        self.assertEqual(obj["gprm"], 3)
        self.assertEqual(obj["value"], 2)
        self.assertEqual(obj["then"], {"op": "set", "gprm": 0,
                                       "operation": "mov", "value": 1})

    def test_set_stn_audio(self):
        self.check(make_fixture.cmd_set_stn_audio_imm(0), "set_audio",
                   stream=0)

    def test_set_gprmmd_counter(self):
        self.check(make_fixture.cmd_set_gprmmd_imm(2, 60, counter=True),
                   "set_gprmmd", gprm=2, value=60, counter_mode=True)

    def test_set_nvtmr(self):
        self.check(make_fixture.cmd_set_nvtmr_imm(30, 1), "set_nvtmr",
                   pgc=1, value=30)

    def test_goto(self):
        obj, _ = decode_command(bytes([0x00, 0x01, 0, 0, 0, 0, 0, 5]))
        self.assertEqual(obj, {"op": "goto", "line": 5})

    def test_type7_unsupported(self):
        obj, status = decode_command(make_fixture.cmd_unsupported_type7())
        self.assertEqual(status, "unsupported")
        self.assertEqual(obj["op"], "unsupported")
        self.assertEqual(obj["raw_hex"], "e000000000000000")

    def test_compound_is_partial_not_dropped(self):
        # Type 4: SetCLnk, set-op mov, cmp eq, dest GPRM 0.
        raw = bytes([0x81, 0xA0, 0x00, 0x00, 0x00, 0x01, 0x00, 0x09])
        obj, status = decode_command(raw)
        self.assertEqual(status, "partial")
        self.assertEqual(obj["op"], "set_compare_link")
        self.assertEqual(obj["raw_hex"], raw.hex())

    def test_every_command_produces_one_object(self):
        # A list with a mix of decodable / undecodable commands keeps
        # its length and order through decode_command_list.
        from vm import decode_command_list
        raws = [make_fixture.cmd_jump_tt(1),
                make_fixture.cmd_unsupported_type7(),
                make_fixture.cmd_set_gprm_imm(0, 1)]
        objs, uns, _par = decode_command_list(raws, "pre")
        self.assertEqual(len(objs), 3)
        self.assertEqual(len(uns), 1)
        self.assertEqual(objs[1]["op"], "unsupported")


class TestFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="odvd-test-")
        cls.disc1 = os.path.join(cls.tmp, "disc1")
        cls.disc2 = os.path.join(cls.tmp, "disc2")
        make_fixture.build_disc1(cls.disc1)
        make_fixture.build_disc2(cls.disc2)
        cls.out1 = os.path.join(cls.tmp, "disc1.json")
        cls.out2 = os.path.join(cls.tmp, "disc2.json")
        r1 = run_cli(os.path.join(cls.disc1, "VIDEO_TS"), "-o", cls.out1)
        assert r1.returncode == 0, r1.stderr
        r2 = run_cli(os.path.join(cls.disc2, "VIDEO_TS"), "-o", cls.out2)
        assert r2.returncode == 0, r2.stderr
        cls.m1 = load_manifest(cls.out1)
        cls.m2 = load_manifest(cls.out2)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_disc1_basics(self):
        m = self.m1
        self.assertEqual(m["odvd_version"], "0.1")
        self.assertEqual(m["disc"]["video_standard"], "NTSC")
        self.assertEqual(m["disc"]["codec_profile"], "av1")
        self.assertEqual(m["disc"]["audio_mode"], "ac3-keep")
        self.assertEqual(len(m["vmg"]["titles"]), 1)
        self.assertEqual(m["vmg"]["titles"][0]["chapters"], 2)
        self.assertEqual(m["vmg"]["titles"][0]["duration_seconds"], 180.0)
        self.assertEqual(m["vmg"]["first_play_pgc"], "vmg-pgc-1")
        self.assertEqual(m["vmg"]["first_play_commands"]["pre_commands"],
                         [{"op": "jump_tt", "title": 1}])
        self.assertEqual(len(m["chapters"]), 2)
        self.assertEqual(m["chapters"][0]["time_seconds"], 0.0)
        self.assertEqual(m["chapters"][1]["time_seconds"], 90.0)
        self.assertEqual(m["diagnostics"]["unsupported_command_count"], 0)

    def test_disc1_tracks_and_hidden(self):
        m = self.m1
        ts = m["titlesets"][0]
        self.assertEqual([a["language"] for a in ts["audio_tracks"]],
                         ["en", "es"])
        self.assertEqual(ts["audio_tracks"][0]["codec"], "ac3")
        self.assertEqual(ts["audio_tracks"][0]["channels"], 6)
        self.assertEqual([s["language"] for s in ts["subtitle_tracks"]],
                         ["en", "en"])
        self.assertTrue(ts["subtitle_tracks"][1]["forced"])
        pgcs = pgcs_of(m)
        self.assertEqual(sorted(pgcs), ["vts1-pgc-1", "vts1-pgc-2"])
        self.assertFalse(pgcs["vts1-pgc-1"]["hidden"])
        self.assertTrue(pgcs["vts1-pgc-2"]["hidden"],
                        "unreferenced PGC must be marked hidden")
        # The guarded Set in PGC1 pre-commands decodes as compare.
        pre = pgcs["vts1-pgc-1"]["pre_commands"]
        self.assertEqual(pre[0], {"op": "set", "gprm": 0,
                                  "operation": "mov", "value": 1})
        self.assertEqual(pre[1]["op"], "compare")
        # Segment hashes are null in v0.1, never invented.
        cell = pgcs["vts1-pgc-1"]["programs"][0]["cells"][0]
        self.assertIsNone(cell["segment_hash"])
        self.assertIsNone(cell["segment_uri"])
        # Menu PGC lives under vmg.menu_pgcs (spec 3.4 has no VMGM slot).
        self.assertEqual(len(m["vmg"]["menu_pgcs"]), 1)
        menu = m["vmg"]["menu_pgcs"][0]
        self.assertEqual(menu["kind"], "menu")
        self.assertEqual(menu["menu"]["buttons"], [])
        self.assertTrue(menu["menu"]["background_loop"])
        self.assertTrue(menu["programs"][0]["cells"][0]["still_infinite"])

    def test_disc2_basics(self):
        m = self.m2
        self.assertEqual(m["disc"]["video_standard"], "PAL")
        self.assertEqual(len(m["vmg"]["titles"]), 2)
        self.assertEqual(m["vmg"]["titles"][0]["chapters"], 3)
        self.assertEqual(m["vmg"]["titles"][0]["duration_seconds"], 205.0)
        self.assertEqual(m["vmg"]["titles"][1]["duration_seconds"], 120.0)
        self.assertEqual([c["time_seconds"] for c in m["chapters"]
                          if c["title"] == 1], [0.0, 30.0, 85.0])
        ts = m["titlesets"][0]
        self.assertEqual(ts["audio_tracks"][1]["codec"], "lpcm")
        self.assertEqual(ts["subtitle_tracks"][0]["language"], "fr")

    def test_disc2_hidden_and_unsupported(self):
        m = self.m2
        pgcs = pgcs_of(m)
        self.assertEqual(sorted(pgcs),
                         ["vts1-menu-pgc-1", "vts1-pgc-1", "vts1-pgc-2",
                          "vts1-pgc-3", "vts1-pgc-4", "vts1-pgc-5"])
        # PGC4 is reached from PGC3's LinkNextPGC; PGC5 is not.
        self.assertFalse(pgcs["vts1-pgc-4"]["hidden"])
        self.assertTrue(pgcs["vts1-pgc-5"]["hidden"])
        self.assertFalse(pgcs["vts1-pgc-3"]["hidden"])
        self.assertEqual(m["diagnostics"]["unsupported_command_count"], 1)
        uns = m["diagnostics"]["unsupported_commands"][0]
        self.assertEqual(uns["raw_hex"], "e000000000000000")
        self.assertEqual(uns["count"], 1)
        # Angle block: first cell angle 1, last cell angle 2, and the
        # block counts once toward the PGC duration.
        cells = pgcs["vts1-pgc-1"]["programs"][0]["cells"]
        self.assertEqual([c["angle"] for c in cells], [1, 2])
        self.assertEqual(pgcs["vts1-pgc-1"]["selected_path_seconds"], 85.0)
        # Still cell (5 s) in program 2.
        still_cell = pgcs["vts1-pgc-1"]["programs"][1]["cells"][1]
        self.assertTrue(still_cell["still"])
        self.assertEqual(still_cell["still_seconds"], 5)
        # VTSM menu: nav-timer timeout and SetSTN / CallSS decodes.
        menu_pgcs = [p for p in pgcs.values() if p["kind"] == "menu"]
        self.assertEqual(len(menu_pgcs), 1)
        self.assertEqual(menu_pgcs[0]["menu"]["timeout_seconds"], 30)
        self.assertEqual(
            pgcs["vts1-pgc-2"]["post_commands"],
            [{"op": "call_ss", "target": "vts_menu", "vts": 1, "title": 1,
              "menu": 3, "menu_name": "root"}])
        # Missing VTS_01_0.BUP is a warning, not a failure.
        self.assertTrue(any("VTS_01_0.BUP is missing" in w
                            for w in m["diagnostics"]["warnings"]),
                        m["diagnostics"]["warnings"])

    def test_iso_input(self):
        iso_path = os.path.join(self.tmp, "disc1.iso")
        build_iso(os.path.join(self.disc1, "VIDEO_TS"), iso_path, "FIXTURE1")
        out = os.path.join(self.tmp, "iso.json")
        res = run_cli(iso_path, "-o", out)
        self.assertEqual(res.returncode, 0, res.stderr)
        m = load_manifest(out)
        self.assertEqual(m["disc"]["title"], "FIXTURE1")
        self.assertEqual(m["disc"]["video_standard"], "NTSC")
        self.assertEqual(len(m["vmg"]["titles"]), 1)
        self.assertEqual(len(m["chapters"]), 2)
        with open(iso_path, "rb") as fh:
            iso_hash = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(m["disc"]["source_hash"], iso_hash)
        self.assertEqual(m["diagnostics"]["input_kind"], "iso")

    def test_missing_ifo_is_fatal(self):
        empty = os.path.join(self.tmp, "empty", "VIDEO_TS")
        os.makedirs(empty, exist_ok=True)
        res = run_cli(empty, "-o", os.path.join(self.tmp, "nope.json"))
        self.assertEqual(res.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope.json")))


if __name__ == "__main__":
    # (Re)build the shipped fixtures next to this file first, so a
    # plain `python3 test_ifo.py` exercises the same bytes the
    # parser ships with.
    make_fixture.main()
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
