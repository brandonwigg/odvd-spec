# ODVD Specification v0.1

**Status: draft. Not stable. Implementations of v0.1 must tolerate forward-compatible additions and must reject bundles whose `odvd_version` major version they do not understand.**

**Audience:** implementers of the ODVD compiler (creator) and the ODVD player / platform. Homelab deployers are not the audience for this document except as background.

---

## 1. Overview

### 1.1 Goals

1. Faithful 1:1 digital recreation of a personal DVD collection.
2. Preserve the original DVD experience: menus, navigation, extras, Easter eggs, hidden titles, and DVD games.
3. Minimize space and serving compute without losing quality or experience: visually lossless video, content-addressed deduplication of shared assets, compile-once / serve-static.
4. Playable in a modern web browser with no plugin, no Kodi, and no VLC install on the viewing device.
5. Self-deployable and open. The format is the contract; anyone can implement a player or a compiler for it.

### 1.2 Non-goals

- Blu-ray, UHD Blu-ray, HD DVD, or any BD-J / HDMV navigation. DVD-only.
- Ripping or decryption. The compiler input is an already-decrypted ISO or `VIDEO_TS` folder. See §14.
- A Netflix-style library. The library UI is a shelf of boxes. Playback is the DVD experience, not a title detail page with a Play button that skips the menu.
- Bit-perfect archival of the MPEG-2 stream. See §2.2. The archival master for audio is preserved (AC-3/DTS/LPCM); video is visually lossless by design.

### 1.3 Principles

1. **Navigation is data, not video.** The manifest describes navigation. The video segments are dumb. This is what makes re-encoding possible without breaking menus.
2. **Compile once, serve static.** All transcoding happens at compile time. At play time the server only serves static files over HTTP range requests. No live transcoding in the normal playback path.
3. **The player is a segment player + VM + overlay renderer.** It does not emulate a physical drive, does not parse MPEG-2 program streams, and does not do sector seeks. See §8.
4. **Deduplication is by canonical hash, not by filename.** Identical content after canonical transcoding hashes identically and is stored once.
5. **Codec is a library-level choice.** All discs in a library use the same video codec profile. This is required for deduplication to work across discs. See §5 and §11.
6. **Preserve first, optimize second.** If a feature works on a DVD player, the format must be able to represent it, even if v0.1 players implement it simplistically. Do not drop a feature to simplify the spec.
7. **Offline-first.** A compiled library must be playable with no internet access. Online metadata is opt-in enhancement only. See §12.

---

## 2. Definitions

### 2.1 1:1

A `.odvd` bundle is 1:1 with its source disc if all of the following hold:

- First Play PGC, FBI / copyright warnings, forced trailers, and studio logos play in the source order, including forced (unskippable) segments.
- Every title, chapter, program, cell, and angle on the disc is present and reachable.
- Every menu (VMGM and VTSM) is present, with its background video, audio, buttons, button geometry, and highlight / select states.
- Every audio track and every subtitle track is present, with correct language, track order, and default / forced flags.
- Every Program Chain (PGC), including hidden PGCs not reachable from any menu, is present in the manifest. Hidden PGCs must remain reachable by the same button sequence or hotspot that reaches them on the disc.
- DVD games work. The VM state model in §8 must be complete enough to play games that use GPRM state, scoring, random branching, and numeric input. Example: the Harry Potter DVD game.
- Still-frame menus wait for input. They do not loop or time out unless the disc specifies a timeout.
- Seamless branching selects the correct cells for the chosen cut / angle / parental level.
- Multi-disc box sets are represented as multiple discs grouped as a set. Each disc is its own bundle. See §12.1.

A bundle that omits menus, drops a hidden title, or flattens extras into a file list is not 1:1 and must not claim to be.

### 2.2 Visually lossless

Video is visually lossless if a viewer cannot tell the difference from the original DVD in normal viewing, on a normal display, at normal viewing distance, while the file is substantially smaller than the source MPEG-2.

Visually lossless is not bit-perfect. The source MPEG-2 is itself lossy. Re-encoding it losslessly (for example H.265 lossless) preserves the decoded pixels exactly but is often larger than the source and saves no space. That mode is not the default and is not required by this spec.

The default profiles in §5 are calibrated to be visually transparent for 480i/576i DVD sources. Exact CRF numbers are to be calibrated against the test corpus in §15 and are marked as provisional in v0.1. Do not compress to the point where artifacts are visible on pause or on large displays. The target is roughly 50–60% smaller than the source MPEG-2 for the main feature, not 80–90%.

Audio has an equivalent rule: the default is to preserve the original AC-3/DTS/LPCM bitstream (see §6). The Opus opt-in is a space-saving conversion that is aurally transparent at the bitrates in §6, not bit-perfect.

---

## 3. Bundle structure

### 3.1 What a `.odvd` is

A `.odvd` bundle is a directory (or a zip-like archive of that directory with the same internal layout). It is **not** a self-contained video file.

```
my-disc.odvd/
  manifest.json
  segments/
    <cell-hash>.mp4          # fMP4, one file per cell, see §4
    <cell-hash>.mp4
  overlays/
    <overlay-hash>.png       # button highlights, menu stills, subtitle bitmaps if stored separately
  inserts/
    cover.jpg                # optional, user-supplied or online, never required for playback
    notes.md                 # optional
```

The bundle on disk is the portable unit. The content-addressed asset store (CAS) is the shared store on the server. See §11.

In the normal server layout, `segments/` inside a bundle are not copies. They are references to the CAS. An implementation may store them as:

- hard links to files in the CAS, or
- a manifest that lists only hashes and the server resolves hashes to CAS paths at request time.

Either is conformant as long as the manifest is self-describing and the player can resolve a segment hash to bytes via HTTP. For portability (copying a single `.odvd` to another machine), an implementation must be able to materialize a self-contained bundle by copying the referenced CAS files into `segments/`.

### 3.2 Physical vs logical layout on the server

Library and assets live together physically but are logically separate:

```
/data/
  library/
    <disc-id>/
      manifest.json
      inserts/
    <set-id>/
      manifest.json          # box-set grouping, see §12.1
  assets/
    <sha256-prefix>/<sha256>.mp4
    <sha256-prefix>/<sha256>.opus
    <sha256-prefix>/<sha256>.png
```

- `library/` holds manifests (small JSON), grouping, resume state, and user metadata.
- `assets/` holds content-addressed segments (large media). It is shared across all libraries on the same server.
- A library must never store a duplicate copy of an asset that already exists in `assets/`. The compiler checks the hash first.

### 3.3 Files

- `manifest.json` — required. UTF-8 JSON. See §3.4. Must be parseable without any other file.
- `segments/` — required if the disc has video. One fMP4 per cell. See §4.
- `overlays/` — optional. PNGs for button highlights and subtitle bitmaps that are not embedded in segments.
- `inserts/` — optional. Cover art, notes, user-supplied metadata. Never required for playback. Must not affect navigation.

A bundle must not contain VOB, IFO, or BUP files. Those are compiler inputs, not bundle outputs.

### 3.4 Manifest schema

`manifest.json` is the whole disc as data. Version field is required.

```json
{
  "odvd_version": "0.1",
  "bundle_id": "dvd-8f3a2c1e-4b5a-4f1a-9c2e-1a2b3c4d5e6f",
  "disc": {
    "title": "Example Film",
    "title_from": "disc",
    "volume_label": "EXAMPLE_FILM",
    "video_standard": "NTSC",
    "disc_side": 1,
    "disc_count_in_set": 1,
    "set_id": null,
    "set_title": null,
    "region": null,
    "source_hash": "sha256-of-source-iso-or-video-ts",
    "compiled_at": "2026-11-21T08:00:00Z",
    "compiler": "odvd-tools 0.1.0",
    "codec_profile": "av1",
    "audio_mode": "ac3-keep"
  },
  "vmg": {
    "first_play_pgc": "vmg-pgc-1",
    "titles": [
      { "title_number": 1, "vts": 1, "entry_pgc": "vts1-pgc-1", "duration_seconds": 7320, "chapters": 28, "angles": 1 }
    ]
  },
  "titlesets": [
    {
      "vts": 1,
      "audio_tracks": [
        { "index": 0, "language": "en", "codec": "ac3", "channels": 6, "bitrate_kbps": 448, "default": true }
      ],
      "subtitle_tracks": [
        { "index": 0, "language": "en", "forced": false, "default": false }
      ],
      "pgcs": [
        {
          "id": "vts1-pgc-1",
          "kind": "title",
          "hidden": false,
          "pre_commands": [
            { "op": "set", "gprm": 0, "value": 1 }
          ],
          "programs": [
            {
              "program_number": 1,
              "cells": [
                {
                  "cell_id": "vts1-pgc1-cell1",
                  "segment_hash": "a1b2c3d4e5f6...",
                  "segment_uri": "assets/a1/b2/a1b2c3d4e5f6....mp4",
                  "duration_seconds": 42.5,
                  "start_pts_seconds": 0.0,
                  "original_dvd_timecode": "00:00:00:00",
                  "frame_count": 1275,
                  "angle": 1,
                  "still": false,
                  "chapter": 1
                }
              ]
            }
          ],
          "post_commands": [
            { "op": "jump", "target_pgc": "vts1-pgc-2" }
          ]
        },
        {
          "id": "vts1-menu-pgc-1",
          "kind": "menu",
          "hidden": false,
          "menu": {
            "background_segment_hash": "b2c3d4e5f6a7...",
            "background_loop": true,
            "audio_track": 0,
            "still": false,
            "timeout_seconds": null,
            "palette": ["#000000", "#ffffff", "#ff0000", "#0000ff"],
            "buttons": [
              {
                "id": 1,
                "rect": { "x": 120, "y": 340, "w": 240, "h": 32 },
                "label": "Play Movie",
                "default": true,
                "up": 4,
                "down": 2,
                "left": 1,
                "right": 1,
                "highlight_overlay_hash": "c3d4e5f6a7b8...",
                "select_overlay_hash": "d4e5f6a7b8c9...",
                "action": { "op": "jump_tt", "title": 1 }
              },
              {
                "id": 2,
                "rect": { "x": 120, "y": 380, "w": 240, "h": 32 },
                "label": "Scene Selection",
                "default": false,
                "up": 1,
                "down": 3,
                "left": 2,
                "right": 2,
                "highlight_overlay_hash": "e5f6a7b8c9d0...",
                "select_overlay_hash": "f6a7b8c9d0e1...",
                "action": { "op": "jump_pgc", "target_pgc": "vts1-menu-pgc-2" }
              }
            ]
          },
          "pre_commands": [],
          "post_commands": []
        }
      ]
    }
  ],
  "chapters": [
    { "title": 1, "chapter": 1, "pgc": "vts1-pgc-1", "program": 1, "time_seconds": 0.0, "name": "Opening" }
  ],
  "dedup": {
    "asset_store": "assets/",
    "references": [
      { "hash": "a1b2c3d4e5f6...", "kind": "segment", "bytes": 12345678, "ref_count": 2 }
    ]
  }
}
```

Field rules:

- `odvd_version`: string, required. `"0.1"` for this spec. See §13 for compatibility.
- `bundle_id`: UUID, required. Stable for this disc. Changing the codec profile requires a new `bundle_id` and a recompile; do not reuse the same ID for a different codec.
- `disc.video_standard`: `"NTSC"` or `"PAL"`, required. See §10.
- `disc.codec_profile`: `"av1"`, `"h264"`, or `"h265"`, required. Library-level, see §5.
- `disc.audio_mode`: `"ac3-keep"` (default) or `"opus-only"` (opt-in), required. See §6.
- `disc.title_from`: `"disc"`, `"user"`, or `"online"`, required. Records provenance. See §12.3.
- `titlesets[].pgcs[].kind`: `"title"`, `"menu"`, `"game"`, or `"other"`. Games are PGCs with `kind: "game"`.
- `titlesets[].pgcs[].hidden`: boolean, required. True for PGCs not reachable from any menu. Compilers must set this by scanning all PGCs, not only menu-reachable ones.
- Button `rect`: coordinates in 720x480 space for NTSC, 720x576 for PAL. See §9. `x`, `y`, `w`, `h` are integers. The player scales them to the display.
- Button `up` / `down` / `left` / `right`: button IDs for remote navigation. Must match the disc's navigation.
- Button `action`: a VM command object. See §8.2.
- Cell `segment_hash`: SHA-256 hex of the canonical transcoded segment file. See §11.
- Cell `still`: true for still-frame cells. Player must pause on the last frame and wait for input.
- Cell `original_dvd_timecode`: source timecode, for diagnostics and frame-accurate seeking. Format `HH:MM:SS:FF`.
- `dedup.references`: informational, for the server. The player must not depend on it; it resolves segments via `segment_uri` or via hash lookup in the asset store.

Unknown fields in the manifest must be ignored by players (forward compatibility). Known fields must not be silently reinterpreted.

---

## 4. Segment format

### 4.1 One segment per cell

Each DVD cell becomes one fMP4 segment file. A cell is the smallest unit the VM can jump to. This is what makes frame-accurate navigation and seamless branching possible.

- Container: fragmented MP4 (fMP4), with `moov` suitable for HTTP range requests and fast start.
- Video: one video track per segment, per §5.
- Audio: one audio track per source audio track present in the cell, per §6. A segment may contain multiple audio tracks. The player selects the active track via SPRM. Do not bake a single audio track into the video and drop the others.
- Subtitles: bitmap subtitle track(s) as timed overlays. See §7. Do not burn subtitles into the video.
- The segment must start with a keyframe at the cell boundary. The player must be able to seek to any cell boundary without decoding the previous cell.
- Segments must be independently playable. A player must be able to load a single segment and play it without loading the whole disc.

### 4.2 Timecode mapping

- Each cell stores `duration_seconds`, `start_pts_seconds`, and `original_dvd_timecode`.
- The player maintains a virtual DVD time: the sum of cell durations in PGC order, plus the VM state. Seeking by chapter or by time must map to the correct cell and offset.
- Cell boundaries must be frame-accurate. The compiler must not drop or duplicate frames at cell boundaries. If the source has a frame that straddles a cell boundary, the compiler must split cleanly and record the split in the manifest. Do not round durations.
- Seamless branching: when a PGC contains alternate cells for different angles or cuts, the manifest lists them as alternate cells for the same program position, with `angle` and a branch condition. The player selects the cell at play time based on VM state, and preloads the next cell in the chosen branch.

### 4.3 Menu segments

Menu background video is a segment like any other, with `background_loop: true` in the menu object. The player loops it until the user makes a selection or a timeout fires. Menu audio is a normal audio track in that segment.

Still menus are segments with a single frame and `still: true`. The player shows the last frame and waits.

---

## 5. Video profiles

Video codec is a **library-level choice**, not per-disc. All discs in a library must use the same `codec_profile`. This is required for deduplication: the same preview transcoded with two different codecs hashes differently and will not deduplicate.

| Profile | Codec | Default | CRF guidance (provisional, v0.1) | Preset guidance | Notes |
|---|---|---|---|---|---|
| `av1` | AV1 | **Yes** | CRF 28–32, to be calibrated | Preset 6 (balanced) | Best space saving, ~20–30% smaller than H.265 at same visual quality for 480p. Decode support in Chrome, Firefox, Edge, Safari 17+. Encode is slowest, one-time only. Royalty-free, open. |
| `h264` | H.264 / AVC | No | CRF 16–18, to be calibrated | Preset slow | Fastest compile, plays everywhere including old devices. Largest files, ~30–40% larger than H.265 at same quality. Use for compatibility or fastest first library. |
| `h265` | H.265 / HEVC | No (opt-in) | CRF 18–20, to be calibrated | Preset slow | Good compression, hardware encode on Intel QuickSync. **Do not make this the default for a web player.** Chrome on desktop does not reliably play H.265 in `<video>`. Useful for Apple-heavy households and for users with existing H.265 libraries. |

Rules:

- The compiler must record the exact encoder, preset, and CRF used in the bundle's build metadata (not necessarily in the player-facing manifest, but in compiler logs / bundle metadata).
- Deinterlacing: DVD video is interlaced 480i / 576i. The compiler must deinterlace with a quality filter (for example yadif) and store as progressive 480p / 576p. Do not store interlaced video in segments. Do not upscale to 720p or 1080p in the segment; the player may upscale at display time.
- Resolution: preserve native resolution. NTSC 720x480, PAL 720x576. Do not resize to a common resolution.
- Frame rate: preserve native frame rate. See §10.
- The exact CRF numbers in the table above are provisional. v0.1 implementations must calibrate them against the test corpus in §15 with blind visual comparison and record the chosen values in the compiler's profile definition. Do not invent a CRF and ship it as final without that calibration.

---

## 6. Audio

### 6.1 Default: keep AC-3

Default `audio_mode` is `"ac3-keep"`.

- Preserve the original AC-3 bitstream in the bundle as the archival master track.
- Preserve original DTS and LPCM when present, as archival tracks. LPCM may be stored as FLAC for archival if the implementation prefers, but the original PCM data must be recoverable. Do not discard a DTS or LPCM track to save space in default mode.
- Preserve all audio tracks: main, commentary, alternate languages, descriptive audio. Track order and language metadata must match the disc.

### 6.2 Browser playback proxy

Browsers do not reliably play AC-3. Safari can play AC-3 in MP4; Chrome and Firefox generally cannot. DTS is not playable in browsers.

Therefore, when `audio_mode` is `"ac3-keep"`, the compiler **must also** generate a lightweight Opus proxy for each audio track at compile time, solely for browser playback:

- Stereo: Opus ~160 kbps, 48 kHz.
- 5.1: Opus ~384 kbps, 48 kHz.
- Commentary / mono speech: Opus ~96 kbps is acceptable, but the compiler may use the stereo rate for simplicity.

The player uses the Opus proxy by default in the browser. The AC-3 master remains in the bundle for archival and for future passthrough. The manifest must list both tracks and mark which is the playback proxy.

This proxy is not optional. A bundle that stores only AC-3 and expects the browser to play it is non-conformant for the web player.

### 6.3 Opt-in: Opus-only

When `audio_mode` is `"opus-only"`, the compiler transcodes each audio track to Opus at the rates above and drops the AC-3/DTS/LPCM master from the bundle. This saves roughly 200–300 MB per typical 2-hour disc versus keeping AC-3, more for LPCM music DVDs.

This is an explicit space-saving opt-in. It is lossy-to-lossy transcoding and is not bit-perfect. It is aurally transparent at the stated bitrates for normal listening, but it is not archival. The user must own the physical disc as the ultimate backup if they choose this mode.

The compiler must record `audio_mode` in the manifest so a player knows whether an archival master is present.

### 6.4 Other audio rules

- Sample rate: preserve 48 kHz. Do not resample to 44.1 kHz.
- Channel layout: preserve 5.1 as 5.1, stereo as stereo. Do not downmix 5.1 to stereo in the archival track. The Opus proxy may be generated from the same channel layout.
- No loudness normalization in the archival track. The proxy may use the same loudness as the source.

---

## 7. Subtitles

- DVD subtitles are bitmaps (subpictures), not text. Preserve them as bitmaps.
- Store subtitle bitmaps as timed PNG overlays, either embedded as a track in the segment or as files in `overlays/` referenced by timecode in the manifest.
- Render subtitles on a canvas / overlay layer above the video. Do not burn them into the video.
- Preserve language, forced flag, and default flag for each track.
- Do not OCR subtitles to text in v0.1. OCR may be added later as an optional enhancement, but it must never replace the bitmap original.
- Button highlights are also subpicture overlays. See §9. They use the same overlay mechanism as subtitles, with palette handling.

---

## 8. Virtual machine

### 8.1 Model

The player implements a DVD virtual machine. It is a segment player + VM + overlay renderer. It is **not** a physical-drive emulator.

State:

- **GPRM:** 16 general-purpose registers, 16-bit each. Read/write by VM commands and by game logic.
- **SPRM:** 24 system registers, 16-bit each. Read-only except as specified by the DVD spec. They hold audio language, subtitle language, angle, parental level, player configuration, and similar system state.
- **Program state:** current domain (VMGM / VTSM / VTS), current PGC, current program, current cell, resume state.
- **Button state:** currently selected button, highlight state.

The VM executes pre-commands before a PGC plays, post-commands after it finishes, and button actions when the user activates a button.

### 8.2 Commands

v0.1 must implement at least:

- `link` — LinkNoLink, LinkTopC, LinkNextC, LinkPrevC, LinkTopPG, LinkNextPG, LinkPrevPG, LinkPGCN, LinkRSM (resume).
- `jump` — JumpTT (jump to title), JumpVTS_TT, JumpVTS_PTT, JumpSS (jump to system space / menu).
- `call` — CallSS, CallVTS_PTT, Return.
- `set` — Set GPRM to value, Set SPRM where allowed.
- `compare` — Compare GPRM / SPRM to value and branch (if / else). Required for games and for conditional menus.
- `set_audio` / `set_subtitle` / `set_angle` — via SPRM.

The manifest represents each command as a JSON object, for example `{"op": "jump_tt", "title": 1}` or `{"op": "set", "gprm": 0, "value": 1}` or `{"op": "compare", "gprm": 3, "value": 2, "then": {...}, "else": {...}}`.

Compilers must translate the disc's native VM commands into this JSON form without dropping semantics. If a native command cannot be represented in v0.1, the compiler must record it as an unsupported command in the bundle's diagnostics and the player must handle it by pausing with a clear error, not by silently skipping it. Silent skipping breaks games.

### 8.3 Still frames, branching, and games

- **Still frames:** when a cell has `still: true`, the player pauses on the last frame and waits for user input or a timeout specified in the manifest. It must not advance automatically.
- **Seamless branching:** the player must select the correct cell at play time based on VM state, and must preload the next cell in the chosen branch to avoid a gap. Branch decisions must be deterministic from VM state.
- **Games:** games use GPRM state to track score, lives, progress, and random branching. The VM must persist GPRM state for the whole session (from First Play until the player is closed or the user ejects). Resume must restore GPRM state as well as position. See §12.4.
- **Parental level:** respect SPRM parental level. If the disc specifies a parental lock, the player must enforce it via the VM, not by hiding titles in the library.

### 8.4 What the VM does not do

- It does not parse MPEG-2 program streams.
- It does not do sector seeks.
- It does not handle CSS decryption. Input is already decrypted.
- It does not implement BD-J, HDMV, or any Blu-ray navigation.

---

## 9. Menus and navigation

### 9.1 Menu model

A menu is a PGC with `kind: "menu"` and a `menu` object in the manifest. See §3.4 for the schema.

- Background video is a segment. It loops if `background_loop` is true.
- Background audio is a normal audio track in that segment.
- Buttons have `rect` in 720x480 (NTSC) or 720x576 (PAL) coordinates. The player scales the rect to the display size. Button geometry must be preserved exactly; do not reflow buttons into a DOM list.
- Buttons have `up` / `down` / `left` / `right` navigation. Remote-style navigation (arrow keys, Enter) is primary. Mouse / touch click on a button is allowed as a secondary input, but must not be the only way to navigate.
- Highlight and select states are subpicture overlays. The player draws the highlight overlay when a button is selected, and the select overlay briefly when it is activated. Palette handling must match the disc's color lookup table.

### 9.2 Navigation inputs

The player must support, at minimum:

- Arrow keys / D-pad: move selection.
- Enter / OK: activate the selected button.
- Menu, Title, Audio, Subtitle buttons: jump to the corresponding menu or cycle tracks, as the disc defines.
- Chapter +/-: next / previous program.
- Number keys 0–9: for games and for direct chapter entry where the disc supports it.
- Mouse / touch: click a button to select and activate it.

### 9.3 First Play and forced content

- On Play, the player must run the First Play PGC. It must not skip directly to the main menu or main title.
- Forced trailers, warnings, and logos must play in order. The player must not provide a skip that the disc does not provide. If the disc allows skipping with Menu or Chapter+, the player must allow it; if it does not, the player must not invent a skip.

---

## 10. PAL vs NTSC

PAL and NTSC are preserved natively. There is no conversion.

- NTSC: 720x480, 29.97 fps (or 23.976 fps for film content with pulldown as stored on the disc — preserve the stored frame rate, do not invent a conversion).
- PAL: 720x576, 25 fps.

Rules:

- Do not convert frame rate.
- Do not resample audio to change pitch. PAL audio must not be pitch-shifted to NTSC speed or vice versa.
- Do not resize PAL to NTSC resolution or vice versa.
- The player must handle both resolutions and frame rates seamlessly. The manifest records `video_standard` so the player can set the correct display aspect and timing.
- Button coordinates are in the native resolution: 720x480 for NTSC, 720x576 for PAL.
- Subtitle timing is in seconds, not frames, so it is resolution-independent. The compiler must convert frame-based subtitle timing to seconds using the native frame rate.

---

## 11. Deduplication and content-addressed storage (CAS)

### 11.1 Canonical hash

Each segment is hashed with SHA-256 after canonical transcoding. Canonical means: same codec profile, same preset, same CRF, same deinterlace, same audio handling, same segment boundaries. Two segments that are the same content after canonical transcoding must hash identically. Two segments that differ by even one frame or one audio sample must hash differently.

The hash is of the segment file bytes, not of the source VOB bytes. Source VOB bytes will differ even when the content is identical, due to multiplexing, padding, and encryption. Hashing the source is not sufficient for deduplication.

### 11.2 Store

- Asset store path: `assets/<first-2-hex>/<full-sha256>.<ext>`, for example `assets/a1/b2/a1b2c3d4e5f6....mp4`.
- The store is shared across all libraries on the same server.
- The compiler checks the hash before storing. If the hash already exists, it does not store a duplicate. It only adds a reference in the new bundle's manifest.
- The player resolves a segment by hash via HTTP. The server may serve from the CAS path directly or via a redirect. Range requests must work.

### 11.3 Reference counting and garbage collection

- Each asset has a reference count: the number of bundles (across all libraries on that server) that reference it.
- Deleting a disc from a library decrements the count for each asset it referenced. It does not delete the asset file if the count is still greater than zero.
- An asset with ref count zero is eligible for garbage collection. GC is a separate, explicit operation, not automatic on delete. The platform must not GC an asset that is still referenced.
- The manifest's `dedup.references` is informational. The server's ref-count database is authoritative.

### 11.4 What deduplicates

- Studio logos, FBI warnings, and identical previews across discs from the same studio (for example Disney trailers reused across many discs).
- Menu loops reused across discs in a series.
- Any cell that is byte-identical after canonical transcoding.

What does **not** deduplicate:

- The same preview transcoded with a different codec profile. This is why codec is library-level. See §5.
- The same preview with a different audio track, different subtitle track, or different cell boundaries. The hash will differ, correctly.
- The same preview with different encoding settings. The compiler must use fixed settings per library.

---

## 12. Library model

The library model is a platform concern, but the format must support it. This section defines what the manifest and the server must provide.

### 12.1 Box sets

- A box set is one library entry with discs inside.
- Each disc is its own `.odvd` bundle with its own `bundle_id` and manifest.
- The set has a set manifest that lists disc IDs in order, set title, and set-level metadata.
- The library UI shows the set as one box. Clicking the set opens the box to an inserts / detail page. Play is per-disc, from that detail page.
- A single-disc title is a set with `disc_count_in_set: 1` and no set grouping UI. Do not create a set wrapper for single discs in the manifest.

### 12.2 Disc detail (inserts) page

Clicking a title in the library opens the box to an inserts / detail page. This page is not the DVD experience. It is the box before you open it.

It may show:

- Cover art (user-supplied or online, see §12.3).
- Disc list (for sets and TV DVDs).
- Notes, disc count, video standard, codec profile, audio mode, duration.
- A Play button per disc. Pressing Play starts the DVD experience from First Play (or from resume, see §12.4).

It must not show a Netflix-style episode list that bypasses menus, or a Play button that skips First Play and jumps directly to the main title. The DVD experience starts at First Play.

### 12.3 Metadata priority

Metadata is resolved in this order, first wins for each field:

1. **Disc itself** — title, structure, chapters, languages, as parsed from the IFO / VOB. This is authoritative for navigation and for the disc's own title.
2. **User uploads / designations** — cover art, custom title, notes, set grouping overrides. The user can correct or supplement what the disc says.
3. **Online databases, opt-in only** — for example TMDB for cover art or synopsis. Pull is opt-in per library. The library must work fully offline with only (1) and (2). Online data must never overwrite (1) or (2) without explicit user action.

The manifest records `disc.title_from` for the title field. Other fields should record provenance in the same way in the platform's database.

### 12.4 Resume

- Resume is toggleable per library, and ON by default.
- When on, the platform saves per-user resume state: current PGC, program, cell, time offset, GPRM state, audio / subtitle / angle selection. See §8.3 and §12.5.
- When off, Play always starts from First Play.
- Resume state is per-user, not per-disc globally. See §12.5.
- The player must restore VM state on resume, not just time offset. Resuming into the middle of a game or a branching title without GPRM state is incorrect.

### 12.5 Multi-user (v2, designed for in v1)

Multi-user logins are essential. They may ship in v2, but they must be designed for from day one.

- The platform's database must have a users table from v1, even if v1 only creates one admin user.
- Resume state is per-user.
- Library access is per-user. Household, friends, and family can have access to a library. The platform must be able to grant and revoke access per user per library.
- Roles are not fully specified in v0.1. At minimum, v1 must not hard-code a single-user assumption into the manifest, the resume store, or the asset store. A future v2 must be able to add users without recompiling discs or migrating bundles.

### 12.6 Failed / partial compiles

- Failed or partial compiles are hidden from the main library.
- They are available in a separate folder / view, for example "Needs Attention" or "Incomplete", with the error reason and a retry action.
- A bundle that fails validation (manifest parse error, missing segment, hash mismatch) must not appear in the main library. It must appear in the incomplete view.
- The compiler must not leave a half-written bundle in the library folder. Compiles write to a temp folder and are atomically moved into the library only on success.

### 12.7 TV DVDs

A TV DVD opens a details page like any other title in the library, with discs to choose from. Example: a 4-disc TV set shows a details page with 4 discs. Each disc is a separate `.odvd`. Episode selection is via the disc's own menus, not via a platform episode list. The platform must not try to parse episodes out of the disc and present them as a Netflix episode list.

---

## 13. Versioning and compatibility

- Spec version is in `manifest.json` as `odvd_version`, for example `"0.1"`.
- Version format is `major.minor`. Major version changes are breaking. Minor version changes are backward-compatible additions.
- A player must reject a bundle whose major version it does not understand, with a clear error. It must ignore unknown fields in a bundle whose major version it does understand.
- A compiler must stamp the spec version it implemented. It must not claim a newer version than it implements.
- The format must be forward-compatible in this sense: adding a new optional field, a new codec profile, or a new VM command in a minor version must not break v0.1 players that ignore unknown fields. Removing a field or changing the meaning of a field requires a major version bump.
- The platform must be able to play old bundles after an update, without recompiling. Migration, if ever needed, must be explicit and must not delete the original bundle until the migrated bundle validates.

---

## 14. Security and legal

- ODVD is for personal backup of discs you own, for personal use.
- Decryption is outside the spec. The compiler input is an already-decrypted ISO or `VIDEO_TS` folder. The user obtains that input via their own tools on their own machine, for example MakeMKV Backup or libdvdcss. This spec and its tools ship no keys, no key database, no AACS / CSS circumvention code, and no instructions for obtaining keys for discs you do not own.
- The platform must not expose a library to the public internet by default. Default is LAN-only, with optional password. Remote access is via the user's own VPN / Tailscale / tunnel, configured by the user. See the platform repo for deployment.
- The platform must not upload discs, manifests, or viewing history to any third party by default. Online metadata pull is opt-in per library.
- Multi-user access is by explicit grant. A user who is granted access to a library can view that library. There is no public sharing link by default in v0.1.

---

## 15. Test corpus

A compiler or player claiming ODVD v0.1 conformance must be tested against at least the following, and must record results:

- Disney DVD — main feature, animated menus, multiple audio tracks, extras, hidden PGC / Easter egg if present.
- Warner DVD — different menu style, seamless branching if present.
- Universal DVD — different menu style.
- Paramount DVD — different menu style.
- TV DVD — multi-disc set, 4 discs, episode menus. Verify the details page shows 4 discs and each disc plays its own menus.
- PAL disc — 720x576, 25 fps. Verify no frame-rate conversion and no audio pitch shift.
- DVD game — for example the Harry Potter DVD game. Verify VM state, scoring, and branching work.
- A disc with a still-frame menu. Verify it waits for input.
- A disc with identical previews shared across at least two other discs in the corpus. Verify deduplication: the shared segment is stored once and both manifests reference the same hash.
- A disc with multiple angles, if available in the corpus. Verify angle switching works.

If a test disc fails, the implementation must record which PGC, button, or VM command failed, not just "playback failed". That diagnostic is part of conformance.

---

## 16. Open questions and v0.1 gaps

These are known gaps in v0.1. They are not to be papered over with invented numbers.

1. **Exact CRF values.** The CRF ranges in §5 are provisional. They must be calibrated against the test corpus with blind visual comparison before v1.0. The compiler's profile definition must record the chosen values.
2. **Exact Opus proxy bitrates for 7.1 and for LPCM music DVDs.** The rates in §6 cover stereo and 5.1. 7.1 and high-resolution LPCM need calibration.
3. **VM command coverage.** §8.2 lists the minimum commands. The full DVD VM command set is larger. Compilers must log unsupported commands as diagnostics. v1.0 should enumerate the full set and mark which are required vs optional.
4. **Button highlight palette handling.** The spec requires palette preservation, but the exact PNG encoding for highlight / select overlays (separate files vs embedded track) is not yet pinned. Implementations must document their choice and must be able to read the other form.
5. **Set manifest schema.** §12.1 describes box sets at a high level. The exact JSON schema for the set manifest is not yet defined in v0.1 and must be defined before the platform implements sets.
6. **Multi-user roles and permissions.** §12.5 requires the design to allow multi-user, but roles, sharing, and revocation semantics are not yet specified. They are v2.
7. **Jellyfin / Plex plugin contract.** The standalone player is primary. The plugin, if built, is a launcher that hands off to the standalone player. The exact handoff contract (URL scheme, auth token passing) is not yet specified.
8. **Garbage collection timing.** §11.3 requires reference counting and explicit GC. The exact GC trigger (manual button, nightly job, on delete) is a platform decision, not a format decision, and must be documented in the platform repo.
9. **Resume for games.** §12.4 requires GPRM restoration on resume. The exact serialization format for VM state on resume is not yet pinned in v0.1 and must be defined before the platform implements resume for games.
10. **Compiler input validation.** How the compiler reports a scratched disc, a region mismatch, or a read error, and what partial output (if any) it keeps, is not yet specified. The compiler must at minimum not leave a half-written bundle in the library.

---

## Appendix A: Glossary

- **CAS:** Content-addressed store. Files named by SHA-256 hash of their contents. See §11.
- **Cell:** Smallest unit the VM can jump to. One segment per cell. See §4.
- **GPRM / SPRM:** General / System Parameter Registers. See §8.1.
- **PGC:** Program Chain. A playable unit: a title, a menu, or a game. Contains programs, cells, pre/post commands, and buttons.
- **VTS / VMGM:** Video Title Set / Video Manager Menu. DVD domains.
- **Visually lossless:** See §2.2.
- **1:1:** See §2.1.
