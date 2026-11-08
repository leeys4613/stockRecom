"""PWA 아이콘 생성 (외부 라이브러리 없이 순수 파이썬 PNG). 결과물은 static/ 에 커밋돼 있다.
실행: .venv/bin/python scripts/make_icons.py
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "static"
BG = (17, 20, 24)
BARS = [((42, 157, 74), 0.34), ((42, 157, 74), 0.52), ((110, 220, 140), 0.74)]


def render(size):
    # maskable 안전영역: 내용은 중앙 60% 안에 둔다
    left, right = int(size * 0.22), int(size * 0.78)
    bottom, top_max = int(size * 0.75), int(size * 0.25)
    gap = int(size * 0.04)
    bar_w = (right - left - gap * (len(BARS) - 1)) // len(BARS)
    rows = []
    for y in range(size):
        row = bytearray([0])  # filter type 0
        for x in range(size):
            px = BG
            for i, (color, frac) in enumerate(BARS):
                x0 = left + i * (bar_w + gap)
                bar_top = bottom - int((bottom - top_max) * frac / 0.74)
                if x0 <= x < x0 + bar_w and bar_top <= y < bottom:
                    px = color
            row += bytes(px)
        rows.append(bytes(row))
    return b"".join(rows)


def write_png(path, size):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = render(size)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        write_png(OUT / name, size)
        print("wrote", OUT / name)
