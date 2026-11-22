# ODVD parser — test notes (v0.1)

Parser: `odvd-parser 0.1.0 (IFO-only)`. Test suite: `python3 test_ifo.py`
— **25 tests, all passing** at the time of writing (24 original + 1
regression test for the register-source `Set` bug found on the real
disc below).

How to re-run everything:

```sh
cd ~/workspace/odvd-spec/parser
python3 test_ifo.py                                   # fixtures + VM vectors + ISO
python3 odvd_parse.py <iso-or-VIDEO_TS> -o <out.json>  # any disc
```

## Per-disc results

| Disc | Parsed | Titles | PGCs (hidden) | Chapters | Audio / subtitle tracks | Unsupported VM cmds | Warnings | Manifest |
|---|---|---|---|---|---|---|---|---|
| fixture disc1 (NTSC, synthetic) | Y | 1 | 3 (1) | 2 | 2 AC-3 (en 5.1, es stereo) / 2 subp (en, en forced) | 0 | 0 | `parser/fixtures/disc1/manifest.json` |
| fixture disc2 (PAL, synthetic) | Y | 2 | 7 (1) | 4 | 2 (en AC-3 stereo, en LPCM stereo) / 1 subp (fr) | 1 (family 7) | 1 (missing `VTS_01_0.BUP`) | `parser/fixtures/disc2/manifest.json` |
| Night of the Living Dead (NTSC, real ISO) | Y | 1 | 5 (0) | 1 | 1 AC-3 stereo, no language code / 0 subp | 0 | 1 (missing `VTS_01_0.BUP`) | `odvd-test/parser-output/night-of-the-living-dead/manifest.json` |
| `odvd-test/downloads/*` (anything else) | — | — | — | — | — | — | — | not yet available |
| `odvd-test/synthetic/*` | — | — | — | — | — | — | — | not yet available (folder empty) |

Counts of PGCs include menu PGCs (VMGM under `vmg.menu_pgcs`,
VTSM under `titlesets[].pgcs[]`).

### Fixture disc1 — NTSC, 1 VTS, 1 title (ground truth: hand-built)

- Title 1: 2 chapters, duration 180.0 s (ch. 2 at 90.0 s). First Play
  pre-commands decode to exactly `[{"op":"jump_tt","title":1}]`.
- `vts1-pgc-2` correctly `hidden: true` (nothing references it or
  links to it); `vts1-pgc-1` not hidden. VMGM menu PGC loops
  (`background_loop: true`, infinite still cell) with empty
  `buttons: []` (PCI gap, by design).
- The compare-guarded Set in PGC1 decodes as a `compare` object
  (`GPRM3 == 2` → `GPRM0 = 1`). All 3 PGCs, both audio tracks, both
  subtitle tracks (incl. forced) present. Nothing missed vs the
  fixture spec.

### Fixture disc2 — PAL, 1 VTS, 2 titles (ground truth: hand-built)

- Title 1 spans PGC1 + PGC2: chapters at 0.0 / 30.0 / 85.0 s,
  duration 205.0 s. Title 2: 1 chapter, 120.0 s. Angle block in
  PGC1: cells carry `angle` 1 and 2 and the block counts once
  (`selected_path_seconds` 85.0). Still cell (5 s) recorded.
- `vts1-pgc-5` correctly `hidden: true`; `vts1-pgc-4` correctly
  **not** hidden — it is reached by PGC3's `LinkNextPGC`
  (reachability-through-relative-links verified).
- Exactly 1 unsupported command: raw `e000000000000000`
  (family 7) in `vts1-pgc-3/pre[0]`, counted in diagnostics.
  VTSM menu PGC: nav-timer timeout 30 s from `SetNVTMR`.
  Missing `VTS_01_0.BUP` (deliberate) surfaces as the only warning.
- Nothing missed vs the fixture spec.

### Night of the Living Dead — real ISO (ground truth: none supplied)

- Source: `odvd-test/downloads/night-of-the-living-dead/Night.iso`
  (public-domain 1968 film). **The ISO was still downloading when
  parsed** (2.15 GB of ~2.45 GB at first parse, 2.38 GB at the last
  re-parse). The IFOs sit at the start of the image so they parsed
  fine, but two consequences:
  1. `disc.source_hash` in the manifest is the SHA-256 of the
     **partial file as it existed at parse time** — it will change
     once the download finishes. **Re-run the parser after the
     download completes.**
  2. `VTS_01_0.BUP` lives at disc sector 1,196,449 (near the end
     of the image) and was past end-of-file at parse time. The
     extractor now skips an unreadable `.BUP` instead of failing
     (a BUP is only a fallback); the manifest records it as the
     warning `VTS_01_0.BUP is missing (no backup copy on disc)`.
     Re-running after the download completes should clear it.
- Parsed result: 1 title, 1 chapter, 1 cell, duration 5711.334 s
  (95 min 11 s + 10 frames — consistent with the film's ~96 min
  runtime). NTSC 4:3 720x480, 1 AC-3 stereo track with **no
  language code** in the IFO (`language: null` — the disc really
  does not set one), no subtitle tracks. 5 PGCs, 0 hidden:
  3 VMGM (one title-menu entry, two command-only PGCs with
  category `0x00000000`), 1 title PGC, 1 VTSM menu PGC (root
  menu, category `0x83000000`, pre-command jumps to VMGM PGC 1).
- First Play decodes as five `Set GPRM = 0` (GPRM 5–9) then
  `JumpSS → VMGM PGC 1` — a plausible disc-authored init.
  Title PGC post-command is `CallSS → VMGM PGC 1` with
  `resume_cell: 1`, verified byte-for-byte against the IFO
  (`30 08 00 01 01 c0 00 00`).
- **Bug found on this disc and fixed**: the title PGC's first
  pre-command is raw `61 00 00 07 00 06 00 00` = `GPRM7 = GPRM6`.
  The decoder originally spread the register source flat into
  the object, so the source `{"gprm": 6}` overwrote the
  destination and produced the nonsense `GPRM6 = GPRM6`. Register
  sources now live under a nested `"source"` key
  (`{"op":"set","gprm":7,"operation":"mov","source":{"gprm":6}}`),
  with a regression test in `test_ifo.py`
  (`test_set_gprm_reg_gprm_keeps_dest`). Same fix applied to
  `SetNVTMR` / `SetGPRMMD` / `SetAMXMD` / `SetHL_BTNN`.
- What the manifest missed vs the real disc (known v0.1 gaps,
  not parse errors): button geometry (PCI/VOB, not parsed);
  chapter names (TXTDT_MG not parsed — and this disc has only
  1 chapter anyway); audio bitrate (`null` — not in the IFO);
  disc title is `Night` from the ISO filename because the PVD
  volume label is blank (spaces). No synthetic README / ground
  truth existed for this disc, so verification was by
  plausibility + raw-byte spot checks as above.

## Not yet available (checked at finish time)

- `~/workspace/odvd-test/downloads/`: only
  `night-of-the-living-dead/Night.iso` (above, still downloading).
  No other ISO / VIDEO_TS folder was present.
- `~/workspace/odvd-test/synthetic/`: empty. No synthetic discs
  from the other track existed yet, so no synthetic-disc results
  could be recorded. Re-run the parser over whatever lands there:
  `python3 odvd_parse.py <path> -o ~/workspace/odvd-test/parser-output/<disc>/manifest.json`.
