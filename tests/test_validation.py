"""Unit tests for CSV parsing and batch validation."""
from __future__ import annotations

import pytest

from app.validation import CsvValidationError, parse_and_validate

from conftest import full_csv, make_csv


def test_valid_full_batch_passes():
    batch = parse_and_validate(full_csv())
    assert batch.line_count == 24
    assert [h for h, _ in batch.rows] == list(range(24))
    assert len(batch.checksum) == 64


def test_chinese_header_accepted():
    raw = make_csv([(h, 1.5) for h in range(24)], header="小时,浓度")
    batch = parse_and_validate(raw)
    assert batch.line_count == 24


def test_empty_file_rejected():
    with pytest.raises(CsvValidationError) as exc:
        parse_and_validate(b"")
    assert any("empty" in r["reason"] for r in exc.value.reasons)


def test_bad_header_rejected():
    with pytest.raises(CsvValidationError) as exc:
        parse_and_validate(b"hour,value\n1,2\n")
    assert any("header" in r["reason"] for r in exc.value.reasons)


def test_missing_hours_listed_individually():
    rows = [(h, 3.2) for h in range(24) if h not in (5, 17)]
    with pytest.raises(CsvValidationError) as exc:
        parse_and_validate(make_csv(rows))
    missing = [r["hour"] for r in exc.value.reasons if r["reason"] == "missing hour"]
    assert missing == [5, 17]


def test_duplicate_hour_reported_with_both_lines():
    rows = [(h, 1.0) for h in range(23)] + [(0, 9.9)]  # hour 0 twice, hour 23 missing
    with pytest.raises(CsvValidationError) as exc:
        parse_and_validate(make_csv(rows))
    dups = [r for r in exc.value.reasons if r["reason"] == "duplicate hour"]
    assert len(dups) == 1
    assert dups[0]["hour"] == 0
    assert dups[0]["line"] == 25 and dups[0]["first_line"] == 2
    assert any(r["reason"] == "missing hour" and r["hour"] == 23 for r in exc.value.reasons)


def test_hour_out_of_range_and_non_integer():
    rows = [(h, 1.0) for h in range(22)] + [(24, 1.0), (23, 1.0)]
    with pytest.raises(CsvValidationError) as exc:
        parse_and_validate(make_csv(rows))
    assert any("range" in r["reason"] and r.get("hour") == 24 for r in exc.value.reasons)

    rows2 = [(h, 1.0) for h in range(23)] + [("8.0", 1.0)]
    with pytest.raises(CsvValidationError) as exc2:
        parse_and_validate(make_csv(rows2))
    assert any("not an integer" in r["reason"] for r in exc2.value.reasons)


@pytest.mark.parametrize("bad", ["-1", "nan", "inf", "-inf", "abc", ""])
def test_invalid_concentrations_rejected(bad):
    rows = [(h, 1.0) for h in range(23)] + [(23, bad)]
    with pytest.raises(CsvValidationError) as exc:
        parse_and_validate(make_csv(rows))
    assert exc.value.reasons  # at least one concrete reason


def test_zero_and_large_finite_concentration_allowed():
    values = {h: 0.0 for h in range(24)}
    values[10] = 1e9
    batch = parse_and_validate(full_csv(values))
    assert batch.rows[10] == (10, 1e9)
    assert batch.rows[0] == (0, 0.0)


def test_wrong_column_count_reports_line():
    text = "hour,pm25\n" + "\n".join(
        f"{h},1.0" if h != 7 else "7,1.0,extra" for h in range(24)
    )
    with pytest.raises(CsvValidationError) as exc:
        parse_and_validate(text.encode())
    assert any(r.get("line") == 9 and "columns" in r["reason"] for r in exc.value.reasons)


def test_blank_lines_are_ignored_but_data_required():
    raw = b"hour,pm25\n\n\n"
    with pytest.raises(CsvValidationError) as exc:
        parse_and_validate(raw)
    assert any("no data" in r["reason"] or "missing hour" in r["reason"]
               for r in exc.value.reasons)


def test_utf8_bom_tolerated():
    raw = b"\xef\xbb\xbf" + full_csv()
    batch = parse_and_validate(raw)
    assert batch.line_count == 24
