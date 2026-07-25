"""Reconstruct an image from captured printer bytes.

This is the most useful debugging tool in the project. A preview shows what was
*intended*; this shows what the printer would actually receive, so it catches
row-padding off-by-ones, MSB/LSB inversion, a missing invert and a wrong rotation
-- every one of which looks correct in a preview and prints as garbage.

On day one with hardware it is how you diff "what I sent" against "what came out".
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageOps

from labelfab.device.escpos import GS_V0


class DecodeError(Exception):
    """The byte stream is not a frame we recognise."""


@dataclass(frozen=True, slots=True)
class Frame:
    width_px: int
    height_px: int
    offset: int
    image: Image.Image

    @property
    def width_bytes(self) -> int:
        return self.width_px // 8


def find_frames(stream: bytes) -> list[int]:
    """Byte offsets of every ``GS v 0`` header in the stream."""
    offsets, start = [], 0
    while (i := stream.find(GS_V0, start)) != -1:
        offsets.append(i)
        start = i + 1
    return offsets


def decode_frames(stream: bytes, *, rotation: int = 270, mirror: bool = False) -> list[Frame]:
    """Decode every frame found in a capture.

    ``rotation`` should match what was used to encode; the inverse is applied so
    the result comes back in the landscape orientation the label was composed in.
    """
    frames: list[Frame] = []
    for offset in find_frames(stream):
        body_start = offset + len(GS_V0) + 4
        if body_start > len(stream):
            raise DecodeError(f"truncated header at offset {offset}")
        width_bytes = int.from_bytes(stream[offset + 4 : offset + 6], "little")
        height_px = int.from_bytes(stream[offset + 6 : offset + 8], "little")
        expected = width_bytes * height_px
        body = stream[body_start : body_start + expected]
        if len(body) != expected:
            raise DecodeError(
                f"frame at offset {offset} claims {width_bytes}B x {height_px} lines "
                f"= {expected}B but only {len(body)}B follow"
            )

        packed = Image.frombytes("1", (width_bytes * 8, height_px), body)
        # Bit 1 means burn, i.e. black; invert back to a normal black-on-white image.
        img = ImageOps.invert(packed.convert("L"))
        if mirror:
            img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if rotation:
            img = img.transpose(
                {
                    90: Image.Transpose.ROTATE_270,
                    180: Image.Transpose.ROTATE_180,
                    270: Image.Transpose.ROTATE_90,
                }[rotation]
            )
        frames.append(Frame(width_px=width_bytes * 8, height_px=height_px, offset=offset, image=img))
    return frames


def decode(stream: bytes, *, rotation: int = 270, mirror: bool = False) -> Image.Image:
    """Decode a capture expected to hold exactly one frame."""
    frames = decode_frames(stream, rotation=rotation, mirror=mirror)
    if not frames:
        raise DecodeError(
            "no GS v 0 frame found; is this a printer capture? "
            "(expected the bytes 1d 76 30 00 somewhere in the stream)"
        )
    if len(frames) > 1:
        raise DecodeError(
            f"{len(frames)} frames in this capture. A strip should be exactly one "
            f"frame -- more than one means the batch was printed discretely and paid "
            f"a leader/trailer feed per label. Use decode_frames() to inspect them."
        )
    return frames[0].image
