"""Fail-closed policies applied before an Agent tool is executed."""

from __future__ import annotations

import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Protocol

from labelkit.agent.tools.contracts import ToolCall, ToolError, ToolSpec

class ToolPolicy(Protocol):
    """A policy may normalize a call or deny it with a structured error."""

    def evaluate(self, spec: ToolSpec, call: ToolCall) -> ToolCall | ToolError: ...


@dataclass(frozen=True, slots=True)
class ToolPathRule:
    """Declare which top-level arguments contain read and write paths."""

    read_arguments: tuple[str, ...] = ()
    write_arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        read = _argument_names(self.read_arguments, "read_arguments")
        write = _argument_names(self.write_arguments, "write_arguments")
        if set(read) & set(write):
            raise ValueError("a path argument cannot be both read and write")
        object.__setattr__(self, "read_arguments", read)
        object.__setattr__(self, "write_arguments", write)


class PathPolicy:
    """Authorize declared paths and return canonical arguments.

    Directory read grants cover their subtree; other grants cover one exact path.
    Every registered tool needs an explicit rule, including pathless tools.
    """

    def __init__(
        self,
        *,
        base_dir: str | Path,
        allowed_reads: tuple[str | Path, ...] = (),
        allowed_write_root: str | Path | None = None,
        rules: Mapping[str, ToolPathRule],
    ) -> None:
        self._base_dir = _configuration_path(base_dir, Path.cwd())
        self._rules = dict(rules)
        if any(not isinstance(name, str) or not name for name in self._rules):
            raise ValueError("policy tool names must be non-empty strings")
        if any(not isinstance(rule, ToolPathRule) for rule in self._rules.values()):
            raise TypeError("path policy rules must be ToolPathRule instances")

        grants: list[tuple[Path, bool]] = []
        for value in allowed_reads:
            path = _configuration_path(value, self._base_dir)
            if _is_sensitive(path):
                raise ValueError("sensitive paths cannot be read grants")
            grants.append((path, path.is_dir()))
        self._allowed_reads = tuple(grants)

        if allowed_write_root is None:
            self._allowed_write_root = None
        else:
            write_root = _configuration_path(allowed_write_root, self._base_dir)
            if _is_sensitive(write_root):
                raise ValueError("sensitive paths cannot be write roots")
            self._allowed_write_root = write_root

    def evaluate(self, spec: ToolSpec, call: ToolCall) -> ToolCall | ToolError:
        rule = self._rules.get(spec.name)
        if rule is None:
            return _denied("undeclared_tool")

        prepared: dict[str, object] = dict(call.arguments)
        for argument in rule.read_arguments:
            error = self._prepare_argument(prepared, argument, write=False)
            if error is not None:
                return error
        for argument in rule.write_arguments:
            error = self._prepare_argument(prepared, argument, write=True)
            if error is not None:
                return error
        return replace(call, arguments=prepared)

    def _prepare_argument(
        self,
        arguments: dict[str, object],
        name: str,
        *,
        write: bool,
    ) -> ToolError | None:
        if name not in arguments:
            return None
        value = arguments[name]
        if not isinstance(value, str):
            return _denied("invalid_path_value")
        if _contains_parent_reference(value):
            return _denied("parent_traversal")

        try:
            raw = Path(value)
            absolute = raw if raw.is_absolute() else self._base_dir / raw
            if _has_link_component(absolute):
                return _denied("linked_path")
            canonical = absolute.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return _denied("invalid_path")

        if _is_sensitive(canonical):
            return _denied("sensitive_path")
        if write:
            if self._allowed_write_root is None or not _within(
                canonical, self._allowed_write_root
            ):
                return _denied("write_scope")
        elif not any(
            canonical == grant or (tree and _within(canonical, grant))
            for grant, tree in self._allowed_reads
        ):
            return _denied("read_scope")

        arguments[name] = str(canonical)
        return None


def _argument_names(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{field} must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _configuration_path(value: str | Path, base_dir: Path) -> Path:
    raw_value = str(value)
    if _contains_parent_reference(raw_value):
        raise ValueError("policy configuration paths cannot contain '..'")
    raw = Path(value)
    absolute = raw if raw.is_absolute() else base_dir / raw
    if _has_link_component(absolute):
        raise ValueError("policy configuration paths cannot contain links")
    try:
        return absolute.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("invalid policy configuration path") from exc


def _contains_parent_reference(value: str) -> bool:
    return ".." in value.replace("\\", "/").split("/")


def _has_link_component(path: Path) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            return True
        if getattr(metadata, "st_file_attributes", 0) & reparse_flag:
            return True
    return False


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _is_sensitive(path: Path) -> bool:
    exact_names = {
        ".git", ".ssh", ".aws", ".env", "mytips.md", "credentials.json", "secrets.json",
        "secrets.toml", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    }
    secret_suffixes = (".pem", ".key", ".p12", ".pfx")
    for component in path.parts:
        name = component.casefold().rstrip(" .").split(":", 1)[0]
        if (
            name in exact_names
            or name.startswith(".env.")
            or name.startswith("credentials.")
            or name.startswith("secrets.")
            or name.endswith(secret_suffixes)
        ):
            return True
    return False


def _denied(rule: str) -> ToolError:
    return ToolError(
        kind="policy_denied",
        message="path access denied by policy",
        details={"rule": rule},
    )


__all__ = ["PathPolicy", "ToolPathRule", "ToolPolicy"]
