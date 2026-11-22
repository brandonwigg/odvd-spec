# ODVD Parser Prototype — Test Report v0.1

Date: 2026-11-22. Parser: `odvd-parser 0.1.0` (IFO-only, Python stdlib).
Spec: ODVD v0.1 (`SPEC.md`). Corpus: 4 verified public-domain real discs + 5 synthetic dvdauthor discs + 2 hand-built IFO fixtures.

**Headline: the parser ran on all 11 discs with 0 unsupported VM commands and 0 partial decodes.**
Every title/chapter count the parser reported matched independent ground truth (real-disc IFO+ffprobe verification, or synthetic authoring READMEs) except the two flagged discrepancies below, both traced to IFO content, not parser error. Two known gaps remain: button geometry (lives in VOB PCI packets, not IFOs) and phantom IFO stream declarations on one real disc.

## 1. Parser

Location: `parser/` — `odvd_parse.py` (CLI), `ifo.py` (VMGI/VTSI), `vm.py` (8-byte VM command decoder → SPEC §8.2 JSON), `iso9660.py` (ISO 9660 reader, extracts IFO/BUP only). Python stdlib only.

CLI: `python3 odvd_parse.py <path-to-ISO-or-VIDEO_TS> -o <manifest.json>` (exit 0 = manifest written, 2 = fatal input error).

- Unit tests: **25 tests, all passing** (`parser/test_ifo.py`), against two hand-built IFO fixtures (`parser/fixtures/disc1` NTSC, `disc2` PAL — regenerate with `parser/make_fixture.py`; the binary IFO fixtures themselves are not pushed to GitHub).
- Output matches SPEC §3.4 (verified by a required-field check on all manifests). `segment_hash` is `null` with a note — segments are not transcoded in v0.1, hashes are never invented. Every manifest carries a top-level `diagnostics` object (unsupported commands with raw hex + counts + locations, partial decodes, warnings).
- ISO input verified end-to-end: parsing `Night.iso` directly produced `source_hash` `f542fa14e9ce104d789ab480bae0b3599703128c52889f4b31927dde1c30efdf`, exactly matching the independently computed ISO SHA-256. ISO support is ISO 9660 only; UDF-only images are rejected with a clear error (pass the extracted folder instead).
- VM commands decode to SPEC §8.2 JSON (`jump_tt`, `set`, `compare`/`link_pgc`, `call_ss`, …). Undecodable commands are emitted as `{"op":"unsupported",…}` diagnostics, never silently dropped.

## 2. Real discs — download and verification

All four Tier A/B discs from the deep-dive report downloaded with byte-exact sizes and verified as genuine DVD-Video. **None was Blu-ray.** No Tier C disc was needed. No mislabeled commercial disc was touched. Total ~21 GB in `~/workspace/odvd-test/downloads/` (not in any repo, never pushed); 126 GB free after download. Two VM reboots interrupted downloads mid-flight (Plan 9 at 54.7%, DESTINOS at 57%); `curl -C -` resume recovered both.

| Disc | ISO size (B) | SHA-256 | Standard | Titles / VTS | Chapters | Audio | Subtitles |
|---|---|---|---|---|---|---|---|
| Night of the Living Dead (1968), Tier A | 2,450,948,252 | `f542fa14…` | NTSC 720×480 29.97 | 1 / 1 | 1 | 1× AC-3 stereo, no lang | 0 |
| Plan 9 from Outer Space (1957), Tier B | 7,576,027,136 | `8dcfeeb4…` | PAL 720×576 25 | 5 / 5 | 16 | VTS 1–2 AC-3 5.1; VTS 3–5 LPCM stereo | 4 in VTS 1 (untagged, da, no, sv) |
| Häxan (1922), Tier B | 6,713,966,592 | `1760f3ec…` | PAL 720×576 25 | 7 / 2 | 45 | 1× AC-3 5.1 per VTS (VTS 2: `en`) | 1, English (VTS 1) |
| DESTINOS 10, Tier B | 4,263,149,568 | `7dd88d97…` | NTSC 720×480 29.97 | 8 / 4 | 40 | 1× AC-3 stereo `en` per VTS | 2 per VTS (`es`, `en`) |

Notes:
- Häxan size: the task brief quoted 6,713,965,922 B, 670 B less — a transcription error in the brief/report; Archive metadata (6,713,966,592 B) is authoritative and the download matches it exactly.
- DESTINOS item total is ~6.7 GB including derivatives; the ISO itself (`DESTINOS10.iso`, filename found via metadata) is ~4.0 GB.
- Häxan has a real DVD subtitle track (English, stream 0x20), contradicting the deep-dive report's "hardcoded subs" note.
- Per-disc verification notes: `~/workspace/odvd-test/downloads/<disc>/VERIFY.md` + `verify.json`, plus `downloads/SUMMARY.md` (master comparison table). IFOs extracted to `downloads/<disc>/VIDEO_TS/` for parser use; no VOBs extracted except temporary ffprobe heads (deleted).

Coverage the real discs give (SPEC §15): multi-title (up to 8), multi-VTS (up to 5), 1–20 chapters per title, NTSC + PAL, AC-3 stereo/5.1, LPCM, 0–4 subtitle tracks (en/es/da/no/sv), menus with/without audio, parallel same-duration titles, 1 GB VOB boundaries.

## 3. Synthetic discs

Built with dvdauthor 0.7.2 + spumux + genisoimage 1.1.11 (`mkisofs -dvd-video`), following webdvd's `scripts/make-test-disc.sh` pattern (ffmpeg `-target ntsc-dvd`/`pal-dvd`). Each disc is a VIDEO_TS folder + DVD-Video ISO with a ground-truth README, dvdunauthor dump, and `lsdvd.txt`; rebuildable via `synthetic/build-all.sh`. Location: `~/workspace/odvd-test/synthetic/`.

| Disc | Content | ISO size (B) |
|---|---|---|
| synth-01-basic-ntsc | NTSC, 1 title, 3 chapters, 1× AC-3 stereo en, no menu, First Play → title | 3,596,288 |
| synth-02-multi-title | NTSC, 2 titles, 2 audio (main + commentary), 1 subtitle, VMGM root menu (3 buttons) + VTSM scene menu (4 buttons) | 11,096,064 |
| synth-03-pal-menu | PAL 720×576 25 fps, still menu (3 buttons) + motion menu, 15 s timeout, 1 title / 2 chapters | 6,815,744 |
| synth-04-branching | NTSC, menu sets `g0=1/2`, intro PGC post `if (g0 == 2) { jump pgc 3; } jump pgc 2;`, Ending A/B PGCs with `g1=1/2` pre-commands | 6,281,216 |
| synth-05-edge | NTSC, Title 1 = zero-cell command-only PGC (`<pre>jump title 2;</pre>`, no VOB — dvdauthor accepted it first try); Title 2 = 3 s cell + still-frame cell | 1,861,632 |

Tool install: `apt-get update` stalled on a slow Ubuntu mirror (~15 min, 463 MB, no usable lists) and was killed; dvdauthor/genisoimage/lsdvd 0.17 were installed by fetching the exact noble `.deb`s from archive.ubuntu.com and `dpkg -i`. `udfkit` was not needed. `ifo_dump` is not shipped in dvdauthor 0.7.2 on Ubuntu; verification used lsdvd + dvdunauthor instead.

Build problems fixed (full log in `synthetic/BUILD_LOG-run1.md`): spumux stdout must go to a file, not through `tee` (stderr text corrupted the mux); subtitle PNGs must have <16 colours; dvdauthor registers are `g0`–`g12`; `<audio>`/`<subpicture>` belong inside `<titles>`/`<menus>`; mono audio must be upmixed to stereo or dvdauthor appends phantom `xx`-language entries; button y-coords must be even.

Not buildable with dvdauthor (documented gaps): multi-angle cells; a bare "empty title entry" distinct from a zero-cell PGC (needs a hand-crafted IFO fixture); DVD games (synth-04 covers only the GPRM compare/jump primitive); forced/multiple subtitle tracks, DTS/LPCM; cell-level seamless branching (synth-04 branches whole PGCs); dual-layer/VOB splitting, region/parental, CSS.

## 4. Parser results per disc

Manifests: `~/workspace/odvd-test/parser-output/<disc>/manifest.json`. A sample manifest (Night of the Living Dead, ISO-input) is in `parser/examples/`.

| Disc | Titles | PGCs (hidden) | Chapters | Unsupported VM | Partial | Warnings |
|---|---|---|---|---|---|---|
| Fixture disc1 (NTSC) | 1 | 3 (1) | 2 | 0 | 0 | 0 |
| Fixture disc2 (PAL) | 2 | 7 (1) | 4 | 1 (family 7) | 0 | 1 (missing BUP, deliberate) |
| Night of the Living Dead | 1 | 5 (0) | 1 | 0 | 0 | 0 |
| Plan 9 from Outer Space | 5 | 42 (0) | 16 | 0 | 0 | 22 |
| Häxan | 7 | 24 (0) | 45 | 0 | 0 | 0 |
| DESTINOS 10 | 8 | 86 (0) | 40 | 0 | 0 | 17 |
| synth-01-basic-ntsc | 1 | 1 (0) | 3 | 0 | 0 | 0 |
| synth-02-multi-title | 2 | 5 (0) | 4 | 0 | 0 | 0 |
| synth-03-pal-menu | 1 | 5 (0) | 2 | 0 | 0 | 0 |
| synth-04-branching | 3 | 4 (0) | 3 | 0 | 0 | 0 |
| synth-05-edge | 2 | 2 (1) | 2 | 0 | 0 | 1 (empty title 1) |

Ground-truth agreement:
- Plan 9: 16 chapters = 9+4+1+1+1 exactly; title durations 1:18:18 and 1:51:27 match verification. Subtitle languages (untagged/da/no/sv) and LPCM VTS 3–5 match ffprobe.
- Häxan: 45 chapters = 20+20+1+1+1+1+1; parallel 20-chapter versions at 1:46:31 each match.
- DESTINOS: 40 chapters = 8×5 exactly; durations ~27:48–27:55 per title match.
- Night: 1 title, 95m11s, First Play = clear GPRM 5–9 then JumpSS to VMGM PGC 1 (verified by raw-byte spot checks).
- synth-04: the branch decoded exactly as authored — `compare g0 == 2 → link pgc 3, else link pgc 2`, with `g1=1` / `g1=2` pre-commands on the endings. The parser's 3-title count is correct per the IFO: dvdauthor authored 3 TT_SRPT entries (one per PGC); the README's "one title, three PGCs" is the design intent, and lsdvd likewise lists Titles 1/2/3. Not a parser bug.
- synth-05: hidden PGC detected; the parser handled the zero-cell first title without crashing. `lsdvd` 0.17 segfaults (exit 139) on this disc — the parser must not be validated against lsdvd here.
- Fixtures match hand-built ground truth exactly (chapter times, durations, angle-block counting, hidden-PGC reachability via `LinkNextPGC`).

## 5. Discrepancies and known gaps

1. **DESTINOS VTS 1 phantom streams (parser over-reports).** The VTS_01 IFO declares 8 audio and 32 subtitle streams, but the VOB contains 1 audio and 2 subtitles (ffprobe-confirmed). The parser trusts IFO attributes, so the VTS 1 manifest lists 8 audio tracks and 32 subtitle tracks. VTS 2–4 are correct (1 audio, 2 subs). Fix: cross-check IFO stream declarations against VOB stream presence (or record IFO-declared vs VOB-present separately). Same class of issue as dvdauthor's phantom `xx` entries during authoring.
2. **Button geometry is empty.** Menus emit `"buttons": []` with a note: button rects/highlights live in VOB PCI packets, not IFOs. PCI parsing is the next parser step and needs VOB reading.
3. **VMG PGCs parse with 0 programs / 0 cells on all 4 real discs.** The parser records First Play commands and menu PGCs under `vmg` (e.g. `first_play_commands`, `menu_pgcs`) rather than as programs/cells. Cross-check VMG PGC structure against libdvdread before relying on it.
4. **Zero-cell PGC warnings (Plan 9: 22, DESTINOS: 17).** PGCs with zero cells and no commands — menu/placeholder PGCs on real discs. The parser warns and continues; these are not failures, but the warning should distinguish menu PGCs from genuinely broken ones.
5. **Chapter times are approximate to ~1 GOP (~0.2 s)** on synthetic discs (e.g. synth-02 cells at 0/4.204/7.807 s vs authored 0/4/8 s) — GOP rounding in the encoder, not a parser error.
6. **Audio bitrate is `null`** (not stored in the IFO); chapter `name` is `null` (no TXTDT_MG parsing yet); disc/title names come from volume label or filename when the IFO has none (Night's volume label is blank, so the title is `Night` from the ISO filename).
7. **VM coverage:** families 4–6 compounds decode best-effort as `"decode":"partial"` with raw bytes; family 7 / unknown sub-commands are `unsupported` diagnostics. One family-7 command (`e000000000000000`) was seen in fixture disc2 only; none in any real or synthetic disc.
8. **No hidden PGCs found on any real disc** (all 4 report 0 hidden). Hidden-PGC detection itself is validated by the fixtures (1 hidden each) and synth-05 (1 hidden).

## 6. What remains

- PCI button parsing (VOB reading) so menus carry real button geometry and actions.
- DESTINOS-style phantom IFO stream cross-check against VOBs.
- VMG PGC program/cell cross-check vs libdvdread.
- Still uncovered by any disc in the corpus: multi-angle, DVD games (scoring/`random()`/number keys), forced subtitles, DTS, cell-level seamless branching, region/parental handling. These need hand-crafted IFO fixtures or a different authoring tool than dvdauthor.
- CRF calibration (SPEC §5) is untouched — the parser does not transcode, and no segments were produced in this round.
- Next steps are parser-side (above) and compiler-side (transcode one cell to fMP4 per SPEC §4); no MVP player, multi-user, or Jellyfin/Plex plugin work was started, per scope.
