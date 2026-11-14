"""CSV parsing and batch validation.

A batch is only valid when *all* of the following hold:

* the header is exactly ``hour,pm25`` (a few common aliases are accepted);
* every row has exactly two fields;
* every hour is an integer in 0..23, every concentration is a non-negative
  finite number;
* hours 0..23 are all present exactly once (no duplicates, no gaps);
* the file is not empty apart from the header.

Any failure produces a structured list of reasons; the caller must never
publish a batch that contains an error.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from typing import Iterable

HOUR_ALIASES = {"hour", "小时", "时"}
PM25_ALIASES = {"pm25", "pm2.5", "concentration", "浓度", "pm2_5"}
REQUIRED_HOURS = set(range(24))


class CsvValidationError(Exception):
    """Raised when the uploaded CSV cannot be parsed or validated."""

    def __init__(self, reasons: list[dict[str, object]]):
        super().__init__("CSV validation failed")
        self.reasons = reasons


@dataclass
class ParsedBatch:
    """A fully validated daily batch."""

    rows: list[tuple[int, float]]  # (hour, concentration), sorted by hour
    raw_bytes: bytes
    checksum: str
    line_count: int  # number of data lines (always 24 for a valid batch)


@dataclass
class _Collector:
    reasons: list[dict[str, object]] = field(default_factory=list)

    def add(self, reason: str, line: int | None = None, **extra: object) -> None:
        entry: dict[str, object] = {"reason": reason}
        if line is not None:
            entry["line"] = line
        entry.update(extra)
        self.reasons.append(entry)


def _reject(reasons: Iterable[dict[str, object]]) -> None:
    raise CsvValidationError(list(reasons))


def parse_and_validate(raw: bytes) -> ParsedBatch:
    """Parse raw CSV bytes and validate the whole batch.

    Raises :class:`CsvValidationError` with a reason list on any problem.
    """
    collector = _Collector()

    # Decode tolerantly: utf-8-sig strips a possible BOM; replace undecodable
    # bytes rather than crashing, then report structural problems precisely.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        collector.add("file is not valid UTF-8 text")

    if not text.strip():
        collector.add("uploaded file is empty")
        _reject(collector.reasons)

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        collector.add("missing header row, expected 'hour,pm25'")
        _reject(collector.reasons)

    normalized_header = [h.strip().lower() for h in header]
    if len(normalized_header) != 2 or normalized_header[0] not in HOUR_ALIASES \
            or normalized_header[1] not in PM25_ALIASES:
        collector.add(
            "invalid header; expected two columns 'hour,pm25'",
            header=header,
        )
        _reject(collector.reasons)

    seen_hours: dict[int, int] = {}
    parsed_rows: list[tuple[int, float]] = []
    data_line = 1  # header is line 1; first data line is line 2

    for row in reader:
        data_line += 1

        # Skip completely blank lines but flag lines with stray whitespace
        # fields rather than silently dropping data.
        if len(row) == 0 or (len(row) == 1 and row[0].strip() == ""):
            continue

        if len(row) != 2:
            collector.add(
                "expected exactly 2 columns (hour, pm25)",
                line=data_line,
                row=row,
            )
            continue

        hour_text, pm_text = row[0].strip(), row[1].strip()

        hour = _parse_hour(hour_text, data_line, collector)
        concentration = _parse_concentration(pm_text, data_line, collector)
        if hour is None or concentration is None:
            continue

        if hour in seen_hours:
            collector.add(
                "duplicate hour",
                line=data_line,
                hour=hour,
                first_line=seen_hours[hour],
            )
            continue

        seen_hours[hour] = data_line
        parsed_rows.append((hour, concentration))

    if not parsed_rows and not collector.reasons:
        collector.add("no data rows found")

    missing = sorted(REQUIRED_HOURS - set(seen_hours))
    if missing:
        # Report each missing hour individually so operators know exactly
        # which slots must be fixed.
        for hour in missing:
            collector.add("missing hour", hour=hour)

    if collector.reasons:
        _reject(collector.reasons)

    parsed_rows.sort(key=lambda item: item[0])

    import hashlib

    return ParsedBatch(
        rows=parsed_rows,
        raw_bytes=raw,
        checksum=hashlib.sha256(raw).hexdigest(),
        line_count=len(parsed_rows),
    )


def _parse_hour(text: str, line: int, collector: _Collector) -> int | None:
    if text == "":
        collector.add("empty hour value", line=line)
        return None
    try:
        # int() rejects floats like "8.0"; require whole-number syntax.
        hour = int(text)
    except ValueError:
        collector.add("hour is not an integer", line=line, value=text)
        return None
    if not 0 <= hour <= 23:
        collector.add(
            "hour out of range (must be 0..23)", line=line, hour=hour
        )
        return None
    return hour


def _parse_concentration(text: str, line: int, collector: _Collector) -> float | None:
    if text == "":
        collector.add("empty pm25 value", line=line)
        return None
    try:
        value = float(text)
    except ValueError:
        collector.add("pm25 concentration is not a number", line=line, value=text)
        return None
    if math.isnan(value) or math.isinf(value):
        collector.add(
            "pm25 concentration must be a finite number", line=line, value=text
        )
        return None
    if value < 0:
        collector.add(
            "pm25 concentration must be non-negative", line=line, value=value
        )
        return None
    return value
