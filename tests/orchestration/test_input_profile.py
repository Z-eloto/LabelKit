"""Offline tests for bounded, content-free text/UI/stream profiles."""
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
from labelkit.orchestration import (
    StreamInputProfile,
    TextInputProfile,
    UIInputProfile,
    profile_input,
    profile_text_input,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


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


def _put_ui_pair(
    root: Path,
    index: int,
    *,
    nodes: int = 1,
    image_bytes: bytes | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tree = "\n".join(
        json.dumps({
            "id": str(node),
            "parent": str(node - 1) if node else None,
            "class": "TextView",
            "text": f"private-node-{index}-{node}",
            "visible": True,
        })
        for node in range(nodes)
    ) + "\n"
    (root / f"uitree_{index}.jsonl").write_text(tree, encoding="utf-8")
    (root / f"image_{index}.png").write_bytes(
        image_bytes if image_bytes is not None else PNG_MAGIC + bytes(index)
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


def test_ui_profile_summarizes_pairs_and_media_without_content(
        tmp_path, monkeypatch, capsys):
    cfg = _make_cfg(tmp_path, modality="ui", on_index_conflict="skip")
    root = tmp_path / "in"
    _put_ui_pair(root, 1, nodes=2, image_bytes=PNG_MAGIC + b"a")
    _put_ui_pair(root, 2, nodes=1, image_bytes=PNG_MAGIC + b"012345")
    _put_ui_pair(root, 3, nodes=1, image_bytes=b"not-a-png")
    (root / "uitree_4.jsonl").write_text("{}\n", encoding="utf-8")
    _put_ui_pair(root, 5)
    (root / "image_5.jpg").write_bytes(JPEG_MAGIC + b"x")
    monkeypatch.setattr(
        "labelkit.common.runtime.llm_client.LLMClient.__init__",
        lambda *args, **kwargs: pytest.fail("profile must not construct LLMClient"),
    )
    monkeypatch.setattr(
        "labelkit.operators.emitter.Emitter.__init__",
        lambda *args, **kwargs: pytest.fail("profile must not construct Emitter"),
    )

    profile = profile_input(cfg, sample_limit=10)

    assert isinstance(profile, UIInputProfile)
    assert profile.sample_complete is True
    assert dataclasses.asdict(profile.pairing) == {
        "estimated_pairs": 3,
        "scanned_indices": 5,
        "sampled_pairs": 2,
        "bad_pairs": 1,
        "missing_pairs": 1,
        "index_conflicts": 1,
    }
    assert (profile.tree_nodes.minimum, profile.tree_nodes.maximum) == (1, 2)
    assert profile.tree_nodes.mean == 1.5
    assert profile.image_bytes.minimum == len(PNG_MAGIC + b"a")
    assert profile.image_bytes.maximum == len(PNG_MAGIC + b"012345")
    assert "private-node" not in repr(dataclasses.asdict(profile))
    assert capsys.readouterr() == ("", "")
    assert not (tmp_path / "out.jsonl").exists()
    assert not (tmp_path / "out.report.json").exists()
    assert not (tmp_path / "out.trace.jsonl").exists()


def test_ui_profile_marks_a_bounded_valid_pair_prefix_incomplete(tmp_path):
    cfg = _make_cfg(tmp_path, modality="ui")
    _put_ui_pair(tmp_path / "in", 1)
    _put_ui_pair(tmp_path / "in", 2)

    profile = profile_input(cfg, sample_limit=1)

    assert isinstance(profile, UIInputProfile)
    assert profile.pairing.sampled_pairs == 1
    assert profile.pairing.scanned_indices == 1
    assert profile.sample_complete is False


def test_text_stream_profile_uses_real_session_and_time_semantics(
        tmp_path, caplog, capsys):
    base = _make_cfg(tmp_path)
    cfg = replace(
        base,
        segment=SegmentConfig(enabled=True, strategy="rules"),
        stream=StreamConfig(
            order_by="meta:ts", key=("meta:device",), gap_s=30,
            on_disorder="skip",
        ),
    )
    _write(tmp_path / "in" / "events.jsonl", [
        {"text": "secret-a", "ts": 0, "device": "a"},
        {"text": "secret-b", "ts": 10, "device": "a"},
        {"missing": "text", "ts": 11, "device": "a"},
        {"text": "secret-disorder", "ts": 5, "device": "a"},
        {"text": "secret-c", "ts": 100, "device": "a"},
        {"text": "secret-d", "ts": 101, "device": "b"},
    ])

    profile = profile_input(cfg, sample_limit=10)

    assert isinstance(profile, StreamInputProfile)
    assert profile.modality == "text"
    assert (profile.estimated_frames, profile.scanned_inputs) == (6, 6)
    assert (profile.sampled_frames, profile.bad_input, profile.disorder) == (4, 2, 1)
    assert profile.sample_complete is True
    assert profile.session_count == 3
    assert profile.session_lengths.minimum == 1
    assert profile.session_lengths.maximum == 2
    assert profile.close_causes == {
        "gap": 1, "key": 1, "max_len": 0, "max_span": 0,
        "eof": 1, "limit": 0,
    }
    assert profile.time_range.order_by == "meta:ts"
    assert profile.time_range.parsed_frames == 4
    assert profile.time_range.minimum_epoch_s == 0
    assert profile.time_range.maximum_epoch_s == 101
    assert profile.time_range.span_s == 101
    assert profile.pairing is None
    assert profile.tree_nodes is None and profile.image_bytes is None
    assert "secret" not in repr(dataclasses.asdict(profile))
    assert not [record for record in caplog.records if record.levelname == "WARNING"]
    assert capsys.readouterr() == ("", "")


def test_stream_profile_is_bounded_and_reports_limit_without_warning(
        tmp_path, caplog):
    base = _make_cfg(tmp_path)
    cfg = replace(base, segment=SegmentConfig(enabled=True, strategy="rules"))
    _write(tmp_path / "in" / "events.jsonl", [
        {"text": str(index)} for index in range(5)
    ])

    profile = profile_input(cfg, sample_limit=3)

    assert profile.sample_complete is False
    assert (profile.scanned_inputs, profile.sampled_frames) == (3, 3)
    assert profile.close_causes["limit"] == 1
    assert profile.close_causes["eof"] == 0
    assert not [record for record in caplog.records if record.levelname == "WARNING"]


def test_stream_profile_normalizes_exact_sample_exhaustion_to_eof(tmp_path):
    base = _make_cfg(tmp_path)
    cfg = replace(base, segment=SegmentConfig(enabled=True, strategy="rules"))
    _write(tmp_path / "in" / "events.jsonl", [{"text": "a"}, {"text": "b"}])

    profile = profile_input(cfg, sample_limit=2)

    assert profile.sample_complete is True
    assert profile.close_causes["limit"] == 0
    assert profile.close_causes["eof"] == 1


def test_ui_stream_profile_keeps_pairing_and_media_summaries(tmp_path):
    base = _make_cfg(tmp_path, modality="ui")
    cfg = replace(base, segment=SegmentConfig(enabled=True, strategy="rules"))
    _put_ui_pair(tmp_path / "in", 1, nodes=1)
    _put_ui_pair(tmp_path / "in", 2, nodes=3)

    profile = profile_input(cfg, sample_limit=10)

    assert isinstance(profile, StreamInputProfile)
    assert profile.modality == "ui"
    assert profile.pairing.sampled_pairs == 2
    assert profile.session_count == 1
    assert profile.close_causes["eof"] == 1
    assert (profile.tree_nodes.minimum, profile.tree_nodes.maximum) == (1, 3)
    assert (profile.image_bytes.minimum, profile.image_bytes.maximum) == (9, 10)
    assert profile.time_range is None


def test_profile_input_dispatches_text_and_rejects_generate_only(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write(tmp_path / "in" / "data.jsonl", [{"text": "safe"}])
    assert isinstance(profile_input(cfg), TextInputProfile)

    gen_cfg = replace(
        cfg,
        run=RunConfig(output=cfg.run.output, modality="text", mode="generate_only"),
    )
    with pytest.raises(ValueError, match="process mode"):
        profile_input(gen_cfg)


@pytest.mark.parametrize("sample_limit", [0, 10_001])
def test_profile_input_rejects_unbounded_sample_limits(tmp_path, sample_limit):
    with pytest.raises(ValueError, match="sample_limit"):
        profile_input(_make_cfg(tmp_path, modality="ui"), sample_limit=sample_limit)
