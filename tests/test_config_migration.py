"""Parse V1 config.local.ps1 → Config dataclass, then re-emit as TOML and parse back."""
from __future__ import annotations

import tomllib

from codesync.config import _to_toml, parse_v1_ps1


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
