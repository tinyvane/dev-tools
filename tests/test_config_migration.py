"""Parse V1 config.local.ps1 → Config dataclass, then re-emit as TOML and parse back."""
from __future__ import annotations

import tomllib

from codesync.config import _to_toml, filter_codesync_self_dirs, parse_v1_ps1


V1_MINIMAL = r'''
# Local config — gitignored.
$CodeRoots = @(
    "C:\Users\yiwang\SyncRepos"
)
'''

V1_FULL = r'''
$CodeRoots = @(
    "C:\Users\yiwang\SyncRepos"
    # "D:\projects"
    "$env:USERPROFILE\code"
)

$AutoClone = @{
    Owner            = 'tinyvane'
    Target           = "$env:USERPROFILE\SyncRepos"
    Skip             = @("private-repo", "tmp-repo")
    SkipConfirmation = $false
    AbortIfShrinkPct = 25
}

$DbSyncTargets = @(
    @{
        Name      = 'jx-perf'
        Container = 'jx-perf-mysql-dev'
        Database  = 'jx_perf'
        User      = 'jx_perf'
        Password  = 'dev_pwd'
        DumpFile  = 'D:\dropbox\db-sync\jx-perf.sql'
    }
    @{
        Name      = 'foo'
        Container = 'foo-mysql'
        Database  = 'foo'
        User      = 'foo'
        Password  = 'pw'
        DumpFile  = '~/Dropbox/foo.sql'
    }
)
'''


def _toml_roundtrip(cfg):
    """Emit cfg as TOML, parse it back as a dict."""
    return tomllib.loads(_to_toml(cfg))


def test_minimal_code_roots() -> None:
    cfg = parse_v1_ps1(V1_MINIMAL)
    assert cfg.code_roots == ["C:\\Users\\yiwang\\SyncRepos"]
    assert cfg.auto_clone is None
    assert cfg.db_sync == []

    parsed = _toml_roundtrip(cfg)
    assert parsed["code_roots"] == ["C:\\Users\\yiwang\\SyncRepos"]


def test_full_config() -> None:
    cfg = parse_v1_ps1(V1_FULL)

    # code_roots: should include the env-var-bearing path; commented one excluded.
    assert "C:\\Users\\yiwang\\SyncRepos" in cfg.code_roots
    assert "$env:USERPROFILE\\code" in cfg.code_roots
    assert "D:\\projects" not in cfg.code_roots

    # auto_clone
    assert cfg.auto_clone is not None
    assert cfg.auto_clone.owner == "tinyvane"
    assert cfg.auto_clone.target == "$env:USERPROFILE\\SyncRepos"
    assert cfg.auto_clone.skip == ["private-repo", "tmp-repo"]
    assert cfg.auto_clone.skip_confirmation is False
    assert cfg.auto_clone.abort_if_shrink_pct == 25

    # db_sync
    assert len(cfg.db_sync) == 2
    names = [d.name for d in cfg.db_sync]
    assert names == ["jx-perf", "foo"]

    jx = cfg.db_sync[0]
    assert jx.container == "jx-perf-mysql-dev"
    assert jx.dump_file == "D:\\dropbox\\db-sync\\jx-perf.sql"


def test_comments_dont_pollute() -> None:
    """Commented-out lines should be invisible to parser."""
    src = r'''
$CodeRoots = @(
    "C:\Users\real"
    # "C:\Users\commented-out"
)
'''
    cfg = parse_v1_ps1(src)
    assert cfg.code_roots == ["C:\\Users\\real"]


def test_emitted_toml_is_parseable() -> None:
    """The TOML we emit must be parseable by tomllib (including Windows paths)."""
    cfg = parse_v1_ps1(V1_FULL)
    parsed = _toml_roundtrip(cfg)

    assert "code_roots" in parsed
    assert isinstance(parsed["code_roots"], list)
    assert "auto_clone" in parsed
    assert parsed["auto_clone"]["owner"] == "tinyvane"
    assert "db_sync" in parsed
    assert len(parsed["db_sync"]) == 2


# ---------- filter_codesync_self_dirs ----------

def test_filter_keeps_normal_dirs(tmp_path) -> None:
    """Dirs without sync.ps1 or src/codesync are kept as-is."""
    normal = tmp_path / "SyncRepos"
    normal.mkdir()
    (normal / "some-other-repo").mkdir()

    kept, dropped = filter_codesync_self_dirs([str(normal)])
    assert kept == [str(normal)]
    assert dropped == []


def test_filter_drops_v1_dev_tools(tmp_path) -> None:
    """A directory containing sync.ps1 is the V1 codesync repo — drop it."""
    dev_tools = tmp_path / "dev-tools"
    dev_tools.mkdir()
    (dev_tools / "sync.ps1").write_text("# v1 sync script", encoding="utf-8")

    kept, dropped = filter_codesync_self_dirs([str(dev_tools)])
    assert kept == []
    assert dropped == [str(dev_tools)]


def test_filter_drops_v2_source_checkout(tmp_path) -> None:
    """A directory with src/codesync/__init__.py is a V2 source checkout — drop it."""
    dev_tools = tmp_path / "dev-tools"
    (dev_tools / "src" / "codesync").mkdir(parents=True)
    (dev_tools / "src" / "codesync" / "__init__.py").write_text("", encoding="utf-8")

    kept, dropped = filter_codesync_self_dirs([str(dev_tools)])
    assert kept == []
    assert dropped == [str(dev_tools)]


def test_filter_keeps_nonexistent_paths(tmp_path) -> None:
    """A path that doesn't exist on this machine could be a valid root on another;
    we don't second-guess — only drop entries we positively identify as codesync."""
    ghost = tmp_path / "does-not-exist"
    kept, dropped = filter_codesync_self_dirs([str(ghost)])
    assert kept == [str(ghost)]
    assert dropped == []


def test_filter_mixed_input(tmp_path) -> None:
    """Real-world case: dev-tools + SyncRepos. Drop only dev-tools."""
    dev_tools = tmp_path / "dev-tools"
    dev_tools.mkdir()
    (dev_tools / "sync.ps1").write_text("", encoding="utf-8")

    sync_repos = tmp_path / "SyncRepos"
    sync_repos.mkdir()

    kept, dropped = filter_codesync_self_dirs([str(dev_tools), str(sync_repos)])
    assert kept == [str(sync_repos)]
    assert dropped == [str(dev_tools)]


def test_filter_keeps_directory_just_named_dev_tools(tmp_path) -> None:
    """A directory called 'dev-tools' that ISN'T the codesync repo (no markers)
    must NOT be silently dropped — the user might have named a normal repo this."""
    fake = tmp_path / "dev-tools"
    fake.mkdir()
    (fake / "README.md").write_text("not the real codesync", encoding="utf-8")

    kept, dropped = filter_codesync_self_dirs([str(fake)])
    assert kept == [str(fake)]


# ---------- is_template_unedited ----------

def test_is_template_unedited_true_for_fresh_template(monkeypatch, tmp_path) -> None:
    """A config.toml whose contents match CONFIG_TEMPLATE byte-for-byte is "untouched"
    and the wizard should re-trigger on next sync."""
    from codesync import paths
    from codesync.config import CONFIG_TEMPLATE, is_template_unedited
    f = tmp_path / "config.toml"
    f.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: f)
    assert is_template_unedited() is True


def test_is_template_unedited_false_when_missing(monkeypatch, tmp_path) -> None:
    """File doesn't exist → False (the caller handles missing via a separate path)."""
    from codesync import paths
    from codesync.config import is_template_unedited
    monkeypatch.setattr(paths, "config_file", lambda: tmp_path / "nonexistent.toml")
    assert is_template_unedited() is False


def test_is_template_unedited_false_when_user_edited(monkeypatch, tmp_path) -> None:
    """Any change (even a single appended comment) → respect the edit, don't re-prompt."""
    from codesync import paths
    from codesync.config import CONFIG_TEMPLATE, is_template_unedited
    f = tmp_path / "config.toml"
    f.write_text(CONFIG_TEMPLATE + "# my note\n", encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: f)
    assert is_template_unedited() is False


def test_include_forks_defaults_true_when_missing_in_toml(monkeypatch, tmp_path) -> None:
    """Pre-v2.2.8 TOMLs (no include_forks field) load with include_forks=True.
    We pick True as the missing-field default because most personal users want
    fork repos auto-cloned (user's stated preference + opt-out is one config line)."""
    from codesync import paths
    from codesync.config import load
    f = tmp_path / "config.toml"
    f.write_text(
        "code_roots = ['~/SyncRepos']\n\n"
        "[auto_clone]\n"
        "owner               = 'me'\n"
        "target              = '~/SyncRepos'\n"
        "skip                = []\n"
        "skip_confirmation   = false\n"
        "abort_if_shrink_pct = 20\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "config_file", lambda: f)
    cfg = load()
    assert cfg.auto_clone is not None
    assert cfg.auto_clone.include_forks is True


def test_include_forks_explicit_false_in_toml(monkeypatch, tmp_path) -> None:
    """include_forks = false → keep pre-v2.2.8 behavior (skip forks)."""
    from codesync import paths
    from codesync.config import load
    f = tmp_path / "config.toml"
    f.write_text(
        "code_roots = ['~/SyncRepos']\n\n"
        "[auto_clone]\n"
        "owner               = 'me'\n"
        "target              = '~/SyncRepos'\n"
        "skip                = []\n"
        "skip_confirmation   = false\n"
        "abort_if_shrink_pct = 20\n"
        "include_forks       = false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "config_file", lambda: f)
    cfg = load()
    assert cfg.auto_clone is not None
    assert cfg.auto_clone.include_forks is False


def test_include_forks_round_trips_through_toml(monkeypatch, tmp_path) -> None:
    """_to_toml always emits include_forks so a generated TOML round-trips cleanly."""
    from codesync import paths
    from codesync.config import AutoCloneConfig, Config, _to_toml, load
    cfg = Config(
        code_roots=["~/SyncRepos"],
        auto_clone=AutoCloneConfig(owner="me", target="~/SyncRepos", include_forks=False),
    )
    f = tmp_path / "config.toml"
    f.write_text(_to_toml(cfg), encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: f)
    loaded = load()
    assert loaded.auto_clone is not None
    assert loaded.auto_clone.include_forks is False


def test_publish_config_round_trips(monkeypatch, tmp_path) -> None:
    """[publish] section round-trips through _to_toml + load."""
    from codesync import paths
    from codesync.config import Config, PublishConfig, _to_toml, load
    cfg = Config(
        code_roots=["~/SyncRepos"],
        publish=PublishConfig(skip=["tmp", "playground"], skip_confirmation=True),
    )
    f = tmp_path / "config.toml"
    f.write_text(_to_toml(cfg), encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: f)
    loaded = load()
    assert loaded.publish is not None
    assert loaded.publish.skip == ["tmp", "playground"]
    assert loaded.publish.skip_confirmation is True


def test_publish_config_absent_is_none(monkeypatch, tmp_path) -> None:
    """No [publish] section → cfg.publish is None (publish.py treats None as defaults)."""
    from codesync import paths
    from codesync.config import load
    f = tmp_path / "config.toml"
    f.write_text("code_roots = ['~/SyncRepos']\n", encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: f)
    loaded = load()
    assert loaded.publish is None


def test_commit_config_absent_defaults_enabled_skip_devtools(monkeypatch, tmp_path) -> None:
    """No [commit] section → auto-commit ON by default, dev-tools skipped."""
    from codesync import paths
    from codesync.config import load
    f = tmp_path / "config.toml"
    f.write_text("code_roots = ['~/SyncRepos']\n", encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: f)
    loaded = load()
    assert loaded.commit is not None
    assert loaded.commit.enabled is True
    assert loaded.commit.skip == ["dev-tools"]


def test_commit_config_explicit_disabled(monkeypatch, tmp_path) -> None:
    from codesync import paths
    from codesync.config import load
    f = tmp_path / "config.toml"
    f.write_text(
        "code_roots = ['~/SyncRepos']\n\n[commit]\nenabled = false\nskip = ['a', 'b']\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "config_file", lambda: f)
    loaded = load()
    assert loaded.commit.enabled is False
    assert loaded.commit.skip == ["a", "b"]


def test_commit_config_explicit_empty_skip_respected(monkeypatch, tmp_path) -> None:
    """Explicit skip = [] means auto-commit everything (don't force dev-tools back in)."""
    from codesync import paths
    from codesync.config import load
    f = tmp_path / "config.toml"
    f.write_text(
        "code_roots = ['~/SyncRepos']\n\n[commit]\nenabled = true\nskip = []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "config_file", lambda: f)
    loaded = load()
    assert loaded.commit.skip == []


def test_commit_config_present_no_skip_key_defaults_devtools(monkeypatch, tmp_path) -> None:
    """[commit] present but skip key omitted → default skip ["dev-tools"]."""
    from codesync import paths
    from codesync.config import load
    f = tmp_path / "config.toml"
    f.write_text("code_roots = ['~/SyncRepos']\n\n[commit]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: f)
    loaded = load()
    assert loaded.commit.skip == ["dev-tools"]


def test_commit_config_round_trips(monkeypatch, tmp_path) -> None:
    from codesync import paths
    from codesync.config import CommitConfig, Config, _to_toml, load
    cfg = Config(code_roots=["~/SyncRepos"],
                 commit=CommitConfig(enabled=False, skip=["x", "dev-tools"]))
    f = tmp_path / "config.toml"
    f.write_text(_to_toml(cfg), encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: f)
    loaded = load()
    assert loaded.commit.enabled is False
    assert loaded.commit.skip == ["x", "dev-tools"]


def test_is_template_unedited_false_after_wizard_writes(monkeypatch, tmp_path) -> None:
    """A wizard-generated config has different content from CONFIG_TEMPLATE → NOT flagged
    as untouched. User is set up; don't re-prompt."""
    from codesync import paths
    from codesync.config import is_template_unedited
    f = tmp_path / "config.toml"
    f.write_text(
        "code_roots = ['~/SyncRepos']\n\n[auto_clone]\nowner = 'real-user'\n"
        "target = '~/SyncRepos'\nskip = []\nskip_confirmation = false\n"
        "abort_if_shrink_pct = 20\n",
        encoding="utf-8"
    )
    monkeypatch.setattr(paths, "config_file", lambda: f)
    assert is_template_unedited() is False
