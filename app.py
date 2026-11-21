#!/usr/bin/env python3
"""
MPC2000XL Kit Builder
Drag audio onto 16 pads x 4 banks, trim/normalize/mono-ize, export a .PGM + .SND/.WAV set.
"""

import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QMimeData, QPoint, QTimer
from PySide6.QtGui import QColor, QDrag, QFont, QIcon, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

import mpc2k_format as fmt

RATE = fmt.SAMPLE_RATE
AUDIO_EXTS = {".wav", ".wave", ".aif", ".aiff", ".aifc", ".flac", ".mp3", ".m4a", ".aac",
              ".ogg", ".oga", ".caf", ".snd", ".au", ".opus"}
BANKS = "ABCD"
PAD_MIME = "application/x-mpc-pad"
IS_MAC = sys.platform == "darwin"
# The physical Control key: Qt reports it as Meta on macOS (where Cmd is Qt's "Control").
RENAME_MOD = Qt.MetaModifier if IS_MAC else Qt.ControlModifier
# Extra-select modifiers: Shift everywhere, plus Cmd on macOS (Ctrl is used for rename).
MULTI_MODS = Qt.ShiftModifier | (Qt.ControlModifier if IS_MAC else Qt.NoModifier)
# Exported sample names longer than NAME_LIMIT characters become: first NAME_KEEP characters + pad label.
NAME_LIMIT, NAME_KEEP = 5, 4
MAX_SEMITONES = 24
BIT_DEPTHS = (16, 12, 8)
SAMPLE_RATES = (44100, 22050, 11025)


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _read_any(path: str):
    """Return (float32 array (frames, ch), samplerate). Falls back to macOS afconvert."""
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        return data, sr
    except Exception:
        pass
    afconvert = shutil.which("afconvert")
    if not afconvert:
        raise RuntimeError(f"Can't decode {Path(path).name}")
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "tmp.wav")
        r = subprocess.run([afconvert, "-f", "WAVE", "-d", "LEI16", path, tmp],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(tmp):
            raise RuntimeError(f"Can't decode {Path(path).name}: {r.stderr.strip()}")
        data, sr = sf.read(tmp, dtype="float32", always_2d=True)
        return data, sr


def load_as_mpc_pcm(path: str) -> np.ndarray:
    """Load any audio file -> int16, 44.1 kHz, mono or stereo (frames, ch)."""
    data, sr = _read_any(path)
    if data.shape[1] > 2:                      # surround etc: keep front L/R
        data = data[:, :2]
    if sr != RATE:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(sr), RATE)
        data = resample_poly(data, RATE // g, int(sr) // g, axis=0).astype("float32")
    data = np.clip(data, -1.0, 1.0)
    pcm = np.rint(data * 32767.0).astype(np.int16)
    if len(pcm) == 0:
        raise RuntimeError(f"{Path(path).name} is empty")
    return pcm


def load_snd_as_mpc_pcm(path):
    """Read an MPC2000XL .SND -> (int16 pcm at 44.1 kHz, start, end) with markers in 44.1k frames."""
    d = fmt.parse_snd(Path(path).read_bytes())
    pcm, rate = d["pcm"], int(d["rate"]) or RATE
    n = pcm.shape[0]
    start, end = int(d["start"]), int(d["end"])
    if not (0 <= start < end <= n):
        start, end = 0, n
    if rate != RATE:
        from scipy.signal import resample_poly
        g = gcd(RATE, rate)
        up, down = RATE // g, rate // g
        out = resample_poly(pcm.astype(np.float32), up, down, axis=0)
        pcm = np.clip(np.rint(out), -32768, 32767).astype(np.int16)
        ratio = pcm.shape[0] / n
        start, end = int(round(start * ratio)), min(pcm.shape[0], int(round(end * ratio)))
        if end <= start:
            start, end = 0, pcm.shape[0]
    return np.ascontiguousarray(pcm), start, end


def pitch_shift(pcm: np.ndarray, semitones: float) -> np.ndarray:
    """Tape-style pitch/speed change by resampling (up = faster/shorter, down = slower/longer)."""
    if abs(semitones) < 1e-6 or len(pcm) < 2:
        return pcm
    from scipy.signal import resample_poly
    speed = 2.0 ** (semitones / 12.0)
    frac = Fraction(1.0 / speed).limit_denominator(1000)   # new_len = old_len * frac
    out = resample_poly(pcm.astype(np.float32), frac.numerator, frac.denominator, axis=0)
    return np.clip(np.rint(out), -32768, 32767).astype(np.int16)


def lofi_reduce(pcm: np.ndarray, bits: int, rate: int):
    """Reduce sample rate (filtered) and bit depth. Returns (int16 pcm at `rate`, rate)."""
    out = pcm.astype(np.float32)
    if rate < RATE:
        from scipy.signal import resample_poly
        g = gcd(RATE, rate)
        out = resample_poly(out, rate // g, RATE // g, axis=0)
    if bits < 16:
        step = float(2 ** (16 - bits))
        out = np.rint(out / step) * step
    return np.clip(np.rint(out), -32768, 32767).astype(np.int16), rate


def hold_upsample(pcm: np.ndarray, rate: int) -> np.ndarray:
    """Sample-and-hold back up to 44.1 kHz (keeps the stair-step lo-fi grit)."""
    if rate >= RATE:
        return pcm
    return np.repeat(pcm, RATE // rate, axis=0)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class Pad:
    name: str = ""
    long_name: str = ""       # up to 60 chars; used by the modern MPC exports
    pcm: np.ndarray = None      # int16 (frames, ch)
    start: int = 0
    end: int = 0
    undo: tuple = None          # one-level undo of destructive edits
    pitch: float = 0.0          # semitones, applied on playback + export
    bits: int = 16              # lo-fi bit depth (non-destructive, applied on playback + export)
    srate: int = RATE           # lo-fi sample rate (non-destructive)
    params: bytes = None        # raw MPC pad settings from an opened .PGM (envelope, filter, ...)
    mixer: bytes = None         # raw MPC mixer settings from an opened .PGM

    @property
    def loaded(self):
        return self.pcm is not None

    @property
    def channels(self):
        return 0 if self.pcm is None else self.pcm.shape[1]

    @property
    def frames(self):
        return 0 if self.pcm is None else self.pcm.shape[0]

    def set_audio(self, name, pcm):
        self.name = fmt.clean_name(name)
        self.long_name = fmt.clean_name(name, maxlen=60)
        self.pcm = pcm
        self.start, self.end = 0, pcm.shape[0]
        self.undo = None
        self.pitch = 0.0
        self.bits, self.srate = 16, RATE

    def clear(self):
        self.name, self.pcm, self.start, self.end, self.undo = "", None, 0, 0, None
        self.long_name = ""
        self.pitch = 0.0
        self.bits, self.srate = 16, RATE
        self.params = self.mixer = None

    def region(self):
        return self.pcm[self.start:self.end]

    def nearest_zero(self, frame):
        """Nearest zero crossing to `frame` (stereo uses the L+R mix). Edges are left alone."""
        n = self.frames
        f = int(frame)
        if n < 3 or f <= 0 or f >= n:
            return f
        for win in (2048, 16384, n):
            a, b = max(0, f - win), min(n, f + win)
            m = self.pcm[a:b].astype(np.int32).sum(axis=1)
            sg = np.sign(m)
            idx = np.nonzero(sg[:-1] * sg[1:] <= 0)[0]
            if idx.size:
                # of the two samples straddling each crossing, take the one closest to zero
                picks = idx + (np.abs(m[idx + 1]) < np.abs(m[idx]))
                return a + int(picks[np.argmin(np.abs(picks - (f - a)))])
        return f            # no zero crossing anywhere (pure DC offset): leave it

    def snap_markers(self):
        s, e = self.nearest_zero(self.start), self.nearest_zero(self.end)
        if e - s >= 2:
            self.start, self.end = s, e

    @property
    def lofi_active(self):
        return self.bits != 16 or self.srate != RATE

    def rendered(self):
        """What you hear: region -> pitch/speed -> lo-fi, always at 44.1 kHz."""
        pcm = pitch_shift(self.region(), self.pitch)
        if self.lofi_active:
            low, r = lofi_reduce(pcm, self.bits, self.srate)
            return hold_upsample(low, r)[:len(pcm)]
        return pcm

    def export_audio(self, true_rate=False):
        """(pcm, sample_rate) to write. true_rate=True keeps the reduced lo-fi rate."""
        pcm = pitch_shift(self.region(), self.pitch)
        if not self.lofi_active:
            return pcm, RATE
        low, r = lofi_reduce(pcm, self.bits, self.srate)
        return (low, r) if true_rate else (hold_upsample(low, r)[:len(pcm)], RATE)

    def _remember(self):
        self.undo = (self.pcm.copy(), self.start, self.end)

    def trim(self):
        if self.end - self.start < 2:
            return
        self._remember()
        self.pcm = self.pcm[self.start:self.end].copy()
        self.start, self.end = 0, self.pcm.shape[0]

    def normalize(self):
        peak = int(np.max(np.abs(self.pcm.astype(np.int32)))) if self.frames else 0
        if peak == 0:
            return
        self._remember()
        scaled = self.pcm.astype(np.float64) * (32767.0 / peak)
        self.pcm = np.clip(np.rint(scaled), -32768, 32767).astype(np.int16)

    def to_mono(self):
        if self.channels != 2:
            return
        self._remember()
        mono = self.pcm.astype(np.int32).sum(axis=1) // 2
        self.pcm = mono.astype(np.int16)[:, None]

    def revert(self):
        if self.undo:
            self.pcm, self.start, self.end = self.undo
            self.undo = None


# ---------------------------------------------------------------------------
# Waveform view
# ---------------------------------------------------------------------------

class WaveformView(QWidget):
    changed = Signal()

    HANDLE_PX = 7

    def __init__(self):
        super().__init__()
        self.pad = None
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(200)
        self._drag = None
        self._peak_cache = None
        self.snap_zero = False
        self.setMouseTracking(True)

    def set_pad(self, pad):
        self.pad = pad
        self.update()

    def invalidate(self):
        self._peak_cache = None
        self.update()

    # -- geometry
    def _x_for(self, frame):
        n = max(1, self.pad.frames)
        return frame / n * self.width()

    def _frame_for(self, x):
        n = self.pad.frames
        return int(round(min(max(x / max(1, self.width()), 0.0), 1.0) * n))

    # -- peaks
    def _peaks(self):
        w = self.width()
        key = (id(self.pad.pcm), self.pad.frames, w)
        if self._peak_cache and self._peak_cache[0] == key:
            return self._peak_cache[1]
        pcm = self.pad.pcm
        n, ch = pcm.shape
        edges = np.linspace(0, n, w + 1).astype(int)
        peaks = np.zeros((ch, w, 2), dtype=np.float32)
        for c in range(ch):
            col = pcm[:, c]
            for i in range(w):
                a, b = edges[i], max(edges[i] + 1, edges[i + 1])
                seg = col[a:b]
                if seg.size:
                    peaks[c, i, 0] = seg.min()
                    peaks[c, i, 1] = seg.max()
        peaks /= 32768.0
        self._peak_cache = (key, peaks)
        return peaks

    # -- painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#14181c"))
        if not self.pad or not self.pad.loaded:
            p.setPen(QColor("#6b7580"))
            p.setFont(QFont("Helvetica Neue", 14))
            p.drawText(self.rect(), Qt.AlignCenter, "Select a loaded pad to edit its waveform")
            return

        pad = self.pad
        peaks = self._peaks()
        ch = peaks.shape[0]
        h = self.height()
        lane_h = h / ch
        for c in range(ch):
            mid = lane_h * c + lane_h / 2
            amp = lane_h / 2 - 6
            p.setPen(QPen(QColor("#2a323a"), 1))
            p.drawLine(0, int(mid), self.width(), int(mid))
            p.setPen(QPen(QColor("#4fd1c5"), 1))
            for x in range(peaks.shape[1]):
                lo, hi = peaks[c, x]
                p.drawLine(x, int(mid - hi * amp), x, int(mid - lo * amp) + 0)
            if ch == 2:
                p.setPen(QColor("#6b7580"))
                p.setFont(QFont("Helvetica Neue", 9))
                p.drawText(6, int(lane_h * c + 14), "L" if c == 0 else "R")

        xs, xe = self._x_for(pad.start), self._x_for(pad.end)
        shade = QColor(0, 0, 0, 150)
        p.fillRect(QRectF(0, 0, xs, h), shade)
        p.fillRect(QRectF(xe, 0, self.width() - xe, h), shade)

        for x, col, label in ((xs, "#f6ad55", "START"), (xe, "#fc8181", "END")):
            p.setPen(QPen(QColor(col), 2))
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            p.setBrush(QColor(col))
            p.drawRect(QRectF(x - 4, 0, 8, 14))
            p.drawRect(QRectF(x - 4, h - 14, 8, 14))

        secs = (pad.end - pad.start) / RATE
        p.setPen(QColor("#cbd5e0"))
        p.setFont(QFont("Menlo", 10))
        info = (f"start {pad.start / RATE:.3f}s   end {pad.end / RATE:.3f}s   "
                f"length {secs:.3f}s   {'stereo' if ch == 2 else 'mono'}   "
                f"{pad.frames * ch * 2 / 1024:.0f} KB"
                + (f"   pitch {pad.pitch:+.2f} st" if abs(pad.pitch) > 1e-6 else "")
                + (f"   lo-fi {pad.bits}-bit {pad.srate} Hz" if pad.lofi_active else ""))
        p.drawText(QRectF(10, h - 20, self.width() - 20, 18), Qt.AlignRight | Qt.AlignVCenter, info)

    # -- mouse
    def _hit(self, x):
        pad = self.pad
        ds, de = abs(x - self._x_for(pad.start)), abs(x - self._x_for(pad.end))
        if min(ds, de) > self.HANDLE_PX + 4:
            return None
        return "start" if ds <= de else "end"

    def mousePressEvent(self, e):
        if self.pad and self.pad.loaded:
            self._drag = self._hit(e.position().x())

    def mouseMoveEvent(self, e):
        if not (self.pad and self.pad.loaded):
            return
        x = e.position().x()
        if self._drag:
            f = self._frame_for(x)
            if self.snap_zero:
                f = self.pad.nearest_zero(f)
            if self._drag == "start":
                self.pad.start = min(f, self.pad.end - 1)
            else:
                self.pad.end = max(f, self.pad.start + 1)
            self.update()
            self.changed.emit()
        else:
            self.setCursor(Qt.SizeHorCursor if self._hit(x) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, _):
        self._drag = None


# ---------------------------------------------------------------------------
# Pad button
# ---------------------------------------------------------------------------

class PadButton(QWidget):
    clicked = Signal(int, object)        # index in bank, keyboard modifiers
    files_dropped = Signal(int, list)    # index in bank, paths
    pad_moved = Signal(int, int)         # source (global index), destination (index in bank)
    cleared = Signal(int)
    rename_requested = Signal(int)       # index in bank

    SIZE = 118

    def __init__(self, index_in_bank):
        super().__init__()
        self.i = index_in_bank
        self.global_index = index_in_bank
        self.pad = None
        self.label = ""
        self.selected = False
        self.primary = False
        self.hover = False
        self.drop_hover = False
        self._press = None
        self._dragged = False
        self._right = None          # pending right-click action: 'delete' or 'rename'
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)

    def bind(self, global_index, pad, label, selected, primary):
        self.global_index, self.pad, self.label = global_index, pad, label
        self.selected, self.primary = selected, primary
        self.update()

    def enterEvent(self, _):
        self.hover = True
        self.update()

    def leaveEvent(self, _):
        self.hover = False
        self.update()

    # -- click / drag source
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()
            self._dragged = False
        elif e.button() == Qt.RightButton and self.pad and self.pad.loaded:
            # macOS turns Ctrl+click into a right-click, so tell the two apart by the modifier.
            # The action fires on release, after this event has finished: opening a dialog from
            # inside a mouse-press handler leaves the pad holding the mouse grab and freezes the UI.
            self._right = "rename" if e.modifiers() & RENAME_MOD else "delete"

    def mouseMoveEvent(self, e):
        if not (e.buttons() & Qt.LeftButton) or self._press is None:
            return
        if not (self.pad and self.pad.loaded):
            return
        if (e.position().toPoint() - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        self._dragged, self._press = True, None
        mime = QMimeData()
        mime.setData(PAD_MIME, str(self.global_index).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        pm = self.grab().scaled(86, 86, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        drag.setPixmap(pm)
        drag.setHotSpot(QPoint(43, 43))
        drag.exec(Qt.MoveAction)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._press is not None and not self._dragged:
            self.clicked.emit(self.i, e.modifiers())
        elif e.button() == Qt.RightButton and self._right:
            action, self._right = self._right, None
            sig = self.rename_requested if action == "rename" else self.cleared
            QTimer.singleShot(0, lambda: sig.emit(self.i))
        self._press = None

    # -- drop target
    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(PAD_MIME) or md.hasUrls():
            self.drop_hover = True
            self.update()
            e.acceptProposedAction()

    def dragLeaveEvent(self, _):
        self.drop_hover = False
        self.update()

    def dropEvent(self, e):
        self.drop_hover = False
        self.update()
        md = e.mimeData()
        if md.hasFormat(PAD_MIME):
            src = int(bytes(md.data(PAD_MIME)).decode())
            e.setDropAction(Qt.MoveAction)
            e.accept()
            self.pad_moved.emit(src, self.i)
            return
        paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(self.i, paths)
            e.acceptProposedAction()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        loaded = self.pad is not None and self.pad.loaded
        base = QColor("#2f6f73") if loaded else QColor("#262c33")
        if self.hover:
            base = base.lighter(120)
        r = QRectF(3, 3, self.width() - 6, self.height() - 6)
        p.setBrush(base)
        if self.drop_hover:
            pen = QPen(QColor("#68d391"), 3)
        elif self.selected:
            pen = QPen(QColor("#f6ad55"), 4 if self.primary else 2)
        else:
            pen = QPen(QColor("#3a424b"), 1)
        p.setPen(pen)
        p.drawRoundedRect(r, 10, 10)

        p.setPen(QColor("#e2e8f0"))
        p.setFont(QFont("Helvetica Neue", 11, QFont.Bold))
        p.drawText(QRectF(10, 8, 60, 18), Qt.AlignLeft, self.label)
        if loaded:
            pad = self.pad
            p.setFont(QFont("Helvetica Neue", 11))
            p.drawText(QRectF(10, 34, self.width() - 20, 40), Qt.AlignLeft | Qt.TextWordWrap,
                       pad.name)
            p.setFont(QFont("Menlo", 9))
            p.setPen(QColor("#a0aec0"))
            tag = "ST" if pad.channels == 2 else "MONO"
            line = f"{tag}  {(pad.end - pad.start) / RATE:.2f}s"
            p.drawText(QRectF(10, self.height() - 26, self.width() - 20, 16), Qt.AlignLeft, line)
            if abs(pad.pitch) > 1e-6:
                p.setPen(QColor("#f6ad55"))
                p.setFont(QFont("Menlo", 9, QFont.Bold))
                p.drawText(QRectF(self.width() - 70, 8, 60, 14), Qt.AlignRight, f"{pad.pitch:+.1f}st")
            if pad.lofi_active:
                p.setPen(QColor("#b794f4"))
                p.setFont(QFont("Menlo", 8, QFont.Bold))
                p.drawText(QRectF(self.width() - 80, 21, 70, 12), Qt.AlignRight,
                           f"{pad.bits}b {pad.srate / 1000:g}k")
        else:
            p.setPen(QColor("#56606b"))
            p.setFont(QFont("Helvetica Neue", 10))
            p.drawText(r, Qt.AlignCenter, "drop audio")


class ResetSlider(QSlider):
    """Vertical slider: double-click resets to 0, and the handle 'sticks' near each octave line."""

    SNAP = 80   # cents on either side of an octave where the handle sticks

    def __init__(self, orientation):
        super().__init__(orientation)
        self.sliderMoved.connect(self._snap)

    def _snap(self, pos):
        nearest = int(round(pos / 1200.0)) * 1200
        if pos != nearest and abs(pos - nearest) <= self.SNAP:
            self.setSliderPosition(nearest)

    def mouseDoubleClickEvent(self, e):
        self.setValue(0)
        e.accept()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MPC2000XL Kit Builder")
        self.pads = [Pad() for _ in range(64)]
        self.bank = 0
        self.primary = 0           # global index of the pad shown in the waveform view
        self.selected = {0}        # global indices of all selected pads
        self.program = None        # settings carried over from an opened .PGM (None = new kit)

        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(10)

        self.wave = WaveformView()
        self.wave.changed.connect(self._refresh)
        v.addWidget(self.wave)

        # edit toolbar
        tools = QHBoxLayout()
        self.loc_label = QLabel("A01")
        self.loc_label.setStyleSheet("font-weight:600; color:#f6ad55;")
        self.loc_label.setMinimumWidth(70)
        tools.addWidget(self.loc_label)
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(16)
        self.name_edit.setPlaceholderText("Sample name (16 chars)")
        self.name_edit.setFixedWidth(170)
        self.name_edit.textEdited.connect(self._rename)
        tools.addWidget(self.name_edit)
        self.btn = {}
        for key, text, fn in (
            ("play", "▶ Play", self.play_selected),
            ("stop", "■ Stop", self.stop_playback),
            ("trim", "Trim to Markers", self.do_trim),
            ("norm", "Normalize", self.do_normalize),
            ("mono", "Stereo → Mono", self.do_mono),
            ("undo", "Undo", self.do_undo),
            ("clear", "Clear", self.do_clear),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            tools.addWidget(b)
            self.btn[key] = b
            if key == "trim":
                self.zero_check = QCheckBox("Zero")
                self.zero_check.setToolTip(
                    "Snap the START/END markers to the nearest zero crossing so a trimmed\n"
                    "sample doesn't pop at its start or end.")
                self.zero_check.toggled.connect(self.on_zero_toggled)
                tools.addWidget(self.zero_check)
        tools.addStretch(1)
        v.addLayout(tools)

        # bank selectors + selection helpers
        bank_row = QHBoxLayout()
        self.bank_btns = []
        for i, letter in enumerate(BANKS):
            b = QPushButton(f"Bank {letter}")
            b.setCheckable(True)
            b.setFixedWidth(90)
            b.clicked.connect(lambda _=False, i=i: self.set_bank(i))
            bank_row.addWidget(b)
            self.bank_btns.append(b)
        bank_row.addSpacing(12)
        sb = QPushButton("Select Bank")
        sb.clicked.connect(self.select_bank)
        bank_row.addWidget(sb)
        sa = QPushButton("Select All")
        sa.clicked.connect(self.select_all)
        bank_row.addWidget(sa)
        bank_row.addStretch(1)
        v.addLayout(bank_row)

        # pad grid (hardware layout: pad 1 bottom-left) + pitch/speed slider
        grid = QGridLayout()
        grid.setSpacing(8)
        self.pad_widgets = []
        for i in range(16):
            w = PadButton(i)
            w.clicked.connect(self.on_pad_click)
            w.files_dropped.connect(self.on_files_dropped)
            w.pad_moved.connect(self.on_pad_moved)
            w.cleared.connect(self.on_pad_cleared)
            w.rename_requested.connect(self.on_pad_rename)
            row, col = 3 - i // 4, i % 4
            grid.addWidget(w, row, col)
            self.pad_widgets.append(w)

        slider_col = QVBoxLayout()
        slider_col.setSpacing(4)
        top = QLabel(f"+{MAX_SEMITONES // 12} oct")
        top.setAlignment(Qt.AlignHCenter)
        bot = QLabel(f"−{MAX_SEMITONES // 12} oct")
        bot.setAlignment(Qt.AlignHCenter)
        self.pitch_slider = ResetSlider(Qt.Vertical)
        self.pitch_slider.setRange(-MAX_SEMITONES * 100, MAX_SEMITONES * 100)   # cents
        self.pitch_slider.setSingleStep(10)
        self.pitch_slider.setPageStep(100)
        self.pitch_slider.setTickPosition(QSlider.TicksBothSides)
        self.pitch_slider.setTickInterval(1200)
        self.pitch_slider.setToolTip("Pitch + speed (tape-style), ±2 octaves. Double-click to reset to 0.")
        self.pitch_slider.valueChanged.connect(self.on_pitch)
        self.pitch_label = QLabel("0.00 st")
        self.pitch_label.setAlignment(Qt.AlignHCenter)
        self.pitch_label.setMinimumWidth(78)
        self.pitch_label.setStyleSheet("font-family:Menlo; font-size:11px;")
        cap = QLabel("pitch / speed")
        cap.setAlignment(Qt.AlignHCenter)
        cap.setStyleSheet("color:#a0aec0; font-size:10px;")
        slider_col.addWidget(top)
        slider_col.addWidget(self.pitch_slider, 1, Qt.AlignHCenter)
        slider_col.addWidget(bot)
        slider_col.addWidget(self.pitch_label)
        slider_col.addWidget(cap)

        # lo-fi panel (single selected pad): bit depth + sample rate, preview, revert
        lofi = QVBoxLayout()
        lofi.setSpacing(5)
        lt = QLabel("LO-FI")
        lt.setStyleSheet("font-weight:700; color:#b794f4; letter-spacing:2px;")
        lofi.addWidget(lt)
        lofi.addWidget(self._small("Bit depth"))
        self.bit_btns, self.rate_btns = {}, {}
        bg = QButtonGroup(self)
        for bits in BIT_DEPTHS:
            b = QPushButton(f"{bits}-bit")
            b.setCheckable(True)
            bg.addButton(b)
            b.clicked.connect(lambda _=False, bits=bits: self.on_bits(bits))
            lofi.addWidget(b)
            self.bit_btns[bits] = b
        lofi.addSpacing(6)
        lofi.addWidget(self._small("Sample rate"))
        rg = QButtonGroup(self)
        for rate in SAMPLE_RATES:
            b = QPushButton(f"{rate} Hz")
            b.setCheckable(True)
            rg.addButton(b)
            b.clicked.connect(lambda _=False, rate=rate: self.on_srate(rate))
            lofi.addWidget(b)
            self.rate_btns[rate] = b
        lofi.addSpacing(6)
        self.lofi_preview = QPushButton("▶ Preview")
        self.lofi_preview.clicked.connect(self.play_selected)
        lofi.addWidget(self.lofi_preview)
        self.lofi_revert = QPushButton("Revert")
        self.lofi_revert.setToolTip("Back to the original 16-bit / 44100 Hz sound")
        self.lofi_revert.clicked.connect(self.on_lofi_revert)
        lofi.addWidget(self.lofi_revert)
        self.lofi_hint = QLabel("")
        self.lofi_hint.setWordWrap(True)
        self.lofi_hint.setStyleSheet("color:#a0aec0; font-size:10px;")
        self.lofi_hint.setMaximumWidth(110)
        lofi.addWidget(self.lofi_hint)
        lofi.addStretch(1)

        gwrap = QHBoxLayout()
        gwrap.addStretch(1)
        gwrap.addLayout(grid)
        gwrap.addSpacing(14)
        gwrap.addLayout(slider_col)
        gwrap.addSpacing(18)
        gwrap.addLayout(lofi)
        gwrap.addStretch(1)
        v.addLayout(gwrap)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#a0aec0")
        v.addWidget(self.status)

        # export options row
        opts = QHBoxLayout()
        opts.addStretch(1)
        opts.addWidget(QLabel("Export for:"))
        self.target_combo = QComboBox()
        self.target_combo.addItem("MPC2000XL  (.PGM + .SND)", "classic")
        self.target_combo.addItem("Modern MPC  (.XPM + WAV)", "modern")
        self.target_combo.addItem("MPC Sample  (.mpcsample + WAV)", "mpcsample")
        self.target_combo.setToolTip(
            "MPC2000XL: .PGM program + .SND samples (16-bit, 44.1 kHz).\n"
            "Modern MPC (Live / Live 2 / Live 3 / X / One / Key / Sample): an .XPM drum program and\n"
            "WAV files in their own folder. Keep the .xpm and the WAVs together.\n"
            "MPC Sample (MPC Software 3.8+ kit format): a .mpcsample plus WAVs named 'A01 - Name.wav' ...\n"
            "in their own folder. Copy the whole folder to the SD card and use Load Kit.")
        self.target_combo.currentIndexChanged.connect(self._target_changed)
        opts.addWidget(self.target_combo)
        self.depth_combo = QComboBox()
        self.depth_combo.addItem("16-bit WAV", 16)
        self.depth_combo.addItem("24-bit WAV", 24)
        self.depth_combo.setToolTip("Modern MPCs accept 16- or 24-bit WAV. 24-bit is just 16-bit audio padded out,\n"
                                    "so it only makes files 50% bigger.")
        opts.addWidget(self.depth_combo)
        self.wav_check = QCheckBox("Also export WAV files")
        self.wav_check.setChecked(False)
        self.wav_check.setToolTip("Unchecked: export .PGM + .SND only. Checked: also write a .WAV copy of each sample.")
        opts.addWidget(self.wav_check)
        self.true_rate_check = QCheckBox("Lo-fi: write true sample rate")
        self.true_rate_check.setToolTip(
            "Unchecked (safe): lo-fi pads are baked into normal 44100 Hz files.\n"
            "Checked: files keep the reduced rate (smaller). Modern MPCs play any WAV rate correctly;\n"
            "the MPC2000XL only natively supports 44100/22050 Hz, so test 11025 Hz on your machine first.")
        opts.addWidget(self.true_rate_check)
        v.addLayout(opts)

        # file row
        bottom = QHBoxLayout()
        op = QPushButton("Open Kit…")
        op.setToolTip("Open a .PGM (MPC2000XL), .xpm (modern MPC) or .mpcsample (MPC Sample) kit")
        op.clicked.connect(self.open_kit)
        bottom.addWidget(op)
        ob = QPushButton("Open Folder…")
        ob.clicked.connect(self.open_folder)
        bottom.addWidget(ob)
        ab = QPushButton("Add Files…")
        ab.clicked.connect(self.add_files)
        bottom.addWidget(ab)
        cb = QPushButton("Clear Kit")
        cb.clicked.connect(self.clear_kit)
        bottom.addWidget(cb)
        bottom.addStretch(1)
        eb = QPushButton("Export…")
        eb.setDefault(True)
        eb.setStyleSheet("font-weight:600; padding:6px 22px;")
        eb.clicked.connect(self.export_kit)
        bottom.addWidget(eb)
        v.addLayout(bottom)

        QShortcut(QKeySequence("Ctrl+Z"), self, self.do_undo)
        QShortcut(QKeySequence("Ctrl+A"), self, self.select_all)
        QShortcut(QKeySequence("Space"), self, self.play_selected)
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#1b2025; color:#e2e8f0; }
            QPushButton { background:#2d353d; border:1px solid #3a424b; border-radius:6px; padding:5px 10px; }
            QPushButton:hover { background:#38424c; }
            QPushButton:checked { background:#c05621; border-color:#f6ad55; }
            QPushButton:disabled { color:#5a6570; }
            QComboBox { background:#2d353d; border:1px solid #3a424b; border-radius:6px; padding:4px 8px; }
            QComboBox QAbstractItemView { background:#2d353d; selection-background-color:#c05621; }
            QLineEdit { background:#14181c; border:1px solid #3a424b; border-radius:6px; padding:4px 6px; }
        """)
        self._target_changed()
        self.set_bank(0)
        self.resize(980, 930)

    # -- helpers
    @staticmethod
    def _small(text):
        l = QLabel(text)
        l.setStyleSheet("color:#a0aec0; font-size:10px;")
        return l

    @property
    def cur(self) -> Pad:
        return self.pads[self.primary]

    def say(self, text):
        self.status.setText(text)

    def _sel_loaded(self):
        return [self.pads[i] for i in sorted(self.selected) if self.pads[i].loaded]

    @staticmethod
    def _label(g):
        return f"{BANKS[g // 16]}{g % 16 + 1:02d}"

    def _refresh(self):
        for i, w in enumerate(self.pad_widgets):
            g = self.bank * 16 + i
            w.bind(g, self.pads[g], self._label(g), g in self.selected, g == self.primary)
        for i, b in enumerate(self.bank_btns):
            b.setChecked(i == self.bank)
            used = sum(p.loaded for p in self.pads[i * 16:(i + 1) * 16])
            b.setText(f"Bank {BANKS[i]}" + (f"  ({used})" if used else ""))

        pad = self.cur
        sel = self._sel_loaded()
        n = len(sel)
        self.wave.set_pad(pad)
        self.btn["play"].setEnabled(pad.loaded)
        for k in ("trim", "norm", "clear"):
            self.btn[k].setEnabled(n > 0)
        self.btn["mono"].setEnabled(any(p.channels == 2 for p in sel))
        self.btn["undo"].setEnabled(any(p.undo is not None for p in sel))

        extra = len(self.selected) - 1
        self.loc_label.setText(self._label(self.primary) + (f" +{extra}" if extra > 0 else ""))
        self.name_edit.setEnabled(pad.loaded and len(self.selected) == 1)
        if self.name_edit.text() != pad.name and not self.name_edit.hasFocus():
            self.name_edit.setText(pad.name)

        self.pitch_slider.setEnabled(n > 0)
        self.pitch_slider.blockSignals(True)
        self.pitch_slider.setValue(int(round(pad.pitch * 100)) if pad.loaded else 0)
        self.pitch_slider.blockSignals(False)
        self._update_pitch_label()

        single = len(self.selected) == 1 and pad.loaded
        for b in list(self.bit_btns.values()) + list(self.rate_btns.values()):
            b.setEnabled(single)
        self.lofi_preview.setEnabled(single)
        self.lofi_revert.setEnabled(single and pad.lofi_active)
        self.lofi_hint.setText("" if single else "Select a single loaded pad")
        for btn, val in ((self.bit_btns, pad.bits), (self.rate_btns, pad.srate)):
            btn[val].blockSignals(True)
            btn[val].setChecked(True)
            btn[val].blockSignals(False)

        total = sum(p.loaded for p in self.pads)
        kb = sum(p.frames * p.channels * 2 for p in self.pads if p.loaded) / 1024
        self.say(f"{total}/64 pads loaded · {kb / 1024:.2f} MB · {n} selected")

    def _update_pitch_label(self):
        v = self.pitch_slider.value()
        st = v / 100.0
        octave = f"{v // 1200:+d} oct" if v and v % 1200 == 0 else "\u00a0"
        self.pitch_label.setText(f"{st:+.2f} st\n×{2 ** (st / 12):.3f}\n{octave}" if v
                                 else "0.00 st\n×1.000\n\u00a0")

    def set_bank(self, i):
        self.bank = i
        self._refresh()

    # -- selection
    def on_pad_click(self, i, mods):
        g = self.bank * 16 + i
        if mods & RENAME_MOD:                      # Control-click renames the sample
            QTimer.singleShot(0, lambda: self.on_pad_rename(i))    # after the click event is done
            return
        # Shift-click (or Cmd-click on a Mac) toggles individual pads, in any order and across banks
        multi = bool(mods & MULTI_MODS)
        if multi:
            if g in self.selected and len(self.selected) > 1:
                self.selected.discard(g)
                if self.primary == g:
                    self.primary = sorted(self.selected)[0]
            else:
                self.selected.add(g)
                self.primary = g
        else:
            self.selected = {g}
            self.primary = g
            self._refresh()
            if self.cur.loaded:
                self.play_selected()
            return
        self._refresh()

    def select_all(self):
        loaded = {i for i, p in enumerate(self.pads) if p.loaded}
        if loaded:
            self.selected = loaded
            if self.primary not in loaded:
                self.primary = min(loaded)
            self._refresh()

    def select_bank(self):
        loaded = {i for i in range(self.bank * 16, self.bank * 16 + 16) if self.pads[i].loaded}
        if loaded:
            self.selected = loaded
            self.primary = min(loaded)
            self._refresh()

    # -- pad interaction
    def on_pad_cleared(self, i):
        g = self.bank * 16 + i
        pad = self.pads[g]
        if not pad.loaded:
            return
        box = QMessageBox(QMessageBox.Question, "Delete?",
                          f"Delete the sample on pad {self._label(g)}?\n\n{pad.long_name or pad.name}",
                          QMessageBox.Yes | QMessageBox.No, self)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        pad.clear()
        self._refresh()

    def on_pad_rename(self, i):
        g = self.bank * 16 + i
        self.selected, self.primary = {g}, g
        pad = self.pads[g]
        if not pad.loaded:
            self._refresh()
            return
        text, ok = QInputDialog.getText(self, "Rename sample", f"Name for the sample on {self._label(g)}:",
                                        QLineEdit.Normal, pad.long_name or pad.name)
        if ok and text.strip():
            pad.long_name = fmt.clean_name(text, maxlen=60)
            pad.name = fmt.clean_name(text)
        self._refresh()

    def on_files_dropped(self, i, paths):
        files = self._expand(paths)
        g = self.bank * 16 + i
        self.selected, self.primary = {g}, g
        self._load_sequence(g, files, overwrite=True)

    def on_pad_moved(self, src, i):
        dst = self.bank * 16 + i
        if src == dst or not self.pads[src].loaded:
            return
        if self.pads[dst].loaded:
            self.pads[src], self.pads[dst] = self.pads[dst], self.pads[src]
            msg = f"Swapped {self._label(src)} ⇄ {self._label(dst)}"
        else:
            self.pads[dst], self.pads[src] = self.pads[src], Pad()
            msg = f"Moved {self._label(src)} → {self._label(dst)}"
        self.selected, self.primary = {dst}, dst
        self.wave.invalidate()
        self._refresh()
        self.say(msg)

    # -- loading
    @staticmethod
    def _expand(paths):
        out = []
        for p in paths:
            pp = Path(p)
            if pp.is_dir():
                out += sorted((f for f in pp.iterdir()
                               if f.suffix.lower() in AUDIO_EXTS and not f.name.startswith(".")),
                              key=lambda f: [int(t) if t.isdigit() else t.lower()
                                             for t in re.split(r"(\d+)", f.name)])
            elif pp.suffix.lower() in AUDIO_EXTS:
                out.append(pp)
        return out

    def _load_sequence(self, start_idx, files, overwrite):
        if not files:
            self.say("No audio files found")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        loaded, failed, skipped, first = 0, [], 0, None
        idx = start_idx
        try:
            for f in files:
                if not overwrite:
                    while idx < 64 and self.pads[idx].loaded:
                        idx += 1
                if idx >= 64:
                    skipped = len(files) - loaded - len(failed)
                    break
                try:
                    pcm = load_as_mpc_pcm(str(f))
                    self.pads[idx].set_audio(re.sub(r"^[A-Ha-h]\d{2}\s*-\s*", "", Path(f).stem), pcm)
                    first = idx if first is None else first
                    loaded += 1
                    idx += 1
                except Exception as ex:  # keep going, report at the end
                    failed.append(f"{Path(f).name}: {ex}")
        finally:
            QApplication.restoreOverrideCursor()
        if first is not None:
            self.bank = first // 16
            if not overwrite:
                self.selected, self.primary = {first}, first
        self.wave.invalidate()
        self._refresh()
        msg = f"Loaded {loaded} file(s)"
        if skipped:
            msg += f"; {skipped} didn't fit (kit is full at 64 pads)"
        if failed:
            msg += f"; {len(failed)} failed"
            QMessageBox.warning(self, "Some files failed", "\n".join(failed[:12]))
        self.say(msg)

    def open_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose a folder of audio files")
        if d:
            self._load_sequence(0, self._expand([d]), overwrite=False)

    def add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))
        fs, _ = QFileDialog.getOpenFileNames(self, "Choose audio files", "", f"Audio ({exts})")
        if fs:
            self._load_sequence(0, [Path(f) for f in fs], overwrite=False)

    def clear_kit(self):
        if any(p.loaded for p in self.pads):
            if QMessageBox.question(self, "Clear kit", "Remove all samples from all 64 pads?") \
                    != QMessageBox.Yes:
                return
        for p in self.pads:
            p.clear()
        self.program = None
        self.setWindowTitle("MPC2000XL Kit Builder")
        self.wave.invalidate()
        self._refresh()

    # -- opening an existing program
    def open_kit(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open kit", "",
            "MPC kits (*.PGM *.pgm *.xpm *.mpcsample);;MPC2000XL program (*.PGM *.pgm);;"
            "Modern MPC program (*.xpm);;MPC Sample kit (*.mpcsample)")
        if not path:
            return
        ext = Path(path).suffix.lower()
        if ext == ".xpm":
            self.open_xpm(path)
        elif ext == ".mpcsample":
            self.open_mpcsample(path)
        elif ext == ".pgm":
            self.open_pgm(path)
        else:
            QMessageBox.critical(self, "Can't open file", "Choose a .PGM, .xpm or .mpcsample file.")

    def _confirm_replace(self):
        if any(p.loaded for p in self.pads):
            return QMessageBox.question(self, "Open kit",
                                        "Replace the current kit with this one?") == QMessageBox.Yes
        return True

    def _finish_open(self, path, program, missing, failed, notes=()):
        self.program = program
        loaded = [i for i, p in enumerate(self.pads) if p.loaded]
        first = loaded[0] if loaded else 0
        self.bank, self.primary, self.selected = first // 16, first, {first}
        self.setWindowTitle(f"MPC2000XL Kit Builder — {Path(path).name}")
        self.wave.invalidate()
        self._refresh()
        self.say(f"Opened {Path(path).name}: {len(loaded)} pad(s) loaded")
        problems = []
        if missing:
            problems.append("Sample files not found next to the kit (those pads are empty):\n  "
                            + "\n  ".join(missing[:15]))
        if failed:
            problems.append("Couldn't read:\n  " + "\n  ".join(failed[:10]))
        problems += list(notes)
        if problems:
            QMessageBox.warning(self, "Opened with notes", "\n\n".join(problems))

    def open_xpm(self, path):
        try:
            d = fmt.parse_xpm(Path(path).read_bytes())
        except Exception as ex:
            QMessageBox.critical(self, "Can't open program", str(ex))
            return
        if d["type"] and d["type"].lower() != "drum":
            QMessageBox.critical(self, "Can't open program",
                                 f"This is a {d['type']} program. Only Drum programs can be opened.")
            return
        if not self._confirm_replace():
            return
        folder = Path(path).parent
        files = {f.name.lower(): f for f in folder.iterdir() if f.is_file()}
        missing, failed, multi, beyond = [], [], 0, 0
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for p in self.pads:
                p.clear()
            for idx, layers in sorted(d["instruments"].items()):
                if not 0 <= idx < 64:
                    beyond += 1
                    continue
                lay = layers[0]
                multi += len(layers) > 1
                base = Path(lay["file"].replace("\\", "/")).name if lay["file"] else ""
                f = (files.get(base.lower()) if base else None) or files.get(f"{lay['name'].lower()}.wav")
                label = lay["file"] or lay["name"]
                if f is None:
                    missing.append(label)
                    continue
                try:
                    pcm = load_as_mpc_pcm(str(f))
                    pad = self.pads[idx]
                    pad.set_audio(lay["name"] or Path(f).stem, pcm)
                    try:
                        n0 = sf.info(str(f)).frames
                    except Exception:
                        n0 = 0
                    if n0 and 0 <= lay["start"] < lay["end"] <= n0 and (lay["start"], lay["end"]) != (0, n0):
                        ratio = pcm.shape[0] / n0
                        pad.start, pad.end = int(round(lay["start"] * ratio)), min(pcm.shape[0], int(round(lay["end"] * ratio)))
                except Exception as ex:
                    failed.append(f"{label}: {ex}")
        finally:
            QApplication.restoreOverrideCursor()
        notes_list = [n if n is not None and 0 <= n <= 127 else fmt.FIRST_NOTE + i
                      for i, n in enumerate(d["pad_notes"][:64])]
        notes = []
        if multi:
            notes.append(f"{multi} pad(s) had several velocity layers; only the first layer was loaded.")
        if beyond:
            notes.append(f"{beyond} sample(s) were on pads beyond pad 64 and were skipped.")
        notes.append("Only samples, pad notes and sample start/end carry over. Tuning, envelopes and "
                     "other pad settings are not kept when you export.")
        self._finish_open(path, dict(name=d["name"] or Path(path).stem, pad_notes=notes_list),
                          missing, failed, notes)

    def open_mpcsample(self, path):
        try:
            d = fmt.parse_mpcsample(Path(path).read_bytes())
        except Exception as ex:
            QMessageBox.critical(self, "Can't open kit", f"This doesn't look like an MPC Sample kit ({ex})")
            return
        if not self._confirm_replace():
            return
        folder = Path(path).parent
        pat = re.compile(r"^([A-Ha-h])(\d{2})\s*-\s*(.*)$")
        by_pad, extra, unlabeled, beyond = {}, 0, 0, 0
        for f in sorted(folder.iterdir(), key=lambda f: f.name.lower()):
            if not f.is_file() or f.suffix.lower() != ".wav":
                continue
            m = pat.match(f.stem)
            if not m or not 1 <= int(m.group(2)) <= 16:
                unlabeled += 1
                continue
            idx = "ABCDEFGH".index(m.group(1).upper()) * 16 + int(m.group(2)) - 1
            if idx >= 64:
                beyond += 1
            elif idx in by_pad:
                extra += 1
            else:
                by_pad[idx] = (f, m.group(3).strip())
        failed = []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for p in self.pads:
                p.clear()
            for idx, (f, name) in sorted(by_pad.items()):
                try:
                    self.pads[idx].set_audio(name or f.stem, load_as_mpc_pcm(str(f)))
                except Exception as ex:
                    failed.append(f"{f.name}: {ex}")
        finally:
            QApplication.restoreOverrideCursor()
        notes = []
        if beyond:
            notes.append(f"{beyond} sample(s) were on banks E-H and were skipped (the app has banks A-D).")
        if unlabeled:
            notes.append(f"{unlabeled} WAV file(s) in the folder have no pad label like 'A01 - ' and were ignored.")
        if extra:
            notes.append(f"{extra} extra WAV file(s) shared a pad label with another; only the first was loaded.")
        if not by_pad:
            notes.append("No WAV files named like 'A01 - Name.wav' were found next to this kit.")
        bpm = {s["key"]: s["value"].get("bpm", 92.0) for s in d["sequences"]}
        self._finish_open(path, dict(name=Path(path).stem, mute_groups=d["mute_groups"],
                                     simult_targets=d["simult_targets"], bpm_by_key=bpm),
                          [], failed, notes)

    def open_pgm(self, path=None):
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Open MPC2000XL program", "",
                                                  "MPC2000XL Program (*.PGM *.pgm)")
        if not path:
            return
        try:
            d = fmt.parse_pgm(Path(path).read_bytes())
        except Exception as ex:
            QMessageBox.critical(self, "Can't open program", str(ex))
            return
        if any(p.loaded for p in self.pads):
            if QMessageBox.question(self, "Open program",
                                    "Replace the current kit with this program?") != QMessageBox.Yes:
                return
        folder = Path(path).parent
        files = {f.name.lower(): f for f in folder.iterdir() if f.is_file()}

        QApplication.setOverrideCursor(Qt.WaitCursor)
        missing, failed, cache = [], [], {}
        try:
            for p in self.pads:
                p.clear()
            seen_notes = set()
            for i in range(fmt.NUM_PADS):
                note = d["pad_notes"][i]
                if not fmt.FIRST_NOTE <= note < fmt.FIRST_NOTE + fmt.NUM_PADS or note in seen_notes:
                    continue
                seen_notes.add(note)
                k = note - fmt.FIRST_NOTE
                pad = self.pads[i]
                pad.params, pad.mixer = d["note_params"][k], d["mixers"][k]
                si = d["note_samples"][k]
                if si >= len(d["samples"]):
                    continue
                name = d["samples"][si]
                if si not in cache:
                    snd, wav = files.get(f"{name.lower()}.snd"), files.get(f"{name.lower()}.wav")
                    try:
                        if snd:
                            cache[si] = load_snd_as_mpc_pcm(snd)
                        elif wav:
                            pcm = load_as_mpc_pcm(str(wav))
                            cache[si] = (pcm, 0, pcm.shape[0])
                        else:
                            cache[si] = None
                            missing.append(name)
                    except Exception as ex:
                        cache[si] = None
                        failed.append(f"{name}: {ex}")
                if cache[si] is not None:
                    pcm, st, en = cache[si]
                    pad.set_audio(name, pcm.copy())
                    pad.start, pad.end = st, en
        finally:
            QApplication.restoreOverrideCursor()

        self.program = dict(name=d["program"] or Path(path).stem, slider=d["slider"],
                            midi=d["midi"], footer=d["footer"], pad_notes=list(d["pad_notes"]))
        loaded = [i for i, p in enumerate(self.pads) if p.loaded]
        first = loaded[0] if loaded else 0
        self.bank, self.primary, self.selected = first // 16, first, {first}
        self.setWindowTitle(f"MPC2000XL Kit Builder — {Path(path).name}")
        self.wave.invalidate()
        self._refresh()
        self.say(f"Opened {Path(path).name}: {len(loaded)} pad(s) loaded")
        problems = []
        if missing:
            problems.append("Sample files not found next to the .PGM (those pads are empty):\n  "
                            + "\n  ".join(missing[:15]))
        if failed:
            problems.append("Couldn't read:\n  " + "\n  ".join(failed[:10]))
        if problems:
            QMessageBox.warning(self, "Some samples didn't load", "\n\n".join(problems))

    # -- editing (all batch-aware: act on every selected, loaded pad)
    @staticmethod
    def _export_name(idx, pad):
        """Sample name used in exported files: names over NAME_LIMIT characters are shortened to
        their first NAME_KEEP characters plus the pad's bank + number (e.g. 'Snare loop' on A01 -> 'SnarA01')."""
        name = fmt.clean_name(pad.long_name or pad.name, maxlen=60)
        if len(name) > NAME_LIMIT:
            name = name[:NAME_KEEP].rstrip() + fmt.pad_label(idx)
        return name

    def _rename(self, text):
        if self.cur.loaded:
            self.cur.name = text
            self.cur.long_name = text
            for w in self.pad_widgets:
                w.update()

    def _batch(self, fn):
        sel = self._sel_loaded()
        if not sel:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for p in sel:
                fn(p)
        finally:
            QApplication.restoreOverrideCursor()
        self.wave.invalidate()
        self._refresh()
        if len(sel) > 1:
            self.say(f"Applied to {len(sel)} pads")

    def on_zero_toggled(self, on):
        self.wave.snap_zero = on
        if on:
            for p in self._sel_loaded():
                p.snap_markers()
            self.wave.update()
            self._refresh()

    def do_trim(self):
        def trim(p):
            if self.zero_check.isChecked():
                p.snap_markers()
            p.trim()
        self._batch(trim)

    def do_normalize(self):
        self._batch(lambda p: p.normalize())

    def do_mono(self):
        self._batch(lambda p: p.to_mono())

    def do_undo(self):
        self._batch(lambda p: p.revert())

    def do_clear(self):
        sel = self._sel_loaded()
        if len(sel) > 1 and QMessageBox.question(
                self, "Clear pads", f"Remove the samples from {len(sel)} selected pads?") != QMessageBox.Yes:
            return
        self._batch(lambda p: p.clear())

    def on_pitch(self, value):
        st = value / 100.0
        for p in self._sel_loaded():
            p.pitch = st
        self._update_pitch_label()
        self.wave.update()
        for w in self.pad_widgets:
            w.update()

    # -- lo-fi (non-destructive: the original stays in memory, applied on playback + export)
    def _lofi_target(self):
        return self.cur if (len(self.selected) == 1 and self.cur.loaded) else None

    def on_bits(self, bits):
        p = self._lofi_target()
        if p:
            p.bits = bits
            self._refresh()

    def on_srate(self, rate):
        p = self._lofi_target()
        if p:
            p.srate = rate
            self._refresh()

    def on_lofi_revert(self):
        p = self._lofi_target()
        if p:
            p.bits, p.srate = 16, RATE
            self._refresh()

    # -- playback
    def play_selected(self):
        pad = self.cur
        if not pad.loaded:
            return
        try:
            import sounddevice as sd
            sd.stop()
            sd.play(np.ascontiguousarray(pad.rendered()), RATE)
        except Exception as ex:
            self.say(f"Playback error: {ex}")

    def stop_playback(self):
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    # -- export
    def _target_changed(self, *_):
        target = self.target_combo.currentData()
        self.depth_combo.setVisible(target in ("modern", "mpcsample"))
        self.wav_check.setVisible(target == "classic")
        if target == "mpcsample":                      # the kits Akai's software exports are 24-bit
            self.depth_combo.setCurrentIndex(self.depth_combo.findData(24))

    def export_kit(self):
        target = self.target_combo.currentData()
        if target == "modern":
            self.export_modern()
        elif target == "mpcsample":
            self.export_mpcsample()
        else:
            self.export_pgm()

    def export_modern(self):
        used = [(i, p) for i, p in enumerate(self.pads) if p.loaded]
        if not used:
            QMessageBox.information(self, "Nothing to export", "Load some samples first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export modern MPC program",
                                              f"{(self.program or {}).get('name', 'KIT')}.xpm",
                                              "MPC Program (*.xpm)")
        if not path:
            return
        kit = fmt.clean_name(Path(path).stem, "KIT", maxlen=60)
        folder = Path(path).parent / kit            # the .xpm and its WAVs must live together
        bits = self.depth_combo.currentData()
        names = fmt.unique_names([self._export_name(i, p) for i, p in used], maxlen=60)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            pads = {}
            for n, (idx, p) in enumerate(used):
                pcm, out_rate = p.export_audio(self.true_rate_check.isChecked())
                wav_name = f"{names[n]}.wav"
                fmt.write_wav(folder / wav_name, pcm, out_rate, bits)
                pads[idx] = wav_name
            notes = (self.program or {}).get("pad_notes")
            (folder / f"{kit}.xpm").write_bytes(fmt.build_xpm(kit, pads, notes))
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export failed", str(ex))
            return
        QApplication.restoreOverrideCursor()
        self.say(f"Exported {kit}.xpm + {len(used)} WAVs to {folder}")
        QMessageBox.information(
            self, "Export complete",
            f"Wrote {kit}.xpm plus {len(used)} {bits}-bit WAV files to:\n{folder}\n\n"
            "Copy the whole folder to your MPC's SD card / USB drive (or into your MPC Software "
            "library), browse to it and load the .xpm.\n\n"
            "The .xpm writer is experimental. If the program doesn't load or the pads land in the "
            "wrong places, load the WAVs from the Sample Pool and assign them by hand.")

    def export_mpcsample(self):
        used = [(i, p) for i, p in enumerate(self.pads) if p.loaded]
        if not used:
            QMessageBox.information(self, "Nothing to export", "Load some samples first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export MPC Sample kit",
                                              f"{(self.program or {}).get('name', 'KIT')}.mpcsample",
                                              "MPC Sample Kit (*.mpcsample)")
        if not path:
            return
        kit = fmt.clean_name(Path(path).stem, "KIT", maxlen=60)
        folder = Path(path).parent / kit              # .mpcsample and its WAVs must stay together
        bits = self.depth_combo.currentData()
        names = fmt.unique_names([self._export_name(i, p) for i, p in used], maxlen=60)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for n, (idx, p) in enumerate(used):
                pcm, out_rate = p.export_audio(self.true_rate_check.isChecked())
                # the pad label at the start of the file name is what places the sample on its pad
                fmt.write_wav_akai(folder / f"{fmt.pad_label(idx)} - {names[n]}.wav", pcm, out_rate, bits)
            prog = self.program or {}
            (folder / f"{kit}.mpcsample").write_bytes(fmt.build_mpcsample(
                mute_groups=prog.get("mute_groups"), simult_targets=prog.get("simult_targets"),
                bpm_by_key=prog.get("bpm_by_key")))
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export failed", str(ex))
            return
        QApplication.restoreOverrideCursor()
        self.say(f"Exported {kit}.mpcsample + {len(used)} WAVs to {folder}")
        QMessageBox.information(
            self, "Export complete",
            f"Wrote {kit}.mpcsample plus {len(used)} {bits}-bit WAV files to:\n{folder}\n\n"
            "Copy the whole folder to your MPC Sample's microSD card (Shift + Pad 16 > SD Card Access), "
            "then on the device go to Load, switch to External, pick the kit and press Load Kit.\n\n"
            "Keep the .mpcsample and the WAVs together. Pads are assigned from the A01-style prefix "
            "on each WAV name.")

    def export_pgm(self):
        used = [(i, p) for i, p in enumerate(self.pads) if p.loaded]
        if not used:
            QMessageBox.information(self, "Nothing to export", "Load some samples first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export MPC2000XL program",
                                              f"{(self.program or {}).get('name', 'KIT')}.PGM",
                                              "MPC2000XL Program (*.PGM)")
        if not path:
            return
        folder = Path(path).parent
        prog_name = fmt.clean_name(Path(path).stem, "KIT")
        pgm_path = folder / f"{prog_name.upper()}.PGM"
        with_wav = self.wav_check.isChecked()

        names = fmt.unique_names([self._export_name(i, p) for i, p in used])
        pad_to_sample = [None] * 64
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for n, (idx, p) in enumerate(used):
                pad_to_sample[idx] = n
                # audio between the markers, with pitch/speed and lo-fi applied
                pcm, out_rate = p.export_audio(self.true_rate_check.isChecked())
                fname = names[n].upper()
                (folder / f"{fname}.SND").write_bytes(fmt.build_snd(names[n], pcm, out_rate))
                if with_wav:
                    fmt.write_wav(folder / f"{fname}.WAV", pcm, out_rate)
            prog = self.program or {}
            pgm_path.write_bytes(fmt.build_pgm(
                prog_name, names, pad_to_sample,
                pad_params=[p.params for p in self.pads], pad_mixers=[p.mixer for p in self.pads],
                pad_notes=prog.get("pad_notes"), slider=prog.get("slider"),
                midi=prog.get("midi"), footer=prog.get("footer")))
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export failed", str(ex))
            return
        QApplication.restoreOverrideCursor()
        kinds = ".SND and .WAV files" if with_wav else ".SND files"
        self.say(f"Exported {pgm_path.name} + {len(used)} samples to {folder}")
        QMessageBox.information(
            self, "Export complete",
            f"Wrote {pgm_path.name} plus {len(used)} {kinds} to:\n{folder}\n\n"
            "Copy all of them to the same folder on your CF card / SD / floppy-emulator "
            "volume, then LOAD the .PGM on the MPC2000XL.")


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MPC2000XL Kit Builder")
    icon = resource_path("icon.png")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
