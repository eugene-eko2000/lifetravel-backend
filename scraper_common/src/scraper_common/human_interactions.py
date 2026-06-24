"""
Human-like interaction generation from recorded user interaction samples.

Synthesises realistic event sequences by finding the closest matching sample
to a requested interaction and transforming its trajectory to fit the target
start/end points.  Interaction data is loaded centrally via data_store.

Usage
-----
    from scraper_common.human_interactions import interactions

    steps = interactions.generate_mousemove(100, 200, 500, 400)
    for step in steps:
        await dispatch_mouse_move(step.x, step.y)
        if step.delay_ms > 0:
            await asyncio.sleep(step.delay_ms / 1000)
"""

import bisect
import logging
import math
import random
from typing import NamedTuple, Optional

from scraper_common import data_store

logger = logging.getLogger("scraper_common.human_interactions")


class MouseStep(NamedTuple):
    """A single waypoint in a synthesised mouse path."""
    x: int
    y: int
    delay_ms: float  # milliseconds to wait after dispatching this step


class HumanInteractions:
    """
    Generates human-like interaction event sequences from recorded samples.

    Instances are thread-safe for reads after the first call to any generate_*
    method triggers lazy processing of the sample data.
    """

    def __init__(self) -> None:
        self._mousemoves: Optional[list] = None
        self._length_index: Optional[dict[float, list[int]]] = None
        self._length_keys: Optional[list[float]] = None
        self._direction_index: Optional[dict[float, list[int]]] = None
        self._direction_keys: Optional[list[float]] = None

    # ── Data loading ──────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._mousemoves is not None:
            return

        logger.info("Processing interaction samples")

        all_samples = data_store.get("processed")
        self._mousemoves = [s for s in all_samples if s["type"] == "mousemove"]

        raw = data_store.get("movements_length_index")
        self._length_index = {float(k): v for k, v in raw.items()}
        self._length_keys = sorted(self._length_index)

        raw = data_store.get("movements_direction_index")
        self._direction_index = {float(k): v for k, v in raw.items()}
        self._direction_keys = sorted(self._direction_index)

        logger.info(
            "Loaded %d mousemove samples (%d length buckets, %d direction buckets)",
            len(self._mousemoves),
            len(self._length_keys),
            len(self._direction_keys),
        )

    # ── Index search ──────────────────────────────────────────────────────────

    def _collect_linear(
        self,
        sorted_keys: list[float],
        index: dict[float, list[int]],
        target: float,
        k: int,
    ) -> set[int]:
        """
        Expand outward from target in sorted_keys collecting sample indices
        until at least k indices are gathered (or all keys are exhausted).
        Uses absolute distance for comparison.
        """
        if not sorted_keys:
            return set()

        result: set[int] = set()
        lo = bisect.bisect_left(sorted_keys, target) - 1
        hi = lo + 1

        while (lo >= 0 or hi < len(sorted_keys)) and len(result) < k:
            lo_dist = abs(sorted_keys[lo] - target) if lo >= 0 else math.inf
            hi_dist = abs(sorted_keys[hi] - target) if hi < len(sorted_keys) else math.inf
            if lo_dist <= hi_dist:
                result.update(index[sorted_keys[lo]])
                lo -= 1
            else:
                result.update(index[sorted_keys[hi]])
                hi += 1

        return result

    def _collect_circular(
        self,
        sorted_keys: list[float],
        index: dict[float, list[int]],
        target: float,
        k: int,
    ) -> set[int]:
        """
        Like _collect_linear but uses circular (angular) distance so that
        angles near +π and -π are treated as neighbours.
        """
        if not sorted_keys:
            return set()

        _TWO_PI = 2.0 * math.pi

        def _cdist(a: float, b: float) -> float:
            d = abs(a - b)
            return min(d, _TWO_PI - d)

        result: set[int] = set()
        lo = bisect.bisect_left(sorted_keys, target) - 1
        hi = lo + 1

        while (lo >= 0 or hi < len(sorted_keys)) and len(result) < k:
            lo_dist = _cdist(sorted_keys[lo], target) if lo >= 0 else math.inf
            hi_dist = _cdist(sorted_keys[hi], target) if hi < len(sorted_keys) else math.inf
            if lo_dist <= hi_dist:
                result.update(index[sorted_keys[lo]])
                lo -= 1
            else:
                result.update(index[sorted_keys[hi]])
                hi += 1

        return result

    def _pick_sample(self, length: float, direction: float) -> int:
        """
        Find a sample index whose movement vector best matches (length, direction).

        Algorithm:
          1. Collect k=20 closest samples by distance.
          2. Collect k=20 closest samples by direction (circular).
          3. Intersect; if empty, double k and retry.
          4. Pick one at random from the intersection.
        """
        k = 20
        total = len(self._mousemoves)

        while k <= total:
            by_length = self._collect_linear(
                self._length_keys, self._length_index, length, k
            )
            by_direction = self._collect_circular(
                self._direction_keys, self._direction_index, direction, k
            )
            candidates = by_length & by_direction
            if candidates:
                return random.choice(list(candidates))
            k *= 2

        # Absolute fallback: closest by length only
        by_length = self._collect_linear(self._length_keys, self._length_index, length, 20)
        if by_length:
            return random.choice(list(by_length))
        return random.randrange(total)

    # ── Trajectory transform ──────────────────────────────────────────────────

    def _transform(
        self,
        sample: dict,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> list[MouseStep]:
        """
        Remap sample's recorded curve so it starts at (x1, y1) and ends at
        (x2, y2), preserving the relative timing between events.

        The transformation is: translate to origin, rotate to align with the
        target direction, scale to match the target distance, translate to the
        target start point.
        """
        events = sample["events"]
        n = len(events)

        if n == 0:
            return [MouseStep(round(x2), round(y2), 0.0)]

        if n == 1:
            return [MouseStep(round(x2), round(y2), 0.0)]

        sx0 = float(events[0]["x"])
        sy0 = float(events[0]["y"])
        sxn = float(events[-1]["x"])
        syn = float(events[-1]["y"])

        sample_len = math.hypot(sxn - sx0, syn - sy0)
        target_len = math.hypot(x2 - x1, y2 - y1)

        if sample_len < 1e-6:
            # Sample is stationary; produce a linear interpolation instead
            steps: list[MouseStep] = []
            for i, evt in enumerate(events):
                alpha = i / (n - 1)
                delay = float(events[i + 1]["timestamp"] - evt["timestamp"]) if i < n - 1 else 0.0
                steps.append(MouseStep(
                    round(x1 + alpha * (x2 - x1)),
                    round(y1 + alpha * (y2 - y1)),
                    delay,
                ))
            return steps

        scale = target_len / sample_len
        sample_angle = math.atan2(syn - sy0, sxn - sx0)
        target_angle = math.atan2(y2 - y1, x2 - x1)
        theta = target_angle - sample_angle
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)

        steps = []
        for i, evt in enumerate(events):
            # Translate to origin
            px = float(evt["x"]) - sx0
            py = float(evt["y"]) - sy0
            # Rotate
            rx = cos_t * px - sin_t * py
            ry = sin_t * px + cos_t * py
            # Scale and translate to target start
            fx = x1 + rx * scale
            fy = y1 + ry * scale

            delay = float(events[i + 1]["timestamp"] - evt["timestamp"]) if i < n - 1 else 0.0
            steps.append(MouseStep(round(fx), round(fy), delay))

        return steps

    # ── Public API ────────────────────────────────────────────────────────────

    def generate_mousemove(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> list[MouseStep]:
        """
        Generate a human-like mouse path from (x1, y1) to (x2, y2).

        Returns a list of MouseStep(x, y, delay_ms).  The caller should
        dispatch each step as a mouse-move event, then sleep delay_ms
        milliseconds before dispatching the next.  The final step always
        has delay_ms=0.

        If the start and end points are the same (distance < 1 px), returns
        a single step at the destination with no delay.
        """
        self._ensure_loaded()

        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)

        if length < 1.0:
            return [MouseStep(round(x2), round(y2), 0.0)]

        direction = math.atan2(dy, dx)
        sample_idx = self._pick_sample(length, direction)

        logger.debug(
            "generate_mousemove (%.0f,%.0f)→(%.0f,%.0f) len=%.1f dir=%.3f rad → sample[%d]",
            x1, y1, x2, y2, length, direction, sample_idx,
        )

        return self._transform(self._mousemoves[sample_idx], x1, y1, x2, y2)


interactions = HumanInteractions()
