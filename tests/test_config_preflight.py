"""Startup configuration validation and interactive code-root repair tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from codesync import cli, config, paths, wizard


def _patch_config_path(monkeypatch, tmp_path: Path) -> Path:
    config_dir = tmp_path / ".config" / "codesync"
    config_file = config_dir / "config.toml"
    monkeypatch.setattr(paths, "config_dir", lambda: config_dir)
    monkeypatch.setattr(paths, "ensure_config_dir", lambda: config_dir.mkdir(
        parents=True, exist_ok=True,
    ) or config_dir)
    monkeypatch.setattr(paths, "config_file", lambda: config_file)
    return config_file


def test_code_root_problems_cover_empty_missing_file_and_valid(tmp_path) -> None:
    valid = tmp_path / "valid"
    valid.mkdir()
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("x", encoding="utf-8")
    missing = tmp_path / "missing"

    assert config.code_root_problems(config.Config()) == [
        config.CodeRootProblem(None, None, "没有配置任何 code_roots")
    ]

    problems = config.code_root_problems(config.Config(code_roots=[
        str(valid), str(missing), str(regular_file), "", "bad\0path",
    ]))
    assert [(p.configured, p.reason) for p in problems[:3]] == [
        (str(missing), "目录不存在"),
        (str(regular_file), "路径不是目录"),
        ("", "路径为空或不是字符串"),
    ]
    assert problems[3].configured == "bad\0path"
    assert problems[3].reason.startswith("目录不可访问:")


def test_save_is_atomic_backs_up_and_preserves_complete_schema(
    monkeypatch, tmp_path,
) -> None:
    config_file = _patch_config_path(monkeypatch, tmp_path)
    config_file.parent.mkdir(parents=True)
    original = "code_roots = ['old']\n# keep me in the backup\n"
    config_file.write_text(original, encoding="utf-8")
    root = tmp_path / "repos"
    root.mkdir()

    cfg = config.Config(
        code_roots=[str(root)],
        auto_clone=config.AutoCloneConfig(
            owner="tinyvane",
            target=str(root),
            abort_if_local_missing_pct=73,
            include_forks=False,
        ),
        update=config.UpdateConfig(check=False, block_if_outdated=False, ttl_hours=7),
        submodules=config.SubmodulesConfig(recurse=False, skip=["vendor"]),
        sync=config.SyncConfig(net_workers=3, local_workers=9, stall_seconds=222),
    )

    backup = config.save(cfg, backup=True)
    assert backup is not None
    assert backup.read_text(encoding="utf-8") == original
    assert not list(config_file.parent.glob("config.toml.tmp-*"))

    loaded = config.load()
    assert loaded.code_roots == [str(root)]
    assert loaded.auto_clone.abort_if_local_missing_pct == 73
    assert loaded.auto_clone.include_forks is False
    assert loaded.update.check is False
    assert loaded.update.block_if_outdated is False
    assert loaded.update.ttl_hours == 7
    assert loaded.submodules.recurse is False
    assert loaded.submodules.skip == ["vendor"]
    assert loaded.sync.net_workers == 3
    assert loaded.sync.local_workers == 9
    assert loaded.sync.stall_seconds == 222


def test_save_failure_keeps_original_and_removes_temporary_file(
    monkeypatch, tmp_path,
) -> None:
    config_file = _patch_config_path(monkeypatch, tmp_path)
    config_file.parent.mkdir(parents=True)
    original = "code_roots = ['old']\n"
    config_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        config.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        config.save(config.Config(code_roots=[str(tmp_path)]), backup=True)

    assert config_file.read_text(encoding="utf-8") == original
    assert config_file.with_name("config.toml.bak").read_text(
        encoding="utf-8"
    ) == original
    assert not list(config_file.parent.glob("config.toml.tmp-*"))


def test_interactive_repair_replaces_roots_and_preserves_other_settings(
    monkeypatch, tmp_path,
) -> None:
    config_file = _patch_config_path(monkeypatch, tmp_path)
    config_file.parent.mkdir(parents=True)
    missing = tmp_path / "old-repos"
    replacement = tmp_path / "new-repos"
    replacement.mkdir()
    cfg = config.Config(
        code_roots=[str(missing)],
        auto_clone=config.AutoCloneConfig(
            owner="tinyvane",
            target=str(missing),
            skip=["private"],
            abort_if_local_missing_pct=66,
        ),
        commit=config.CommitConfig(enabled=False, skip=["handmade"]),
        sync=config.SyncConfig(countdown_seconds=3, cleanup_stale_packs=False),
    )
    original = config._to_toml(cfg)
    config_file.write_text(original, encoding="utf-8")
    answers = iter(["y", str(replacement)])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert wizard.repair_code_roots(cfg, config.code_root_problems(cfg)) is True

    repaired = config.load()
    assert repaired.code_roots == [str(replacement)]
    assert repaired.auto_clone.target == str(replacement)
    assert repaired.auto_clone.skip == ["private"]
    assert repaired.auto_clone.abort_if_local_missing_pct == 66
    assert repaired.commit.enabled is False
    assert repaired.commit.skip == ["handmade"]
    assert repaired.sync.countdown_seconds == 3
    assert repaired.sync.cleanup_stale_packs is False
    assert config_file.with_name("config.toml.bak").read_text(
        encoding="utf-8"
    ) == original


def test_interactive_repair_defaults_to_no(monkeypatch, tmp_path) -> None:
    config_file = _patch_config_path(monkeypatch, tmp_path)
    config_file.parent.mkdir(parents=True)
    cfg = config.Config(code_roots=[str(tmp_path / "missing")])
    original = config._to_toml(cfg)
    config_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    assert wizard.repair_code_roots(cfg, config.code_root_problems(cfg)) is False
    assert config_file.read_text(encoding="utf-8") == original
    assert not config_file.with_name("config.toml.bak").exists()


def test_noninteractive_preflight_fails_before_ssh_or_repo_work(
    monkeypatch, tmp_path, capsys,
) -> None:
    config_file = _patch_config_path(monkeypatch, tmp_path)
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        config._to_toml(config.Config(code_roots=[str(tmp_path / "missing")])),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "configure_github_ssh_over_443", lambda: None)
    monkeypatch.setattr(cli, "configure_http_stall_detection", lambda: None)
    monkeypatch.setattr(cli, "_can_prompt", lambda: False)
    monkeypatch.setattr(
        cli,
        "_configure_ssh_if_needed",
        lambda _args: (_ for _ in ()).throw(AssertionError("SSH must not start")),
    )
    monkeypatch.setattr("codesync.updater.report_pending_update", lambda: None)

    assert cli.main(["sync", "--status"]) == 2
    output = capsys.readouterr().out
    assert "启动配置检查" in output
    assert "目录不存在" in output
    assert "未执行任何仓库或网络操作" in output


def test_interactive_preflight_repairs_then_allows_command(
    monkeypatch, tmp_path,
) -> None:
    config_file = _patch_config_path(monkeypatch, tmp_path)
    config_file.parent.mkdir(parents=True)
    missing = tmp_path / "moved-from"
    replacement = tmp_path / "moved-to"
    replacement.mkdir()
    cfg = config.Config(
        code_roots=[str(missing)],
        auto_clone=config.AutoCloneConfig(owner="tinyvane", target=str(missing)),
    )
    config_file.write_text(config._to_toml(cfg), encoding="utf-8")
    answers = iter(["y", str(replacement)])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(cli, "_can_prompt", lambda: True)
    args = cli._build_parser().parse_args(["sync", "--status"])

    assert cli._ensure_runtime_config(args) is True
    repaired = config.load()
    assert repaired.code_roots == [str(replacement)]
    assert repaired.auto_clone.target == str(replacement)


def test_config_free_commands_bypass_root_preflight(monkeypatch) -> None:
    parser = cli._build_parser()
    for argv in (["--version"], ["--update"], ["init"], ["config-path"], []):
        assert cli._uses_code_roots(parser.parse_args(argv)) is False

    for argv in (
        ["sync"], ["pull"], ["push"], ["fork-setup"],
        ["rename", "a", "b"], ["delete", "x"], ["trash", "list"],
    ):
        assert cli._uses_code_roots(parser.parse_args(argv)) is True
