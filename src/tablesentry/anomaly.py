"""Robust anomaly detection for a metric against its own history.

Uses the median and the median absolute deviation (MAD) rather than mean and standard
deviation, so one past incident does not blind the detector to the next one.

    score = |value - median(history)| / max(1.4826 * MAD, min_scale)

``min_scale`` stops a perfectly flat history (e.g. a null rate that is always 0%) from
turning a trivial wobble into an infinite score. Each check chooses a sensible floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

MAD_TO_SIGMA = 1.4826  # makes MAD comparable to a standard deviation for normal data


@dataclass(frozen=True)
class Verdict:
    is_anomaly: bool
    score: float
    median: float
    scale: float
    history_size: int

    def describe(self, unit: str = "") -> str:
        return (
            f"median of last {self.history_size} runs is {self.median:,.4g}{unit}, "
            f"score {self.score:.1f}"
        )


def detect(
    value: float, history: list[float], sensitivity: float, min_history: int, min_scale: float
) -> Verdict | None:
    """Return None while there is not enough history to judge."""
    if len(history) < min_history:
        return None
    med = median(history)
    mad = median(abs(x - med) for x in history)
    scale = max(MAD_TO_SIGMA * mad, min_scale)
    score = abs(value - med) / scale
    return Verdict(score > sensitivity, score, med, scale, len(history))
