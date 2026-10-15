from __future__ import annotations
import math
import statistics
from collections import deque
from typing import Optional


class RatioSignal:
    """z du ratio log(WTI/Brent) sur une fenêtre glissante (un point par seconde au plus)."""

    def __init__(self, window_min: int) -> None:
        self.window_s = window_min * 60
        self._pts: deque[tuple[float, float]] = deque()

    def add(self, ts_s: float, wti: float, brent: float) -> None:
        if wti <= 0 or brent <= 0:
            return
        sec = math.floor(ts_s)
        r = math.log(wti / brent)
        if self._pts and math.floor(self._pts[-1][0]) == sec:
            self._pts[-1] = (ts_s, r)
        else:
            self._pts.append((ts_s, r))
        while self._pts and self._pts[0][0] < ts_s - self.window_s:
            self._pts.popleft()

    def reset(self) -> None:
        """Oublie tous les points (fermeture de séance) : nouveau préchauffage à la réouverture."""
        self._pts.clear()

    def last_ts(self) -> Optional[float]:
        return self._pts[-1][0] if self._pts else None

    def ready(self) -> bool:
        return len(self._pts) >= 2 and (self._pts[-1][0] - self._pts[0][0]) >= 0.9 * self.window_s

    def z(self) -> Optional[float]:
        if not self.ready():
            return None
        vals = [r for _, r in self._pts]
        sd = statistics.pstdev(vals)
        if sd <= 0:
            return 0.0
        return (vals[-1] - statistics.fmean(vals)) / sd

    def side(self, prev: str) -> str:
        z = self.z()
        if z is None:
            return prev
        return "A" if z >= 0 else "B"


def select_volume_side(decision, current=None, previous=None):
    """Choose the scheduled cycle direction from fresh evidence before old exposure."""
    for candidate in (decision.get("volume_side"), decision.get("side")):
        if candidate in ("A", "B"):
            return candidate
    slope = decision.get("slope_bps_min")
    if isinstance(slope, (int, float)) and math.isfinite(slope) and slope != 0:
        return "B" if slope > 0 else "A"
    for candidate in (current, previous):
        if candidate in ("A", "B"):
            return candidate
    return "B"


class TrendSignal:
    """Régression du log WTI/Brent sur minutes closes, confirmée à court terme.

    Une pente positive correspond à B (long WTI / short Brent). Il s'agit
    d'un filtre de tendance passée, pas d'une prévision de rendement.
    """

    def __init__(self, window_min=30, confirm_min=5, min_move_bps=3.0, min_r2=0.4,
                 context_min=0):
        self.window_min, self.confirm_min = window_min, confirm_min
        self.min_move_bps, self.min_r2 = min_move_bps, min_r2
        self.context_min = context_min
        self._pts = deque()

    def add(self, ts_s, wti, brent):
        if not all(math.isfinite(v) for v in (ts_s, wti, brent)) or wti <= 0 or brent <= 0:
            return
        minute = math.floor(ts_s / 60) * 60
        value = math.log(wti / brent)
        if self._pts and minute < self._pts[-1][0]:
            return
        if self._pts and minute == self._pts[-1][0]:
            self._pts[-1] = (minute, value)
        else:
            self._pts.append((minute, value))
        history_min = max(self.window_min, self.context_min)
        while self._pts and self._pts[0][0] < minute - history_min * 60:
            self._pts.popleft()

    def reset(self):
        self._pts.clear()

    def replace_closed(self, points, now):
        """Load synchronized, fully closed minute bars plus an excluded current-minute marker."""
        self.reset()
        for ts, wti, brent in points:
            if ts + 60 <= now:
                self.add(ts, wti, brent)
        if self._pts:
            self._pts.append((math.floor(now / 60) * 60, self._pts[-1][1]))

    @staticmethod
    def _fit(points):
        xs = [(t - points[0][0]) / 60 for t, _ in points]
        ys = [v for _, v in points]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        xx = sum((x - mx) ** 2 for x in xs)
        yy = sum((y - my) ** 2 for y in ys)
        xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        slope = xy / xx if xx else 0.0
        r2 = min(1.0, max(0.0, xy * xy / (xx * yy))) if xx and yy else 0.0
        return slope * 1e4, r2

    def _context_status(self, closed):
        result = {"ready": False, "side": None, "window_min": self.context_min,
                  "slope_bps_min": None, "r2": None, "move_bps": None,
                  "reason": "contexte désactivé" if not self.context_min else "préchauffage contexte"}
        if not self.context_min:
            return result
        points = closed[-self.context_min:]
        if len(points) < self.context_min:
            return result
        if any(b[0] - a[0] != 60 for a, b in zip(points, points[1:])):
            result["reason"] = "historique contexte discontinu"
            return result
        slope, r2 = self._fit(points)
        move = slope * self.context_min
        result.update(ready=True, slope_bps_min=slope, r2=r2, move_bps=move)
        if abs(move) >= self.min_move_bps and r2 >= self.min_r2:
            result.update(side="B" if slope > 0 else "A",
                          reason="contexte haussier confirmé" if slope > 0 else "contexte baissier confirmé")
        else:
            result["reason"] = "contexte faible ou irrégulier"
        return result

    def status(self):
        # Le dernier point est la minute en cours : exclu des décisions.
        closed = list(self._pts)[:-1]
        context = self._context_status(closed)
        points = ([p for p in closed if p[0] >= closed[-1][0] - (self.window_min - 1) * 60]
                  if closed else [])
        result = {"ready": False, "side": None, "window_min": self.window_min, "slope_bps_min": None,
                  "r2": None, "move_bps": None, "confirm_slope_bps_min": None,
                  "volume_side": None, "context": context, "reason": "préchauffage tendance"}
        if len(points) < max(3, math.ceil(self.window_min * 0.8)):
            return result
        # Five closed 1m candles have four minutes between their opening times.
        if points[-1][0] - points[0][0] < (self.window_min - 1) * 60 * 0.9:
            return result
        if any(b[0] - a[0] != 60 for a, b in zip(points, points[1:])):
            result["reason"] = "historique tendance discontinu"
            return result
        short = [p for p in points if p[0] >= points[-1][0] - self.confirm_min * 60]
        if len(short) < max(3, self.confirm_min):
            return result
        slope, r2 = self._fit(points)
        fast, _ = self._fit(short)
        move = slope * self.window_min
        result.update(ready=True, slope_bps_min=slope, r2=r2,
                      move_bps=move, confirm_slope_bps_min=fast)
        if abs(move) < self.min_move_bps or r2 < self.min_r2:
            result["reason"] = "tendance faible ou irrégulière"
        elif slope * fast <= 0:
            result["reason"] = "tendance non confirmée à court terme"
        else:
            result.update(side="B" if slope > 0 else "A",
                          reason="ratio haussier confirmé" if slope > 0 else "ratio baissier confirmé")
        if self.context_min and not context["ready"]:
            result.update(ready=False, side=None, reason=context["reason"])
            return result
        if context["side"] is not None and result["side"] is not None and context["side"] != result["side"]:
            result.update(side=None, reason="tendance courte opposée au contexte confirmé")
        result["volume_side"] = (context["side"] or result["side"]
                                 or ("B" if slope > 0 else "A" if slope < 0 else None))
        return result
