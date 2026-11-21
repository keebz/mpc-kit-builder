"""Round-trip tests for the file-format writers/readers in mpc2k_format.py."""
import gzip
import xml.etree.ElementTree as ET

import numpy as np
import pytest
import soundfile as sf

import mpc2k_format as fmt


def test_names_are_unique_and_short():
    assert fmt.clean_name("A very long sample name that exceeds sixteen") == "A very long samp"
    assert fmt.unique_names(["Kick", "KICK"]) == ["Kick", "KICK_1"]


def test_pgm_round_trip():
    pgm = fmt.build_pgm("TESTKIT", ["KICK", "SNARE"], [0, 1] + [None] * 62)
    d = fmt.parse_pgm(pgm)
    assert d["program"] == "TESTKIT"
    assert d["samples"] == ["KICK", "SNARE"]
    assert d["note_samples"][:3] == [0, 1, 255]
    assert d["pad_notes"][:3] == [35, 36, 37]


def test_pgm_preserves_opened_kit_settings():
    params = bytearray(fmt.NOTE_PARAMS_DEFAULT)
    params[10] = 17
    notes = [37, 36, 42, 82] + [35 + i for i in range(4, 64)]
    pgm = fmt.build_pgm("X", ["A", "B"], [0, 1] + [None] * 62,
                        pad_params=[bytes(params)] + [None] * 63, pad_notes=notes, midi=3)
    d = fmt.parse_pgm(pgm)
    assert d["midi"] == 3
    assert d["note_samples"][37 - 35] == 0          # pad A01 now answers to note 37
    assert d["note_params"][37 - 35][10] == 17


def test_pgm_rejects_junk():
    with pytest.raises(ValueError):
        fmt.parse_pgm(b"junk")


@pytest.mark.parametrize("channels", [1, 2])
def test_snd_round_trip(channels):
    pcm = (np.random.default_rng(0).standard_normal((1000, channels)) * 1000).astype("int16")
    d = fmt.parse_snd(fmt.build_snd("X", pcm))
    assert d["channels"] == channels and d["rate"] == 44100
    assert np.array_equal(d["pcm"], pcm)


@pytest.mark.parametrize("bits", [16, 24])
def test_wav_writers_are_readable(tmp_path, bits):
    pcm = (np.random.default_rng(1).standard_normal((500, 2)) * 5000).astype("int16")
    for name, writer in (("plain.wav", fmt.write_wav), ("akai.wav", fmt.write_wav_akai)):
        path = tmp_path / name
        writer(path, pcm, 44100, bits)
        info = sf.info(path)
        assert info.subtype == f"PCM_{bits}" and info.channels == 2 and info.frames == 500
        back = sf.read(path, dtype="int32", always_2d=True)[0] >> 16
        assert np.array_equal(back.astype("int16"), pcm)


def test_xpm_round_trip():
    pads = {0: "kick.wav", 5: "snare.wav"}
    xml = fmt.build_xpm("My Kit", pads)
    root = ET.fromstring(xml)
    assert root.tag == "MPCVObject"
    notes = [n.findtext("Note") for n in root.findall("Program/PadNoteMap/PadNote")]
    assert len(notes) == len(set(notes)) == 128
    d = fmt.parse_xpm(xml)
    assert d["name"] == "My Kit"
    assert {p: l[0]["file"] for p, l in d["instruments"].items()} == pads


def test_pad_labels():
    assert [fmt.pad_label(i) for i in (0, 15, 16, 63, 127)] == ["A01", "A16", "B01", "D16", "H16"]


def test_mpcsample_round_trip():
    mute = [2, 2, 1] + [0] * 125
    data = fmt.build_mpcsample(mute_groups=mute, bpm_by_key={0: 84.0})
    assert gzip.decompress(data).startswith(b"ACVS\n")
    d = fmt.parse_mpcsample(data)
    assert d["mute_groups"][:3] == [2, 2, 1] and len(d["mute_groups"]) == 128
    assert len(d["sequences"]) == 128
    assert {s["key"]: s["value"]["bpm"] for s in d["sequences"]}[0] == 84.0


def test_mpcsample_rejects_non_kit():
    with pytest.raises(Exception):
        fmt.parse_mpcsample(gzip.compress(b"hello"))
