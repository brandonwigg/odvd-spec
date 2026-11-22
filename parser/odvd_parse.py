#!/usr/bin/env python3
"""odvd_parse.py -- ODVD v0.1 IFO-only parser (compiler front end).

Usage:
    python3 odvd_parse.py <path-to-ISO-or-VIDEO_TS> -o <manifest.json>

Input is an already-decrypted ISO image or a VIDEO_TS folder
(SPEC section 14: no CSS/decryption, no MPEG-2 decode in v0.1).
Output is a manifest.json matching SPEC section 3.4, with
segment_hash left null ("segments not transcoded in v0.1 parser")
and a top-level "diagnostics" object recording every VM command
that could not be decoded, every partial decode, and every
warning.  Nothing the disc says is silently dropped.

Run from this directory (parser/) so the sibling modules
(ifo.py, vm.py, iso9660.py) import; or add parser/ to PYTHONPATH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ifo as ifo_mod
from ifo import (
    _cell_start_times,
    _selected_path_seconds,
    _timecode_from_seconds,
    parse_video_ts,
)
from iso9660 import IsoError, extract_video_ts
from vm import PARSER_VERSION

COMPILER = f"odvd-parser {PARSER_VERSION} (IFO-only)"
SEGMENT_NOTE = "segments not transcoded in v0.1 parser"


# ---------------------------------------------------------------- input

def resolve_input(path: str):
    """Return (video_ts_dir, kind, volume_label, cleanup_dir).

    kind is "iso" or "folder".  cleanup_dir is a temp dir to remove
    after parsing, or None.
    """
    if os.path.isfile(path) and path.lower().endswith((".iso", ".img", ".iso9660")):
        tmpdir = tempfile.mkdtemp(prefix="odvd-iso-")
        try:
            vts_dir, label = extract_video_ts(path, tmpdir)
        except IsoError:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        return vts_dir, "iso", label, tmpdir
    if os.path.isdir(path):
        base = os.path.basename(os.path.normpath(path))
        if base.upper() == "VIDEO_TS":
            return path, "folder", None, None
        vts = os.path.join(path, "VIDEO_TS")
        if os.path.isdir(vts):
            return vts, "folder", None, None
        if os.path.exists(os.path.join(path, "VIDEO_TS.IFO")):
            return path, "folder", None, None
        raise SystemExit(f"error: {path} is a directory but contains no "
                         "VIDEO_TS folder or VIDEO_TS.IFO")
    raise SystemExit(f"error: no such file or directory: {path}")


def _source_hash(kind, path, video_ts_dir):
    h = hashlib.sha256()
    if kind == "iso":
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    names = sorted(n for n in os.listdir(video_ts_dir)
                   if n.upper().endswith((".IFO", ".BUP")))
    for name in names:
        with open(os.path.join(video_ts_dir, name), "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- PGC -> manifest

def _pgc_cells(pgc, pgc_id, fps, chapter_of_program):
    """Build the programs[] list for one PGC."""
    cells = pgc["cells"]
    starts = _cell_start_times(cells, fps)
    # Which program each cell (1-based) belongs to, via the program map.
    cell_program = {}
    pmap = pgc["program_map"]
    if pmap:
        for pi, first_cell in enumerate(pmap):
            end = (pmap[pi + 1] - 1) if pi + 1 < len(pmap) else len(cells)
            for ci in range(first_cell, end + 1):
                cell_program[ci] = pi + 1
    elif cells:
        for ci in range(1, len(cells) + 1):
            cell_program[ci] = 1
    angle_idx = 0
    cell_objs = []
    for i, c in enumerate(cells):
        if c["block_mode"] == 1:
            angle_idx = 1
        elif c["block_mode"] in (2, 3):
            angle_idx += 1
        else:
            angle_idx = 1
        dur = c["duration_seconds"]
        cell_cmd = None
        if c["cell_command_nr"] and 1 <= c["cell_command_nr"] <= len(pgc["cell_commands"]):
            cell_cmd = pgc["cell_commands"][c["cell_command_nr"] - 1]
        cell_objs.append({
            "cell_id": f"{pgc_id}-cell{i + 1}",
            "segment_hash": None,
            "segment_uri": None,
            "segment_note": SEGMENT_NOTE,
            "duration_seconds": dur,
            "start_pts_seconds": round(starts[i], 3),
            "original_dvd_timecode": _timecode_from_seconds(starts[i], fps),
            "frame_count": int(round(dur * fps)) if fps else None,
            "angle": angle_idx,
            "still": c["still_time"] > 0,
            "still_seconds": c["still_time"] if 0 < c["still_time"] < 255 else None,
            "still_infinite": c["still_time"] == 255,
            "chapter": chapter_of_program.get(cell_program.get(i + 1)),
            "vob_id": c.get("vob_id"),
            "cell_id_in_vob": c.get("cell_id_in_vob"),
            "first_sector": c["first_sector"],
            "last_sector": c["last_sector"],
            "block_mode": c["block_mode"],
            "block_type": c["block_type"],
            "interleaved": c["interleaved"],
            "cell_command": cell_cmd,
        })
        if c["block_mode"] == 3:
            angle_idx = 0
    # Group cells into programs via the program map (1-based first
    # cell of each program).  A program runs to the next program's
    # first cell, or to the end of the PGC.
    programs = []
    pmap = pgc["program_map"]
    if pmap and cells:
        for pi, first_cell in enumerate(pmap):
            start = first_cell - 1
            end = (pmap[pi + 1] - 1) if pi + 1 < len(pmap) else len(cell_objs)
            if 0 <= start < len(cell_objs):
                programs.append({
                    "program_number": pi + 1,
                    "cells": cell_objs[start:end],
                })
    elif cells:
        programs.append({"program_number": 1, "cells": cell_objs})
    return programs


def _menu_obj(pgc, audio_tracks):
    post = pgc["post_commands"]

    def _is_top_pgc_link(cmd):
        if not isinstance(cmd, dict):
            return False
        if cmd.get("op") == "link" and cmd.get("target") == "top_pgc":
            return True
        if cmd.get("op") == "compare":
            return _is_top_pgc_link(cmd.get("then"))
        return False

    loops = any(_is_top_pgc_link(c) for c in post)
    timeout = None
    for cmd in pgc["pre_commands"]:
        if isinstance(cmd, dict) and cmd.get("op") == "set_nvtmr":
            timeout = cmd.get("value")
    audio_track = None
    for i, ctl in enumerate(pgc["audio_control"]):
        if ctl["present"]:
            audio_track = i
            break
    return {
        "background_segment_hash": None,
        "background_segment_note": SEGMENT_NOTE,
        "background_loop": bool(loops),
        "audio_track": audio_track,
        "still": pgc["still_time"] == 255,
        "timeout_seconds": timeout,
        "palette": pgc["palette"],
        "buttons": [],
        "buttons_note": ("button geometry lives in VOB PCI packets, "
                         "not in the IFO; PCI parsing is a known v0.1 "
                         "gap -- see parser README.md"),
    }


def _pgc_manifest(pgc, pgc_id, kind, hidden, fps, chapter_of_program,
                  audio_tracks):
    if pgc is None:
        return None
    obj = {
        "id": pgc_id,
        "kind": kind,
        "hidden": hidden,
        "pre_commands": pgc["pre_commands"],
        "programs": _pgc_cells(pgc, pgc_id, fps, chapter_of_program),
        "post_commands": pgc["post_commands"],
        "cell_commands": pgc["cell_commands"],
        "playback_time_seconds": pgc["playback_time_seconds"],
        "selected_path_seconds": _selected_path_seconds(pgc["cells"], fps),
        "palette": pgc["palette"],
        "still_time": pgc["still_time"],
        "next_pgc": pgc["next_pgc"],
        "prev_pgc": pgc["prev_pgc"],
        "goup_pgc": pgc["goup_pgc"],
    }
    if kind == "menu":
        obj["menu"] = _menu_obj(pgc, audio_tracks)
    return obj


# ---------------------------------------------------------------- reachability

def _command_targets(cmds):
    """Yield (kind, payload) reachability edges from a command list."""
    for cmd in cmds:
        if not isinstance(cmd, dict):
            continue
        if cmd.get("op") == "compare":
            yield from _command_targets([cmd.get("then")] if cmd.get("then") else [])
            continue
        op = cmd.get("op")
        if op == "link_pgc":
            yield ("link_pgc", cmd.get("pgc"))
        elif op == "link":
            # Link-subset target (top/next/prev PGC, cell, program...).
            # Only the PGC-relative ones move to another PGC.
            if cmd.get("target") in ("next_pgc", "prev_pgc", "top_pgc",
                                     "goup_pgc"):
                yield ("link", cmd)
        elif op == "jump_tt":
            yield ("jump_tt", cmd.get("title"))
        elif op == "jump_vts_tt":
            yield ("jump_vts_tt", cmd.get("title"))
        elif op == "jump_vts_ptt":
            yield ("jump_vts_ptt", (cmd.get("title"), cmd.get("chapter")))
        elif op in ("jump_ss", "call_ss"):
            yield (op, cmd)
        elif op == "link_ptt":
            yield ("link_ptt", cmd.get("chapter"))
        # A trailing link on a Set command is stored under "link".
        link = cmd.get("link")
        if isinstance(link, dict):
            yield from _command_targets([link])


def _collect_edges(pgc):
    edges = []
    for lst in (pgc["pre_commands"], pgc["post_commands"], pgc["cell_commands"]):
        edges.extend(_command_targets(lst))
    return edges


def _compute_hidden(vmgi, vts_map, pgc_nodes):
    """pgc_nodes: dict node -> {"domain":..., "vts":..., "pgc_number":...,
    "pgc": parsed pgc or None, "titles_using": set of title numbers}.

    Returns the set of hidden nodes.
    """
    # Title entry PGCs and every PTT PGC.
    title_entry = {}   # title_number -> node
    ptt_nodes = {}     # (title_number, chapter) -> node
    for t in vmgi["titles"]:
        vts = t["vts"]
        vtsi = vts_map.get(vts)
        if vtsi is None:
            continue
        entries = vtsi["ptt"].get(t["vts_ttn"], [])
        for ci, (pgcn, pgn) in enumerate(entries, start=1):
            node = ("vts", vts, pgcn)
            ptt_nodes[(t["title_number"], ci)] = node
            if ci == 1:
                title_entry[t["title_number"]] = node

    def node_for_title_entry(title_number):
        return title_entry.get(title_number)

    def vts_entry_node(vts, vts_ttn):
        vtsi = vts_map.get(vts)
        if vtsi is None:
            return None
        entries = vtsi["ptt"].get(vts_ttn, [])
        if not entries:
            return None
        return ("vts", vts, entries[0][0])

    def vts_ptt_node(vts, vts_ttn, chapter):
        vtsi = vts_map.get(vts)
        if vtsi is None:
            return None
        entries = vtsi["ptt"].get(vts_ttn, [])
        if 1 <= chapter <= len(entries):
            return ("vts", vts, entries[chapter - 1][0])
        return None

    def menu_nodes_for_jump_ss(cmd, from_node):
        """Nodes a JumpSS/CallSS can land on."""
        target = cmd.get("target")
        out = []
        if target == "first_play":
            out.append(("vmgfp",))
        elif target == "vmgm_menu":
            menu = cmd.get("menu_name")
            for node, info in pgc_nodes.items():
                if info["domain"] == "vmgm" and (
                        menu in (None, "title") or
                        info.get("menu_name") == menu):
                    out.append(node)
        elif target == "vmgm_pgc":
            out.append(("vmgm", cmd.get("pgc")))
        elif target == "vts_menu":
            vts = cmd.get("vts")
            menu = cmd.get("menu_name")
            for node, info in pgc_nodes.items():
                if info["domain"] == "vtsm" and info["vts"] == vts and (
                        menu is None or info.get("menu_name") == menu):
                    out.append(node)
        return out

    seeds = {("vmgfp",)}
    for node, info in pgc_nodes.items():
        if info["domain"] in ("vmgm", "vtsm"):
            seeds.add(node)
    seeds.update(title_entry.values())
    seeds.update(ptt_nodes.values())
    seeds = {s for s in seeds if s in pgc_nodes or s == ("vmgfp",)}

    reachable = set(seeds)
    work = list(reachable)
    while work:
        node = work.pop()
        info = pgc_nodes.get(node)
        if info is None:
            continue
        pgc = info["pgc"]
        if pgc is None:
            continue
        neighbours = set()
        for kind, payload in _collect_edges(pgc):
            if kind == "link":
                # Link-subset PGC-relative target, same domain.
                delta = {"next_pgc": 1, "prev_pgc": -1}.get(
                    payload.get("target"))
                if delta is not None:
                    n = info["pgc_number"] + delta
                    if info["domain"] == "vts":
                        neighbours.add(("vts", info["vts"], n))
                    elif info["domain"] == "vtsm":
                        neighbours.add(("vtsm", info["vts"], n))
                    elif info["domain"] == "vmgm":
                        neighbours.add(("vmgm", n))
            elif kind == "link_pgc":
                cand = (info["domain"], info.get("vts"), payload) \
                    if info["domain"] in ("vts", "vtsm") else \
                    (info["domain"], payload)
                # Normalise: title-domain nodes are ("vts", vts, n);
                # menu-domain nodes are ("vmgm", n) / ("vtsm", vts, n).
                if info["domain"] == "vts":
                    cand = ("vts", info["vts"], payload)
                elif info["domain"] == "vtsm":
                    cand = ("vtsm", info["vts"], payload)
                elif info["domain"] == "vmgm":
                    cand = ("vmgm", payload)
                neighbours.add(cand)
            elif kind == "jump_tt":
                n = node_for_title_entry(payload)
                if n:
                    neighbours.add(n)
            elif kind == "jump_vts_tt":
                n = vts_entry_node(info.get("vts"), payload)
                if n:
                    neighbours.add(n)
            elif kind == "jump_vts_ptt":
                title, chapter = payload
                n = vts_ptt_node(info.get("vts"), title, chapter)
                if n:
                    neighbours.add(n)
            elif kind in ("jump_ss", "call_ss"):
                neighbours.update(menu_nodes_for_jump_ss(payload, node))
            elif kind == "link_ptt":
                for tnum in info.get("titles_using", ()):
                    n = ptt_nodes.get((tnum, payload))
                    if n:
                        neighbours.add(n)
        for nxt in (info["pgc"]["next_pgc"], info["pgc"]["prev_pgc"],
                    info["pgc"]["goup_pgc"]):
            if nxt:
                if info["domain"] == "vts":
                    neighbours.add(("vts", info["vts"], nxt))
                elif info["domain"] == "vtsm":
                    neighbours.add(("vtsm", info["vts"], nxt))
                elif info["domain"] == "vmgm":
                    neighbours.add(("vmgm", nxt))
        for n in neighbours:
            if n in pgc_nodes and n not in reachable:
                reachable.add(n)
                work.append(n)
    hidden = {node for node in pgc_nodes if node not in reachable}
    return hidden


# ---------------------------------------------------------------- manifest

def build_manifest(vmgi, vts_map, kind, source_path, volume_label,
                   title_override=None):
    warnings = list(vmgi["warnings"])
    unsupported_all, partial_all = [], []

    def note_unsupported(items, where):
        for it in items:
            unsupported_all.append({**it, "location": f"{where}{it['location']}"})

    def note_partial(items, where):
        for it in items:
            partial_all.append({**it, "location": f"{where}{it['location']}"})

    # FP PGC diagnostics.
    note_unsupported(vmgi["first_play_unsupported"], "vmg-first-play/")
    note_partial(vmgi["first_play_partial"], "vmg-first-play/")
    warnings.extend(vmgi["first_play_warnings"])

    # Per-VTS structures, PGC nodes for reachability, chapter maps.
    pgc_nodes = {}
    vts_audio_tracks = {}
    vts_subtitle_tracks = {}
    vts_fps = {}
    chapter_of_program = {}  # (vts, pgcn) -> {program: chapter} (first title wins)
    title_durations = {}
    title_chapter_times = {}  # title_number -> [(chapter, pgc_id, pgn, time)]

    for vts_no in sorted(vts_map):
        vtsi = vts_map[vts_no]
        warnings.extend(vtsi["warnings"])
        fps = vtsi["title_video"]["fps"] or 30000 / 1001
        vts_fps[vts_no] = fps
        # Audio / subtitle tracks for this VTS.
        entry_pgc = None
        if vtsi["pgcs"]:
            entry_pgc = next((p for p in vtsi["pgcs"] if p["entry"]),
                             vtsi["pgcs"][0])
        audio_tracks = []
        for i, attr in enumerate(vtsi["audio"]):
            present = False
            if entry_pgc and entry_pgc["pgc"]:
                ctl = entry_pgc["pgc"]["audio_control"][i]
                present = ctl["present"]
            audio_tracks.append({
                "index": i,
                "language": attr["language"],
                "codec": attr["codec"],
                "channels": attr["channels"],
                "bitrate_kbps": attr["bitrate_kbps"],
                "sample_rate_hz": attr["sample_rate_hz"],
                "default": i == 0,
                "code_extension": attr["code_extension_name"],
                "present_in_entry_pgc": present,
            })
        vts_audio_tracks[vts_no] = audio_tracks
        sub_tracks = []
        for i, attr in enumerate(vtsi["subp"]):
            sub_tracks.append({
                "index": i,
                "language": attr["language"],
                "forced": attr["forced"],
                "default": False,
                "code_extension": attr["code_extension_name"],
            })
        vts_subtitle_tracks[vts_no] = sub_tracks
        # Title PGC nodes.
        for rec in vtsi["pgcs"]:
            node = ("vts", vts_no, rec["pgc_number"])
            pgc_nodes[node] = {
                "domain": "vts", "vts": vts_no,
                "pgc_number": rec["pgc_number"], "pgc": rec["pgc"],
                "titles_using": set(), "entry": rec["entry"],
            }
            note_unsupported(rec["unsupported"],
                             f"vts{vts_no}-pgc-{rec['pgc_number']}/")
            note_partial(rec["partial"],
                         f"vts{vts_no}-pgc-{rec['pgc_number']}/")
            warnings.extend(rec["warnings"])
            if rec["pgc"] is None:
                warnings.append(f"vts{vts_no}-pgc-{rec['pgc_number']} "
                                "could not be parsed")
        # Menu PGC nodes.
        for lu in vtsi["menu_units"]:
            for rec in lu["pgcs"]:
                node = ("vtsm", vts_no, rec["pgc_number"])
                pgc_nodes[node] = {
                    "domain": "vtsm", "vts": vts_no,
                    "pgc_number": rec["pgc_number"], "pgc": rec["pgc"],
                    "titles_using": set(), "menu_name": rec["menu_name"],
                    "language": lu["language"],
                }
                note_unsupported(rec["unsupported"],
                                 f"vts{vts_no}-menu-pgc-{rec['pgc_number']}/")
                note_partial(rec["partial"],
                             f"vts{vts_no}-menu-pgc-{rec['pgc_number']}/")
                warnings.extend(rec["warnings"])
        # VMGM nodes are added by the caller loop below (single VMG).

    for lu in vmgi["menu_units"]:
        for rec in lu["pgcs"]:
            node = ("vmgm", rec["pgc_number"])
            pgc_nodes[node] = {
                "domain": "vmgm", "vts": None,
                "pgc_number": rec["pgc_number"], "pgc": rec["pgc"],
                "titles_using": set(), "menu_name": rec["menu_name"],
                "language": lu["language"],
            }
            note_unsupported(rec["unsupported"],
                             f"vmg-menu-pgc-{rec['pgc_number']}/")
            note_partial(rec["partial"],
                         f"vmg-menu-pgc-{rec['pgc_number']}/")
            warnings.extend(rec["warnings"])

    # Which titles use which PGCs (for link_ptt resolution and kind).
    ptt_pgcs = {}  # (vts, pgcn) -> set of title numbers
    for t in vmgi["titles"]:
        vtsi = vts_map.get(t["vts"])
        if vtsi is None:
            warnings.append(f"title {t['title_number']} points at VTS "
                            f"{t['vts']}, whose IFO is missing")
            continue
        entries = vtsi["ptt"].get(t["vts_ttn"], [])
        if len(entries) != t["chapters"]:
            warnings.append(
                f"title {t['title_number']}: TT_SRPT says "
                f"{t['chapters']} chapters but VTS_PTT_SRPT lists "
                f"{len(entries)}")
        for ci, (pgcn, pgn) in enumerate(entries, start=1):
            node = ("vts", t["vts"], pgcn)
            if node in pgc_nodes:
                pgc_nodes[node]["titles_using"].add(t["title_number"])
            ptt_pgcs.setdefault((t["vts"], pgcn), set()).add(t["title_number"])
            chapter_of_program.setdefault((t["vts"], pgcn), {}) \
                .setdefault(pgn, ci)

    hidden = _compute_hidden(vmgi, vts_map, pgc_nodes)

    # Chapters + title durations.
    chapters_out = []
    vmg_titles = []
    for t in vmgi["titles"]:
        vtsi = vts_map.get(t["vts"])
        entries = vtsi["ptt"].get(t["vts_ttn"], []) if vtsi else []
        fps = vts_fps.get(t["vts"], 30000 / 1001)
        times = []
        time_base = 0.0
        cur_pgcn = None
        cur_dur = 0.0
        for ci, (pgcn, pgn) in enumerate(entries, start=1):
            if pgcn != cur_pgcn:
                if cur_pgcn is not None:
                    time_base += cur_dur
                cur_pgcn = pgcn
                node = ("vts", t["vts"], pgcn)
                rec = pgc_nodes.get(node)
                cur_dur = _selected_path_seconds(
                    rec["pgc"]["cells"], fps) if rec and rec["pgc"] else 0.0
            # Start time of program pgn inside this PGC.
            prog_start = 0.0
            rec = pgc_nodes.get(("vts", t["vts"], pgcn))
            if rec and rec["pgc"]:
                pmap = rec["pgc"]["program_map"]
                starts = _cell_start_times(rec["pgc"]["cells"], fps)
                if 1 <= pgn <= len(pmap):
                    first_cell = pmap[pgn - 1]
                    if 1 <= first_cell <= len(starts):
                        prog_start = starts[first_cell - 1]
            ct = round(time_base + prog_start, 3)
            times.append(ct)
            pgc_id = f"vts{t['vts']}-pgc-{pgcn}"
            chapters_out.append({
                "title": t["title_number"], "chapter": ci, "pgc": pgc_id,
                "program": pgn, "time_seconds": ct, "name": None,
            })
        if entries:
            last_pgcn, last_pgn = entries[-1]
            rec = pgc_nodes.get(("vts", t["vts"], last_pgcn))
            dur = times[-1]
            if rec and rec["pgc"]:
                pmap = rec["pgc"]["program_map"]
                starts = _cell_start_times(rec["pgc"]["cells"], fps)
                if 1 <= last_pgn <= len(pmap):
                    first_cell = pmap[last_pgn - 1]
                    if 1 <= first_cell <= len(starts):
                        dur = times[-1] + (_selected_path_seconds(
                            rec["pgc"]["cells"], fps) - starts[first_cell - 1])
            title_durations[t["title_number"]] = round(dur, 3)
        else:
            title_durations[t["title_number"]] = 0.0
            if t["chapters"]:
                warnings.append(f"title {t['title_number']} has "
                                f"{t['chapters']} chapters in TT_SRPT but "
                                "no PTT entries in its VTS")
        entry_pgc_id = None
        if entries:
            entry_pgc_id = f"vts{t['vts']}-pgc-{entries[0][0]}"
        vmg_titles.append({
            "title_number": t["title_number"],
            "vts": t["vts"],
            "entry_pgc": entry_pgc_id,
            "duration_seconds": title_durations[t["title_number"]],
            "chapters": len(entries),
            "angles": t["angles"],
        })
        if not entries:
            warnings.append(f"title {t['title_number']} is empty "
                            "(no chapters / no entry PGC)")

    # VMGM menu PGCs (spec section 3.4 has no VMGM slot; they are
    # carried as a vmg extension so they are not lost).
    vmg_menu_pgcs = []
    for lu in vmgi["menu_units"]:
        for rec in lu["pgcs"]:
            node = ("vmgm", rec["pgc_number"])
            pgc_id = f"vmg-menu-pgc-{rec['pgc_number']}"
            obj = _pgc_manifest(
                rec["pgc"], pgc_id, "menu", node in hidden,
                vmgi["menu_video"]["fps"] or 30000 / 1001, {},
                vmgi["menu_audio"])
            if obj is not None:
                obj["language"] = lu["language"]
                obj["menu_id"] = rec["menu_id"]
                obj["menu_name"] = rec["menu_name"]
                obj["category_raw"] = rec["category_raw"]
                obj["entry"] = rec["entry"]
                vmg_menu_pgcs.append(obj)

    titlesets = []
    for vts_no in sorted(vts_map):
        vtsi = vts_map[vts_no]
        fps = vts_fps[vts_no]
        pgcs_out = []
        for rec in vtsi["pgcs"]:
            node = ("vts", vts_no, rec["pgc_number"])
            pgc_id = f"vts{vts_no}-pgc-{rec['pgc_number']}"
            is_title = (vts_no, rec["pgc_number"]) in ptt_pgcs
            pgc_kind = "title" if (is_title or rec["entry"]) else "other"
            obj = _pgc_manifest(
                rec["pgc"], pgc_id, pgc_kind, node in hidden, fps,
                chapter_of_program.get((vts_no, rec["pgc_number"]), {}),
                vts_audio_tracks[vts_no])
            if obj is not None:
                obj["entry"] = rec["entry"]
                pgcs_out.append(obj)
        for lu in vtsi["menu_units"]:
            for rec in lu["pgcs"]:
                node = ("vtsm", vts_no, rec["pgc_number"])
                pgc_id = f"vts{vts_no}-menu-pgc-{rec['pgc_number']}"
                obj = _pgc_manifest(
                    rec["pgc"], pgc_id, "menu", node in hidden, fps, {},
                    vts_audio_tracks[vts_no])
                if obj is not None:
                    obj["language"] = lu["language"]
                    obj["menu_id"] = rec["menu_id"]
                    obj["menu_name"] = rec["menu_name"]
                    obj["category_raw"] = rec["category_raw"]
                    obj["entry"] = rec["entry"]
                    pgcs_out.append(obj)
        titlesets.append({
            "vts": vts_no,
            "video_standard": vtsi["title_video"]["video_standard"],
            "aspect_ratio": vtsi["title_video"]["aspect_ratio"],
            "resolution": vtsi["title_video"]["resolution"],
            "audio_tracks": vts_audio_tracks[vts_no],
            "subtitle_tracks": vts_subtitle_tracks[vts_no],
            "pgcs": pgcs_out,
        })

    standards = {ts["video_standard"] for ts in titlesets}
    if len(standards) > 1:
        warnings.append(f"title sets disagree on video standard: "
                        f"{sorted(standards)}; disc.video_standard uses "
                        "VTS 1")
    video_standard = titlesets[0]["video_standard"] if titlesets else \
        vmgi["menu_video"]["video_standard"]
    if video_standard not in ("NTSC", "PAL"):
        warnings.append(f"video standard {video_standard!r} is not NTSC/PAL; "
                        "recording it as-is")
        video_standard = "NTSC" if video_standard != "PAL" else "PAL"

    if not vmgi["titles"]:
        warnings.append("disc has no titles in TT_SRPT")

    # Disc title provenance.
    if title_override:
        disc_title, title_from, title_source = title_override, "user", "cli --title"
    elif volume_label:
        disc_title, title_from, title_source = volume_label, "disc", "iso_volume_label"
    else:
        parent = os.path.basename(os.path.normpath(source_path))
        if parent.upper() == "VIDEO_TS":
            # The disc name is the folder holding VIDEO_TS/.
            parent = os.path.basename(
                os.path.dirname(os.path.normpath(source_path)))
        elif parent.lower().endswith((".iso", ".img")):
            parent = parent.rsplit(".", 1)[0]
        if parent and parent.upper() != "VIDEO_TS":
            disc_title, title_from, title_source = (
                parent, "disc",
                "iso_filename" if kind == "iso" else "folder_name")
        elif vmgi["provider_id"]:
            disc_title, title_from, title_source = (
                vmgi["provider_id"], "disc", "provider_id")
        else:
            disc_title, title_from, title_source = (
                "Unknown DVD", "disc", "unknown")

    # Collapse duplicate unsupported commands into counted entries.
    def _collapse(items):
        by_hex = {}
        for it in items:
            key = it["raw_hex"]
            entry = by_hex.setdefault(key, {
                "raw_hex": key, "count": 0, "locations": [],
                "note": it.get("note", "")})
            entry["count"] += 1
            entry["locations"].append(it["location"])
        return sorted(by_hex.values(), key=lambda e: -e["count"])

    manifest = {
        "odvd_version": "0.1",
        "bundle_id": f"dvd-{uuid.uuid4()}",
        "disc": {
            "title": disc_title,
            "title_from": title_from,
            "title_source": title_source,
            "volume_label": volume_label,
            "video_standard": video_standard,
            "disc_side": vmgi["side_id"] or 1,
            "disc_count_in_set": vmgi["nr_volumes"] or 1,
            "set_id": None,
            "set_title": None,
            "region": None,
            "region_mask": vmgi["region_mask"],
            "source_hash": None,  # filled in by main() (needs input path)
            "compiled_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "compiler": COMPILER,
            "codec_profile": "av1",
            "audio_mode": "ac3-keep",
        },
        "vmg": {
            "first_play_pgc": "vmg-pgc-1" if vmgi["first_play_pgc"] else None,
            "first_play_commands": {
                "pre_commands": vmgi["first_play_pgc"]["pre_commands"]
                if vmgi["first_play_pgc"] else [],
                "post_commands": vmgi["first_play_pgc"]["post_commands"]
                if vmgi["first_play_pgc"] else [],
                "cell_commands": vmgi["first_play_pgc"]["cell_commands"]
                if vmgi["first_play_pgc"] else [],
            },
            "titles": vmg_titles,
            "menu_pgcs": vmg_menu_pgcs,
        },
        "titlesets": titlesets,
        "chapters": chapters_out,
        "dedup": {"asset_store": "assets/", "references": []},
        "diagnostics": {
            "parser_version": PARSER_VERSION,
            "parser": COMPILER,
            "input_kind": kind,
            "unsupported_commands": _collapse(unsupported_all),
            "unsupported_command_count": len(unsupported_all),
            "partial_decodes": _collapse(partial_all),
            "partial_decode_count": len(partial_all),
            "warnings": warnings,
            "vts_parsed": sorted(vts_map),
            "vts_declared": vmgi["nr_vts"],
        },
    }
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="ODVD v0.1 IFO-only parser: ISO / VIDEO_TS -> manifest.json")
    ap.add_argument("input", help="path to a decrypted .iso or a VIDEO_TS folder")
    ap.add_argument("-o", "--output", default="manifest.json",
                    help="output manifest.json path (default: manifest.json)")
    ap.add_argument("--title", default=None,
                    help="override the disc title (title_from becomes 'user')")
    args = ap.parse_args(argv)

    try:
        video_ts_dir, kind, label, cleanup = resolve_input(args.input)
    except (IsoError, SystemExit) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        vmgi, vts_map = parse_video_ts(video_ts_dir)
    except ifo_mod.IfoError as exc:
        print(f"error: cannot parse {video_ts_dir}: {exc}", file=sys.stderr)
        return 2
    manifest = build_manifest(vmgi, vts_map, kind, args.input, label,
                              title_override=args.title)
    # source_hash needs the real input path / folder; it was left
    # None above only to keep build_manifest side-effect free.
    manifest["disc"]["source_hash"] = _source_hash(
        kind, args.input, video_ts_dir)
    out_path = args.output
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    diag = manifest["diagnostics"]
    n_pgcs = sum(len(ts["pgcs"]) for ts in manifest["titlesets"]) + \
        len(manifest["vmg"]["menu_pgcs"])
    n_hidden = sum(1 for ts in manifest["titlesets"] for p in ts["pgcs"]
                   if p["hidden"]) + sum(1 for p in manifest["vmg"]["menu_pgcs"]
                                         if p["hidden"])
    print(f"wrote {out_path}")
    print(f"disc: {manifest['disc']['title']!r} "
          f"({manifest['disc']['video_standard']})")
    print(f"titles: {len(manifest['vmg']['titles'])}  "
          f"titlesets: {len(manifest['titlesets'])}  "
          f"pgcs: {n_pgcs} ({n_hidden} hidden)  "
          f"chapters: {len(manifest['chapters'])}")
    print(f"unsupported VM commands: {diag['unsupported_command_count']}  "
          f"partial decodes: {diag['partial_decode_count']}  "
          f"warnings: {len(diag['warnings'])}")
    for w in diag["warnings"]:
        print(f"  warning: {w}", file=sys.stderr)
    if cleanup:
        shutil.rmtree(cleanup, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
