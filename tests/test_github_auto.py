"""Tests for github_auto.run() archive/clone decision logic.

Focus on the two v2.6.2 safety fixes:
  1. `known` is seeded from locally-present repos only — never from all active
     GitHub repos (the mass-archive root cause).
  2. A mass-archive guard aborts when a large fraction of should-be-local repos
     are missing locally (misconfigured code_roots / failed scan).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import codesync.github_auto as ga
from codesync import auth
from codesync.config import AutoCloneConfig


def _repo(name: str, *, fork: bool = False, archived: bool = False, owner: str = "me") -> dict:
    return {
        "name": name,
        "isFork": fork,
        "isArchived": archived,
        "sshUrl": f"git@github.com:{owner}/{name}.git",
        "owner": {"login": owner},
    }


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Patch all of run()'s side-effecting helpers; return a state dict the test
    fills in (gh repos, local repos, known) and reads back (archived, saved)."""
    state = {
        "gh": [],          # list[dict] as returned by _gh_repo_list
        "local": [],       # names found locally
        "known": None,     # list[str] or None (None = first run)
        "tombstones": {},  # name -> iso ts, as read by _read_tombstones
        "archived": [],    # names passed to _gh_repo_archive
        "saved": None,     # known names passed to _save_state
        "saved_tombstones": None,  # tombstones passed to _save_state
        "removed": [],     # paths passed to _rmtree
        "dirty": set(),    # names _repo_dirty reports True for
        "ahead": set(),    # names _repo_ahead reports >0 for
        "cloned": [],      # names git-cloned
    }
    monkeypatch.setattr(auth, "ensure_gh_authenticated", lambda: True)
    monkeypatch.setattr(ga, "_gh_repo_list", lambda owner: state["gh"])

    def fake_local(roots, owner):
        removed_names = {Path(p).name for p in state["removed"]}
        return {n: tmp_path / n for n in state["local"] if n not in removed_names}

    monkeypatch.setattr(ga, "_local_repos_by_owner", fake_local)
    monkeypatch.setattr(ga, "_read_known", lambda: state["known"])
    monkeypatch.setattr(ga, "_read_tombstones", lambda: dict(state["tombstones"]))
    monkeypatch.setattr(
        ga, "_save_state",
        lambda names, tombstones=None: (
            state.__setitem__("saved", list(names)),
            state.__setitem__("saved_tombstones", dict(tombstones or {})),
        ),
    )
    monkeypatch.setattr(
        ga, "_gh_repo_archive",
        lambda owner, name: (state["archived"].append(name), True)[1],
    )
    monkeypatch.setattr(
        ga, "_rmtree", lambda path: (state["removed"].append(str(path)), (True, ""))[1],
    )
    monkeypatch.setattr(ga, "_repo_dirty", lambda p: Path(p).name in state["dirty"])
    monkeypatch.setattr(ga, "_repo_ahead", lambda p: 1 if Path(p).name in state["ahead"] else 0)

    # Fake out `git clone` so to_clone doesn't hit the network.
    real_run = ga.subprocess.run

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, list) and cmd[:2] == ["git", "clone"]:
            state["cloned"].append(cmd[-1])
            class R:  # noqa: E306
                returncode = 0
            return R()
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(ga.subprocess, "run", fake_run)
    state["tmp"] = tmp_path
    return state


def _ac(tmp_path, **kw) -> AutoCloneConfig:
    return AutoCloneConfig(owner="me", target=str(tmp_path), skip_confirmation=True, **kw)


def test_known_seeded_from_local_only(harness):
    """A GitHub repo that's active but NOT cloned locally must not end up in
    `known` (pre-v2.6.2 it did, via active_managed.keys() — the archive trap)."""
    harness["gh"] = [_repo("r1"), _repo("r2"), _repo("r3")]
    harness["local"] = ["r1"]            # r2/r3 active on GitHub but not local
    harness["known"] = ["r1"]            # established baseline

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=True, auto_migrate=False)

    # r2/r3 get cloned (mirror), but `known` records only what's actually local.
    assert harness["saved"] == ["r1"]
    assert harness["archived"] == []     # nothing wrongly archived


def test_archive_on_genuine_local_delete(harness):
    """The intended feature still works: a repo that WAS local (in known) and is
    now gone, while still active on GitHub, gets archived."""
    harness["gh"] = [_repo("r1"), _repo("r2"), _repo("r3")]
    harness["local"] = ["r1", "r2"]      # r3 deleted locally
    harness["known"] = ["r1", "r2", "r3"]

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=True, auto_migrate=False)

    assert harness["archived"] == ["r3"]
    assert harness["saved"] == ["r1", "r2"]   # known follows local
    # The archiving machine pins the delete intent: a later unarchive on the
    # web must not auto-clone r3 back here.
    assert "r3" in harness["saved_tombstones"]


def test_mass_archive_guard_aborts(harness):
    """If most should-be-local repos vanish at once (bad code_roots / failed
    scan), abort before archiving anything."""
    names = [f"r{i}" for i in range(10)]
    harness["gh"] = [_repo(n) for n in names]
    harness["local"] = ["r0"]            # 9 of 10 known+active repos missing
    harness["known"] = names

    with pytest.raises(SystemExit):
        ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=True, auto_migrate=False)

    assert harness["archived"] == []     # guard fired before any archive
    assert harness["saved"] is None      # state not updated on abort


def test_mass_archive_guard_disabled_by_threshold(harness):
    """abort_if_local_missing_pct=100 lets a deliberate bulk delete through."""
    names = [f"r{i}" for i in range(10)]
    harness["gh"] = [_repo(n) for n in names]
    harness["local"] = ["r0"]
    harness["known"] = names

    ga.run(_ac(harness["tmp"], abort_if_local_missing_pct=100),
           [harness["tmp"]], push=True, auto_migrate=False)

    assert sorted(harness["archived"]) == sorted(names[1:])   # r1..r9 archived
    assert harness["saved"] == ["r0"]


def test_no_archive_without_push(harness):
    """Pull-only mode never archives, even with repos missing locally."""
    harness["gh"] = [_repo("r1"), _repo("r2")]
    harness["local"] = ["r1"]
    harness["known"] = ["r1", "r2"]

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=False, auto_migrate=False)

    assert harness["archived"] == []


# ---------- v2.15.0: tombstones (the claude-hub delete→re-clone flap) ----------

def test_rm_on_archive_signal_writes_tombstone(harness):
    """Acting on the cross-machine delete signal (repo archived elsewhere →
    delete local) must record a tombstone so the repo can't come back."""
    harness["gh"] = [_repo("r1"), _repo("r2", archived=True)]
    harness["local"] = ["r1", "r2"]
    harness["known"] = ["r1", "r2"]

    # 2 known → 1 active is a 50% "shrink"; irrelevant to this test, allow it.
    ga.run(_ac(harness["tmp"], abort_if_shrink_pct=90),
           [harness["tmp"]], push=True, auto_migrate=False)

    assert any(p.endswith("r2") for p in harness["removed"])   # local deleted
    assert "r2" in harness["saved_tombstones"]                 # intent pinned
    assert harness["saved"] == ["r1"]


def test_tombstone_blocks_reclone(harness):
    """The flap: a repo this machine deleted on the delete signal reappears in
    active (unarchived on the web). It must NOT be auto-cloned again."""
    harness["gh"] = [_repo("r1"), _repo("r2")]    # r2 active again
    harness["local"] = ["r1"]
    harness["known"] = ["r1"]                     # r2 already dropped from known
    harness["tombstones"] = {"r2": "2026-06-11T00:00:00+00:00"}

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=True, auto_migrate=False)

    assert harness["cloned"] == []                          # not resurrected
    assert "r2" in harness["saved_tombstones"]              # tombstone kept
    assert harness["archived"] == []


def test_tombstone_cleared_when_restored_locally(harness):
    """Manually cloning a deleted repo back = explicit restore; the tombstone
    must clear so the repo is managed normally again."""
    harness["gh"] = [_repo("r1"), _repo("r2")]
    harness["local"] = ["r1", "r2"]               # user cloned r2 back
    harness["known"] = ["r1"]
    harness["tombstones"] = {"r2": "2026-06-11T00:00:00+00:00"}

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=True, auto_migrate=False)

    assert harness["saved_tombstones"] == {}      # restored → tombstone gone
    assert sorted(harness["saved"]) == ["r1", "r2"]
    assert harness["archived"] == []              # and definitely not archived


def test_rm_held_when_local_changes_exist(harness):
    """The delete signal must never destroy work that exists only here:
    dirty / unpushed repos are held back (warned), not deleted."""
    harness["gh"] = [_repo("r1")]                 # r2 archived away on GitHub
    harness["local"] = ["r1", "r2"]
    harness["known"] = ["r1", "r2"]
    harness["dirty"] = {"r2"}

    ga.run(_ac(harness["tmp"], abort_if_shrink_pct=90),
           [harness["tmp"]], push=True, auto_migrate=False)

    assert harness["removed"] == []                         # nothing deleted
    assert "r2" not in (harness["saved_tombstones"] or {})  # no tombstone either
    assert "r2" in harness["saved"]                         # stays known → re-warned


def test_case_mismatch_is_not_a_delete_plus_clone(harness):
    """GitHub names are case-insensitive. An origin URL cased differently from
    the canonical name must not read as delete-local + clone-fresh (the flap)."""
    harness["gh"] = [_repo("foo")]                # canonical lowercase
    harness["local"] = ["Foo"]                    # origin URL cased differently
    harness["known"] = ["Foo"]

    ga.run(_ac(harness["tmp"]), [harness["tmp"]], push=True, auto_migrate=False)

    assert harness["removed"] == []               # no spurious local delete
    assert harness["cloned"] == []                # no spurious re-clone
    assert harness["archived"] == []
