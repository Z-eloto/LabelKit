"""Schedule multiple generated scenario streams onto one all-day timeline.

The input is a small TOML manifest.  Each ``[[scenes]]`` entry points at one
generated ``*.stream.jsonl`` artifact and declares one or more local-time
windows.  Source sequences are assigned to those windows, all frames are
merged, and timestamp-bearing payload fields are rebuilt on one strictly
increasing timeline.  Matching intent annotations are copied and their
``actionInfo.timestamp`` plus stream line references are synchronized.

Example::

    uv run python build_all_day_stream.py all-day.example.toml \
        --output-dir out/all-day --export-views

Paths in the manifest are resolved relative to the manifest itself.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from export_stream_views import (
    _assign_payload_start,
    _format_calibrated_stream_time,
    _frame_start_ms,
    _parse_time,
    export_stream_views,
)


DEFAULT_FRAME_GAP_MS = 1_000
DEFAULT_SESSION_GAP_MS = 15 * 60 * 1_000
WEEK_TYPES = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)
_WINDOW_RE = re.compile(
    r"^(?P<start>\d{1,2}:\d{2}(?::\d{2})?)-"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)$"
)


class ManifestError(ValueError):
    """The all-day manifest or one of its source artifacts is invalid."""


@dataclass(frozen=True)
class TimeWindow:
    label: str
    start_ms: int
    end_ms: int


@dataclass
class SourceFrame:
    scene_index: int
    scene_name: str
    source_path: Path
    source_line: int
    row: dict[str, Any]
    source_time_ms: int
    relative_ms: int = 0
    desired_ms: int = 0
    assigned_ms: int = 0
    output_line: int = 0
    window: TimeWindow | None = None
    duration_adjusted: bool = False


@dataclass
class SequenceGroup:
    scene_index: int
    scene_name: str
    key: tuple[Any, ...]
    frames: list[SourceFrame] = field(default_factory=list)
    window: TimeWindow | None = None
    anchor_ms: int = 0

    @property
    def span_ms(self) -> int:
        if not self.frames:
            return 0
        return max(frame.relative_ms for frame in self.frames)


@dataclass(frozen=True)
class SceneSpec:
    index: int
    name: str
    stream_path: Path
    intent_path: Path | None
    windows: tuple[TimeWindow, ...]


@dataclass(frozen=True)
class AllDayConfig:
    manifest_path: Path
    day_start: datetime
    frame_gap_ms: int
    session_gap_ms: int
    duration_policy: str
    output_stream_name: str
    output_intent_name: str
    public_holidays: frozenset[date]
    makeup_workdays: frozenset[date]
    scenes: tuple[SceneSpec, ...]
    export_views: bool


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ManifestError(
                f"{path}:{line_number}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(row, dict):
            raise ManifestError(f"{path}:{line_number}: row must be an object")
        rows.append(row)
    return rows


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


def _positive_int(value: Any, location: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestError(f"{location} must be a positive integer")
    return value


def _parse_date_list(value: Any, location: str) -> frozenset[date]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError(f"{location} must be an array of YYYY-MM-DD strings")
    parsed: set[date] = set()
    for item in value:
        try:
            parsed.add(date.fromisoformat(item))
        except ValueError as exc:
            raise ManifestError(f"{location}: invalid date {item!r}") from exc
    return frozenset(parsed)


def _parse_day_start(day_text: Any, timezone_text: Any) -> datetime:
    if not isinstance(day_text, str):
        raise ManifestError("date must be a YYYY-MM-DD string")
    if not isinstance(timezone_text, str) or not timezone_text:
        raise ManifestError("timezone must be an ISO-8601 offset such as +08:00")
    try:
        parsed = datetime.fromisoformat(f"{day_text}T00:00:00{timezone_text}")
    except ValueError as exc:
        raise ManifestError("date/timezone cannot form an ISO-8601 instant") from exc
    if parsed.tzinfo is None:
        raise ManifestError("timezone must include an explicit UTC offset")
    return parsed


def _clock_seconds(value: str, location: str) -> int:
    pieces = value.split(":")
    try:
        hour, minute = int(pieces[0]), int(pieces[1])
        second = int(pieces[2]) if len(pieces) == 3 else 0
    except (ValueError, IndexError) as exc:
        raise ManifestError(f"{location}: invalid clock time {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ManifestError(f"{location}: invalid clock time {value!r}")
    return hour * 3600 + minute * 60 + second


def _parse_window(value: Any, day_start: datetime, location: str) -> TimeWindow:
    if not isinstance(value, str):
        raise ManifestError(f"{location} must be a string such as 07:30-09:30")
    match = _WINDOW_RE.fullmatch(value.strip())
    if match is None:
        raise ManifestError(f"{location}: invalid window {value!r}")
    start_seconds = _clock_seconds(match.group("start"), location)
    end_seconds = _clock_seconds(match.group("end"), location)
    start = day_start + timedelta(seconds=start_seconds)
    end = day_start + timedelta(seconds=end_seconds)
    if end <= start:
        end += timedelta(days=1)
    return TimeWindow(
        label=value,
        start_ms=round(start.timestamp() * 1000),
        end_ms=round(end.timestamp() * 1000),
    )


def _resolve_input_path(root: Path, value: Any, location: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{location} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_file():
        raise ManifestError(f"{location}: file does not exist: {path}")
    return path


def _default_intent_path(stream_path: Path) -> Path | None:
    suffix = ".stream.jsonl"
    if not stream_path.name.endswith(suffix):
        return None
    candidate = stream_path.with_name(stream_path.name[:-len(suffix)] + ".jsonl")
    return candidate if candidate.is_file() else None


def load_manifest(path: str | Path) -> AllDayConfig:
    manifest_path = Path(path).resolve()
    try:
        with manifest_path.open("rb") as manifest_file:
            raw = tomllib.load(manifest_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"cannot load manifest {manifest_path}: {exc}") from exc

    day_start = _parse_day_start(raw.get("date"), raw.get("timezone", "+08:00"))
    frame_gap_ms = _positive_int(
        raw.get("frame_gap_ms"), "frame_gap_ms", DEFAULT_FRAME_GAP_MS
    )
    session_gap_ms = _positive_int(
        raw.get("session_gap_ms"), "session_gap_ms", DEFAULT_SESSION_GAP_MS
    )
    duration_policy = raw.get("duration_policy", "fit")
    if duration_policy not in {"fit", "preserve"}:
        raise ManifestError('duration_policy must be "fit" or "preserve"')
    output_stream_name = raw.get("output_stream", "all-day.stream.jsonl")
    output_intent_name = raw.get("output_intent", "all-day.jsonl")
    for value, location in (
        (output_stream_name, "output_stream"),
        (output_intent_name, "output_intent"),
    ):
        if not isinstance(value, str) or not value or Path(value).name != value:
            raise ManifestError(f"{location} must be a plain file name")

    scene_rows = raw.get("scenes")
    if not isinstance(scene_rows, list) or not scene_rows:
        raise ManifestError("at least one [[scenes]] entry is required")

    root = manifest_path.parent
    scenes: list[SceneSpec] = []
    seen_names: set[str] = set()
    for index, scene_raw in enumerate(scene_rows):
        location = f"scenes[{index}]"
        if not isinstance(scene_raw, dict):
            raise ManifestError(f"{location} must be a table")
        name = scene_raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(f"{location}.name must be a non-empty string")
        if name in seen_names:
            raise ManifestError(f"duplicate scene name {name!r}")
        seen_names.add(name)
        stream_path = _resolve_input_path(root, scene_raw.get("stream"), f"{location}.stream")
        intent_value = scene_raw.get("intent")
        intent_path = (
            _resolve_input_path(root, intent_value, f"{location}.intent")
            if intent_value is not None
            else _default_intent_path(stream_path)
        )
        window_values = scene_raw.get("windows")
        if not isinstance(window_values, list) or not window_values:
            raise ManifestError(f"{location}.windows must be a non-empty array")
        windows = tuple(
            _parse_window(value, day_start, f"{location}.windows[{window_index}]")
            for window_index, value in enumerate(window_values)
        )
        scenes.append(SceneSpec(index, name, stream_path, intent_path, windows))

    export_views_value = raw.get("export_views", False)
    if not isinstance(export_views_value, bool):
        raise ManifestError("export_views must be true or false")

    return AllDayConfig(
        manifest_path=manifest_path,
        day_start=day_start,
        frame_gap_ms=frame_gap_ms,
        session_gap_ms=session_gap_ms,
        duration_policy=duration_policy,
        output_stream_name=output_stream_name,
        output_intent_name=output_intent_name,
        public_holidays=_parse_date_list(raw.get("public_holidays"), "public_holidays"),
        makeup_workdays=_parse_date_list(raw.get("makeup_workdays"), "makeup_workdays"),
        scenes=tuple(scenes),
        export_views=export_views_value,
    )


def _source_time_ms(row: Mapping[str, Any], path: Path, line_number: int) -> int:
    parsed = _parse_time(row.get("ts"))
    if parsed is None:
        text = row.get("text")
        parsed_ms = _frame_start_ms(text) if isinstance(text, Mapping) else None
        if parsed_ms is None:
            raise ManifestError(f"{path}:{line_number}: cannot find a frame timestamp")
        return parsed_ms
    return round(parsed * 1000)


def _group_key(row: Mapping[str, Any], line_number: int) -> tuple[Any, ...]:
    truth = row.get("truth")
    if not isinstance(truth, Mapping):
        return ("line", line_number)
    sequence_class = truth.get("sequence_class")
    sequence = truth.get("sequence")
    if sequence is not None:
        return ("sequence", sequence_class, sequence)
    if truth.get("duplicate_of") is not None:
        return (
            "duplicate",
            sequence_class,
            truth.get("duplicate_of"),
            truth.get("session"),
        )
    return ("session", truth.get("session"), sequence_class)


def _load_scene_groups(scene: SceneSpec) -> list[SequenceGroup]:
    rows = _read_jsonl(scene.stream_path)
    grouped: dict[tuple[Any, ...], SequenceGroup] = {}
    for line_number, source_row in enumerate(rows, start=1):
        row = copy.deepcopy(source_row)
        text = row.get("text")
        if not isinstance(text, dict):
            raise ManifestError(
                f"{scene.stream_path}:{line_number}: field 'text' must be an object"
            )
        key = _group_key(row, line_number)
        group = grouped.setdefault(
            key,
            SequenceGroup(scene.index, scene.name, key),
        )
        group.frames.append(SourceFrame(
            scene_index=scene.index,
            scene_name=scene.name,
            source_path=scene.stream_path,
            source_line=line_number,
            row=row,
            source_time_ms=_source_time_ms(row, scene.stream_path, line_number),
        ))

    groups = list(grouped.values())
    groups.sort(key=lambda group: min(frame.source_line for frame in group.frames))
    for group in groups:
        group.frames.sort(key=lambda frame: (frame.source_time_ms, frame.source_line))
        first_ms = group.frames[0].source_time_ms
        for frame in group.frames:
            frame.relative_ms = max(0, frame.source_time_ms - first_ms)
    return groups


def _assign_scene_windows(groups: Sequence[SequenceGroup], scene: SceneSpec) -> None:
    buckets: dict[int, list[SequenceGroup]] = defaultdict(list)
    for group_index, group in enumerate(groups):
        window_index = group_index % len(scene.windows)
        group.window = scene.windows[window_index]
        buckets[window_index].append(group)

    for window_index, bucket in buckets.items():
        window = scene.windows[window_index]
        max_span = max(group.span_ms for group in bucket)
        available = window.end_ms - window.start_ms - max_span
        if available < 0:
            raise ManifestError(
                f"scene {scene.name!r}: a source sequence spans {max_span} ms, "
                f"longer than window {window.label}"
            )
        if len(bucket) == 1:
            anchors = [window.start_ms + available // 2]
        else:
            anchors = [
                window.start_ms + round(available * index / (len(bucket) - 1))
                for index in range(len(bucket))
            ]
        for group, anchor in zip(bucket, anchors):
            group.anchor_ms = anchor
            for frame in group.frames:
                frame.window = window
                frame.desired_ms = anchor + frame.relative_ms


def _duration_ms(payload: Mapping[str, Any], location: str) -> int:
    value = payload.get("duration")
    if value is None:
        return 0
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ManifestError(f"{location}: duration must be a finite number")
    return max(1_000, round(float(value)))


def _public_time_period(timestamp_ms: int, timezone: Any) -> str:
    hour = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone).hour
    if hour < 6:
        return "MIDNIGHT"
    if hour < 8:
        return "MORNING"
    if hour < 12:
        return "FORENOON"
    if hour < 14:
        return "NOON"
    if hour < 18:
        return "AFTERNOON"
    return "NIGHT"


def _public_workday(timestamp_ms: int, cfg: AllDayConfig) -> str:
    local_date = datetime.fromtimestamp(
        timestamp_ms / 1000, tz=cfg.day_start.tzinfo
    ).date()
    if local_date in cfg.makeup_workdays:
        return "PUBLIC_WORKDAY"
    if local_date in cfg.public_holidays:
        return "PUBLIC_HOLIDAY"
    return "PUBLIC_WORKDAY" if local_date.weekday() < 5 else "PUBLIC_HOLIDAY"


def _recompute_time_semantics(payload: dict[str, Any], timestamp_ms: int,
                              cfg: AllDayConfig) -> None:
    data_name = payload.get("dataName")
    if data_name == "publicTimePeriodEvent":
        payload["publicTimePeriodType"] = _public_time_period(
            timestamp_ms, cfg.day_start.tzinfo
        )
    elif data_name == "publicWorkDayEvent":
        payload["publicWorkDayType"] = _public_workday(timestamp_ms, cfg)
    elif data_name == "weekEvent":
        weekday = datetime.fromtimestamp(
            timestamp_ms / 1000, tz=cfg.day_start.tzinfo
        ).weekday()
        payload["weekType"] = WEEK_TYPES[weekday]


def _schedule_frames(groups: Sequence[SequenceGroup], cfg: AllDayConfig
                     ) -> list[SourceFrame]:
    frames = [frame for group in groups for frame in group.frames]
    frames.sort(key=lambda frame: (
        frame.desired_ms,
        frame.scene_index,
        frame.source_line,
    ))
    schedule_end_ms = max(
        round((cfg.day_start + timedelta(days=1)).timestamp() * 1000),
        *(window.end_ms for scene in cfg.scenes for window in scene.windows),
    )
    cursor_end_ms: int | None = None
    for frame_index, frame in enumerate(frames):
        position = frame_index + 1
        payload = frame.row["text"]
        assert isinstance(payload, dict)
        assigned_ms = frame.desired_ms
        if cursor_end_ms is not None:
            assigned_ms = max(assigned_ms, cursor_end_ms + cfg.frame_gap_ms)
        original_start_ms = _frame_start_ms(payload)
        duration_ms = _duration_ms(
            payload, f"{frame.source_path}:{frame.source_line}"
        )
        if cfg.duration_policy == "fit" and duration_ms:
            next_desired_ms = (
                frames[frame_index + 1].desired_ms
                if frame_index + 1 < len(frames)
                else schedule_end_ms
            )
            available_ms = max(
                1_000,
                min(next_desired_ms - cfg.frame_gap_ms, schedule_end_ms)
                - assigned_ms,
            )
            if duration_ms > available_ms:
                duration_ms = available_ms
                frame.duration_adjusted = True
        if assigned_ms >= schedule_end_ms or assigned_ms + duration_ms > schedule_end_ms:
            raise ManifestError(
                f"scene {frame.scene_name!r}: scheduling overflowed the all-day "
                "horizon; widen its windows, reduce frame_gap_ms, or use "
                'duration_policy = "fit"'
            )
        if "duration" in payload:
            payload["duration"] = duration_ms
        _assign_payload_start(
            payload,
            assigned_ms,
            original_start_ms,
            duration_ms or None,
        )
        _recompute_time_semantics(payload, assigned_ms, cfg)
        frame.row["ts"] = _format_calibrated_stream_time(
            frame.row.get("ts"), assigned_ms
        )
        frame.assigned_ms = assigned_ms
        cursor_end_ms = assigned_ms + duration_ms
        frame.output_line = position

    session = 0
    previous_ms: int | None = None
    for frame in frames:
        if previous_ms is not None and frame.assigned_ms - previous_ms > cfg.session_gap_ms:
            session += 1
        truth = frame.row.get("truth")
        if isinstance(truth, dict):
            truth["session"] = session
        previous_ms = frame.assigned_ms
    return frames


def _member_line_numbers(intent: Mapping[str, Any]) -> list[int]:
    meta = intent.get("_meta")
    stream = meta.get("stream") if isinstance(meta, Mapping) else None
    sources = stream.get("member_sources") if isinstance(stream, Mapping) else None
    if not isinstance(sources, list):
        return []
    result: list[int] = []
    for source in sources:
        line_number = source.get("line_no") if isinstance(source, Mapping) else None
        if isinstance(line_number, int) and not isinstance(line_number, bool):
            result.append(line_number)
    return result


def _synchronize_intents(
    cfg: AllDayConfig,
    scheduled: Sequence[SourceFrame],
    output_stream: Path,
) -> list[dict[str, Any]]:
    frame_by_source = {
        (frame.scene_index, frame.source_line): frame for frame in scheduled
    }
    output_rows: list[tuple[int, int, dict[str, Any]]] = []
    for scene in cfg.scenes:
        if scene.intent_path is None:
            continue
        for intent_line, source_intent in enumerate(
            _read_jsonl(scene.intent_path), start=1
        ):
            intent = copy.deepcopy(source_intent)
            source_lines = _member_line_numbers(intent)
            member_frames = [
                frame_by_source[(scene.index, line_number)]
                for line_number in source_lines
                if (scene.index, line_number) in frame_by_source
            ]
            action_info = intent.get("actionInfo")
            if isinstance(action_info, dict) and "timestamp" in action_info:
                app_frames = [
                    frame for frame in member_frames
                    if frame.row["text"].get("dataName") == "appUsageEvent"
                    or (
                        isinstance(frame.row.get("truth"), Mapping)
                        and frame.row["truth"].get("frame_class") == "app_usage"
                    )
                ]
                if not app_frames:
                    raise ManifestError(
                        f"{scene.intent_path}:{intent_line}: timestamp-bearing intent "
                        "has no mapped app_usage member"
                    )
                action_info["timestamp"] = min(
                    frame.assigned_ms for frame in app_frames
                )

            meta = intent.get("_meta")
            if isinstance(meta, dict):
                stream_meta = meta.get("stream")
                if isinstance(stream_meta, dict) and member_frames:
                    ordered_members = sorted(
                        member_frames, key=lambda frame: frame.output_line
                    )
                    stream_meta["member_sources"] = [
                        {"file": output_stream.name, "line_no": frame.output_line}
                        for frame in ordered_members
                    ]
                    stream_meta["order_span"] = [
                        f"{output_stream.name}:{ordered_members[0].output_line}",
                        f"{output_stream.name}:{ordered_members[-1].output_line}",
                    ]
                source_meta = meta.get("source")
                if isinstance(source_meta, dict):
                    old_line = source_meta.get("line_no")
                    mapped = frame_by_source.get((scene.index, old_line))
                    if mapped is not None:
                        source_meta["file"] = output_stream.name
                        source_meta["line_no"] = mapped.output_line
                meta["all_day"] = {
                    "scene": scene.name,
                    "source_stream": str(scene.stream_path),
                    "source_intent": str(scene.intent_path),
                    "source_line": intent_line,
                }

            timestamp = (
                action_info.get("timestamp")
                if isinstance(action_info, Mapping)
                else None
            )
            sort_timestamp = timestamp if isinstance(timestamp, int) else (
                min((frame.assigned_ms for frame in member_frames), default=0)
            )
            output_rows.append((sort_timestamp, len(output_rows), intent))

    output_rows.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in output_rows]


def build_all_day_stream(
    manifest_path: str | Path,
    output_dir: str | Path | None = None,
    export_views_override: bool | None = None,
) -> dict[str, Any]:
    """Build an all-day stream and synchronized intent artifact."""
    cfg = load_manifest(manifest_path)
    destination = (
        Path(output_dir).resolve()
        if output_dir is not None
        else (cfg.manifest_path.parent / "out" / "all-day").resolve()
    )
    destination.mkdir(parents=True, exist_ok=True)
    output_stream = destination / cfg.output_stream_name
    output_intent = destination / cfg.output_intent_name
    input_paths = {
        scene.stream_path for scene in cfg.scenes
    } | {
        scene.intent_path for scene in cfg.scenes if scene.intent_path is not None
    }
    if output_stream.resolve() in input_paths or output_intent.resolve() in input_paths:
        raise ManifestError("output files must not overwrite a source artifact")

    all_groups: list[SequenceGroup] = []
    scene_counts: dict[str, dict[str, int]] = {}
    for scene in cfg.scenes:
        groups = _load_scene_groups(scene)
        _assign_scene_windows(groups, scene)
        all_groups.extend(groups)
        scene_counts[scene.name] = {
            "sequences": len(groups),
            "frames": sum(len(group.frames) for group in groups),
        }

    scheduled = _schedule_frames(all_groups, cfg)
    _write_jsonl(output_stream, (frame.row for frame in scheduled))
    intents = _synchronize_intents(cfg, scheduled, output_stream)
    _write_jsonl(output_intent, intents)

    views_result: dict[str, Any] | None = None
    should_export_views = (
        cfg.export_views
        if export_views_override is None
        else export_views_override
    )
    if should_export_views:
        views_result = export_stream_views(
            output_stream,
            destination / "views",
            calibrate_timestamps=False,
        )

    report = {
        "date": cfg.day_start.date().isoformat(),
        "timezone": str(cfg.day_start.tzinfo),
        "frame_count": len(scheduled),
        "intent_count": len(intents),
        "duration_adjusted_count": sum(
            1 for frame in scheduled if frame.duration_adjusted
        ),
        "first_timestamp": scheduled[0].assigned_ms if scheduled else None,
        "last_timestamp": scheduled[-1].assigned_ms if scheduled else None,
        "stream_output": str(output_stream),
        "intent_output": str(output_intent),
        "views_output": str(destination / "views") if views_result else None,
        "scenes": scene_counts,
    }
    report_path = destination / "all-day.report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report["report_output"] = str(report_path)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge generated scenario streams onto one all-day timeline."
    )
    parser.add_argument("manifest", type=Path, help="all-day TOML manifest")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="destination directory (default: <manifest>/out/all-day)",
    )
    parser.add_argument(
        "--export-views",
        action="store_true",
        help="also create all_event/all_accessor JSON and accessor CSV views",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = build_all_day_stream(
            args.manifest,
            args.output_dir,
            export_views_override=True if args.export_views else None,
        )
    except ManifestError as exc:
        print(f"all-day build failed: {exc}")
        return 2
    print(f"Frames: {report['frame_count']} -> {report['stream_output']}")
    print(f"Intents: {report['intent_count']} -> {report['intent_output']}")
    if report["views_output"] is not None:
        print(f"Views: {report['views_output']}")
    print(f"Report: {report['report_output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
