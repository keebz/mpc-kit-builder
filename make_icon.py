#!/usr/bin/env python3
"""Regenerates icon.png (1024) and icon.icns from scratch: 'MPC' stacked above 'PGM'."""
import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen)

TOP, BOTTOM = "MPC", "PGM"
HERE = os.path.dirname(os.path.abspath(__file__))


def draw(size=1024):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
    s = size / 1024.0
    p.scale(s, s)

    # macOS-style rounded tile (824px on a 1024 canvas)
    tile = QRectF(100, 100, 824, 824)
    path = QPainterPath()
    path.addRoundedRect(tile, 185, 185)
    g = QLinearGradient(0, 100, 0, 924)
    g.setColorAt(0, QColor("#2b3a42"))
    g.setColorAt(1, QColor("#14181c"))
    p.fillPath(path, g)
    p.setPen(QPen(QColor(255, 255, 255, 28), 4))
    p.drawPath(path)

    def fit(text, target_w, family="Poppins", weight=QFont.Black):
        f = QFont(family)
        f.setWeight(weight)
        f.setPixelSize(100)
        w = QFontMetricsF(f).horizontalAdvance(text)
        f.setPixelSize(int(100 * target_w / w))
        return f

    for text, color, cy in ((TOP, "#f7fafc", 425), (BOTTOM, "#f6ad55", 640)):
        f = fit(text, 560)
        p.setFont(f)
        p.setPen(QColor(color))
        fm = QFontMetricsF(f)
        cap = fm.capHeight()
        p.drawText(QRectF(100, cy - cap / 2 - fm.ascent() + cap, 824, fm.height() * 1.2)
                   .translated(0, -0), Qt.AlignHCenter | Qt.AlignTop, text)
    p.end()
    return img


def png_bytes(img):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def main():
    QGuiApplication(sys.argv)
    master = draw(1024)
    master.save(os.path.join(HERE, "icon.png"), "PNG")

    # .icns with PNG payloads (supported by macOS 10.7+)
    chunks = [(b"ic07", 128), (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024),
              (b"ic11", 32), (b"ic12", 64), (b"ic13", 256), (b"ic14", 512)]
    body = b""
    for code, px in chunks:
        data = png_bytes(master.scaled(px, px, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        body += code + struct.pack(">I", len(data) + 8) + data
    with open(os.path.join(HERE, "icon.icns"), "wb") as f:
        f.write(b"icns" + struct.pack(">I", len(body) + 8) + body)
    print("wrote icon.png and icon.icns")


if __name__ == "__main__":
    main()
