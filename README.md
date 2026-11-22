# ODVD — Open DVD Format

**Status: v0.1 draft — for review, not yet stable. No code in this repo yet.**

ODVD is a web-native format for a faithful 1:1 digital recreation of a personal DVD collection. It preserves the original DVD experience — menus, extras, Easter eggs, navigation, DVD games — while storing video in modern, visually lossless codecs and deduplicating shared assets across discs.

It is not a Plex/Jellyfin-style MKV library. A `.odvd` bundle is not a movie file. It is a compiled disc: a manifest describing the disc's navigation, plus per-cell media segments, plus references to a shared content-addressed asset store.

## DVD-only

ODVD is DVD-only, by design. Blu-ray is out of scope. Blu-ray menus are BD-J Java programs; DVD menus are a simple virtual machine with a small register set and a well-understood navigation model. That simplicity is what makes a faithful, implementable web player possible.

## Two repos

This project is split into two closely related repos:

- **`odvd-spec` (this repo)** — the format itself: the spec, the manifest schema, the segment format, the VM definition, the CAS rules, and the compiler tools that make `.odvd` files.
- **`odvd-platform` (separate repo)** — the infrastructure that plays and uses `.odvd` files: server, library, box-set UI, web player with VM, compile queue, auth and multi-user.

The spec is the contract. The platform depends on the spec. Anyone else can write a different player for `.odvd` files without using our platform, or a different compiler without using our server.

## What 1:1 means here

- First Play, warnings, forced trailers, and studio logos play in order.
- Main menu, scene selection, setup, and extras menus work with remote-style navigation.
- All audio tracks, subtitle tracks, chapters, and angles are kept.
- Extras are reachable the way the disc intended them, not dumped in a list.
- Hidden PGCs and Easter eggs are preserved, even if they are not listed anywhere.
- DVD games work. If it works on a DVD player, it must work here. Example: the Harry Potter DVD game.
- Multi-disc box sets are treated as multi-disc: each disc is its own `.odvd`, grouped as a set in the library.
- PAL and NTSC are preserved natively. No frame-rate conversion, no audio pitch shift.

## Quality target

Visually lossless. Not archival bit-perfect, not heavy compression. Nobody can tell the difference visually, but the storage saving is obvious.

Video codec is a library-level choice, not per-disc, to preserve deduplication. AV1 is the default. H.264 is the fast/compatible option. H.265 is opt-in. AV1 encode compute is higher, but that cost is one-time at compile time.

Audio: keep AC-3 by default, Opus as an opt-in conversion to save space. For browser playback, keep the AC-3 master plus a lightweight Opus proxy generated at compile time. Bitmap subtitles are preserved as timed overlays, not OCR text.

## How to read this repo

- `SPEC.md` is the full spec v0.1. Start there.
- The spec is written to be implementable. Where a value is not yet calibrated (for example exact CRF numbers), the spec says so explicitly rather than inventing a number.
- v0.1 gaps are listed at the end of `SPEC.md` under Open Questions.

## Legal

ODVD is for personal backup of discs you own, for personal use. Decryption is outside the spec: the compiler starts from an already-decrypted ISO or `VIDEO_TS` folder, for example via MakeMKV Backup or libdvdcss on the user's own machine. This repo ships no keys, no decryption code, and no circumvention tools.

## Audience

Homelab folks who already run Plex or a home server. Plex/Kodi-level install difficulty is acceptable. The platform is deployed via one compose file (multiple services inside are fine if that is easier to build); the format itself has no deployment requirement beyond a file store and an HTTP server that supports range requests.
