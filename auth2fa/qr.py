"""Render an otpauth:// URI as a scannable QR code.

We emit a PNG (as a data: URI inside an <img>), not inline SVG. segno's inline
SVG draws each module run as a stroke line; when a browser anti-aliases or
downscales that, the strokes thin out and shift, and the code stops decoding --
which is why it rendered but no scanner could read it. A PNG drawn at an integer
module scale is pixel-exact in every browser, with black modules on a white
field and a 4-module quiet zone, which is what a camera needs.

segno writes PNG natively (pure Python, no Pillow). If segno isn't installed,
qr_img returns None and the caller falls back to showing the secret for manual
entry -- every authenticator app accepts a typed-in key.
"""
from __future__ import annotations

from typing import Optional


def qr_img(data: str, scale: int = 6, border: int = 4) -> Optional[str]:
    """An <img> element with a PNG QR for `data`, or None if no backend exists."""
    try:
        import segno
    except Exception:
        return None
    src = segno.make(data, error="m").png_data_uri(
        scale=scale, border=border, dark="#000000", light="#ffffff"
    )
    return f'<img class="qrcode" alt="Two-factor enrollment QR code" src="{src}">'
