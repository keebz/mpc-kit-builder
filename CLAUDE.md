# MPC Kit Builder: notes for Claude Code

Mac GUI app that builds and edits sampler kits for Akai MPCs. Python 3.10+, PySide6, numpy, scipy, soundfile, sounddevice.

## Layout
- `app.py`: the whole GUI (`MainWindow`, `PadButton`, `WaveformView`, `Pad` model) plus audio helpers (import conversion to 16-bit/44.1k, pitch shift, lo-fi, zero-crossing snap).
- `mpc2k_format.py`: pure file-format code, no Qt. Writers/readers for `.PGM`, `.SND`, `.xpm`, `.mpcsample`, WAV (plain and Akai-style with `atem`/`smpl` chunks).
- `make_icon.py`: regenerates `icon.png` / `icon.icns`.
- `tests/`: pytest. `test_format.py` is round-trip tests for the formats; `test_app.py` drives the GUI headless.
- `run.command` / `build_app.sh`: launch script and PyInstaller `.app` build.

## Behaviour worth knowing
- Pads are 0-based global indexes 0..63 (A01..D16). Hardware layout: pad 1 is bottom-left.
- Trim / Normalize / Stereo->Mono are destructive with one level of undo per pad. Pitch and lo-fi are non-destructive and applied at playback/export (`Pad.rendered()`, `Pad.export_audio()`).
- Exported sample names longer than `NAME_LIMIT` (5) become the first `NAME_KEEP` (4) characters plus the pad label (`SnarA01`). Applies to all three export formats.
- Right-click deletes (with confirm), Control-click renames. Both dialogs are opened via `QTimer.singleShot(0, ...)` after the mouse event ends. Opening them inside `mousePressEvent` froze the UI on macOS; keep it deferred. On macOS Qt reports physical Control as `Meta` (see `RENAME_MOD`).
- `.PGM` open/export preserves per-pad params, mixer, note map, slider block, MIDI channel and footer so editing an old kit does not reset it. `.mpcsample` open preserves mute groups, simultaneous-play targets and per-sequence BPM. `.xpm` open only brings in samples, pad notes and start/end.

## Format confidence
- `.PGM` / `.SND`: reverse-engineered community specs, round-trip tested, not verified on hardware.
- `.mpcsample`: decoded from one real kit exported by MPC Software 3.8 (gzip text: 5-line header then JSON). Sample-to-pad placement comes from WAV file names (`A01 - Name.wav`). The generated file is byte-identical to the real one's decompressed contents and WAV headers match Akai's. Not tested on a device.
- `.xpm`: least certain. Only partly documented; the writer is deliberately minimal. Instrument numbering (0- vs 1-based) is auto-detected when reading. If real files from a Live 3 disagree, fix `build_xpm` / `parse_xpm` against them.

## Before committing
Run `pytest`. Do not commit exported kits or third-party sample audio (`.gitignore` already excludes kit files).
