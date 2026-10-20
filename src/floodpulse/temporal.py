"""Retrospective temporal analysis of stored FloodPulse history (M16).

This is the Python side of the M16 temporal contract defined by
``tests/fixtures/m16_temporal_cases.json``. ``web/lib.js`` mirrors the same fixture for the
dashboard, and ``tools/history_analysis.py`` imports *this* module rather than repeating the
rules, so the tool and the page cannot drift apart. Everything here is retrospective: it
describes what the stored scores did. It is not a forecast, and nothing is invented - a value
that cannot be computed from the stored samples is ``None``.

The definitions, in the words the frontend uses:

* **sample**      one stored history row: a timestamp, a risk score and the readings stored with it
* **window**      a slice by age: the newest sample plus everything at most N minutes older
* **duration**    newest timestamp minus oldest timestamp inside the window
* **valid**       a row whose timestamp is usable and whose score is a finite number inside 0..1
* **net change**  last valid score minus first valid score
* **peak, minimum** the largest and smallest valid score in the window; on a tie the earlier row wins
* **trend**       the direction of the least-squares slope of score against elapsed hours,
                  described with an explicit tolerance. It is retrospective: what the stored
                  scores did, never what they will do. A window that rose and then eased is
                  still reported by its slope, and the peak and net change are reported beside
                  it rather than bent to agree.
* **gap**         an interval longer than ``max(3 x median interval, 900 s)``, which qualifies
                  how far the direction can be trusted

A row is dropped, never guessed at, when its timestamp is unusable or its score is not a finite
number inside 0..1. Rows that arrive out of order are sorted; when two rows share a timestamp
the later one wins.

Trend needs at least 3 valid samples spanning at least 600 s; inside that it is
``INSUFFICIENT`` and the trend is unknown. The band labels come from the engine's own
``risk.classify`` (the frozen ``heuristic-v1`` edges 0.35 / 0.60 / 0.80), never from a second
copy of those numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from .config import RISK_ORDER
from .risk import classify

#: The fixture's ``rules`` block, as constants. ``RULE_CONSTANTS`` maps the fixture's own key
#: spelling onto each one, and the M16 tests assert the fixture still agrees with this module.
TEMPORAL_MIN_SAMPLES = 3
TEMPORAL_MIN_DURATION_SECONDS = 600
TEMPORAL_SLOPE_TOLERANCE_PER_HOUR = 0.02
TEMPORAL_GAP_MEDIAN_MULTIPLIER = 3
TEMPORAL_GAP_FLOOR_SECONDS = 900

RULE_CONSTANTS: dict[str, float] = {
    "min_samples": TEMPORAL_MIN_SAMPLES,
    "min_duration_seconds": TEMPORAL_MIN_DURATION_SECONDS,
    "slope_tolerance_per_hour": TEMPORAL_SLOPE_TOLERANCE_PER_HOUR,
    "gap_median_multiplier": TEMPORAL_GAP_MEDIAN_MULTIPLIER,
    "gap_floor_seconds": TEMPORAL_GAP_FLOOR_SECONDS,
}

STATUS_OK = "OK"
STATUS_INSUFFICIENT = "INSUFFICIENT"

REASON_NO_SAMPLES = "no_samples"
REASON_TOO_FEW_SAMPLES = "too_few_samples"
REASON_TOO_SHORT = "too_short"
REASON_NO_ELAPSED_TIME = "no_elapsed_time"

TREND_RISING = "RISING"
TREND_STEADY = "STEADY"
TREND_FALLING = "FALLING"

#: What an incident record says when it does not name a severity. Never a guessed band.
SEVERITY_UNKNOWN = "UNKNOWN"

#: The one metric the temporal summary reports on (rainfall intensity), spelled as stored.
RAINFALL_METRIC = "rainfall_mm_per_hr"


def number(value: Any) -> float | None:
    """``value`` as a finite float, or ``None``.

    A numeric string is accepted (a JSON number arrives as text until it is converted);
    anything else is not a usable number, and neither is ``NaN``, ``Infinity`` or a boolean.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            candidate = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return candidate if math.isfinite(candidate) else None


def iso_epoch(value: Any) -> float | None:
    """Epoch seconds for an ISO-8601 timestamp string (``Z`` accepted), else ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True)
class Sample:
    """One valid stored row: a timestamp, a score and the readings known alongside it."""

    ts_epoch: float
    score: float
    rainfall: float | None = None
    risk_class: str | None = None


@dataclass
class Series:
    """The valid samples of a window, plus what had to be dropped to get them."""

    samples: list[Sample]
    invalid_dropped: int = 0
    duplicates_removed: int = 0
    out_of_order_pairs: int = 0


def _plain_number(value: Any) -> float | int | None:
    """A JSON-friendly number: whole values as ``int``, everything else as ``float``."""
    if value is None:
        return None
    as_float = float(value)
    return int(as_float) if as_float.is_integer() else as_float


def _sample_field(item: Any, name: str, position: int) -> Any:
    """A field of one row, from a mapping or from a positional ``(ts, score, rainfall)`` tuple."""
    if isinstance(item, Mapping):
        return item.get(name)
    if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
        return item[position] if len(item) > position else None
    return None


def stamp_of(item: Any) -> float | None:
    """A row's epoch seconds: ``ts_epoch``, else the ISO ``timestamp``, else a positional tuple."""
    if isinstance(item, Mapping):
        stamp = number(item.get("ts_epoch"))
        return stamp if stamp is not None else iso_epoch(item.get("timestamp"))
    return number(_sample_field(item, "ts_epoch", 0))


def score_of(item: Any) -> float | None:
    """A row's stored risk score, or ``None`` when it is not a usable number."""
    return number(_sample_field(item, "risk_score", 1))


def rainfall_of(item: Any) -> float | None:
    """A row's stored rainfall intensity, from the point itself or its nested ``readings``."""
    value = number(_sample_field(item, RAINFALL_METRIC, 2))
    if value is not None:
        return value
    if isinstance(item, Mapping):
        readings = item.get("readings")
        if isinstance(readings, Mapping):
            return number(readings.get(RAINFALL_METRIC))
    return None


def class_of(item: Any) -> str | None:
    """A row's stored band label, when the row carries one."""
    value = _sample_field(item, "risk_class", 3)
    return value.strip() if isinstance(value, str) and value.strip() else None


def sample_of(item: Any) -> Sample | None:
    """One row as a :class:`Sample`, or ``None`` when it is not a valid sample."""
    stamp = stamp_of(item)
    score = score_of(item)
    if stamp is None or score is None or not 0.0 <= score <= 1.0:
        return None
    return Sample(
        ts_epoch=stamp,
        score=score,
        rainfall=rainfall_of(item),
        risk_class=class_of(item),
    )


def clean_series(items: Iterable[Any]) -> Series:
    """Keep, order and de-duplicate a window's rows: the only place a row is dropped.

    ``invalid_dropped`` counts rows that are not valid samples, ``out_of_order_pairs`` counts
    pairs of kept rows that arrived in the wrong order (inversions in arrival order), and
    ``duplicates_removed`` counts rows replaced by a later row carrying the same timestamp.
    """
    kept: list[Sample] = []
    stamps: list[float] = []
    invalid = 0
    for item in items:
        sample = sample_of(item)
        if sample is None:
            invalid += 1
            continue
        stamps.append(sample.ts_epoch)
        kept.append(sample)

    inversions = 0
    for index, stamp in enumerate(stamps):
        inversions += sum(1 for other in stamps[index + 1 :] if stamp > other)

    kept.sort(key=lambda sample: sample.ts_epoch)  # stable: equal stamps keep arrival order
    series: list[Sample] = []
    duplicates = 0
    for sample in kept:
        if series and series[-1].ts_epoch == sample.ts_epoch:
            duplicates += 1
            series[-1] = sample  # the later row for an identical timestamp wins
            continue
        series.append(sample)
    return Series(
        samples=series,
        invalid_dropped=invalid,
        duplicates_removed=duplicates,
        out_of_order_pairs=inversions,
    )


def slope_per_hour(samples: Sequence[Sample]) -> float | None:
    """Least-squares slope of score against elapsed **hours**, or ``None`` without elapsed time."""
    count = len(samples)
    if count < 2:
        return None
    mean_ts = sum(sample.ts_epoch for sample in samples) / count
    mean_score = sum(sample.score for sample in samples) / count
    covariance = 0.0
    variance = 0.0
    for sample in samples:
        hours = (sample.ts_epoch - mean_ts) / 3600.0
        covariance += hours * (sample.score - mean_score)
        variance += hours * hours
    if variance <= 0.0:
        return None
    return covariance / variance


def band_for(score: float) -> str:
    """The band a score falls in, in the engine's own wording (one definition, in ``risk``)."""
    return classify(score)


@dataclass
class Summary:
    """What a window of stored samples says, with ``None`` wherever nothing can be computed."""

    count: int = 0
    status: str = STATUS_INSUFFICIENT
    reason: str | None = REASON_NO_SAMPLES
    trend: str | None = None
    slope_per_hour: float | None = None
    net_change: float | None = None
    first_score: float | None = None
    last_score: float | None = None
    peak: float | None = None
    min: float | None = None
    peak_ts: float | None = None
    min_ts: float | None = None
    first_ts: float | None = None
    last_ts: float | None = None
    duration_seconds: float | None = None
    median_interval: float | None = None
    largest_gap: float | None = None
    gap_limit: float | None = None
    gap_count: int = 0
    rainfall_samples: int = 0
    first_rainfall: float | None = None
    last_rainfall: float | None = None
    newest_rainfall: float | None = None
    min_rainfall: float | None = None
    max_rainfall: float | None = None
    newest_age_seconds: float | None = None
    classes: list[str] | None = None
    invalid_dropped: int = 0
    duplicates_removed: int = 0
    out_of_order_pairs: int = 0

    def as_dict(self) -> dict[str, Any]:
        """The summary as JSON-ready data: whole numbers as ``int``, unknown values as ``None``."""
        return {
            "count": self.count,
            "status": self.status,
            "reason": self.reason,
            "trend": self.trend,
            "slope_per_hour": _plain_number(self.slope_per_hour),
            "net_change": _plain_number(self.net_change),
            "first_score": _plain_number(self.first_score),
            "last_score": _plain_number(self.last_score),
            "peak": _plain_number(self.peak),
            "min": _plain_number(self.min),
            "peak_ts": _plain_number(self.peak_ts),
            "min_ts": _plain_number(self.min_ts),
            "first_ts": _plain_number(self.first_ts),
            "last_ts": _plain_number(self.last_ts),
            "duration_seconds": _plain_number(self.duration_seconds),
            "median_interval": _plain_number(self.median_interval),
            "largest_gap": _plain_number(self.largest_gap),
            "gap_limit": _plain_number(self.gap_limit),
            "gap_count": self.gap_count,
            "rainfall_samples": self.rainfall_samples,
            "first_rainfall": _plain_number(self.first_rainfall),
            "last_rainfall": _plain_number(self.last_rainfall),
            "newest_rainfall": _plain_number(self.newest_rainfall),
            "min_rainfall": _plain_number(self.min_rainfall),
            "max_rainfall": _plain_number(self.max_rainfall),
            "newest_age_seconds": _plain_number(self.newest_age_seconds),
            "classes": list(self.classes or []),
            "invalid_dropped": self.invalid_dropped,
            "duplicates_removed": self.duplicates_removed,
            "out_of_order_pairs": self.out_of_order_pairs,
        }


def summarise(series: Series, now_epoch: float | None = None) -> Summary:
    """Describe one cleaned window.

    ``now_epoch`` only ever sets ``newest_age_seconds``: the summary describes the samples it
    was given, never the wall clock, so a stale window is still summarised and simply reports
    how old its newest sample is.
    """
    summary = Summary(
        count=len(series.samples),
        invalid_dropped=series.invalid_dropped,
        duplicates_removed=series.duplicates_removed,
        out_of_order_pairs=series.out_of_order_pairs,
        classes=[],
    )
    samples = series.samples
    if not samples:
        return summary

    summary.status = STATUS_INSUFFICIENT
    summary.reason = REASON_TOO_FEW_SAMPLES
    summary.first_ts = samples[0].ts_epoch
    summary.last_ts = samples[-1].ts_epoch
    summary.duration_seconds = int(summary.last_ts - summary.first_ts) if summary.count > 1 else 0
    summary.first_score = samples[0].score
    summary.last_score = samples[-1].score
    summary.net_change = summary.last_score - summary.first_score
    summary.classes = [sample.risk_class or band_for(sample.score) for sample in samples]
    if now_epoch is not None:
        summary.newest_age_seconds = max(0.0, float(now_epoch) - summary.last_ts)

    peak_index = 0
    min_index = 0
    for index in range(1, summary.count):
        if samples[index].score > samples[peak_index].score:
            peak_index = index
        if samples[index].score < samples[min_index].score:
            min_index = index
    summary.peak = samples[peak_index].score
    summary.min = samples[min_index].score
    summary.peak_ts = samples[peak_index].ts_epoch
    summary.min_ts = samples[min_index].ts_epoch

    intervals = [
        int(samples[index].ts_epoch - samples[index - 1].ts_epoch)
        for index in range(1, summary.count)
        if samples[index].ts_epoch > samples[index - 1].ts_epoch
    ]
    if intervals:
        summary.median_interval = median(intervals)
        summary.largest_gap = max(intervals)
        summary.gap_limit = max(
            summary.median_interval * TEMPORAL_GAP_MEDIAN_MULTIPLIER, TEMPORAL_GAP_FLOOR_SECONDS
        )
        summary.gap_count = sum(1 for interval in intervals if interval > summary.gap_limit)

    rainfalls = [sample.rainfall for sample in samples if sample.rainfall is not None]
    summary.rainfall_samples = len(rainfalls)
    summary.newest_rainfall = samples[-1].rainfall
    if rainfalls:
        summary.first_rainfall = rainfalls[0]
        summary.last_rainfall = rainfalls[-1]
        summary.min_rainfall = min(rainfalls)
        summary.max_rainfall = max(rainfalls)

    if summary.count < TEMPORAL_MIN_SAMPLES:
        return summary
    if summary.duration_seconds < TEMPORAL_MIN_DURATION_SECONDS:
        summary.reason = REASON_TOO_SHORT
        return summary
    slope = slope_per_hour(samples)
    if slope is None:
        summary.reason = REASON_NO_ELAPSED_TIME
        return summary

    summary.slope_per_hour = slope
    summary.status = STATUS_OK
    summary.reason = None
    if slope > TEMPORAL_SLOPE_TOLERANCE_PER_HOUR:
        summary.trend = TREND_RISING
    elif slope < -TEMPORAL_SLOPE_TOLERANCE_PER_HOUR:
        summary.trend = TREND_FALLING
    else:
        summary.trend = TREND_STEADY
    return summary


def analyse(items: Iterable[Any], now_epoch: float | None = None) -> Summary:
    """Clean a window of rows and describe it (the one call the tool needs)."""
    return summarise(clean_series(items), now_epoch)


def severity_of(value: Any) -> str:
    """A recorded band label, or ``UNKNOWN`` when the record does not state a known one."""
    if isinstance(value, str) and value.strip() in RISK_ORDER:
        return value.strip()
    return SEVERITY_UNKNOWN


def incident_summary(record: Mapping[str, Any], now_epoch: float | None = None) -> dict[str, Any]:
    """What one stored incident record states - and nothing that it does not.

    The record owns its own openness, severity and duration, using the API's field names
    (``opened_ts_epoch``, ``closed_ts_epoch``, ``severity``, ``severity_at_open``, ``is_open``,
    ``current_status``, ``duration_seconds``). A record that omits them reports ``open: None``
    (unknown), severity ``UNKNOWN`` and ``duration_seconds: None``; nothing is inferred from
    ordinary zone history, and an escalation is only claimed when the record names both the
    class it opened with and a strictly higher one.
    """
    opened = number(record.get("opened_ts_epoch"))
    closed = number(record.get("closed_ts_epoch"))
    is_open = record.get("is_open")
    if not isinstance(is_open, bool):
        status = record.get("current_status")
        is_open = (status == "OPEN") if isinstance(status, str) and status.strip() else None
    severity = severity_of(record.get("severity"))
    at_open = severity_of(record.get("severity_at_open"))
    duration = number(record.get("duration_seconds"))
    if duration is None:
        if is_open and opened is not None and now_epoch is not None:
            duration = max(0.0, float(now_epoch) - opened)
        elif closed is not None and opened is not None:
            duration = max(0.0, closed - opened)
    escalated = (
        severity in RISK_ORDER
        and at_open in RISK_ORDER
        and RISK_ORDER[severity] > RISK_ORDER[at_open]
    )
    return {
        "open": is_open,
        "severity": severity,
        "severity_at_open": at_open,
        "escalated": escalated,
        "opened_ts_epoch": _plain_number(opened),
        "closed_ts_epoch": _plain_number(closed),
        "duration_seconds": _plain_number(duration),
    }
