# MPC Kit Builder

A Mac desktop app (Python + PySide6) for building sampler kits for Akai MPCs. Drop audio onto 16 pads across 4 banks, trim, normalize, pitch and lo-fi them, then export for:

- **MPC2000XL**: `.PGM` program + `.SND` samples
- **Modern MPCs** (Live / Live 2 / Live 3 / X / One / Key / Sample): `.xpm` drum program + WAVs
- **MPC Sample**: `.mpcsample` kit + WAVs named by pad

It can also open existing `.PGM`, `.xpm` and `.mpcsample` kits for editing.

> **Status:** the file formats come from community reverse-engineering and from one real `.mpcsample` kit. They are tested with round-trip unit tests, **not yet on hardware**. The `.xpm` writer is the least certain. Test a kit in VMPC2000XL or on your unit before trusting it.

## Run
- Quick: double-click `run.command` (first launch creates a virtualenv; needs Python 3.10+).
- Or: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python app.py`
- Standalone app: `./build_app.sh` -> `dist/MPC2000XL Kit Builder.app` (unsigned: right-click > Open the first time).

## Develop
```
pip install -r requirements-dev.txt
pytest
```
Tests run headless (`QT_QPA_PLATFORM=offscreen`). See `CLAUDE.md` for the code layout and format notes.

## Use
- **Open an existing kit:** **Open Kit…** reads an MPC2000XL `.PGM`, a modern MPC `.xpm` drum program, or an MPC Sample `.mpcsample` kit. Samples are loaded from the same folder as the kit file: `.PGM` kits use the `.SND` (or `.WAV`) with each sample's name, `.xpm` kits use the WAV each layer points to, and `.mpcsample` kits use the WAVs named `A01 - Name.wav` and so on. From a `.PGM`, the original pad settings (envelopes, filters, mixer, note assignments, program name) are kept and written back when you export. From a `.mpcsample`, the mute groups and tempo settings are kept for re-export. From an `.xpm`, only the samples, pad notes and sample start/end carry over; other program settings (tuning, envelopes, loops, mute groups) aren't kept, and only the first velocity layer of each pad is loaded. Keygroup `.xpm` programs aren't supported. 22.05 kHz SNDs are upsampled to 44.1 kHz for editing; use the lo-fi 22050 Hz option plus "write true sample rate" to shrink them again. Missing samples are listed and leave their pads empty. Two pads that shared one sample become independent copies. Banks E-H of a `.mpcsample` are skipped (the app has banks A-D).
- **Load:** drag audio onto a pad (several files fill pads in order), or **Open Folder…** to fill empty pads across banks A-D (natural sort, 64 max). Everything is converted on import to 16-bit / 44.1 kHz.
- **Play:** click a pad to select + play it. Space replays. Right-click a pad to delete it (you'll be asked to confirm). Control-click a pad to rename its sample.
- **Move / swap:** drag a pad onto another pad. Empty target = the sound moves; full target = the two swap.
- **Select several pads:** Shift-click (or Cmd-click) toggles individual pads, in any order and across banks. **Select Bank** / **Select All** (Cmd-A) grab every loaded pad. Trim, Normalize, Stereo -> Mono, Undo, Clear and the pitch slider act on every selected pad. The waveform shows the primary (outlined) pad.
- **Edit:** drag the START / END handles on the waveform, then **Trim to Markers**. Tick **Zero** to snap the markers to the nearest zero crossing (stereo uses the L+R mix) so the cut doesn't pop; it snaps while dragging, when you tick it (for all selected pads), and again on Trim. Markers sitting at the very start/end of a sample are left alone. **Normalize** peaks to 0 dBFS. **Stereo -> Mono** replaces the stereo audio. Undo (Cmd-Z) reverts the last destructive edit per pad.
- **Pitch / speed slider** (right of the pads): +/-2 octaves, tape-style (pitch and speed change together). Double-click to reset to 0. The handle sticks briefly at each octave line (+/-1, +/-2, 0). It is applied to playback and to the exported files; the pad shows its offset.
- **Lo-fi panel** (right of the slider, one selected pad): pick 16/12/8-bit and 44100/22050/11025 Hz. Click the pad or **Preview** to hear it; **Revert** returns to the original. Non-destructive: the original stays in memory and the lo-fi is applied at export.
- **Export…** sends the kit to the target chosen in **Export for:**
  - **MPC2000XL (.PGM + .SND):** writes `NAME.PGM` plus a `.SND` per sample. Tick **Also export WAV files** for `.WAV` copies too.
  - **Modern MPC (.XPM + WAV):** for MPC Live / Live 2 / Live 3 / X / One / Key / Sample. Creates a folder named after the kit containing `Kit.xpm` and one WAV per sample (16- or 24-bit, mono or stereo, 44.1 kHz). Keep the `.xpm` and the WAVs together. The program uses your pad note assignments (35 + pad number for a new kit).
  - **MPC Sample (.mpcsample + WAV):** for the MPC Sample (MPC Software 3.8+ kit format). Creates a folder named after the kit holding `Kit.mpcsample` and WAVs named `A01 - Name.wav` ... (the pad label at the start of each name is what places the sample on its pad; banks A-D). WAVs are written like Akai's own exports (44.1 kHz, 24-bit by default, with their `atem`/`smpl` chunks). Copy the whole folder to the microSD card, then Load > External > Load Kit on the device. Choke (mute) groups are all off. The format was decoded from a kit exported by MPC Software, but I couldn't test it on a device.
  - **Sample names in exported files:** a name longer than 5 characters is shortened to its first 4 characters plus the pad's bank and number, so "Snare from that sample" on pad A01 becomes `SnarA01`. Names of 5 characters or fewer are left alone. This applies to every export format. Rename a sample (Control-click) to control the result.
  - **Both:** export uses the audio between the START/END markers with pitch and lo-fi applied. By default lo-fi pads are baked into normal 44100 Hz files (sample-and-hold, so the grit is kept). Tick **Lo-fi: write true sample rate** to keep the reduced rate (smaller files). Modern MPCs play any WAV rate correctly; the MPC2000XL natively supports 44100/22050 Hz only, so test 11025 on your machine first.
  - The `.xpm` writer is experimental: Akai's XPM format is only partly documented. If a program doesn't load, or pads land in the wrong place, load the WAVs from the Sample Pool and assign them by hand.

## Icon
`icon.icns` (used by `build_app.sh`) and `icon.png` (used when launching with `run.command`) show MPC above PGM. `python3 make_icon.py` regenerates both.

## Notes
- Pad A01 = MIDI note 35 ... pad D16 = note 98. Programs use default envelope/filter/mixer values.
- SNDs are written one-shot (loop off). The MPC2000XL tends to loop imported WAVs, so load the SNDs (the .PGM looks for them by name).
- The PGM layout comes from community reverse-engineering, not an official spec. Test a kit in the free VMPC2000XL emulator, then on hardware.

## License
MIT, see `LICENSE`.
