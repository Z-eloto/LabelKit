from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).parents[1] / "examples" / "synth-stream"
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location(
    "build_all_day_stream_example",
    SCRIPT_DIR / "build_all_day_stream.py",
)
assert SPEC is not None and SPEC.loader is not None
build_all_day_stream = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_all_day_stream
SPEC.loader.exec_module(build_all_day_stream)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _intent(stream_name: str, behavior: str, member_lines: list[int]) -> dict:
    return {
        "behaviorType": behavior,
        "actionInfo": {"timestamp": 123},
        "_meta": {
            "source": {"file": stream_name, "line_no": member_lines[0]},
            "stream": {
                "member_sources": [
                    {"file": stream_name, "line_no": line_number}
                    for line_number in member_lines
                ],
                "order_span": [
                    f"{stream_name}:{member_lines[0]}",
                    f"{stream_name}:{member_lines[-1]}",
                ],
            },
        },
    }


def test_build_all_day_retimes_synchronizes_and_exports_views(tmp_path):
    check_stream = tmp_path / "check.stream.jsonl"
    check_intent = tmp_path / "check.jsonl"
    food_stream = tmp_path / "food.stream.jsonl"
    food_intent = tmp_path / "food.jsonl"
    manifest = tmp_path / "all-day.toml"
    output_dir = tmp_path / "built"

    _write_jsonl(check_stream, [
        {
            "ts": "2024-01-01T00:00:00+08:00",
            "text": {
                "dataName": "publicWorkDayEvent",
                "timestamp": 1_700_000_000_000,
                "publicWorkDayType": "PUBLIC_WORKDAY",
            },
            "truth": {
                "session": 0,
                "sequence_class": "check_in",
                "sequence": 0,
                "frame_class": "public_workday",
            },
        },
        {
            "ts": "2024-01-01T00:00:02+08:00",
            "text": {
                "dataName": "publicTimePeriodEvent",
                "timestamp": 1_700_000_002_000,
                "publicTimePeriodType": "NIGHT",
            },
            "truth": {
                "session": 0,
                "sequence_class": "check_in",
                "sequence": 0,
                "frame_class": "public_time_period",
            },
        },
        {
            "ts": "2024-01-01T00:00:04+08:00",
            "text": {
                "dataName": "appUsageEvent",
                "timestamp": 1_700_000_004_000,
                "startTime": 1_700_000_004_000,
                "bundleName": "com.dingtalk.hmos",
                "duration": 2_000,
            },
            "truth": {
                "session": 0,
                "sequence_class": "check_in",
                "sequence": 0,
                "frame_class": "app_usage",
            },
        },
    ])
    _write_jsonl(check_intent, [_intent(check_stream.name, "check_in", [1, 2, 3])])
    _write_jsonl(food_stream, [{
        "ts": "2024-01-01T00:00:00+08:00",
        "text": {
            "dataName": "appUsageEvent",
            "timestamp": 1_700_100_000_000,
            "startTime": 1_700_100_000_000,
            "bundleName": "com.meituan.takeaway",
            "duration": 5_000,
        },
        "truth": {
            "session": 0,
            "sequence_class": "food_order",
            "sequence": 0,
            "frame_class": "app_usage",
        },
    }])
    _write_jsonl(food_intent, [_intent(food_stream.name, "food_ordering", [1])])
    manifest.write_text(
        """
date = "2026-01-10"
timezone = "+08:00"
frame_gap_ms = 1000
output_stream = "day.stream.jsonl"
output_intent = "day.jsonl"
export_views = true

[[scenes]]
name = "check_in"
stream = "check.stream.jsonl"
intent = "check.jsonl"
windows = ["07:00-07:30"]

[[scenes]]
name = "food_ordering"
stream = "food.stream.jsonl"
intent = "food.jsonl"
windows = ["12:00-12:30"]
""".strip(),
        encoding="utf-8",
    )

    report = build_all_day_stream.build_all_day_stream(manifest, output_dir)

    stream_rows = [
        json.loads(line)
        for line in (output_dir / "day.stream.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    outer_times = [
        round(build_all_day_stream._parse_time(row["ts"]) * 1000)
        for row in stream_rows
    ]
    assert outer_times == sorted(outer_times)
    assert all(right > left for left, right in zip(outer_times, outer_times[1:]))
    assert stream_rows[0]["text"] == {
        "dataName": "publicWorkDayEvent",
        "timestamp": outer_times[0],
        "publicWorkDayType": "PUBLIC_HOLIDAY",
    }
    assert stream_rows[1]["text"] == {
        "dataName": "publicTimePeriodEvent",
        "timestamp": outer_times[1],
        "publicTimePeriodType": "MORNING",
    }
    assert stream_rows[2]["text"]["timestamp"] == outer_times[2]
    assert stream_rows[2]["text"]["startTime"] == outer_times[2]
    assert stream_rows[3]["text"]["timestamp"] == outer_times[3]
    assert stream_rows[3]["text"]["startTime"] == outer_times[3]

    intent_rows = [
        json.loads(line)
        for line in (output_dir / "day.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [row["behaviorType"] for row in intent_rows] == [
        "check_in",
        "food_ordering",
    ]
    assert intent_rows[0]["actionInfo"]["timestamp"] == outer_times[2]
    assert intent_rows[1]["actionInfo"]["timestamp"] == outer_times[3]
    assert intent_rows[0]["_meta"]["stream"]["member_sources"] == [
        {"file": "day.stream.jsonl", "line_no": 1},
        {"file": "day.stream.jsonl", "line_no": 2},
        {"file": "day.stream.jsonl", "line_no": 3},
    ]

    public_workday_csv = output_dir / "views" / "accessors" / "publicWorkDay.csv"
    with public_workday_csv.open(encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        rows = list(reader)
    assert reader.fieldnames == ["timestamp", "publicWorkDayType"]
    assert rows == [{
        "timestamp": str(outer_times[0]),
        "publicWorkDayType": "PUBLIC_HOLIDAY",
    }]
    assert report["frame_count"] == 4
    assert report["intent_count"] == 2


def test_makeup_workday_takes_precedence_over_public_holiday(tmp_path):
    stream = tmp_path / "source.stream.jsonl"
    manifest = tmp_path / "manifest.toml"
    _write_jsonl(stream, [{
        "ts": "2024-01-01T00:00:00+08:00",
        "text": {
            "dataName": "publicWorkDayEvent",
            "timestamp": 1,
            "publicWorkDayType": "PUBLIC_HOLIDAY",
        },
        "truth": {"sequence_class": "x", "sequence": 0, "session": 0},
    }])
    manifest.write_text(
        """
date = "2026-01-10"
timezone = "+08:00"
public_holidays = ["2026-01-10"]
makeup_workdays = ["2026-01-10"]

[[scenes]]
name = "x"
stream = "source.stream.jsonl"
windows = ["09:00-10:00"]
""".strip(),
        encoding="utf-8",
    )

    build_all_day_stream.build_all_day_stream(manifest, tmp_path / "output")
    row = json.loads((tmp_path / "output" / "all-day.stream.jsonl").read_text(
        encoding="utf-8"
    ))
    assert row["text"]["publicWorkDayType"] == "PUBLIC_WORKDAY"
