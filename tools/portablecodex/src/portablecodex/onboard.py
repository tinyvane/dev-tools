from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Literal

from portablecodex import config, output, portable


OnboardMode = Literal["connect", "initialize"]


def _registration(layout: portable.PortableLayout) -> dict[str, object] | None:
    if not layout.registration.is_file():
        return None
    value = json.loads(layout.registration.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("portable registration must be a JSON object")
    return value


def _recommended_mode(registration: dict[str, object] | None) -> OnboardMode:
    if registration is None:
        return "initialize"
    if registration.get("status") == "complete" and registration.get("mode") == "dual":
        return "connect"
    return "initialize"


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirmed(prompt: str, input_fn: Callable[[str], str]) -> bool:
    try:
        answer = input_fn(prompt).strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes", "是"}


def _print_inventory(
    layout: portable.PortableLayout,
    source_home: Path,
    registration: dict[str, object] | None,
    mode: OnboardMode,
) -> None:
    output.section("PortableCodex onboarding")
    sessions_dir = source_home / "sessions"
    try:
        session_count = sum(
            1 for path in sessions_dir.glob("*/*/*/rollout-*.jsonl")
            if path.is_file()
        ) if sessions_dir.is_dir() else 0
    except OSError:
        session_count = -1
    memory_present = (source_home / "memories").is_dir()
    output.info(f"本机 Codex: {source_home}")
    output.detail("将继续作为不依赖 V: 的 LOCAL fallback")
    output.info(
        "本机历史:  "
        f"{'无法读取' if session_count < 0 else f'{session_count} 个 session'}；"
        f"memory {'存在' if memory_present else '未发现'}"
    )
    output.info(f"移动工作区: {layout.root}")
    output.info(
        f"V: 状态:   {registration.get('status') if registration else '尚未初始化'}"
    )
    if registration and registration.get("volume_unique_id"):
        output.detail(f"登记设备: {registration['volume_unique_id']}")
    output.info(f"推荐操作:  {mode}")
    if mode == "connect":
        output.detail("连接已有 V:；保留本机历史，不导入或合并本机 SQLite")
    else:
        output.detail("从这台 PC 的权威本机状态创建第一份 V: workspace")


def run_onboard(
    *,
    root: str,
    mode: OnboardMode | None,
    execute: bool,
    source_home: str | None = None,
    sessions_source: str | None = None,
    interactive: bool | None = None,
    input_fn: Callable[[str], str] = input,
) -> int:
    layout = portable.PortableLayout.from_root(root)
    local_home = (
        Path(source_home).expanduser().absolute()
        if source_home else portable._default_source_home().absolute()
    )
    try:
        portable._validate_layout(layout)
        registration = _registration(layout)
        recommended = _recommended_mode(registration)
        selected = mode or recommended
        _print_inventory(layout, local_home, registration, selected)

        if selected == "connect":
            if registration is None:
                raise ValueError("cannot connect: portable workspace is not initialized")
            if registration.get("status") != "complete":
                raise ValueError(
                    f"cannot connect: portable phase is {registration.get('status')!r}"
                )
            if registration.get("mode") != "dual":
                raise ValueError("connect onboarding requires a completed dual workspace")
        elif registration is not None and registration.get("status") == "complete":
            raise ValueError(
                "refusing to initialize over an existing complete portable workspace"
            )

        tty = _is_interactive() if interactive is None else interactive
        if execute and mode is None and not tty:
            raise ValueError("non-interactive --execute requires --mode connect|initialize")
        should_execute = execute
        if not execute and tty:
            should_execute = _confirmed(
                f"现在执行 {selected}？输入 y 才会继续 [y/N]: ",
                input_fn,
            )
            if not should_execute:
                output.warn("未做任何修改。")
                return 0
        elif not execute:
            output.warn(
                "当前仅展示计划。请在交互式终端确认，或显式重跑："
                f" --mode {selected} --execute"
            )
            return 0

        if selected == "connect":
            result = portable.configure_portable_alias(
                str(layout.root), execute=True, remove=False,
            )
        else:
            if registration is None:
                result = portable.prepare_portable(
                    str(layout.root),
                    source_home=str(local_home),
                    sessions_source=sessions_source,
                )
                if result != 0:
                    return result
            result = portable.migrate_portable(
                str(layout.root), execute=True, mode="dual",
            )
            if result == 0:
                result = portable.configure_portable_alias(
                    str(layout.root), execute=True, remove=False,
                )
        if result != 0:
            return result
        config.remember_root(str(layout.root))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        output.err(f"Onboarding failed closed: {exc}")
        return 1

    output.good("当前 PC 已完成配置。")
    output.info("运行 `codexv` 使用 PORTABLE V:；普通 `codex` 仍使用 LOCAL C:。")
    return 0
