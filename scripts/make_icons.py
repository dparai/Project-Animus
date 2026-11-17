"""Draw the Animus app icons (a warm ring with a dot on the dark ground) as PNG files.

Standard library only. Run from the repository root:

    python -m scripts.make_icons

Writes app/static/icons/icon-192.png, icon-512.png, icon-maskable-512.png and
apple-touch-icon.png. The files are committed; run this again only to change the mark.
"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
GROUND = (13, 10, 9)
WARM_A = (255, 187, 116)
WARM_B = (255, 125, 88)


def _mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _cover(distance: float, edge: float) -> float:
    """1 inside a shape, 0 outside, with a one-pixel soft edge (distance < 0 is inside)."""
    return min(1.0, max(0.0, 0.5 - distance / edge))


def _pixel(x: float, y: float, size: int, scale: float):
    """Colour at one pixel centre; `scale` shrinks the mark (maskable icons keep a safe zone)."""
    u, v = (x + 0.5) / size - 0.5, (y + 0.5) / size - 0.5
    r = math.hypot(u, v) / scale
    edge = 1.0 / (size * scale)
    warm = _mix(WARM_A, WARM_B, min(1.0, max(0.0, (u + v) / scale + 0.5)))
    halo = 0.22 * math.exp(-((r / 0.34) ** 2))  # soft light behind the mark
    colour = _mix(GROUND, warm, halo)
    ring = _cover(abs(r - 0.255) - 0.045, edge)
    dot = _cover(r - 0.075, edge)
    return _mix(colour, warm, max(ring, dot))


def render(size: int, scale: float = 1.0) -> bytes:
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter type: none
        for x in range(size):
            rows.extend(int(round(c)) for c in _pixel(x, y, size, scale))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB, no alpha
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size, scale in (
        ("icon-192.png", 192, 1.0),
        ("icon-512.png", 512, 1.0),
        ("icon-maskable-512.png", 512, 0.72),
        ("apple-touch-icon.png", 180, 0.9),
    ):
        (OUT / name).write_bytes(render(size, scale))
        print(name)


if __name__ == "__main__":
    main()
