"""Export time-ordered event and accessor views from a generated stream JSONL.

The frame splitting contract is shared with ``split.py``.  This script writes:

* ``all_event.json``: every non-empty event, ordered by occurrence time;
* ``all_standard_event.json``: poiEvent, commutePeriodEvent and
  pasteBoardEvent filtered from the ordered event list;
* ``all_accessor.json``: every non-empty accessor, ordered by occurrence time;
* ``accessors/<accessor_type>.csv``: one flattened table per accessor type.

Run, for example::

    uv run python export_stream_views.py out/synth-caffe3.stream.jsonl \
        --output-dir out/exported

Add ``--calibrate-timestamps`` to rebuild payload timestamps before splitting.
By default the calibration gap follows the outer stream ``ts`` intervals; pass
``--calibration-gap-ms`` to use one fixed positive gap instead.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from split import FRAME_SPLIT_RULES, split_frame


STANDARD_EVENT_TYPES = {
    "poievent",
    "commuteperiodevent",
    "pasteboardevent",
}

COMMUTE_TRANSITIONS = {
    "LEAVE_COMPANY",
    "ARRIVE_HOME",
    "LEAVE_HOME",
    "ARRIVE_COMPANY",
}

# The first present field is treated as the occurrence time.  The list covers
# the current split rules and gives common future event/accessor types useful
# defaults without altering their payloads.
TIME_FIELD_CANDIDATES = (
    "timestamp",
    "messageTime",
    "createTime",
    "startTime",
    "eventTime",
    "time",
    "endTime",
    "updateTime",
)

PRIMARY_TIME_FIELDS = {
    "notificationEvent": "messageTime",
    "appUsageEvent": "startTime",
    "companionMemoryEvent": "createTime",
    "screenMemoryEvent": "createTime",
    "pasteBoardEvent": "timestamp",
    "poiEvent": "timestamp",
    "commutePeriodEvent": "startTime",
}

MINIMUM_DURATION_MS = 1000
FALLBACK_GAP_MS = 1000

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class _TimedValue:
    value: dict[str, Any]
    sort_time: float
    input_order: tuple[int, int]


@dataclass
class TimestampCalibrator:
    """Assign a monotonic occurrence time to each frame payload.

    ``cursor_end_ms`` always represents the previous frame's end.  A new frame
    starts at ``cursor_end_ms + gap``.  For duration-bearing frames the cursor
    then advances to ``start + duration``; otherwise it remains at ``start``.
    """

    fixed_gap_ms: int | None = None
    minimum_duration_ms: int = MINIMUM_DURATION_MS
    fallback_gap_ms: int = FALLBACK_GAP_MS
    cursor_end_ms: int | None = None
    previous_stream_time: float | None = None

    def calibrate(self, frame: dict[str, Any], line_number: int) -> None:
        payload = frame.get("text")
        if not isinstance(payload, dict):
            raise ValueError(f"line {line_number}: field 'text' must be an object")

        original_start_ms = _frame_start_ms(payload)
        stream_time = _parse_time(frame.get("ts"))
        if self.cursor_end_ms is None:
            if original_start_ms is not None:
                assigned_start_ms = original_start_ms
            elif stream_time is not None:
                assigned_start_ms = round(stream_time * 1000)
            else:
                raise ValueError(
                    f"line {line_number}: cannot find a timestamp for calibration start"
                )
        else:
            gap_ms = self._next_gap_ms(stream_time)
            assigned_start_ms = self.cursor_end_ms + gap_ms

        duration_ms = self._calibrate_duration(payload, line_number)
        _assign_payload_start(payload, assigned_start_ms, original_start_ms, duration_ms)
        self.cursor_end_ms = assigned_start_ms + (duration_ms or 0)
        self.previous_stream_time = stream_time

    def _next_gap_ms(self, stream_time: float | None) -> int:
        if self.fixed_gap_ms is not None:
            return self.fixed_gap_ms
        if stream_time is not None and self.previous_stream_time is not None:
            observed_gap = round((stream_time - self.previous_stream_time) * 1000)
            if observed_gap > 0:
                return observed_gap
        return self.fallback_gap_ms

    def _calibrate_duration(
        self,
        payload: dict[str, Any],
        line_number: int,
    ) -> int | None:
        if "duration" not in payload:
            return None
        duration = payload["duration"]
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
        ):
            raise ValueError(
                f"line {line_number}: duration must be a finite number"
            )
        duration_ms = max(round(float(duration)), self.minimum_duration_ms)
        payload["duration"] = duration_ms
        return duration_ms


def _parse_time(value: Any) -> float | None:
    """Convert common epoch or ISO-8601 values to epoch seconds."""
    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        magnitude = abs(number)
        if magnitude >= 1e17:       # nanoseconds
            return number / 1e9
        if magnitude >= 1e14:       # microseconds
            return number / 1e6
        if magnitude >= 1e11:       # milliseconds
            return number / 1e3
        return number               # seconds

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _parse_time(float(text))
        except ValueError:
            pass
        try:
            normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return None

    return None


def _payload_time(payload: Mapping[str, Any], fallback: float | None) -> float:
    for field in TIME_FIELD_CANDIDATES:
        parsed = _parse_time(payload.get(field))
        if parsed is not None:
            return parsed
    return fallback if fallback is not None else math.inf


def _frame_start_ms(payload: Mapping[str, Any]) -> int | None:
    data_name = str(payload.get("dataName", ""))
    primary_field = PRIMARY_TIME_FIELDS.get(data_name)
    fields = (
        (primary_field,) + TIME_FIELD_CANDIDATES
        if primary_field is not None
        else TIME_FIELD_CANDIDATES
    )
    for field in fields:
        parsed = _parse_time(payload.get(field))
        if parsed is not None:
            return round(parsed * 1000)
    return None


def _assign_payload_start(
    payload: dict[str, Any],
    start_ms: int,
    original_start_ms: int | None,
    duration_ms: int | None,
) -> None:
    """Update the occurrence fields while preserving meaningful local offsets."""
    data_name = str(payload.get("dataName", ""))

    if data_name == "notificationEvent":
        payload["messageTime"] = start_ms
        if "timestamp" in payload:
            payload["timestamp"] = start_ms
        return

    if data_name == "appUsageEvent":
        payload["startTime"] = start_ms
        if "timestamp" in payload:
            payload["timestamp"] = start_ms
        return

    if data_name in {"companionMemoryEvent", "screenMemoryEvent"}:
        update_time = _parse_time(payload.get("updateTime"))
        update_offset_ms = 0
        if update_time is not None and original_start_ms is not None:
            update_offset_ms = max(0, round(update_time * 1000) - original_start_ms)
        payload["createTime"] = start_ms
        if "updateTime" in payload:
            payload["updateTime"] = start_ms + update_offset_ms
        return

    if data_name == "pasteBoardEvent":
        payload["timestamp"] = start_ms
        return

    primary_field = PRIMARY_TIME_FIELDS.get(data_name)
    if primary_field is None or primary_field not in payload:
        primary_field = next(
            (field for field in TIME_FIELD_CANDIDATES if field in payload),
            None,
        )
    if primary_field is not None:
        payload[primary_field] = start_ms
    if duration_ms is not None and "endTime" in payload:
        payload["endTime"] = start_ms + duration_ms


def _accessor_payloads(accessor: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Yield payload objects contained in one accessor wrapper."""
    for payload in accessor.values():
        if isinstance(payload, Mapping):
            yield payload
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, Mapping):
                    yield item


def _accessor_time(accessor: Mapping[str, Any], fallback: float | None) -> float:
    times = [
        _payload_time(payload, fallback)
        for payload in _accessor_payloads(accessor)
    ]
    return min(times) if times else (fallback if fallback is not None else math.inf)


def collect_stream_views(
    input_path: str | Path,
    calibrate_timestamps: bool = False,
    calibration_gap_ms: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split every stream frame and return ordered events and accessors."""
    timed_events: list[_TimedValue] = []
    timed_accessors: list[_TimedValue] = []
    input_file = Path(input_path)
    calibrator = (
        TimestampCalibrator(fixed_gap_ms=calibration_gap_ms)
        if calibrate_timestamps
        else None
    )

    with input_file.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{input_file}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(frame, dict):
                raise ValueError(
                    f"{input_file}:{line_number}: each JSONL row must be an object"
                )
            if not isinstance(frame.get("text"), dict):
                raise ValueError(
                    f"{input_file}:{line_number}: field 'text' must be an object"
                )

            if calibrator is not None:
                calibrator.calibrate(frame, line_number)

            split = split_frame(frame, FRAME_SPLIT_RULES)
            stream_time = _parse_time(frame.get("ts"))

            emitted_event = False
            for event_position, event_key in enumerate(("event1", "event2")):
                event = split.get(event_key)
                if isinstance(event, dict) and event:
                    emitted_event = True
                    timed_events.append(_TimedValue(
                        value=event,
                        sort_time=_payload_time(event, stream_time),
                        input_order=(line_number, event_position),
                    ))

            # ``split.py`` deliberately returns ``raw`` for frame types without
            # a declared split rule.  Preserve such frames as events instead of
            # silently dropping future types such as poiEvent and
            # commutePeriodEvent.  Their standard projections below select only
            # the required fields.
            raw_event = split.get("raw")
            if not emitted_event and isinstance(raw_event, dict) and raw_event:
                timed_events.append(_TimedValue(
                    value=raw_event,
                    sort_time=_payload_time(raw_event, stream_time),
                    input_order=(line_number, 0),
                ))

            accessor = split.get("accessor")
            if isinstance(accessor, dict) and accessor:
                timed_accessors.append(_TimedValue(
                    value=accessor,
                    sort_time=_accessor_time(accessor, stream_time),
                    input_order=(line_number, 0),
                ))

    timed_events.sort(key=lambda item: (item.sort_time, item.input_order))
    timed_accessors.sort(key=lambda item: (item.sort_time, item.input_order))
    return (
        [item.value for item in timed_events],
        [item.value for item in timed_accessors],
    )


def _flatten_object(
    value: Mapping[str, Any],
    prefix: str = "",
) -> dict[str, Any]:
    """Flatten nested objects with dotted column names for CSV output."""
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        column = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping):
            flattened.update(_flatten_object(item, column))
        elif isinstance(item, (list, tuple)):
            flattened[column] = json.dumps(item, ensure_ascii=False)
        elif item is None:
            flattened[column] = ""
        else:
            flattened[column] = item
    return flattened


def _group_accessor_rows(
    accessors: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for accessor in accessors:
        for accessor_type, payload in accessor.items():
            type_name = str(accessor_type)
            if isinstance(payload, Mapping):
                grouped.setdefault(type_name, []).append(_flatten_object(payload))
            elif isinstance(payload, list):
                for item in payload:
                    if isinstance(item, Mapping):
                        grouped.setdefault(type_name, []).append(_flatten_object(item))
                    else:
                        grouped.setdefault(type_name, []).append({"value": item})
            else:
                grouped.setdefault(type_name, []).append({"value": payload})
    return grouped


def _safe_csv_name(accessor_type: str, used_names: set[str]) -> str:
    stem = _INVALID_FILENAME_CHARS.sub("_", accessor_type).strip(" .") or "accessor"
    candidate = f"{stem}.csv"
    suffix = 2
    while candidate.casefold() in used_names:
        candidate = f"{stem}_{suffix}.csv"
        suffix += 1
    used_names.add(candidate.casefold())
    return candidate


def write_accessor_csvs(
    accessors: Sequence[Mapping[str, Any]],
    csv_dir: str | Path,
) -> dict[str, Path]:
    """Write one time-ordered CSV per accessor type."""
    output_dir = Path(csv_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    grouped = _group_accessor_rows(accessors)
    written: dict[str, Path] = {}
    used_names: set[str] = set()

    for accessor_type, rows in grouped.items():
        columns: list[str] = []
        seen_columns: set[str] = set()
        for row in rows:
            for column in row:
                if column not in seen_columns:
                    seen_columns.add(column)
                    columns.append(column)

        csv_path = output_dir / _safe_csv_name(accessor_type, used_names)
        with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        written[accessor_type] = csv_path

    return written


def _required_event_time_ms(
    event: Mapping[str, Any],
    fields: Sequence[str] = TIME_FIELD_CANDIDATES,
) -> int:
    for field in fields:
        parsed = _parse_time(event.get(field))
        if parsed is not None:
            return round(parsed * 1000)
    data_name = event.get("dataName", "unknown")
    raise ValueError(f"{data_name}: cannot find a valid event timestamp")


def _standard_poi_events(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    poi_type = event.get("poiType")
    if not isinstance(poi_type, str) or not poi_type:
        raise ValueError("poiEvent: poiType must be a non-empty string")

    state_value = event.get("state")
    state = state_value.upper() if isinstance(state_value, str) else None
    if state in {"IN", "OUT"}:
        return [{
            "eventType": "poiType",
            "eventTime": _required_event_time_ms(event),
            "payload": {
                "poiType": poi_type,
                "state": state,
            },
        }]

    start_ms = _required_event_time_ms(
        event,
        ("startTime", "timestamp", "eventTime", "enterTime", "arrivalTime", "createTime"),
    )
    end_ms: int | None = None
    for field in ("endTime", "exitTime", "leaveTime", "departureTime"):
        parsed = _parse_time(event.get(field))
        if parsed is not None:
            end_ms = round(parsed * 1000)
            break

    if end_ms is None:
        duration = event.get("duration")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
        ):
            raise ValueError(
                "poiEvent: an event without state requires endTime or numeric duration"
            )
        end_ms = start_ms + round(float(duration))

    if end_ms < start_ms:
        raise ValueError("poiEvent: OUT eventTime must not precede IN eventTime")

    return [
        {
            "eventType": "poiType",
            "eventTime": start_ms,
            "payload": {"poiType": poi_type, "state": "IN"},
        },
        {
            "eventType": "poiType",
            "eventTime": end_ms,
            "payload": {"poiType": poi_type, "state": "OUT"},
        },
    ]


def _standard_commute_event(event: Mapping[str, Any]) -> dict[str, Any]:
    transition_value = event.get("commuteTransition")
    transition = (
        transition_value.upper()
        if isinstance(transition_value, str)
        else None
    )
    if transition not in COMMUTE_TRANSITIONS:
        raise ValueError(
            "commutePeriodEvent: commuteTransition must be one of "
            + ", ".join(sorted(COMMUTE_TRANSITIONS))
        )
    return {
        "eventType": "commutePeriodType",
        "eventTime": _required_event_time_ms(event),
        "payload": {"commuteTransition": transition},
    }


def _standard_pasteboard_event(event: Mapping[str, Any]) -> dict[str, Any]:
    content = event.get("content")
    source = event.get("source")
    if source is None:
        source = event.get("sourceApp")
    if not isinstance(content, str):
        raise ValueError("pasteBoardEvent: content must be a string")
    if not isinstance(source, str):
        raise ValueError("pasteBoardEvent: source/sourceApp must be a string")
    return {
        "eventType": "pasteBoardType",
        "eventTime": _required_event_time_ms(event),
        "payload": {
            "content": content,
            "source": source,
        },
    }


def build_standard_events(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert supported source events to the standard event contract."""
    standard_events: list[dict[str, Any]] = []
    for event in events:
        data_name = str(event.get("dataName", "")).casefold()
        if data_name == "poievent":
            standard_events.extend(_standard_poi_events(event))
        elif data_name == "commuteperiodevent":
            standard_events.append(_standard_commute_event(event))
        elif data_name == "pasteboardevent":
            standard_events.append(_standard_pasteboard_event(event))
    standard_events.sort(key=lambda event: event["eventTime"])
    return standard_events


def export_stream_views(
    input_path: str | Path,
    output_dir: str | Path,
    calibrate_timestamps: bool = False,
    calibration_gap_ms: int | None = None,
) -> dict[str, Any]:
    """Create all JSON views and accessor CSV files for one stream artifact."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    events, accessors = collect_stream_views(
        input_path,
        calibrate_timestamps=calibrate_timestamps,
        calibration_gap_ms=calibration_gap_ms,
    )
    standard_events = build_standard_events(events)

    json_outputs = {
        "all_event": output_path / "all_event.json",
        "all_standard_event": output_path / "all_standard_event.json",
        "all_accessor": output_path / "all_accessor.json",
    }
    json_values = {
        "all_event": events,
        "all_standard_event": standard_events,
        "all_accessor": accessors,
    }
    for name, path in json_outputs.items():
        with path.open("w", encoding="utf-8") as output_file:
            json.dump(json_values[name], output_file, ensure_ascii=False, indent=2)

    csv_outputs = write_accessor_csvs(accessors, output_path / "accessors")
    return {
        "event_count": len(events),
        "standard_event_count": len(standard_events),
        "accessor_count": len(accessors),
        "timestamps_calibrated": calibrate_timestamps,
        "json_outputs": json_outputs,
        "csv_outputs": csv_outputs,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Split a generated stream JSONL into ordered event/accessor views."
    )
    parser.add_argument("input", type=Path, help="generated *.stream.jsonl file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("out") / "stream_views",
        help="output directory (default: out/stream_views)",
    )
    parser.add_argument(
        "--calibrate-timestamps",
        action="store_true",
        help=(
            "rebuild payload timestamps so every frame starts after the "
            "previous frame ends (default: disabled)"
        ),
    )
    parser.add_argument(
        "--calibration-gap-ms",
        type=int,
        metavar="MS",
        help=(
            "fixed positive inter-frame gap used during calibration; by "
            "default gaps are derived from outer stream ts values"
        ),
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.calibration_gap_ms is not None and args.calibration_gap_ms <= 0:
        parser.error("--calibration-gap-ms must be greater than 0")
    if args.calibration_gap_ms is not None and not args.calibrate_timestamps:
        parser.error("--calibration-gap-ms requires --calibrate-timestamps")
    result = export_stream_views(
        args.input,
        args.output_dir,
        calibrate_timestamps=args.calibrate_timestamps,
        calibration_gap_ms=args.calibration_gap_ms,
    )
    calibration_status = "enabled" if result["timestamps_calibrated"] else "disabled"
    print(f"Timestamp calibration: {calibration_status}")
    print(f"Events: {result['event_count']} -> {result['json_outputs']['all_event']}")
    print(
        f"Standard events: {result['standard_event_count']} -> "
        f"{result['json_outputs']['all_standard_event']}"
    )
    print(
        f"Accessors: {result['accessor_count']} -> "
        f"{result['json_outputs']['all_accessor']}"
    )
    for accessor_type, path in result["csv_outputs"].items():
        print(f"Accessor CSV [{accessor_type}]: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
