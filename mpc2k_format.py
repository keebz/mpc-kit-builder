"""
Writers for Akai MPC2000XL files: .PGM (program), .SND (sample) and .WAV.

PGM layout (little-endian), reconstructed from the community reverse-engineered
specs (Alesclandre/mpc2000xlapp IO_PGM.as, p3r7/mpc-tools Kaitai struct):

    07 04                         file header
    u8   N                        number of samples (<= 64)
    N x  [00][16-byte name]       sample names, space padded
    00 1E 00 [16-byte name]       program name, space padded
    00                            padding
    9 slider bytes                note, tune lo/hi, decay lo/hi, attack lo/hi, filter lo/hi
    u8   midi channel
    23 40 00 19  00               magic
    64 x 25 bytes                 per-note (35..98) sound parameters
    64 x  6 bytes                 per-note mixer parameters
    00 00 40 00                   magic
    64 x s8                       pad (A01..D16) -> MIDI note
    footer                        fixed block of default program/effects data

SND layout: 42-byte header followed by 16-bit little-endian PCM. Stereo SNDs
store the whole left channel followed by the whole right channel.
"""

import struct
import wave
import re
import xml.etree.ElementTree as ET

import numpy as np

SAMPLE_RATE = 44100
MAX_SAMPLES = 64
NUM_PADS = 64
FIRST_NOTE = 35  # pad A01 -> note 35 ... pad D16 -> note 98

# --- default data -----------------------------------------------------------

SLIDER_DEFAULT = bytes([0, 136, 120, 12, 45, 0, 20, 206, 50])  # note off, +-120 tune, ...
MIDI_CHANNEL = 0

# 24 bytes following the sample-number byte of each note entry
NOTE_PARAMS_DEFAULT = bytes([
    0, 44, 0, 88, 0, 0, 0, 0, 0, 0, 0, 0, 0, 100, 0, 0, 0, 0, 100, 0, 0, 0, 0, 0,
])
MIXER_DEFAULT = bytes([6, 0, 100, 50, 100, 0])

PGM_FOOTER = bytes([
    0x02, 0x00, 0x48, 0x00, 0xD0, 0x07, 0x00, 0x00, 0x63, 0x01, 0x14, 0x08, 0x1D, 0xFC, 0x32, 0x33,
    0x02, 0x32, 0x3C, 0x08, 0x05, 0x0A, 0x14, 0x14, 0x32, 0x00, 0x00, 0x02, 0x0F, 0x19, 0x00, 0x05,
    0x41, 0x14, 0x1E, 0x01, 0x05, 0x00, 0x00, 0x05, 0x63, 0x00, 0xF4, 0xFF, 0x0C, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x4F, 0x01, 0x4F, 0x01, 0x00, 0x42, 0x4F, 0x01, 0x00, 0x42,
    0x4F, 0x01, 0x00, 0x42, 0x32, 0x00, 0x63, 0x28, 0x00, 0x3C, 0x00, 0x00, 0xD0, 0x07, 0x00, 0x00,
    0x63, 0x01, 0x14, 0x08, 0x1D, 0xFC, 0x32, 0x33, 0x02, 0x32, 0x3C, 0x08, 0x05, 0x0A, 0x14, 0x14,
    0x32, 0x00, 0x00, 0x02, 0x0F, 0x19, 0x00, 0x05, 0x41, 0x14, 0x1E, 0x01, 0x05, 0x00, 0x00, 0x05,
    0x63, 0x00, 0xF4, 0xFF, 0x0C, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x4F, 0x01,
    0x4F, 0x01, 0x00, 0x42, 0x4F, 0x01, 0x00, 0x42, 0x4F, 0x01, 0x00, 0x42, 0x32, 0x00, 0x63, 0x28,
    0x00, 0x3C, 0x00, 0x00, 0x04, 0x00, 0x0C, 0x00, 0x00, 0x00, 0x32, 0x00, 0x23, 0x00, 0x3E, 0x33,
    0x5A, 0x32, 0x14, 0x00, 0x00, 0x00, 0x32, 0x00, 0x23, 0x00, 0x3E, 0x33, 0x5A, 0x32, 0x14, 0x00,
    0x00, 0x00, 0x32, 0x00, 0x23, 0x00, 0x3E, 0x33, 0x5A, 0x32, 0x14, 0x00, 0x00, 0x00, 0x32, 0x00,
    0x23, 0x00, 0x3E, 0x33, 0x5A, 0x32, 0x14, 0x00,
])

# --- names -------------------------------------------------------------------

_BAD = re.compile(r"[^A-Za-z0-9 _\-!#$%&'()@^`~+=,;\[\]{}]")


def clean_name(name: str, fallback: str = "SAMPLE", maxlen: int = 16) -> str:
    """MPC names: max 16 printable ASCII chars (modern MPCs allow longer); strip anything risky."""
    n = _BAD.sub("_", name.strip())[:maxlen].rstrip()
    return n or fallback


def unique_names(names, maxlen: int = 16):
    """Make names unique (case-insensitively), keeping order."""
    seen, out = set(), []
    for raw in names:
        base = clean_name(raw, maxlen=maxlen)
        cand, i = base, 1
        while cand.upper() in seen:
            suffix = f"_{i}"
            cand = base[: maxlen - len(suffix)] + suffix
            i += 1
        seen.add(cand.upper())
        out.append(cand)
    return out


def _name16(name: str) -> bytes:
    return name.encode("ascii", "replace")[:16].ljust(16, b" ")


# --- PGM ---------------------------------------------------------------------

def _valid_note(n):
    return FIRST_NOTE <= n < FIRST_NOTE + NUM_PADS


def build_pgm(program_name: str, sample_names, pad_to_sample, *, pad_params=None,
              pad_mixers=None, pad_notes=None, slider=None, midi=None, footer=None) -> bytes:
    """
    program_name   : <= 16 chars
    sample_names   : list of <= 64 already-cleaned sample names (order = sample number)
    pad_to_sample  : list of 64 entries (pad A01..D16): sample index or None

    Optional, to preserve an opened kit's settings (all default when omitted):
    pad_params / pad_mixers : per pad, 24 / 6 raw bytes or None
    pad_notes               : per pad, the MIDI note it is assigned to (default 35 + pad)
    slider, midi, footer    : raw program-level blocks (9 bytes / int / 200 bytes)
    """
    if len(sample_names) > MAX_SAMPLES:
        raise ValueError("A program can reference at most 64 samples")
    if len(pad_to_sample) != NUM_PADS:
        raise ValueError("pad_to_sample must have 64 entries")
    pad_params = pad_params or [None] * NUM_PADS
    pad_mixers = pad_mixers or [None] * NUM_PADS
    notes = [int(n) for n in pad_notes] if pad_notes else [FIRST_NOTE + i for i in range(NUM_PADS)]

    # Work out which note slot (0..63) each pad writes into.
    slot_of_pad, taken = [None] * NUM_PADS, set()
    for i in range(NUM_PADS):
        if _valid_note(notes[i]) and notes[i] not in taken:
            slot_of_pad[i] = notes[i] - FIRST_NOTE
            taken.add(notes[i])
    free = [n for n in range(FIRST_NOTE, FIRST_NOTE + NUM_PADS)
            if n not in taken and n not in notes]
    for i in range(NUM_PADS):
        needs = pad_to_sample[i] is not None or pad_params[i] is not None
        if slot_of_pad[i] is None and needs and free:
            n = free.pop(0)
            notes[i] = n
            slot_of_pad[i] = n - FIRST_NOTE

    note_sample = [0xFF] * NUM_PADS
    note_params = [NOTE_PARAMS_DEFAULT] * NUM_PADS
    note_mixer = [MIXER_DEFAULT] * NUM_PADS
    for i in range(NUM_PADS):
        k = slot_of_pad[i]
        if k is None:
            continue
        if pad_to_sample[i] is not None:
            note_sample[k] = pad_to_sample[i]
        if pad_params[i] is not None:
            note_params[k] = bytes(pad_params[i])
        if pad_mixers[i] is not None:
            note_mixer[k] = bytes(pad_mixers[i])

    out = bytearray()
    out += struct.pack("<H", 0x0407)
    out.append(len(sample_names))
    for n in sample_names:
        out.append(0x00)
        out += _name16(n)

    out += bytes([0x00, 0x1E, 0x00])
    out += _name16(clean_name(program_name, "PROGRAM"))

    out.append(0x00)
    out += bytes(slider) if slider is not None else SLIDER_DEFAULT
    out.append(MIDI_CHANNEL if midi is None else midi & 0xFF)
    out += struct.pack("<I", 0x19004023)
    out.append(0x00)

    for k in range(NUM_PADS):
        out.append(note_sample[k])
        out += note_params[k]
    for k in range(NUM_PADS):
        out += note_mixer[k]

    out += bytes([0x00, 0x00, 0x40, 0x00])
    out += bytes(n & 0xFF for n in notes)
    out += bytes(footer) if footer is not None else PGM_FOOTER
    return bytes(out)


def _dec(raw: bytes) -> str:
    return raw.decode("ascii", "replace").rstrip(" \x00")


def parse_pgm(data: bytes) -> dict:
    """Read an MPC2000XL program. Raises ValueError if it doesn't look like one."""
    if len(data) < 4 or data[0:2] != b"\x07\x04":
        raise ValueError("This isn't an MPC2000XL program (.PGM) file")
    n = data[2]
    fixed = 3 + 17 * n + 3 + 16 + 1 + 9 + 1 + 5 + NUM_PADS * 25 + NUM_PADS * 6 + 4 + NUM_PADS
    if n > MAX_SAMPLES or len(data) < fixed:
        raise ValueError("This .PGM file is truncated or isn't an MPC2000XL program")
    p = 3
    names = []
    for _ in range(n):
        names.append(_dec(data[p + 1:p + 17]))
        p += 17
    p += 3                                   # 00 1E 00
    prog = _dec(data[p:p + 16])
    p += 16
    p += 1                                   # padding
    slider = bytes(data[p:p + 9])
    p += 9
    midi = data[p]
    p += 1
    p += 5                                   # magic block
    note_samples, note_params = [], []
    for _ in range(NUM_PADS):
        note_samples.append(data[p])
        note_params.append(bytes(data[p + 1:p + 25]))
        p += 25
    mixers = [bytes(data[p + 6 * k:p + 6 * k + 6]) for k in range(NUM_PADS)]
    p += NUM_PADS * 6
    p += 4                                   # magic block
    assign = list(data[p:p + NUM_PADS])
    p += NUM_PADS
    return dict(program=prog, samples=names, slider=slider, midi=midi,
                note_samples=note_samples, note_params=note_params, mixers=mixers,
                pad_notes=assign, footer=bytes(data[p:]))


# --- SND / WAV ---------------------------------------------------------------

def build_snd(name: str, pcm: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    """pcm: int16 array shaped (frames, channels) with 1 or 2 channels."""
    pcm = np.asarray(pcm, dtype="<i2")
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    frames, ch = pcm.shape
    if ch not in (1, 2):
        raise ValueError("SND supports mono or stereo")
    hdr = bytearray()
    hdr += bytes([0x01, 0x04])
    hdr += _name16(name)
    hdr += bytes([0x00])
    hdr += bytes([100])                 # level
    hdr += struct.pack("<b", 0)         # tune
    hdr += bytes([0 if ch == 1 else 1])  # 0 = mono, 1 = stereo
    hdr += struct.pack("<I", 0)         # start
    hdr += struct.pack("<I", frames)    # loop end
    hdr += struct.pack("<I", frames)    # end
    hdr += struct.pack("<I", 0)         # loop length
    hdr += bytes([0])                   # loop mode off (one-shot)
    hdr += bytes([1])                   # beats in loop
    hdr += struct.pack("<H", rate)
    assert len(hdr) == 42
    if ch == 1:
        body = pcm[:, 0].tobytes()
    else:
        body = pcm[:, 0].tobytes() + pcm[:, 1].tobytes()  # left block, then right block
    return bytes(hdr) + body


def parse_snd(data: bytes):
    if len(data) < 42 or data[0:2] != b"\x01\x04":
        raise ValueError("Not an MPC2000XL .SND file")
    name = data[2:18].decode("ascii", "replace").rstrip(" \x00")
    ch = 2 if data[21] else 1
    start, loop_end, end, loop_len = struct.unpack("<IIII", data[22:38])
    rate = struct.unpack("<H", data[40:42])[0]
    body = data[42:]
    raw = np.frombuffer(body[:len(body) // 2 * 2], dtype="<i2")
    if ch == 2:
        half = len(raw) // 2
        pcm = np.stack([raw[:half], raw[half:]], axis=1)
    else:
        pcm = raw[:, None]
    return dict(name=name, channels=ch, start=start, end=end, loop_end=loop_end,
                loop_len=loop_len, loop_mode=data[38], rate=rate, pcm=pcm)


def write_wav(path, pcm: np.ndarray, rate: int = SAMPLE_RATE, bits: int = 16):
    """pcm is int16 (frames, channels); bits 16 or 24 (24-bit is the 16-bit data padded)."""
    pcm = np.asarray(pcm, dtype="<i2")
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    if bits == 24:
        v = (pcm.astype("<i4") << 8).astype("<i4")
        body = v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
        width = 3
    else:
        body, width = pcm.tobytes(), 2
    with wave.open(str(path), "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(body)


# --- Modern MPC (MPC Live / Live 2 / Live 3 / X / One / Key / Sample): .XPM drum program ----------
# NOTE: .xpm is Akai's XML program format. Only part of it is publicly documented, so this writer
# emits a deliberately minimal drum program (name, pad -> note map, one layer per pad). MPC
# software fills in defaults for everything omitted. Verify on your unit.

def build_xpm(program_name: str, pads, pad_notes=None, total_pads: int = 128) -> bytes:
    """
    pads       : dict {pad_index (0-based, A01 = 0): wav_file_name}  (sample name = file stem)
    pad_notes  : optional per-pad MIDI notes for the first pads (default 35 + pad)
    """
    notes = []
    for i in range(total_pads):
        n = pad_notes[i] if pad_notes and i < len(pad_notes) else FIRST_NOTE + i
        notes.append(int(n) if 0 <= int(n) <= 127 else -1)
    used = set()
    for i, n in enumerate(notes):                # no repeated notes allowed
        if n in used or n < 0:
            notes[i] = -1
        else:
            used.add(n)
    free = (n for n in range(128) if n not in used)
    notes = [n if n >= 0 else next(free) for n in notes]

    root = ET.Element("MPCVObject")
    ver = ET.SubElement(root, "Version")
    ET.SubElement(ver, "File_Version").text = "2.1"
    ET.SubElement(ver, "Application").text = "MPC-V"
    ET.SubElement(ver, "Application_Version").text = "2.10.0.0"
    ET.SubElement(ver, "Platform").text = "OSX"
    prog = ET.SubElement(root, "Program", {"type": "Drum"})
    ET.SubElement(prog, "ProgramName").text = program_name

    pnm = ET.SubElement(prog, "PadNoteMap")
    for i, n in enumerate(notes):
        pn = ET.SubElement(pnm, "PadNote", {"number": str(i + 1)})
        ET.SubElement(pn, "Note").text = str(n)

    instruments = ET.SubElement(prog, "Instruments")
    for i in sorted(pads):
        inst = ET.SubElement(instruments, "Instrument", {"number": str(i + 1)})
        layers = ET.SubElement(inst, "Layers")
        layer = ET.SubElement(layers, "Layer", {"number": "1"})
        fname = pads[i]
        ET.SubElement(layer, "Active").text = "True"
        ET.SubElement(layer, "VelStart").text = "0"
        ET.SubElement(layer, "VelEnd").text = "127"
        ET.SubElement(layer, "SampleName").text = fname.rsplit(".", 1)[0]
        ET.SubElement(layer, "SampleFile").text = fname

    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


# --- MPC Sample (.mpcsample kit, MPC Software 3.8+) -----------------------------------------------
# Decoded from a real kit exported by Akai's software: the .mpcsample is a gzip-compressed text file
# (a 5-line header followed by JSON with per-pad mute groups, simultaneous-play targets and 128
# empty pad sequences). The sample-to-pad assignment lives in the loose WAV file names, which start
# with the pad label ("A01 - Name.wav" ... "H16 - Name.wav"). WAVs carry Akai 'atem' + 'smpl' chunks.

import gzip
import io
import json

MPCSAMPLE_HEADER = "ACVS\n3.8.0.25\nSerialisableAC50ExportData\njson\nLinux\n"
MPCSAMPLE_PADS = 128
_SEQ_KEY_ORDER = ([127] + list(range(58, 28, -1)) + list(range(12, -1, -1))
                  + list(range(13, 29)) + list(range(59, 127)))
_ATEM_JSON = ('{\n    "version": 1,\n    "value0": {\n        "defaultSlice": {\n            "Start": 0,\n'
              '            "End": 0,\n            "LoopStart": 0,\n            "LoopMode": 0,\n'
              '            "PulsePosition": 0,\n            "LoopCrossfadeLength": 0,\n'
              '            "LoopCrossfadeType": 0,\n            "TailLength": 0.0,\n'
              '            "TailLoopPosition": 0.5,\n            "NumLoopRepeats": 0\n        },\n'
              '        "numBars": 2,\n        "Num slices": 0\n    }\n}\x00').encode("ascii")
_SMPL_TEMPLATE = bytes.fromhex(
    "470000015e000000935800003c0000000000000019000000000000000100000000000000000000000000000000000000"
    "00000000000000000000000003003c0000000000000000000000000000000000000000000000000000000000003f")


def pad_label(index: int) -> str:
    """Pad 0 -> 'A01', pad 15 -> 'A16', pad 16 -> 'B01' ... pad 127 -> 'H16'."""
    return f"{'ABCDEFGH'[index // 16]}{index % 16 + 1:02d}"


def build_mpcsample(mute_groups=None, simult_targets=None, bpm_by_key=None, bpm: float = 92.0) -> bytes:
    """Return the gzip-compressed .mpcsample bytes (128 pads)."""
    mute = list(mute_groups) if mute_groups is not None else [0] * MPCSAMPLE_PADS
    simult = list(simult_targets) if simult_targets is not None else [0] * MPCSAMPLE_PADS
    mute = (mute + [0] * MPCSAMPLE_PADS)[:MPCSAMPLE_PADS]
    simult = (simult + [0] * MPCSAMPLE_PADS)[:MPCSAMPLE_PADS]
    bpm_by_key = bpm_by_key or {}

    def seq(key):
        return {"key": key, "value": {
            "bpm": float(bpm_by_key.get(key, bpm)), "lengthBars": 4, "lengthPulses": 15360,
            "tempoEnable": True,
            "timeSignatureList": {"timeSignatures": [{"beatsPerBar": 4, "beatLength": 960, "barStart": 0}]},
            "eventList": {"length": 9223372036854775807, "events": [], "version": 2,
                          "quantisation": {"version": 1, "pulses": 0, "swing": 0.0, "strength": 1.0},
                          "numFilterTypes": 30}}}

    body = json.dumps({"data": {"sequences": [seq(k) for k in _SEQ_KEY_ORDER],
                                "muteGroups": mute, "simultPlayTargets": simult}}, indent=0)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as z:
        z.write((MPCSAMPLE_HEADER + body).encode("utf-8"))
    return buf.getvalue()


def parse_mpcsample(data: bytes) -> dict:
    text = gzip.decompress(data).decode("utf-8")
    hdr, _, body = text.partition("\n{\n")
    if not hdr.startswith("ACVS"):
        raise ValueError("Not an MPC Sample (.mpcsample) file")
    obj = json.loads("{\n" + body)["data"]
    return dict(header=hdr.split("\n"), mute_groups=obj["muteGroups"],
                simult_targets=obj["simultPlayTargets"], sequences=obj["sequences"])


def write_wav_akai(path, pcm: np.ndarray, rate: int = SAMPLE_RATE, bits: int = 24):
    """WAV laid out like the ones MPC Software exports: fmt, atem, smpl, data."""
    pcm = np.asarray(pcm, dtype="<i2")
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    ch = pcm.shape[1]
    if bits == 24:
        v = (pcm.astype("<i4") << 8).astype("<i4")
        body, width = v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes(), 3
    else:
        body, width = pcm.tobytes(), 2
    smpl = bytearray(_SMPL_TEMPLATE)
    smpl[8:12] = struct.pack("<I", int(1e9 / rate))             # sample period in ns (Akai truncates)
    fmt_chunk = struct.pack("<HHIIHH", 1, ch, rate, rate * ch * width, ch * width, width * 8)

    def chunk(cid, payload):
        return cid + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) & 1 else b"")

    chunks = (chunk(b"fmt ", fmt_chunk) + chunk(b"atem", _ATEM_JSON) + chunk(b"smpl", bytes(smpl))
              + chunk(b"data", body))
    # MPC Software's own files carry a RIFF size that leaves out the 'atem' chunk; match that.
    riff_size = 4 + len(chunks) - (8 + len(_ATEM_JSON))
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + chunks)


# --- reading modern .xpm drum programs -------------------------------------------------------------

def _to_int(text, default=None):
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return default


def parse_xpm(data: bytes) -> dict:
    """
    Read an MPC .xpm program. Returns dict(name, type, base, pad_notes, instruments) where
    instruments maps a 0-based pad index to its list of layers (each: file, name, start, end,
    vel_start, vel_end). Instrument/pad numbers are treated as 0-based if a 0 appears, else 1-based.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as ex:
        raise ValueError(f"Not a valid .xpm file ({ex})")
    prog = root.find("Program") if root.tag == "MPCVObject" else None
    if prog is None:
        raise ValueError("No MPC program found in this .xpm file")
    name = (prog.findtext("ProgramName") or "").strip()

    notes = {}
    for pn in prog.findall("PadNoteMap/PadNote"):
        num, note = _to_int(pn.get("number")), _to_int(pn.findtext("Note"))
        if num is not None and note is not None:
            notes[num] = note
    note_base = 0 if 0 in notes else 1

    insts = prog.findall("Instruments/Instrument")
    numbers = [_to_int(i.get("number")) for i in insts if _to_int(i.get("number")) is not None]
    base = 0 if 0 in numbers else 1
    instruments = {}
    for inst in insts:
        num = _to_int(inst.get("number"))
        if num is None:
            continue
        layers = []
        for lay in inst.findall("Layers/Layer"):
            f = (lay.findtext("SampleFile") or "").strip()
            n = (lay.findtext("SampleName") or "").strip()
            if not f and not n:
                continue
            layers.append(dict(file=f, name=n,
                               start=_to_int(lay.findtext("SampleStart"), 0),
                               end=_to_int(lay.findtext("SampleEnd"), 0),
                               vel_start=_to_int(lay.findtext("VelStart"), 0),
                               vel_end=_to_int(lay.findtext("VelEnd"), 127)))
        if layers:
            instruments[num - base] = layers
    pad_notes = [notes.get(i + note_base) for i in range(128)]
    return dict(name=name, type=prog.get("type", ""), base=base, pad_notes=pad_notes,
                instruments=instruments)
