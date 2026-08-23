"""Read-only, bounded input profiling for Agent-facing library APIs."""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from labelkit.common.config.model import ResolvedConfig
from labelkit.orchestration.results import (
    JsonValueKind,
    SensitivePattern,
    TextFieldProfile,
    TextInputProfile,
    TextLengthProfile,
)
from labelkit.operators.ingest import Ingestor

_DEFAULT_SAMPLE_LIMIT = 1_000
_MAX_SAMPLE_LIMIT = 10_000
_MAX_FIELDS = 256
_KIND_ORDER: tuple[JsonValueKind, ...] = (
    "null", "boolean", "integer", "number", "string", "array", "object",
)

# Heuristic risk signals only. The profile counts records with a match and
# never retains or returns a matched value.
_SENSITIVE_PATTERNS: tuple[tuple[SensitivePattern, re.Pattern[str]], ...] = (
    ("email_like", re.compile(
        r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@"
        r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"
    )),
    ("phone_like", re.compile(r"(?<!\d)\+?\d(?:[ ()-]*\d){7,14}(?!\d)")),
    ("cn_id_like", re.compile(r"(?<!\d)\d{17}[\dXx](?!\w)")),
    ("credential_like", re.compile(
        r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
        r"AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    )),
)


@dataclass
class _FieldStats:
    """Mutable accumulator kept private to a single profiling call."""

    present: int = 0
    nulls: int = 0
    kinds: set[JsonValueKind] = field(default_factory=set)


def _json_kind(value: Any) -> JsonValueKind:
    """Return the closed JSON value-kind label for one field value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _normalized_digest(text: str) -> bytes:
    """Hash NFC text with collapsed whitespace without retaining sample text."""
    normalized = " ".join(unicodedata.normalize("NFC", text).split())
    return hashlib.sha256(normalized.encode("utf-8")).digest()


def _percentile(sorted_values: list[int], percentile: float) -> int:
    """Return a deterministic nearest-rank percentile from a non-empty list."""
    index = max(0, math.ceil(percentile * len(sorted_values)) - 1)
    return sorted_values[index]


def _length_profile(lengths: list[int]) -> TextLengthProfile:
    """Freeze aggregate character lengths; raw lengths do not leave the call."""
    ordered = sorted(lengths)
    return TextLengthProfile(
        minimum=ordered[0],
        maximum=ordered[-1],
        mean=sum(ordered) / len(ordered),
        p50=_percentile(ordered, 0.50),
        p95=_percentile(ordered, 0.95),
    )


def profile_text_input(
    cfg: ResolvedConfig,
    *,
    sample_limit: int = _DEFAULT_SAMPLE_LIMIT,
) -> TextInputProfile:
    """Profile a bounded prefix of text input without LLM or output channels.

    The complete input is scanned once for its file list and non-empty-line
    estimate. At most ``sample_limit`` valid records are then parsed through
    the real M2 iterator, so bad-line policy and text-field extraction stay
    identical to execution. Only aggregate shape/risk statistics survive.

    @param cfg: Validated process-mode text project configuration.
    @param sample_limit: Maximum valid records to parse, from 1 through 10,000.
    @return: Content-free, frozen text input profile.
    @raises ValueError: Unsupported mode/modality or invalid sample limit.
    @raises InputError: Existing M2 input and bad-line failures.
    """
    if cfg.run.mode != "process" or cfg.run.modality != "text":
        raise ValueError("profile_text_input requires process mode with text modality")
    if not 1 <= sample_limit <= _MAX_SAMPLE_LIMIT:
        raise ValueError("sample_limit must be between 1 and 10000")

    ingestor = Ingestor(cfg)
    plan = ingestor.scan(estimate=True)
    fields: dict[str, _FieldStats] = {}
    fields_truncated = False
    lengths: list[int] = []
    digests: set[bytes] = set()
    duplicate_texts = 0
    sensitive_counts: dict[SensitivePattern, int] = {
        name: 0 for name, _ in _SENSITIVE_PATTERNS
    }

    for record in ingestor.records():
        text = record.text or ""
        lengths.append(len(text))
        digest = _normalized_digest(text)
        if digest in digests:
            duplicate_texts += 1
        else:
            digests.add(digest)
        for name, pattern in _SENSITIVE_PATTERNS:
            if pattern.search(text):
                sensitive_counts[name] += 1

        raw = record.raw or {}
        text_root = cfg.input.text_field.split(".", 1)[0]
        field_names = sorted(raw, key=lambda name: (name != text_root, name))
        for name in field_names:
            stats = fields.get(name)
            if stats is None:
                if len(fields) >= _MAX_FIELDS:
                    fields_truncated = True
                    continue
                stats = fields[name] = _FieldStats()
            value = raw[name]
            kind = _json_kind(value)
            stats.present += 1
            stats.nulls += int(value is None)
            stats.kinds.add(kind)

        if len(lengths) >= sample_limit:
            break

    sampled_records = len(lengths)
    field_profiles = tuple(
        TextFieldProfile(
            name=name,
            present=stats.present,
            nulls=stats.nulls,
            kinds=tuple(kind for kind in _KIND_ORDER if kind in stats.kinds),
        )
        for name, stats in sorted(fields.items())
    )
    report = ingestor.report
    return TextInputProfile(
        config_digest=cfg.config_digest,
        project_digest=cfg.project_digest,
        text_field=cfg.input.text_field,
        files=plan.files,
        estimated_lines=plan.estimated_records,
        sample_limit=sample_limit,
        sampled_lines=report.scanned,
        sampled_records=sampled_records,
        bad_lines=report.bad_input,
        sample_complete=report.scanned >= plan.estimated_records,
        fields=field_profiles,
        fields_truncated=fields_truncated,
        text_lengths=_length_profile(lengths),
        duplicate_texts=duplicate_texts,
        duplicate_rate=duplicate_texts / sampled_records,
        sensitive_record_counts=sensitive_counts,
    )
