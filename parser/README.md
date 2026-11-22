# ODVD parser — v0.1 (IFO-only)

Parses an already-decrypted DVD (ISO image or `VIDEO_TS` folder)
into a `manifest.json` matching ODVD spec v0.1 §3.4. **IFO parsing
only**: no CSS/decryption, no MPEG-2 decode, no transcoding.
Segment hashes are `null` with a note — the parser never invents
them; a later compile step fills them in from transcoded cells.

## Run it

```sh
cd ~/workspace/odvd-spec/parser
python3 odvd_parse.py <path-to-ISO-or-VIDEO_TS> -o <manifest.json>
python3 odvd_parse.py fixtures/disc1/VIDEO_TS -o /tmp/disc1.json
python3 odvd_parse.py /path/to/disc.iso -o /tmp/disc.json
python3 odvd_parse.py <input> -o <out.json> --title "My Disc"  # title_from=user
```

Exit codes: `0` manifest written (warnings go to stderr),
`2` fatal input error (no `VIDEO_TS.IFO`/`.BUP`, no `VIDEO_TS`
directory in the ISO, unreadable ISO). Requires Python 3.8+
(stdlib only — no third-party packages).

## Files

| File | What it is |
|---|---|
| `odvd_parse.py` | CLI + manifest builder (titles, chapters, hidden-PGC detection, diagnostics) |
| `ifo.py` | IFO parser: VMGI, VTSI, TT_SRPT, VTS_PTT_SRPT, VTS_PGCIT, PGCI_UT, PGCs, cells, attributes |
| `vm.py` | 8-byte DVD VM command decoder → spec §8.2 JSON |
| `iso9660.py` | Minimal ISO 9660 reader: extracts `VIDEO_TS/*.IFO|*.BUP` to a temp dir |
| `make_fixture.py` | Builds the hand-crafted IFO fixtures in `fixtures/` |
| `test_ifo.py` | 24 tests: VM vectors, both fixtures end-to-end, ISO input, fatal-error case |
| `fixtures/disc1/`, `fixtures/disc2/` | Synthetic discs (NTSC 1-title; PAL 2-title with angles/stills/hidden PGC) |
| `TEST_NOTES.md` | Per-disc results table |

Run the tests: `python3 test_ifo.py` (rebuilds the fixtures first).

## What it parses

- **VMGI (`VIDEO_TS.IFO`)**: provider ID, volume/side, region mask,
  number of title sets, First Play PGC (byte offset `0x84`),
  title search pointers (title → VTS, VTS_TTN, chapters, angles),
  VMG menu PGCs by language unit, menu video/audio/subpicture
  attributes. A bad/missing `.IFO` falls back to the `.BUP`.
- **VTSI (`VTS_XX_0.IFO`)**: title/menu video attributes
  (NTSC/PAL, 4:3/16:9, resolution), up to 8 audio attributes
  (codec AC-3/MPEG/LPCM/DTS, channels, sample rate, language,
  code extension) and up to 32 subpicture attributes (language,
  forced flag via code extension 9), PTT/chapter table, all title
  PGCs, all menu PGCs by language unit.
- **PGCs**: programs + program map, cells (playback time, still
  time, block mode/type for angle/interleaved blocks, VOB/cell
  IDs, first/last sectors, cell-command number), pre/post/cell
  commands, palette (YCbCr → `#RRGGBB`, BT.601), next/prev/go-up
  PGC numbers, still time.
- **Chapters**: from `VTS_PTT_SRPT`, with times computed as the
  sum of selected-path cell durations (angle blocks count once)
  in PTT playback order; a title spanning several PGCs accumulates
  across them. BCD times use the literal interpretation
  (H/M/S literal + frames/fps, as in libdvdnav's
  `dvdnav_convert_time`), not the frame-count interpretation.
- **Hidden PGCs**: `pgcs[].hidden=true` for PGCs not reachable
  from First Play, any menu PGC, any title entry PGC, or any
  chapter PGC, following Link/Jump/Call targets and
  next/prev/go-up edges transitively. Reachable extras (e.g. a
  PGC reached only by `LinkNextPGC` from a title's post-commands)
  are correctly *not* hidden.
- **Menus**: kind `menu`, palette, still/timeout (from a
  `SetNVTMR` pre-command), background-loop heuristic (post-command
  links back to the top of the PGC), first present audio track.
  VMG menu PGCs have no slot in spec §3.4, so they are carried as
  a `vmg.menu_pgcs` extension (players ignore unknown fields);
  VTS menu PGCs live in `titlesets[].pgcs[]` as
  `vts<N>-menu-pgc-<k>`.

## VM command coverage (§8.2)

Decoded (status `ok`): NOP, Goto, Break, SetTmpPML; Link subset /
LinkPGCN / LinkPTTN / LinkPGN / LinkCN; Exit, JumpTT, JumpVTS_TT,
JumpVTS_PTT, JumpSS (First Play / VMG menu / VTS menu / VMG PGC),
CallSS (same targets + resume cell); SetSTN (audio/subpicture/
angle, immediate or register — emitted as `set_audio` /
`set_subtitle` / `set_angle`, or one combined `set_stn` when
several are set at once); SetNVTMR, SetGPRMMD (incl. counter
mode), SetAMXMD, SetHL_BTNN; SetGPRM move/swap/add/sub/mul/div/
mod/random/and/or/xor (immediate or register source).
Compare-guarded commands (byte 1 bits 6–4 ≠ 0) are emitted as
`{"op":"compare","operator":...,"left":...,"right":...,"then":<inner>,
"else":null}` — the `else` is fall-through to the next command,
which is what the disc does. A trailing Link on a Set command is
kept under the command's `"link"` key.

Not fully decoded — recorded, never silently dropped:

- **Compound Set/Compare/Link (families 4–6)**: decoded
  best-effort with `"decode":"partial"` and the raw bytes; the
  set source / compare operand packing is not pinned down by any
  verifiable public source. Counted in
  `diagnostics.partial_decodes`.
- **Family 7 and unknown sub-commands / illegal CMP+LNK
  combinations**: `{"op":"unsupported","raw_hex":"...","note":"..."}`,
  counted in `diagnostics.unsupported_commands` (collapsed by
  raw hex, with a count and every location).

## Diagnostics object

Top-level `"diagnostics"` in every manifest:

```json
{
  "parser_version": "0.1.0",
  "parser": "odvd-parser 0.1.0 (IFO-only)",
  "input_kind": "iso" | "folder",
  "unsupported_commands": [{"raw_hex", "count", "locations", "note"}],
  "unsupported_command_count": 0,
  "partial_decodes": [{"raw_hex", "count", "locations", "note"}],
  "partial_decode_count": 0,
  "warnings": ["VTS_01_0.BUP is missing (no backup copy on disc)", "..."],
  "vts_parsed": [1],
  "vts_declared": 1
}
```

Warnings also cover: missing BUP, empty title (TT_SRPT chapters
with no PTT entries), TT_SRPT/PTT chapter-count mismatch, VTS
whose IFO is missing, video-standard disagreement between title
sets, TXTDT_MG present but unparsed, truncated tables, a PGC
with cells but no programs, a command table over the 128-command
spec limit.

## Known gaps (v0.1 — do not mistake these for parser bugs)

1. **Button geometry**: lives in VOB PCI packets (`HLI`), not in
   the IFO. Every menu PGC is emitted with `"buttons": []` and a
   `buttons_note` saying so. PCI parsing is the next parser step;
   it needs VOB reading, which this version deliberately avoids.
2. **ISO support is ISO 9660 only.** DVD ISOs are normally
   ISO 9660 + UDF 1.02 bridges, so the ISO 9660 side is present.
   A UDF-only image is rejected with a clear error — extract its
   `VIDEO_TS` folder and pass the folder instead. Only
   `.IFO`/`.BUP` are extracted (the v0.1 parser is IFO-only; VOBs
   are hundreds of MB and unread yet). Multi-extent files and
   Joliet/Rock Ridge long names are not needed for `VIDEO_TS`.
3. **No disc/title/chapter names**: they live in `TXTDT_MG`,
   which is not parsed in v0.1. The disc title comes from the ISO
   volume label, else the folder name, else the provider ID, else
   `"Unknown DVD"` (provenance in `disc.title_source`). Chapter
   `name` is `null`.
4. **Audio bitrate is `null`**: the IFO carries no bitrate; it
   needs the VOB stream. Channels / sample rate / codec /
   language come from the IFO.
5. **VM coverage is incomplete by design** (spec §16.3): see
   "VM command coverage" above. Unsupported commands are
   diagnostics, not errors.
6. **Menu PGC category decoding is an assumption**: the PGCI_UT
   search-pointer category is read as entry = bit 31, menu ID =
   bits 27–24. The raw category hex is kept in the parsed data
   path (and the decoded `menu_id`/`menu_name` in the manifest)
   so a real-disc check can confirm or correct it.
7. **Chapter times assume each PGC plays to its end** before the
   next PTT PGC starts (the standard reading; post-command
   early exits are not modelled).
8. **Cell `original_dvd_timecode`** is the cell's start time
   within its PGC (there is no absolute disc timecode in the IFO);
   `frame_count` is `round(duration × fps)`.

## Design notes / sources

Field offsets follow `dvdnav.mplayerhq.hu/dvdinfo/ifo.html`,
`freemkv.org/docs/dvd/` (IFO structures, PGC fields, cell
playback, command encoding), `dvd.sourceforge.net/dvdinfo/
vmi.html` + `ifo_vts.html` (VM instruction set, PGCI_UT), and
libdvdread's `ifo_types.h` (structure field order, audio/subpicture
control bitfields: audio present = `0x8000`, stream = bits 10–8;
subpicture per display mode byte = `0x80 | stream`).

Conditional-compare operand positions follow freemkv's
"Command encoding" table (Special/Link: left = byte 3, right =
BE16 bytes 4–5 or register byte 5; SetGPRM: left = byte 2,
right = BE16 bytes 6–7 or register byte 7; SetSystem/Jump:
left = byte 6, right = byte 7). This is the least-certain part
of the decoder. Decoded commands do **not** carry their raw bytes
(the output follows the spec §8.2 shapes exactly); only
unsupported / partial commands keep `raw_hex`. If a guarded
command ever decodes wrong against a reference player, the fix
belongs here, in `vm._compare_guard`.
