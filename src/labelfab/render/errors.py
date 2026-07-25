"""Render failures.

Every one of these is *terminal*: retrying will produce the same result. The agent
must never re-queue a job that raised one of these, and must never fall back to
printing something smaller — an unscannable label costs tape and reads as success.
"""


class RenderError(Exception):
    """Base class for anything that makes a label unprintable."""


class QrTooDense(RenderError):
    """The QR would fall below the minimum device pixels per module.

    Below 2px/module (0.25mm at 203dpi) the module edges blur on thermal media and
    phone cameras stop decoding. Shorten the payload, lower the quiet zone, or use
    a short-link redirector.
    """


class BarcodeTooWide(RenderError):
    """The barcode does not fit the label at or above its narrow-bar floor.

    Auto-shrinking is deliberately not offered: it silently drops below the
    scannable floor and produces a label that looks fine and does not work.
    """


class LayoutOverflow(RenderError):
    """Content cannot be placed in the available area even at minimum sizes."""


class FontUnavailable(RenderError):
    """A requested font could not be resolved.

    Falling back to ``ImageFont.load_default()`` is not an option: it silently
    substitutes a tiny bitmap face and yields a label that looks broken rather
    than failing loudly.
    """


class UnknownPreset(RenderError):
    """The job named a layout preset this agent does not have."""
