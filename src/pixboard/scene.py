"""Scenes: pre-rendered static layers + cheap per-frame live overlays, with click hit-testing."""

from __future__ import annotations

import math
from typing import Any

from .canvas import Canvas, mix

SELECT = 0x39C5FF
HOVER = 0x8A9AAD
HIGHLIGHT = 0xC58CFF


class Scene:
    title = ""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.static = Canvas(w, h)
        self.comps: dict[str, Any] = {}
        self.boxes: dict[str, tuple[int, int, int, int]] = {}  # pixel boxes
        self.highlight: set[str] = set()
        self._last_t: float | None = None

    @property
    def order(self) -> list[str]:
        """Selectable ids in reading order (top-to-bottom, left-to-right)."""
        return sorted(self.boxes, key=lambda cid: (self.boxes[cid][1] // 6, self.boxes[cid][0]))

    def hit(self, cx: int, cy: int) -> str | None:
        """Component under cell (cx, cy); the smallest box wins."""
        best, best_area = None, None
        for cid, (x0, y0, x1, y1) in self.boxes.items():
            if x0 <= cx < max(x1, x0 + 1) and y0 // 2 <= cy <= max(y1 - 1, y0) // 2:
                area = (x1 - x0) * (y1 - y0)
                if best_area is None or area < best_area:
                    best, best_area = cid, area
        return best

    def neighbour(self, cid: str | None, dx: int, dy: int) -> str | None:
        """Nearest component in a direction, for arrow-key navigation."""
        if not self.boxes:
            return None
        if cid not in self.boxes:
            return self.order[0]
        x0, y0, x1, y1 = self.boxes[cid]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        best, best_d = None, math.inf
        for other, (a0, b0, a1, b1) in self.boxes.items():
            if other == cid:
                continue
            ox, oy = (a0 + a1) / 2 - cx, (b0 + b1) / 2 - cy
            along = ox * dx + oy * dy
            if along <= 0.5:
                continue
            across = abs(ox * dy) + abs(oy * dx)
            d = along + 2.5 * across
            if d < best_d:
                best, best_d = other, d
        return best or cid

    def dt(self, t: float) -> float:
        d = 0.0 if self._last_t is None else max(0.0, min(0.5, t - self._last_t))
        self._last_t = t
        return d

    def frame(self, t: float, snap: Any = None, selected: str | None = None,
              hover: str | None = None, mode: str = "live",
              highlight: set[str] | None = None) -> Canvas:
        cv = self.static.copy()
        self.draw_live(cv, t, snap, mode)
        ids = self.highlight if highlight is None else highlight
        if ids:
            self.draw_highlight(cv, t, ids, selected)
        self.draw_selection(cv, t, selected, hover)
        return cv

    def draw_live(self, cv: Canvas, t: float, snap: Any, mode: str) -> None:
        pass

    def draw_highlight(self, cv: Canvas, t: float, ids: set[str], selected: str | None) -> None:
        pulse = mix(HIGHLIGHT, 0xFFFFFF, 0.25 + 0.25 * math.sin(t * 5))
        for cid in ids:
            if cid == selected or cid not in self.boxes:
                continue
            x0, y0, x1, y1 = self.boxes[cid]
            cv.frame(x0 - 1, y0 - 1, x1 + 1, y1 + 1, pulse)

    def draw_selection(self, cv: Canvas, t: float, selected: str | None, hover: str | None) -> None:
        if hover and hover != selected and hover in self.boxes:
            x0, y0, x1, y1 = self.boxes[hover]
            cv.frame(x0 - 1, y0 - 1, x1 + 1, y1 + 1, HOVER)
        if selected and selected in self.boxes:
            x0, y0, x1, y1 = self.boxes[selected]
            c = mix(SELECT, 0xFFFFFF, 0.5 + 0.5 * math.sin(t * 6))
            cv.frame(x0 - 1, y0 - 1, x1 + 1, y1 + 1, c)
            if x1 - x0 > 6 and y1 - y0 > 6:
                cv.frame(x0 - 2, y0 - 2, x1 + 2, y1 + 2, mix(c, 0x000000, 0.5))
