"""Filesystem tests for the owned Agent workspace lifecycle."""

from __future__ import annotations

import dataclasses
import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from labelkit.agent import (
    AgentWorkspace,
    PathPolicy,
    ToolCall,
    ToolPathRule,
    ToolRegistry,
    ToolRouter,
    ToolSpec,
    WorkspaceError,
    WorkspaceExistsError,
    WorkspaceIntegrityError,
)

workspace_module = importlib.import_module("labelkit.agent.workspace")


def _hidden_artifacts(parent):
    return sorted(path.name for path in parent.iterdir() if path.name.startswith("."))


def test_create_publishes_only_the_owned_layout_and_metadata(tmp_path):
    sentinel = tmp_path / "project.toml"
    sentinel.write_text("[run]\n", encoding="utf-8")

    workspace = AgentWorkspace.create(tmp_path, "run-001")

    assert workspace.root == (tmp_path / "out" / "agent" / "run-001").resolve()
    assert workspace.source_dir == workspace.root / "source"
    assert workspace.candidates_dir == workspace.root / "candidates"
    assert set(path.name for path in workspace.root.iterdir()) == {
        ".workspace.json", "source", "candidates",
    }
    assert json.loads((workspace.root / ".workspace.json").read_text("utf-8")) == {
        "agent_run_id": "run-001",
        "kind": "agent_workspace",
        "schema_version": 1,
    }
    assert sentinel.read_text(encoding="utf-8") == "[run]\n"
    assert set(path.name for path in tmp_path.iterdir()) == {"project.toml", "out"}
    assert _hidden_artifacts(tmp_path / "out" / "agent") == []


def test_open_returns_the_same_verified_workspace_without_writing(tmp_path):
    created = AgentWorkspace.create(tmp_path, "run-001")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    opened = AgentWorkspace.open(tmp_path, "run-001")

    assert opened == created
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_workspace_root_is_the_exact_path_policy_write_grant(tmp_path):
    workspace = AgentWorkspace.create(tmp_path, "run-001")
    registry = ToolRegistry()
    calls = []
    registry.register(ToolSpec(
        "write_tool", "Workspace policy integration test.", "R1",
        {"type": "object", "properties": {"output_path": {"type": "string"}},
         "required": ["output_path"], "additionalProperties": False},
        {"type": "object", "properties": {"ok": {"const": True}},
         "required": ["ok"], "additionalProperties": False},
    ), lambda arguments: calls.append(arguments) or {"ok": True})
    policy = PathPolicy(
        base_dir=tmp_path,
        allowed_write_root=workspace.root,
        rules={"write_tool": ToolPathRule(write_arguments=("output_path",))},
    )
    router = ToolRouter(registry, policies=(policy,))

    allowed = router.route(ToolCall(
        "call-1", "write_tool", {"output_path": "out/agent/run-001/result.json"},
        "idem-1"))
    denied = router.route(ToolCall(
        "call-2", "write_tool", {"output_path": "out/agent/run-002/result.json"},
        "idem-2"))

    assert allowed.status == "success"
    assert calls == [{"output_path": str((workspace.root / "result.json").resolve())}]
    assert denied.error.details == {"rule": "write_scope"}


def test_create_candidate_publishes_and_reopens_owned_run_directory(tmp_path):
    workspace = AgentWorkspace.create(tmp_path, "run-001")

    candidate = workspace.create_candidate("candidate-000")
    reopened = workspace.open_candidate("candidate-000")

    assert reopened == candidate
    assert candidate.root == workspace.candidates_dir / "candidate-000"
    assert candidate.run_dir == candidate.root / "run"
    assert set(path.name for path in candidate.root.iterdir()) == {
        ".candidate.json", "run",
    }
    assert json.loads((candidate.root / ".candidate.json").read_text("utf-8")) == {
        "agent_run_id": "run-001",
        "candidate_id": "candidate-000",
        "kind": "agent_candidate",
        "schema_version": 1,
    }


def test_existing_workspace_and_candidate_are_never_overwritten(tmp_path):
    workspace = AgentWorkspace.create(tmp_path, "run-001")
    marker = workspace.root / ".workspace.json"
    original = marker.read_bytes()
    workspace.create_candidate("candidate-000")

    with pytest.raises(WorkspaceExistsError):
        AgentWorkspace.create(tmp_path, "run-001")
    with pytest.raises(WorkspaceExistsError):
        workspace.create_candidate("candidate-000")

    assert marker.read_bytes() == original
    assert _hidden_artifacts(tmp_path / "out" / "agent") == []
    assert _hidden_artifacts(workspace.candidates_dir) == []


@pytest.mark.parametrize("run_id", [
    "", ".hidden", "../escape", "nested/run", "run.name", "white space", "x" * 65,
])
def test_invalid_run_identity_is_rejected_before_any_write(tmp_path, run_id):
    with pytest.raises(ValueError, match="agent_run_id"):
        AgentWorkspace.create(tmp_path, run_id)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("candidate_id", [
    "candidate-1", "candidate-0000000", "Candidate-001", "../candidate-001",
    "candidate-001/run", ".candidate-001",
])
def test_invalid_candidate_identity_is_rejected_without_artifacts(tmp_path, candidate_id):
    workspace = AgentWorkspace.create(tmp_path, "run-001")

    with pytest.raises(ValueError, match="candidate_id"):
        workspace.create_candidate(candidate_id)

    assert list(workspace.candidates_dir.iterdir()) == []


def test_missing_or_non_directory_project_root_is_rejected(tmp_path):
    missing = tmp_path / "missing"
    regular_file = tmp_path / "file"
    regular_file.write_text("x", encoding="utf-8")

    with pytest.raises(WorkspaceIntegrityError):
        AgentWorkspace.create(missing, "run-001")
    with pytest.raises(WorkspaceIntegrityError):
        AgentWorkspace.create(regular_file, "run-001")
    with pytest.raises(WorkspaceIntegrityError):
        AgentWorkspace.open(tmp_path, "run-001")

    assert not missing.exists()
    assert not (tmp_path / "out").exists()


def test_forbidden_project_component_is_rejected(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    with pytest.raises(WorkspaceIntegrityError, match="forbidden"):
        AgentWorkspace.create(git_dir, "run-001")

    assert not (git_dir / "out").exists()


def test_non_directory_out_path_is_not_replaced(tmp_path):
    out = tmp_path / "out"
    out.write_text("owned", encoding="utf-8")

    with pytest.raises(WorkspaceError):
        AgentWorkspace.create(tmp_path, "run-001")

    assert out.read_text(encoding="utf-8") == "owned"


def test_initialization_failure_leaves_no_final_or_private_artifacts(tmp_path, monkeypatch):
    agent_root = tmp_path / "out" / "agent"
    unrelated = agent_root / ".unrelated.part"

    def fail_write(path, metadata):
        unrelated.mkdir(exist_ok=True)
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(workspace_module, "_write_metadata", fail_write)

    with pytest.raises(WorkspaceError, match="initialize"):
        AgentWorkspace.create(tmp_path, "run-001")

    assert not (agent_root / "run-001").exists()
    assert unrelated.is_dir()
    assert _hidden_artifacts(agent_root) == [".unrelated.part"]


def test_publish_failure_cleans_staging_and_claim(tmp_path, monkeypatch):
    def fail_publish(staging, target):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(workspace_module, "_publish_directory", fail_publish)

    with pytest.raises(WorkspaceError, match="initialize"):
        AgentWorkspace.create(tmp_path, "run-001")

    agent_root = tmp_path / "out" / "agent"
    assert list(agent_root.iterdir()) == []


def test_publish_never_replaces_a_target_created_during_initialization(tmp_path, monkeypatch):
    original = workspace_module._write_metadata

    def racing_write(path, metadata):
        original(path, metadata)
        target = tmp_path / "out" / "agent" / "run-001"
        target.mkdir()
        (target / "external.txt").write_text("owned", encoding="utf-8")

    monkeypatch.setattr(workspace_module, "_write_metadata", racing_write)

    with pytest.raises(WorkspaceExistsError):
        AgentWorkspace.create(tmp_path, "run-001")

    agent_root = tmp_path / "out" / "agent"
    assert (agent_root / "run-001" / "external.txt").read_text("utf-8") == "owned"
    assert _hidden_artifacts(agent_root) == []


def test_final_workspace_is_invisible_until_fully_initialized(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    original = workspace_module._write_metadata

    def blocking_write(path, metadata):
        started.set()
        assert release.wait(timeout=10)
        original(path, metadata)

    monkeypatch.setattr(workspace_module, "_write_metadata", blocking_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(AgentWorkspace.create, tmp_path, "run-001")
        assert started.wait(timeout=10)
        agent_root = tmp_path / "out" / "agent"
        assert not (agent_root / "run-001").exists()
        assert any(path.name.endswith(".part") for path in agent_root.iterdir())
        assert (agent_root / ".run-001.claim").is_file()
        release.set()
        workspace = future.result(timeout=10)

    assert workspace.root.is_dir()
    assert (workspace.root / ".workspace.json").is_file()
    assert _hidden_artifacts(agent_root) == []


def test_concurrent_same_run_identity_has_exactly_one_winner(tmp_path):
    def attempt(_):
        try:
            return AgentWorkspace.create(tmp_path, "run-001")
        except WorkspaceExistsError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(20)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert AgentWorkspace.open(tmp_path, "run-001") == winners[0]
    assert _hidden_artifacts(tmp_path / "out" / "agent") == []


def test_concurrent_distinct_candidates_are_all_published(tmp_path):
    workspace = AgentWorkspace.create(tmp_path, "run-001")
    identities = [f"candidate-{number:03d}" for number in range(20)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        candidates = list(pool.map(workspace.create_candidate, identities))

    assert {candidate.candidate_id for candidate in candidates} == set(identities)
    assert {path.name for path in workspace.candidates_dir.iterdir()} == set(identities)
    assert all(candidate.run_dir.is_dir() for candidate in candidates)


@pytest.mark.parametrize("mutation", ["missing_marker", "bad_json", "wrong_owner", "large"])
def test_open_rejects_invalid_workspace_ownership_marker(tmp_path, mutation):
    workspace = AgentWorkspace.create(tmp_path, "run-001")
    marker = workspace.root / ".workspace.json"
    if mutation == "missing_marker":
        marker.unlink()
    elif mutation == "bad_json":
        marker.write_text("{", encoding="utf-8")
    elif mutation == "wrong_owner":
        marker.write_text('{"agent_run_id":"other"}', encoding="utf-8")
    else:
        marker.write_bytes(b"x" * 4097)

    with pytest.raises(WorkspaceIntegrityError):
        AgentWorkspace.open(tmp_path, "run-001")


def test_open_rejects_missing_or_replaced_owned_directory(tmp_path):
    workspace = AgentWorkspace.create(tmp_path, "run-001")
    workspace.source_dir.rmdir()
    workspace.source_dir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(WorkspaceIntegrityError):
        AgentWorkspace.open(tmp_path, "run-001")


def test_forged_workspace_fields_cannot_redirect_candidate_writes(tmp_path):
    workspace = AgentWorkspace.create(tmp_path, "run-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    forged = dataclasses.replace(
        workspace, root=outside, source_dir=outside / "source",
        candidates_dir=outside / "candidates")

    with pytest.raises(WorkspaceIntegrityError):
        forged.create_candidate("candidate-000")

    assert list(outside.iterdir()) == []


def test_linked_out_parent_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    out = tmp_path / "out"
    try:
        out.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(WorkspaceIntegrityError):
        AgentWorkspace.create(tmp_path, "run-001")

    assert list(outside.iterdir()) == []


def test_output_link_gate_runs_before_the_first_output_write(tmp_path, monkeypatch):
    out = tmp_path / "out"
    original = workspace_module._has_link_component

    def report_link(path):
        return path == out or original(path)

    monkeypatch.setattr(workspace_module, "_has_link_component", report_link)

    with pytest.raises(WorkspaceIntegrityError, match="output parent"):
        AgentWorkspace.create(tmp_path, "run-001")

    assert not out.exists()


def test_linked_candidate_container_is_rejected(tmp_path):
    workspace = AgentWorkspace.create(tmp_path, "run-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace.candidates_dir.rmdir()
    try:
        workspace.candidates_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(WorkspaceIntegrityError):
        workspace.create_candidate("candidate-000")

    assert list(outside.iterdir()) == []


def test_existing_claim_blocks_creation_without_touching_it(tmp_path):
    agent_root = tmp_path / "out" / "agent"
    agent_root.mkdir(parents=True)
    claim = agent_root / ".run-001.claim"
    claim.write_text("external owner", encoding="utf-8")

    with pytest.raises(WorkspaceExistsError, match="being created"):
        AgentWorkspace.create(tmp_path, "run-001")

    assert claim.read_text(encoding="utf-8") == "external owner"
    assert not (agent_root / "run-001").exists()
