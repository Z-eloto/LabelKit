from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).parents[1] / "examples" / "synth-stream"
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location(
    "export_stream_views_example",
    SCRIPT_DIR / "export_stream_views.py",
)
assert SPEC is not None and SPEC.loader is not None
export_stream_views = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = export_stream_views
SPEC.loader.exec_module(export_stream_views)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_poi_frame_splits_to_poi_today_accessor():
    split = export_stream_views.split_frame({
        "ts": "2026-01-01T08:00:00+08:00",
        "text": {
            "dataName": "poiEvent",
            "poiType": "COMPANY",
            "poiName": "研发中心",
            "longitude": 121.1,
            "latitude": 31.2,
            "startTime": 1_700_000_000_000,
            "endTime": 1_700_000_060_000,
            "duration": 60_000,
        },
        "truth": {"sequence": 0, "frame_class": "poi"},
    })

    assert split["event1"] == {
        "dataName": "poiEvent",
        "poiType": "COMPANY",
        "poiName": "研发中心",
        "longitude": 121.1,
        "latitude": 31.2,
        "state": "IN",
        "timestamp": 1_700_000_000_000,
    }
    assert split["event2"] == {
        "dataName": "poiEvent",
        "poiType": "COMPANY",
        "poiName": "研发中心",
        "longitude": 121.1,
        "latitude": 31.2,
        "state": "OUT",
        "timestamp": 1_700_000_060_000,
    }
    assert split["accessor"] == {
        "poiToday": [{
            "poiType": "COMPANY",
            "startTime": 1_700_000_000_000,
            "endTime": 1_700_000_060_000,
        }]
    }


def test_check_in_scalar_and_object_accessors_have_declared_shapes():
    commute = export_stream_views.split_frame({
        "text": {
            "dataName": "commutePeriodEvent",
            "timestamp": 1_700_000_000_000,
            "periodType": "ARRIVE_COMPANY",
        }
    })
    public_time = export_stream_views.split_frame({
        "text": {
            "dataName": "publicTimePeriodEvent",
            "timestamp": 1_700_000_000_000,
            "publicTimePeriodType": "FORENOON",
        }
    })
    memory = export_stream_views.split_frame({
        "truth": {"sequence_class": "check_in"},
        "text": {
            "dataName": "companionMemoryEvent",
            "createTime": 1_700_000_000_000,
            "updateTime": 1_700_000_000_100,
            "entityId": "entity-1",
            "pageId": "page-1",
            "caption": "钉钉考勤页",
            "bundleName": "com.dingtalk.hmos",
            "status": "Finish",
        }
    })

    assert commute["accessor"] == {"commutePeriod": "ARRIVE_COMPANY"}
    assert public_time["accessor"] == {
        "publicTimePeriod": {"publicTimePeriodType": "FORENOON"}
    }
    assert memory["accessor"] == {
        "companionMemoryInfo30min": [{
            "timestamp": 1_700_000_000_000,
            "entityId": "entity-1",
            "pageId": "page-1",
            "caption": "钉钉考勤页",
            "bundleName": "com.dingtalk.hmos",
            "status": "Finish",
        }]
    }
    assert export_stream_views._standard_commute_event(commute["event1"]) == {
        "eventType": "commutePeriodType",
        "eventTime": 1_700_000_000_000,
        "payload": {"commuteTransition": "ARRIVE_COMPANY"},
    }


def test_caffe_accessor_shape_is_not_changed_by_check_in_overrides():
    split = export_stream_views.split_frame({
        "truth": {"sequence_class": "coffee_order"},
        "text": {
            "dataName": "appUsageEvent",
            "timestamp": 1_700_000_000_000,
            "bundleName": "coffee.app",
            "state": "IN",
            "startTime": 1_700_000_000_000,
            "duration": 1_000,
        },
    })

    assert split["accessor"] == {
        "appUsage30min": {
            "bundleName": "coffee.app",
            "startTime": 1_700_000_000_000,
            "duration": 1_000,
        }
    }


def test_navigation_reuses_check_in_array_accessor_shapes():
    app_usage = export_stream_views.split_frame({
        "truth": {"sequence_class": "navigation"},
        "text": {
            "dataName": "appUsageEvent",
            "timestamp": 1_700_000_000_000,
            "bundleName": "com.amap.hmapp",
            "state": "IN",
            "startTime": 1_700_000_000_000,
            "duration": 60_000,
        },
    })
    companion = export_stream_views.split_frame({
        "truth": {"sequence_class": "navigation"},
        "text": {
            "dataName": "companionMemoryEvent",
            "createTime": 1_700_000_001_000,
            "updateTime": 1_700_000_001_100,
            "entityId": "entity-1",
            "pageId": "page-1",
            "caption": "高德地图路线规划",
            "bundleName": "com.amap.hmapp",
            "status": "Finish",
        },
    })
    screen = export_stream_views.split_frame({
        "truth": {"sequence_class": "navigation"},
        "text": {
            "dataName": "screenMemoryEvent",
            "createTime": 1_700_000_002_000,
            "updateTime": 1_700_000_002_100,
            "entityId": "entity-2",
            "pageId": "page-2",
            "caption": "高德地图导航中",
            "bundleName": "com.amap.hmapp",
            "status": "Finish",
        },
    })

    assert app_usage["accessor"]["appUsage30min"] == [{
        "bundleName": "com.amap.hmapp",
        "startTime": 1_700_000_000_000,
        "duration": 60_000,
    }]
    assert companion["accessor"]["companionMemoryInfo30min"][0]["timestamp"] == 1_700_000_001_000
    assert screen["accessor"]["screenMemoryInfo30min"][0]["timestamp"] == 1_700_000_002_000


def test_calibration_copies_intent_and_synchronizes_app_usage_timestamp(tmp_path):
    stream_path = tmp_path / "synth-caffe.stream.jsonl"
    intent_path = tmp_path / "synth-caffe.jsonl"
    output_dir = tmp_path / "exported"
    _write_jsonl(stream_path, [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "text": {
                "dataName": "notificationEvent",
                "messageTime": 1_700_000_000_000,
                "timestamp": 1_700_000_000_000,
                "content": "通知",
                "appName": "咖啡",
                "bundleName": "coffee.app",
            },
            "truth": {"sequence": 0, "frame_class": "notification"},
        },
        {
            "ts": "2026-01-01T00:00:10+00:00",
            "text": {
                "dataName": "appUsageEvent",
                "timestamp": 123,
                "bundleName": "coffee.app",
                "state": "IN",
                "startTime": 123,
                "duration": 1_000,
            },
            "truth": {"sequence": 0, "frame_class": "app_usage"},
        },
    ])
    original_timestamp = 999
    _write_jsonl(intent_path, [{
        "behaviorType": "coffee_ordering",
        "actionInfo": {"timestamp": original_timestamp},
        "_meta": {
            "stream": {
                "member_sources": [
                    {"file": "out\\synth-caffe.stream.jsonl", "line_no": 1},
                    {"file": "out\\synth-caffe.stream.jsonl", "line_no": 2},
                ]
            }
        },
    }])

    result = export_stream_views.export_stream_views(
        stream_path,
        output_dir,
        calibrate_timestamps=True,
        calibration_gap_ms=100,
    )

    copied_rows = [json.loads(line) for line in result["intent_output"].read_text(
        encoding="utf-8"
    ).splitlines()]
    assert result["intent_input"] == intent_path
    assert result["intent_output"] == output_dir / "synth-caffe.jsonl"
    assert result["intent_updated_count"] == 1
    assert result["calibrated_stream_output"] == (
        output_dir / "synth-caffe.stream.jsonl"
    )
    assert copied_rows[0]["actionInfo"]["timestamp"] == 1_700_000_000_100
    assert json.loads(intent_path.read_text(encoding="utf-8"))["actionInfo"][
        "timestamp"
    ] == original_timestamp

    calibrated_stream = [
        json.loads(line)
        for line in result["calibrated_stream_output"].read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert calibrated_stream[0]["ts"] == "2023-11-14T22:13:20.000000+00:00"
    assert calibrated_stream[0]["text"]["messageTime"] == 1_700_000_000_000
    assert calibrated_stream[0]["text"]["timestamp"] == 1_700_000_000_000
    assert calibrated_stream[1]["ts"] == "2023-11-14T22:13:20.100000+00:00"
    assert calibrated_stream[1]["text"]["startTime"] == 1_700_000_000_100
    assert calibrated_stream[1]["text"]["timestamp"] == 1_700_000_000_100
    original_stream = [
        json.loads(line) for line in stream_path.read_text(encoding="utf-8").splitlines()
    ]
    assert original_stream[0]["ts"] == "2026-01-01T00:00:00+00:00"
    assert original_stream[1]["text"]["timestamp"] == 123


def test_intent_sync_uses_earliest_calibrated_member(tmp_path):
    source = tmp_path / "intent.jsonl"
    destination = tmp_path / "copied" / "intent.jsonl"
    _write_jsonl(source, [{
        "actionInfo": {"timestamp": 1},
        "_meta": {
            "stream": {
                "member_sources": [
                    {"file": "out/stream.jsonl", "line_no": 8},
                    {"file": "out/stream.jsonl", "line_no": 3},
                ]
            }
        },
    }])

    updated = export_stream_views.synchronize_intent_timestamps(
        source,
        destination,
        tmp_path / "stream.jsonl",
        {3: 3_000, 8: 8_000},
    )

    assert updated == 1
    assert json.loads(destination.read_text(encoding="utf-8"))[
        "actionInfo"
    ]["timestamp"] == 3_000


def test_intent_sync_rejects_rows_without_calibrated_app_usage(tmp_path):
    source = tmp_path / "intent.jsonl"
    destination = tmp_path / "copied.jsonl"
    _write_jsonl(source, [{
        "actionInfo": {"timestamp": 1},
        "_meta": {
            "stream": {
                "member_sources": [
                    {"file": "out/stream.jsonl", "line_no": 1},
                ]
            }
        },
    }])

    with pytest.raises(ValueError, match="no calibrated app_usage"):
        export_stream_views.synchronize_intent_timestamps(
            source,
            destination,
            tmp_path / "stream.jsonl",
            {},
        )
    assert not destination.exists()


def test_intent_without_timestamp_is_copied_without_new_slot(tmp_path):
    source = tmp_path / "intent.jsonl"
    destination = tmp_path / "copied.jsonl"
    row = {
        "behaviorType": "check_in",
        "actionInfo": {
            "checkInType": "start",
            "checkInPoiType": "COMPANY",
        },
    }
    _write_jsonl(source, [row])

    updated = export_stream_views.synchronize_intent_timestamps(
        source,
        destination,
        tmp_path / "stream.jsonl",
        {},
    )

    assert updated == 0
    assert json.loads(destination.read_text(encoding="utf-8")) == row
