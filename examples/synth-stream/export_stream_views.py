"""Export time-ordered event and accessor views from a generated stream JSONL.

The frame splitting contract is shared with ``split.py``.  This script writes:

* ``all_event.json``: every non-empty event, ordered by occurrence time;
* ``all_standard_event.json``: poiEvent, commutePeriodEvent and
  pasteBoardEvent filtered from the ordered event list;
* ``all_accessor.json``: every non-empty accessor, ordered by occurrence time;
* ``accessors/<accessor_type>.csv``: one flattened table per accessor type.
* when timestamp calibration is enabled, a calibrated copy of the input stream
  is written to the output directory with the same file name; both the outer
  ``ts`` and timestamp-bearing fields inside ``text`` use the rebuilt timeline.
* when timestamp calibration is enabled, a matching intent JSONL (for example
  ``synth-caffe.jsonl`` beside ``synth-caffe.stream.jsonl``) is copied into the
  output directory and each ``actionInfo.timestamp`` is synchronized to the
  calibrated timestamp of its earliest member ``app_usage`` frame.

Run, for example::

    uv run python export_stream_views.py out/synth-caffe3.stream.jsonl \
        --output-dir out/exported

Add ``--calibrate-timestamps`` to rebuild payload timestamps before splitting.
By default the calibration gap follows the outer stream ``ts`` intervals; pass
``--calibration-gap-ms`` to use one fixed positive gap instead.
The sibling intent file is discovered automatically from the
``*.stream.jsonl`` name.  Pass ``--intent-input`` to select it explicitly.
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

    def calibrate(self, frame: dict[str, Any], line_number: int) -> int:
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
        frame["ts"] = _format_calibrated_stream_time(frame.get("ts"), assigned_start_ms)
        self.cursor_end_ms = assigned_start_ms + (duration_ms or 0)
        self.previous_stream_time = stream_time
        return assigned_start_ms

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


def _format_calibrated_stream_time(original: Any, timestamp_ms: int) -> Any:
    """Represent a calibrated outer ``ts`` in the source field's time format."""
    if isinstance(original, str):
        text = original.strip()
        try:
            normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return str(timestamp_ms)
        if parsed.tzinfo is None:
            calibrated = datetime.fromtimestamp(timestamp_ms / 1000).replace(tzinfo=None)
        else:
            calibrated = datetime.fromtimestamp(timestamp_ms / 1000, tz=parsed.tzinfo)
        return calibrated.isoformat(timespec="microseconds")
    if isinstance(original, (int, float)) and not isinstance(original, bool):
        magnitude = abs(float(original))
        if magnitude >= 1e17:
            return timestamp_ms * 1_000_000
        if magnitude >= 1e14:
            return timestamp_ms * 1_000
        if magnitude >= 1e11:
            return timestamp_ms
        return timestamp_ms / 1000
    return timestamp_ms


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
    calibrated_app_usage_times: dict[int, int] | None = None,
    calibrated_frames: list[dict[str, Any]] | None = None,
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
                calibrated_start_ms = calibrator.calibrate(frame, line_number)
                if (
                    calibrated_app_usage_times is not None
                    and frame["text"].get("dataName") == "appUsageEvent"
                ):
                    calibrated_app_usage_times[line_number] = calibrated_start_ms
                if calibrated_frames is not None:
                    calibrated_frames.append(frame)

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


def _default_intent_input(stream_path: str | Path) -> Path | None:
    """Return the conventional intent artifact beside a ``*.stream.jsonl``."""
    path = Path(stream_path)
    suffix = ".stream.jsonl"
    if not path.name.endswith(suffix):
        return None
    return path.with_name(path.name[:-len(suffix)] + ".jsonl")


def write_calibrated_stream(
    frames: Sequence[Mapping[str, Any]],
    output_path: str | Path,
) -> Path:
    """Write a complete calibrated stream copy as compact UTF-8 JSONL."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as output_file:
        for frame in frames:
            output_file.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
            output_file.write("\n")
    return destination


def _source_basename(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def synchronize_intent_timestamps(
    intent_input: str | Path,
    intent_output: str | Path,
    stream_input: str | Path,
    calibrated_app_usage_times: Mapping[int, int],
) -> int:
    """Copy intent JSONL while replacing each intent's app-usage timestamp.

    ``_meta.stream.member_sources`` is the stable join between an intent row
    and the stream rows that formed it.  An intent that declares
    ``actionInfo.timestamp`` must contain at least one member line recorded as
    a calibrated ``appUsageEvent``; multiple matches use the earliest
    calibrated start, matching the caffe annotation contract.  Intents without
    that slot are copied unchanged.
    The complete destination is validated in memory before it is written, so a
    mapping error cannot leave a partially synchronized file behind.
    """
    source_path = Path(intent_input)
    destination_path = Path(intent_output)
    stream_name = Path(stream_input).name
    if not source_path.is_file():
        raise FileNotFoundError(f"intent annotation file does not exist: {source_path}")
    if source_path.resolve() == destination_path.resolve():
        raise ValueError(
            "intent output would overwrite its source; choose a different output directory"
        )

    synchronized_rows: list[dict[str, Any]] = []
    updated_count = 0
    with source_path.open("r", encoding="utf-8") as intent_file:
        for line_number, line in enumerate(intent_file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{source_path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(
                    f"{source_path}:{line_number}: each JSONL row must be an object"
                )

            action_info = row.get("actionInfo")
            if not isinstance(action_info, dict):
                raise ValueError(
                    f"{source_path}:{line_number}: field 'actionInfo' must be an object"
                )
            # Some scenarios (for example check_in) have no intent timestamp.
            # Copy those rows byte-semantically without inventing an undeclared slot.
            if "timestamp" not in action_info:
                synchronized_rows.append(row)
                continue

            meta = row.get("_meta")
            stream_meta = meta.get("stream") if isinstance(meta, Mapping) else None
            member_sources = (
                stream_meta.get("member_sources")
                if isinstance(stream_meta, Mapping)
                else None
            )
            if not isinstance(member_sources, list):
                raise ValueError(
                    f"{source_path}:{line_number}: missing _meta.stream.member_sources"
                )

            matched_times: list[int] = []
            for member in member_sources:
                if not isinstance(member, Mapping):
                    continue
                if _source_basename(member.get("file")) != stream_name:
                    continue
                member_line = member.get("line_no")
                if isinstance(member_line, int) and not isinstance(member_line, bool):
                    timestamp = calibrated_app_usage_times.get(member_line)
                    if timestamp is not None:
                        matched_times.append(timestamp)

            if not matched_times:
                raise ValueError(
                    f"{source_path}:{line_number}: intent has no calibrated app_usage "
                    f"member from {stream_name}"
                )
            action_info["timestamp"] = min(matched_times)
            updated_count += 1
            synchronized_rows.append(row)

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with destination_path.open("w", encoding="utf-8", newline="\n") as output_file:
        for row in synchronized_rows:
            output_file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            output_file.write("\n")
    return updated_count


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
    transition_value = event.get("commuteTransition", event.get("periodType"))
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
    intent_input_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create all JSON views and accessor CSV files for one stream artifact."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    input_stream_path = Path(input_path)
    calibrated_stream_output = (
        output_path / input_stream_path.name if calibrate_timestamps else None
    )
    if (
        calibrated_stream_output is not None
        and input_stream_path.resolve() == calibrated_stream_output.resolve()
    ):
        raise ValueError(
            "calibrated stream output would overwrite its source; "
            "choose a different output directory"
        )
    calibrated_app_usage_times: dict[int, int] = {}
    calibrated_frames: list[dict[str, Any]] = []
    events, accessors = collect_stream_views(
        input_path,
        calibrate_timestamps=calibrate_timestamps,
        calibration_gap_ms=calibration_gap_ms,
        calibrated_app_usage_times=(
            calibrated_app_usage_times if calibrate_timestamps else None
        ),
        calibrated_frames=(calibrated_frames if calibrate_timestamps else None),
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
    if calibrated_stream_output is not None:
        write_calibrated_stream(calibrated_frames, calibrated_stream_output)
    intent_source: Path | None = None
    intent_output: Path | None = None
    intent_updated_count = 0
    if calibrate_timestamps:
        if intent_input_path is not None:
            intent_source = Path(intent_input_path)
        else:
            candidate = _default_intent_input(input_path)
            if candidate is not None and candidate.is_file():
                intent_source = candidate
        if intent_source is not None:
            intent_output = output_path / intent_source.name
            intent_updated_count = synchronize_intent_timestamps(
                intent_source,
                intent_output,
                input_path,
                calibrated_app_usage_times,
            )
    return {
        "event_count": len(events),
        "standard_event_count": len(standard_events),
        "accessor_count": len(accessors),
        "timestamps_calibrated": calibrate_timestamps,
        "calibrated_stream_output": calibrated_stream_output,
        "intent_input": intent_source,
        "intent_output": intent_output,
        "intent_updated_count": intent_updated_count,
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
    parser.add_argument(
        "--intent-input",
        type=Path,
        help=(
            "intent annotation JSONL whose actionInfo.timestamp values should "
            "follow calibrated app_usage frames; with timestamp calibration, "
            "defaults to the sibling file obtained by removing '.stream'"
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
    if args.intent_input is not None and not args.calibrate_timestamps:
        parser.error("--intent-input requires --calibrate-timestamps")
    result = export_stream_views(
        args.input,
        args.output_dir,
        calibrate_timestamps=args.calibrate_timestamps,
        calibration_gap_ms=args.calibration_gap_ms,
        intent_input_path=args.intent_input,
    )
    calibration_status = "enabled" if result["timestamps_calibrated"] else "disabled"
    print(f"Timestamp calibration: {calibration_status}")
    if result["calibrated_stream_output"] is not None:
        print(f"Calibrated stream: {result['calibrated_stream_output']}")
    print(f"Events: {result['event_count']} -> {result['json_outputs']['all_event']}")
    print(
        f"Standard events: {result['standard_event_count']} -> "
        f"{result['json_outputs']['all_standard_event']}"
    )
    print(
        f"Accessors: {result['accessor_count']} -> "
        f"{result['json_outputs']['all_accessor']}"
    )
    if result["intent_output"] is not None:
        print(
            f"Intent annotations: {result['intent_updated_count']} timestamps updated -> "
            f"{result['intent_output']}"
        )
    elif result["timestamps_calibrated"]:
        print("Intent annotations: skipped (no matching intent JSONL found)")
    for accessor_type, path in result["csv_outputs"].items():
        print(f"Accessor CSV [{accessor_type}]: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
