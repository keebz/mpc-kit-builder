"""Headless GUI tests: load, edit, rename, move/swap, export, reopen."""
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app as A
import mpc2k_format as fmt


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def audio_dir(tmp_path):
    t = np.linspace(0, 1, 44100, endpoint=False)
    for name, f in (("Snare from that sample", 200), ("Kick", 60), ("Open Hat 2", 4000)):
        sf.write(tmp_path / f"{name}.wav", 0.4 * np.sin(2 * np.pi * f * t) * np.exp(-3 * t), 44100)
    return tmp_path


@pytest.fixture
def win(qapp, audio_dir, monkeypatch):
    monkeypatch.setattr(A.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(A.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    w = A.MainWindow()
    w._load_sequence(0, sorted(audio_dir.glob("*.wav")), overwrite=False)
    return w


def export(win, monkeypatch, path, target):
    monkeypatch.setattr(A.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))
    win.target_combo.setCurrentIndex(target)
    win.export_kit()


def test_long_names_shortened_on_export(win):
    names = {A.MainWindow._label(i): A.MainWindow._export_name(i, p)
             for i, p in enumerate(win.pads) if p.loaded}
    assert names == {"A01": "Kick", "A02": "OpenA02", "A03": "SnarA03"}


def test_batch_mono_and_normalize(win):
    win.select_all()
    win.do_mono()
    win.do_normalize()
    for p in win.pads:
        if p.loaded:
            assert p.channels == 1 and int(np.abs(p.pcm.astype(int)).max()) == 32767


def test_pitch_octave_halves_length(win):
    win.pitch_slider.setValue(1200)
    assert len(win.cur.rendered()) == win.cur.frames // 2


def test_move_and_swap(win):
    before = {i: p.name for i, p in enumerate(win.pads) if p.loaded}
    win.on_pad_moved(0, 9)
    assert win.pads[9].name == before[0] and not win.pads[0].loaded
    win.on_pad_moved(9, 1)
    assert win.pads[1].name == before[0] and win.pads[9].name == before[1]


def test_delete_asks_first_and_does_not_freeze(win, monkeypatch):
    answers = []

    class FakeBox:
        def __init__(self, *a, **k): pass
        def setDefaultButton(self, b): pass
        def exec(self): answers.append(1); return A.QMessageBox.No

    monkeypatch.setattr(A, "QMessageBox", type("QB", (), dict(
        Question=A.QMessageBox.Question, Yes=A.QMessageBox.Yes, No=A.QMessageBox.No,
        information=staticmethod(lambda *a, **k: None), __new__=lambda cls, *a, **k: FakeBox())))
    pw = win.pad_widgets[0]
    QTest.mouseClick(pw, Qt.RightButton, Qt.NoModifier, QPoint(40, 40))
    QApplication.processEvents()
    assert answers and win.pads[0].loaded            # asked, answered No, pad kept
    QTest.mouseClick(win.bank_btns[1], Qt.LeftButton)
    QApplication.processEvents()
    assert win.bank == 1                             # UI still responds afterwards


def test_control_click_renames(win, monkeypatch):
    monkeypatch.setattr(A.QInputDialog, "getText", staticmethod(lambda *a: ("New Name", True)))
    QTest.mouseClick(win.pad_widgets[1], Qt.LeftButton, A.RENAME_MOD, QPoint(40, 40))
    QApplication.processEvents()
    assert win.pads[1].long_name == "New Name"


def test_zero_snap_lands_near_zero(win):
    t = np.arange(44100) / 44100
    win.pads[0].set_audio("sine", (20000 * np.sin(2 * np.pi * 220 * t + 0.7)).astype(np.int16)[:, None])
    win.pads[0].start, win.pads[0].end = 10001, 30003
    win.pads[0].snap_markers()
    assert abs(int(win.pads[0].pcm[win.pads[0].start, 0])) < 1500


@pytest.mark.parametrize("target,name", [(0, "K.PGM"), (1, "K.xpm"), (2, "K.mpcsample")])
def test_export_then_reopen(win, monkeypatch, tmp_path, target, name):
    out = tmp_path / "out"
    out.mkdir()
    export(win, monkeypatch, out / name, target)
    kit_file = next(out.rglob(name))
    monkeypatch.setattr(A.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(kit_file), "")))
    monkeypatch.setattr(A.QMessageBox, "question", staticmethod(lambda *a, **k: A.QMessageBox.Yes))
    w2 = A.MainWindow()
    w2.open_kit()
    assert sum(p.loaded for p in w2.pads) == 3
    assert sorted(p.name for p in w2.pads if p.loaded) == ["Kick", "OpenA02", "SnarA03"]


def test_mpcsample_wavs_are_named_by_pad(win, monkeypatch, tmp_path):
    export(win, monkeypatch, tmp_path / "Ms.mpcsample", 2)
    names = sorted(p.name for p in (tmp_path / "Ms").glob("*.wav"))
    assert names == ["A01 - Kick.wav", "A02 - OpenA02.wav", "A03 - SnarA03.wav"]
