"""Offline tests for the bounded, content-free text input profile."""
from __future__ import annotations

import dataclasses
import json
from dataclasses import replace
from pathlib import Path

import pytest

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    SegmentConfig,
    StitchConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.errors import InputError
from labelkit.orchestration import TextInputProfile, profile_text_input


def _make_cfg(tmp_path: Path, *, modality: str = "text", **input_kw) -> ResolvedConfig:
    """Build the minimal validated-shape config needed by the read-only API."""
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(output=str(tmp_path / "out.jsonl"), modality=modality,
                      input=str(tmp_path / "in")),
        input=InputConfig(**input_kw),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="t", criteria=()),
        class_views={},
        user_schema={"type": "object"},
        limit=None,
        strict=False,
        dry_run=False,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:config",
        project_digest="sha256:project",
    )


def _write(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_text_profile_returns_aggregates_without_content(
        tmp_path, monkeypatch, capsys):
    credential = "sk-" + "x" * 24
    rows = [
        {"text": "联系 alice@example.com", "source": "a", "nullable": None},
        {"text": "  联系   alice@example.com  ", "source": "b", "nullable": "x"},
        {"query": "missing configured field", "source": "bad"},
        {"text": f"电话 +86 138-0013-8000，令牌 {credential}", "count": 1},
    ]
    cfg = _make_cfg(tmp_path)
    _write(tmp_path / "in" / "data.jsonl", rows)
    monkeypatch.setattr(
        "labelkit.common.runtime.llm_client.LLMClient.__init__",
        lambda *args, **kwargs: pytest.fail("profile must not construct LLMClient"),
    )
    monkeypatch.setattr(
        "labelkit.operators.emitter.Emitter.__init__",
        lambda *args, **kwargs: pytest.fail("profile must not construct Emitter"),
    )

    profile = profile_text_input(cfg, sample_limit=10)

    assert isinstance(profile, TextInputProfile)
    assert profile.files == ("data.jsonl",)
    assert (profile.estimated_lines, profile.sampled_lines) == (4, 4)
    assert (profile.sampled_records, profile.bad_lines) == (3, 1)
    assert profile.sample_complete is True
    assert profile.duplicate_texts == 1
    assert profile.duplicate_rate == pytest.approx(1 / 3)
    assert profile.sensitive_record_counts == {
        "email_like": 2,
        "phone_like": 1,
        "cn_id_like": 0,
        "credential_like": 1,
    }
    by_name = {field.name: field for field in profile.fields}
    assert by_name["text"].present == 3
    assert by_name["nullable"].nulls == 1
    assert by_name["nullable"].kinds == ("null", "string")
    assert by_name["count"].kinds == ("integer",)
    lengths = sorted(len(rows[i]["text"]) for i in (0, 1, 3))
    assert profile.text_lengths.minimum == lengths[0]
    assert profile.text_lengths.maximum == lengths[-1]
    assert profile.text_lengths.mean == pytest.approx(sum(lengths) / 3)
    assert profile.text_lengths.p50 == lengths[1]
    assert profile.text_lengths.p95 == lengths[-1]

    rendered = repr(dataclasses.asdict(profile))
    for private_value in (
        rows[0]["text"], rows[1]["text"], rows[3]["text"], credential,
        "alice@example.com", "138-0013-8000",
    ):
        assert private_value not in rendered
    assert capsys.readouterr() == ("", "")
    assert not (tmp_path / "out.jsonl").exists()
    assert not (tmp_path / "out.report.json").exists()
    assert not (tmp_path / "out.trace.jsonl").exists()


def test_text_profile_is_bounded_and_marks_incomplete_prefix(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write(tmp_path / "in" / "data.jsonl", [
        {"text": "one", "first": 1},
        {"other": "bad"},
        {"text": "two", "second": 2},
        {"text": "three", "third": 3},
    ])

    profile = profile_text_input(cfg, sample_limit=2)

    assert profile.estimated_lines == 4
    assert profile.sampled_lines == 3
    assert profile.sampled_records == 2
    assert profile.bad_lines == 1
    assert profile.sample_complete is False
    assert {field.name for field in profile.fields} == {"text", "first", "second"}


def test_text_profile_caps_unique_field_names(tmp_path):
    cfg = _make_cfg(tmp_path)
    row = {"text": "safe", **{f"field_{i:03d}": i for i in range(300)}}
    _write(tmp_path / "in" / "data.jsonl", [row])

    profile = profile_text_input(cfg)

    assert len(profile.fields) == 256
    assert profile.fields_truncated is True
    assert "text" in {field.name for field in profile.fields}


def test_cn_id_signal_does_not_double_count_as_phone(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write(tmp_path / "in" / "data.jsonl", [{"text": "身份证 11010519491231002X"}])

    profile = profile_text_input(cfg)

    assert profile.sensitive_record_counts["cn_id_like"] == 1
    assert profile.sensitive_record_counts["phone_like"] == 0


@pytest.mark.parametrize("sample_limit", [0, 10_001])
def test_text_profile_rejects_unbounded_sample_limits(tmp_path, sample_limit):
    cfg = _make_cfg(tmp_path)
    with pytest.raises(ValueError, match="sample_limit"):
        profile_text_input(cfg, sample_limit=sample_limit)


def test_text_profile_rejects_non_text_and_generate_only(tmp_path):
    ui_cfg = _make_cfg(tmp_path, modality="ui")
    with pytest.raises(ValueError, match="process mode with text modality"):
        profile_text_input(ui_cfg)

    text_cfg = _make_cfg(tmp_path)
    gen_cfg = replace(
        text_cfg,
        run=RunConfig(output=text_cfg.run.output, modality="text", mode="generate_only"),
    )
    with pytest.raises(ValueError, match="process mode with text modality"):
        profile_text_input(gen_cfg)


def test_text_profile_reuses_fail_policy_and_writes_no_products(tmp_path):
    cfg = _make_cfg(tmp_path, on_bad_line="fail")
    _write(tmp_path / "in" / "data.jsonl", [
        {"text": "valid"},
        {"other": "invalid"},
    ])

    with pytest.raises(InputError, match="input.on_bad_line"):
        profile_text_input(cfg)

    assert not (tmp_path / "out.jsonl").exists()
    assert not (tmp_path / "out.report.json").exists()
    assert not (tmp_path / "out.trace.jsonl").exists()


def test_text_profile_contract_is_frozen(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write(tmp_path / "in" / "data.jsonl", [{"text": "safe"}])
    profile = profile_text_input(cfg)

    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.sampled_records = 99
