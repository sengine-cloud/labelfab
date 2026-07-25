"""A two-pass measure/place layout solver.

Deliberately not a template engine. Labels are three or four boxes on a bitmap a
few hundred pixels wide; a flexbox-shaped solver in ~150 lines covers every layout
anyone actually prints, and adding a new one is a small pull request rather than a
config language nobody remembers the syntax of.

All units here are device pixels. Millimetres are converted at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from PIL import Image, ImageDraw


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


@runtime_checkable
class Drawable(Protocol):
    """Anything the solver can size and place."""

    flex: float

    def measure(self, avail_w: int, avail_h: int) -> tuple[int, int]:
        """Natural size, not exceeding the available area."""

    def draw(self, img: Image.Image, d: ImageDraw.ImageDraw, box: Rect) -> None:
        """Render into ``box``, which is exactly the size granted by the solver."""


def _distribute(
    naturals: list[int],
    flexes: list[float],
    budget: int,
) -> list[int]:
    """Share ``budget`` main-axis pixels across children.

    Surplus goes to flex children in proportion to their weight. A deficit is taken
    from flex children first (they are the ones declaring themselves elastic) and
    only then, proportionally, from everyone.
    """
    sizes = list(naturals)
    leftover = budget - sum(sizes)
    if leftover == 0:
        return sizes

    total_flex = sum(flexes)
    if leftover > 0:
        if total_flex <= 0:
            return sizes  # nothing wants to grow; leave the slack unused
        for i, f in enumerate(flexes):
            if f > 0:
                sizes[i] += int(leftover * f / total_flex)
        # Integer division loses up to n-1 px; hand the remainder to the greediest.
        widest_flex = max(range(len(sizes)), key=lambda i: flexes[i])
        sizes[widest_flex] += budget - sum(sizes)
        return sizes

    elastic = sum(sizes[i] for i, f in enumerate(flexes) if f > 0)
    if elastic > 0:
        take = min(-leftover, elastic)
        for i, f in enumerate(flexes):
            if f > 0:
                sizes[i] -= int(take * sizes[i] / elastic)

    still_over = sum(sizes) - budget
    if still_over > 0:
        total = sum(sizes) or 1
        for i in range(len(sizes)):
            sizes[i] = max(0, sizes[i] - int(still_over * sizes[i] / total))
    return sizes


@dataclass
class BoxLayout:
    """A container that lays its children out along one axis."""

    children: list[Drawable]
    direction: str = "row"
    gap_px: int = 5
    padding_px: int = 4
    align: str = "center"
    flex: float = 1.0

    @property
    def _row(self) -> bool:
        return self.direction == "row"

    def _inner(self, w: int, h: int) -> tuple[int, int]:
        p2 = self.padding_px * 2
        return max(0, w - p2), max(0, h - p2)

    def _gaps(self) -> int:
        return self.gap_px * max(0, len(self.children) - 1)

    def measure(self, avail_w: int, avail_h: int) -> tuple[int, int]:
        inner_w, inner_h = self._inner(avail_w, avail_h)
        budget = (inner_w if self._row else inner_h) - self._gaps()
        cross_avail = inner_h if self._row else inner_w

        main_total, cross_max = 0, 0
        for child in self.children:
            cw, ch = (
                child.measure(max(0, budget - main_total), cross_avail)
                if self._row
                else child.measure(cross_avail, max(0, budget - main_total))
            )
            main_total += cw if self._row else ch
            cross_max = max(cross_max, ch if self._row else cw)

        main = main_total + self._gaps() + self.padding_px * 2
        cross = cross_max + self.padding_px * 2
        return (main, cross) if self._row else (cross, main)

    def draw(self, img: Image.Image, d: ImageDraw.ImageDraw, box: Rect) -> None:
        inner_w, inner_h = self._inner(box.w, box.h)
        ox, oy = box.x + self.padding_px, box.y + self.padding_px
        budget = (inner_w if self._row else inner_h) - self._gaps()
        cross_avail = inner_h if self._row else inner_w

        measured = [
            child.measure(budget, cross_avail) if self._row else child.measure(cross_avail, budget)
            for child in self.children
        ]
        naturals = [m[0] if self._row else m[1] for m in measured]
        crosses = [m[1] if self._row else m[0] for m in measured]
        flexes = [getattr(c, "flex", 0.0) for c in self.children]
        mains = _distribute(naturals, flexes, budget)

        cursor = 0
        for child, main, cross in zip(self.children, mains, crosses, strict=True):
            if main <= 0:
                cursor += self.gap_px
                continue
            span = cross_avail if self.align == "stretch" else min(cross, cross_avail)
            if self.align == "start" or self.align == "stretch":
                off = 0
            elif self.align == "end":
                off = cross_avail - span
            else:
                off = (cross_avail - span) // 2

            rect = (
                Rect(ox + cursor, oy + off, main, span)
                if self._row
                else Rect(ox + off, oy + cursor, span, main)
            )
            child.draw(img, d, rect)
            cursor += main + self.gap_px
