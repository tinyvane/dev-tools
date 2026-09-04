from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from portablecodex import paths


@dataclass(frozen=True)
class ContextConfig:
    sessions_dir: str | None = None
    transport_root: str | None = None


@dataclass(frozen=True)
class Config:
    portable_root: str | None = None
    context: ContextConfig | None = None


def _optional_string(table: dict[str, object], name: str) -> str | None:
    value = table.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _parse(path: Path) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    portable = raw.get("portable")
    if portable is not None and not isinstance(portable, dict):
        raise TypeError("[portable] must be a TOML table")
    context = raw.get("context")
    if context is not None and not isinstance(context, dict):
        raise TypeError("[context] must be a TOML table")
    return Config(
        portable_root=(
            _optional_string(portable, "root") if isinstance(portable, dict) else None
        ),
        context=(
            ContextConfig(
                sessions_dir=_optional_string(context, "sessions_dir"),
                transport_root=_optional_string(context, "transport_root"),
            )
            if isinstance(context, dict) else None
        ),
    )


def load(*, include_legacy: bool = True) -> Config:
    path = paths.config_file()
    if path.is_file():
        return _parse(path)
    if include_legacy:
        legacy = paths.legacy_codesync_config_file()
        if legacy.is_file():
            legacy_config = _parse(legacy)
            return Config(context=legacy_config.context)
    return Config()


def load_context_config() -> ContextConfig | None:
    return load().context


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _to_toml(config: Config) -> str:
    lines: list[str] = []
    if config.portable_root:
        lines.extend(["[portable]", f"root = {_toml_string(config.portable_root)}", ""])
    if config.context:
        lines.append("[context]")
        if config.context.sessions_dir:
            lines.append(
                f"sessions_dir = {_toml_string(config.context.sessions_dir)}"
            )
        if config.context.transport_root:
            lines.append(
                f"transport_root = {_toml_string(config.context.transport_root)}"
            )
        lines.append("")
    return "\n".join(lines)


def save(config: Config) -> None:
    path = paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_to_toml(config))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def remember_root(root: str) -> None:
    current = load()
    save(Config(portable_root=root, context=current.context))
