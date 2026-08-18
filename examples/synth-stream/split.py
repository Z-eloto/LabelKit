import json
from pathlib import Path
from typing import Dict, List, Any, Optional, Union
from copy import deepcopy

FRAME_SPLIT_RULES: Dict[str, Dict[str, Any]] = {
    "notificationEvent": {
        "event1": ['dataName', 'messageTime', 'content', 'appName', 'bundleName'],
        "event2": [],
        "accessor": {
            "type": "object",
            "key": "notifications10min",
            "fields": ['timestamp', 'content', 'source'],
            "field_map": {"source": "bundleName"}
        }
    },
    "appUsageEvent": {
        "event1": ['dataName', 'timestamp', 'bundleName', 'state'],
        "event2": ['dataName', 'timestamp', 'bundleName', 'state'],
        "accessor": {
            "type": "object",
            "key": "appUsage30min",
            "fields": ['bundleName', 'startTime', 'duration']
        }
    },
    "companionMemoryEvent": {
        "event1": ['dataName', 'createTime', 'updateTime', 'caption', 'bundleName', 'status'],
        "event2": [],
        "accessor": {
            "type": "object",
            "key": "companionMemoryInfo30min",
            "fields": ['createTime', 'entityId', 'pageId', 'caption', 'bundleName', 'status']
        }
    },
    "screenMemoryEvent": {
        "event1": ['dataName', 'createTime', 'updateTime', 'caption', 'bundleName', 'status'],
        "event2": [],
        "accessor": {
            "type": "object",
            "key": "screenMemoryInfo30min",
            "fields": ['createTime', 'entityId', 'pageId', 'caption', 'bundleName', 'status']
        }
    },
    "pasteBoardEvent": {
        "event1": ['dataName', 'timestamp', 'content', 'sourceApp'],
        "event2": [],
        "accessor": {
            "type": "array",
            "key": "pasteBoard5min",
            "fields": ['timestamp', 'content', 'source'],
            "field_map": {"source": "sourceApp"}
        }
    }
}

EVENT_TIMESTAMP_FIELDS: Dict[str, str] = {
    "notificationEvent": "messageTime",
    "appUsageEvent": "timestamp",
    "companionMemoryEvent": "createTime",
    "screenMemoryEvent": "createTime",
    "pasteBoardEvent": "timestamp",
}

def split_frame(frame: Dict[str, Any], rules: Dict[str, Dict[str, Any]] = None) -> Dict[str, Any]:
    if rules is None:
        rules = FRAME_SPLIT_RULES

    data = frame.get("text", {})
    data_name = data.get("dataName", "unknown")

    rule = rules.get(data_name, None)
    if rule is None:
        return {
            "ts": frame.get("ts"),
            "truth": frame.get("truth"),
            "dataName": data_name,
            "raw": data
        }

    event1_fields = rule.get("event1", [])
    event2_fields = rule.get("event2", [])
    accessor_config = rule.get("accessor", {"type": "object", "fields": []})

    event1 = {k: data.get(k) for k in event1_fields if k in data}
    event2 = {k: data.get(k) for k in event2_fields if k in data}

    accessor_result = _build_accessor(data, accessor_config)

    if data_name == "appUsageEvent":
        start_time = data.get("startTime", 0)
        duration = data.get("duration", 0)
        event1 = {
            "dataName": "appUsageEvent",
            "bundleName": data.get("bundleName"),
            "state": "IN",
            "timestamp": start_time
        }
        event2 = {
            "dataName": "appUsageEvent",
            "bundleName": data.get("bundleName"),
            "state": "OUT",
            "timestamp": start_time + duration
        }

    result = {
        "ts": frame.get("ts"),
        "truth": frame.get("truth"),
        "event1": event1,
        "event2": event2,
        "accessor": accessor_result
    }

    return result

def _build_accessor(data: Dict[str, Any], accessor_config: Dict[str, Any]) -> Any:
    accessor_type = accessor_config.get("type", "object")

    fields = accessor_config.get("fields", [])
    field_map = accessor_config.get("field_map", {})
    item = {
        output_field: data.get(field_map.get(output_field, output_field))
        for output_field in fields
        if field_map.get(output_field, output_field) in data
    }

    if accessor_type == "object":
        key = accessor_config.get("key")
        return {key: item} if key else item

    elif accessor_type == "array":
        key = accessor_config.get("key", "unknown")
        return {key: [item]}

    return {}

def process_stream(input_path: str, output_path: str, rules: Dict[str, Dict[str, Any]] = None):
    if rules is None:
        rules = FRAME_SPLIT_RULES

    input_file = Path(input_path)
    output_file = Path(output_path)

    results = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            frame = json.loads(line)
            split_result = split_frame(frame, rules)
            results.append(split_result)

    export_rules = {}
    for k, v in rules.items():
        export_rules[k] = {
            "event1": v.get("event1", []),
            "event2": v.get("event2", []),
            "accessor": v.get("accessor", {})
        }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump({
            "total_frames": len(results),
            "split_rules": export_rules,
            "frames": results
        }, f, ensure_ascii=False, indent=2)

    print(f"Processed {len(results)} frames")
    print(f"Output: {output_file}")

    stats = {"by_dataName": {}}
    for r in results:
        dn = r.get("event1", {}).get("dataName", "unknown")
        stats["by_dataName"][dn] = stats["by_dataName"].get(dn, 0) + 1
    print(f"Stats: {stats['by_dataName']}")

    return results

def update_rules(data_name: str, event1_fields: List[str] = None, event2_fields: List[str] = None,
                 accessor_config: Dict[str, Any] = None):
    FRAME_SPLIT_RULES[data_name] = {
        "event1": event1_fields or [],
        "event2": event2_fields or [],
        "accessor": accessor_config or {"type": "object", "fields": []}
    }
    print(f"Updated rules for {data_name}")
    print(f"  event1: {event1_fields}")
    print(f"  event2: {event2_fields}")
    print(f"  accessor: {accessor_config}")


def _sequence_group(frame: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Return a stable output group for one stream frame.

    Regular generated frames carry ``truth.sequence`` and are grouped by that
    sequence id even when two sequences are interleaved in the same session.
    Replay frames deliberately carry ``sequence = null``; distinguish replay
    instances by both their source sequence and replay session so multiple
    duplicates are never merged into one ``None`` bucket.  Noise and malformed
    unassigned frames live in separate namespaces for the same reason.
    """
    truth = frame.get("truth")
    if not isinstance(truth, dict):
        truth = {}

    sequence = truth.get("sequence")
    session = truth.get("session")
    duplicate_of = truth.get("duplicate_of")
    noise = truth.get("noise") is True

    if sequence is not None:
        return f"seq_{sequence}", {
            "kind": "sequence",
            "sequence": sequence,
        }
    if duplicate_of is not None:
        return f"seq_duplicate_of_{duplicate_of}_session_{session}", {
            "kind": "duplicate",
            "sequence": None,
            "duplicate_of": duplicate_of,
            "session": session,
        }
    if noise:
        return f"noise_session_{session}", {
            "kind": "noise",
            "sequence": None,
            "session": session,
        }
    return f"unassigned_session_{session}", {
        "kind": "unassigned",
        "sequence": None,
        "session": session,
    }


def _build_sequence_projections(
    sequences: Dict[str, Dict[str, Any]],
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Build event-only and accessor-only views from grouped split frames.

    Events keep their frame order; within one frame ``event1`` precedes
    ``event2``.  Empty event objects and empty accessors are omitted.  Group
    metadata is copied to both views so regular, duplicate, noise and
    unassigned groups remain distinguishable without the full frame payload.
    """
    event_sequences: Dict[str, Dict[str, Any]] = {}
    accessor_sequences: Dict[str, Dict[str, Any]] = {}

    for group_name, group in sequences.items():
        metadata = {k: v for k, v in group.items() if k != "frames"}
        events: List[Dict[str, Any]] = []
        accessors: List[Dict[str, Any]] = []

        for frame in group.get("frames", []):
            for event_key in ("event1", "event2"):
                event = frame.get(event_key)
                if isinstance(event, dict) and event:
                    events.append(event)

            accessor = frame.get("accessor")
            if isinstance(accessor, dict) and accessor:
                accessors.append(accessor)

        event_sequences[group_name] = {
            **metadata,
            "event_count": len(events),
            "events": events,
        }
        accessor_sequences[group_name] = {
            **metadata,
            "accessor_count": len(accessors),
            "accessors": accessors,
        }

    return event_sequences, accessor_sequences


def validate_event_timestamps(
    event_sequences: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate that event timestamps strictly increase inside every group.

    Event types use different timestamp field names, so the occurrence time is
    selected through ``EVENT_TIMESTAMP_FIELDS``.  Missing, boolean and
    non-numeric timestamps are validation errors as well.  All errors are
    collected instead of stopping at the first invalid sequence.
    """
    sequence_results: Dict[str, Dict[str, Any]] = {}
    total_errors = 0
    invalid_sequence_count = 0

    for group_name, group in event_sequences.items():
        events = group.get("events", [])
        errors: List[Dict[str, Any]] = []
        previous_timestamp: Optional[Union[int, float]] = None
        previous_event_index: Optional[int] = None

        for event_index, event in enumerate(events, start=1):
            data_name = event.get("dataName")
            timestamp_field = EVENT_TIMESTAMP_FIELDS.get(data_name)
            timestamp = event.get(timestamp_field) if timestamp_field else None

            if timestamp_field is None:
                errors.append({
                    "type": "unknown_event_type",
                    "event_index": event_index,
                    "dataName": data_name,
                    "message": "no timestamp field is configured for this event type",
                })
                continue

            if (
                not isinstance(timestamp, (int, float))
                or isinstance(timestamp, bool)
            ):
                errors.append({
                    "type": "invalid_timestamp",
                    "event_index": event_index,
                    "dataName": data_name,
                    "timestamp_field": timestamp_field,
                    "timestamp": timestamp,
                    "message": "timestamp must be a number",
                })
                continue

            if previous_timestamp is not None and timestamp <= previous_timestamp:
                errors.append({
                    "type": "not_strictly_increasing",
                    "event_index": event_index,
                    "previous_event_index": previous_event_index,
                    "dataName": data_name,
                    "timestamp_field": timestamp_field,
                    "timestamp": timestamp,
                    "previous_timestamp": previous_timestamp,
                    "message": "timestamp must be greater than the previous event timestamp",
                })

            previous_timestamp = timestamp
            previous_event_index = event_index

        is_valid = not errors
        if not is_valid:
            invalid_sequence_count += 1
            total_errors += len(errors)

        sequence_results[group_name] = {
            "valid": is_valid,
            "event_count": len(events),
            "errors": errors,
        }

    sequence_count = len(event_sequences)
    return {
        "valid": invalid_sequence_count == 0,
        "sequence_count": sequence_count,
        "valid_sequence_count": sequence_count - invalid_sequence_count,
        "invalid_sequence_count": invalid_sequence_count,
        "error_count": total_errors,
        "sequences": sequence_results,
    }


def _print_timestamp_validation(report: Dict[str, Any]) -> None:
    status = "PASSED" if report["valid"] else "FAILED"
    print(
        f"Event timestamp validation: {status} "
        f"({report['valid_sequence_count']}/{report['sequence_count']} groups valid, "
        f"{report['error_count']} errors)"
    )

    for group_name, result in report["sequences"].items():
        for error in result["errors"]:
            if error["type"] == "not_strictly_increasing":
                print(
                    f"  {group_name}: event #{error['event_index']} timestamp "
                    f"{error['timestamp']} is not greater than event "
                    f"#{error['previous_event_index']} timestamp "
                    f"{error['previous_timestamp']}"
                )
            else:
                print(
                    f"  {group_name}: event #{error['event_index']} "
                    f"{error['message']}"
                )


def split_and_separate(input_path: str, output_dir: str, rules: Dict[str, Dict[str, Any]] = None):
    if rules is None:
        rules = FRAME_SPLIT_RULES

    input_file = Path(input_path)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    sequences: Dict[str, Dict[str, Any]] = {}

    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            frame = json.loads(line)
            split_result = split_frame(frame, rules)

            group_name, group_meta = _sequence_group(frame)
            if group_name not in sequences:
                sequences[group_name] = {
                    **group_meta,
                    "frames": [],
                }
            sequences[group_name]["frames"].append(split_result)

    # 不再为每条序列分别写 ``seq_*_split.json``；全部分组统一保存在
    # ``all_split.json`` 中。保留下面的旧实现作为需要时可恢复的参考。
    # for group_name, group in sequences.items():
    #     frames = group["frames"]
    #     seq_file = output_path / f"{group_name}_split.json"
    #
    #     with open(seq_file, "w", encoding="utf-8") as f:
    #         json.dump({
    #             **{k: v for k, v in group.items() if k != "frames"},
    #             "frame_count": len(frames),
    #             "frames": frames
    #         }, f, ensure_ascii=False, indent=2)
    #
    #     print(f"  {group_name}: {len(frames)} frames -> {seq_file.name}")

    all_file = output_path / "all_split.json"
    with open(all_file, "w", encoding="utf-8") as f:
        json.dump(sequences, f, ensure_ascii=False, indent=2)
    print(f"\nCombined file: {all_file.name}")

    event_sequences, accessor_sequences = _build_sequence_projections(sequences)

    timestamp_validation = validate_event_timestamps(event_sequences)
    _print_timestamp_validation(timestamp_validation)

    events_file = output_path / "all_events.json"
    with open(events_file, "w", encoding="utf-8") as f:
        json.dump(event_sequences, f, ensure_ascii=False, indent=2)
    print(f"Event-only file: {events_file.name}")

    accessors_file = output_path / "all_accessors.json"
    with open(accessors_file, "w", encoding="utf-8") as f:
        json.dump(accessor_sequences, f, ensure_ascii=False, indent=2)
    print(f"Accessor-only file: {accessors_file.name}")

    return timestamp_validation

def convert_to_final_format(input_path: str, output_path: str, rules: Dict[str, Dict[str, Any]] = None):
    if rules is None:
        rules = FRAME_SPLIT_RULES

    input_file = Path(input_path)
    output_file = Path(output_path)

    results = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            frame = json.loads(line)
            split_result = split_frame(frame, rules)
            results.append(split_result)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Converted {len(results)} frames to final format")
    print(f"Output: {output_file}")

if __name__ == "__main__":
    base_dir = Path(r"D:\CollegeStudy\实习\项目代码\LabelKit\examples\synth-stream\out")
    input_stream = base_dir / "synth-caffe2.stream.jsonl"
    output_dir = base_dir / "split"

    print("=" * 50)
    print("Splitting stream file by frame type...")
    print("=" * 50)

    split_and_separate(str(input_stream), str(output_dir))

    print()
    print("=" * 50)
    print("Current split rules:")
    print("=" * 50)
    for data_name, rule in FRAME_SPLIT_RULES.items():
        print(f"  {data_name}:")
        print(f"    event1: {rule['event1']}")
        print(f"    event2: {rule['event2']}")
        print(f"    accessor: {rule['accessor']}")
