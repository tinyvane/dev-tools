from __future__ import annotations

import pytest

import codesync.github_auto as ga
from codesync import trash
from tests.test_github_auto import _ac, _repo, harness  # noqa: F401


def _local_only_record(repo_id: str = "RID-foo", name: str = "foo") -> dict:
    """The exact shape `delete --local-only` stores: the remote kept its live name."""
    return {
        "repo_id": repo_id,
        "owner": "me",
        "original_name": name,
        "remote_name": name,
        "trashed_at": "2026-10-25T11:00:00+00:00",
        "local_only": True,
        "original_path": f"/code/{name}",
        "local_path": f"/code/.codesync-trash/{name}",
    }


@pytest.fixture
def env(request):
    return request.getfixturevalue("harness")


def _seed(env, record: dict) -> None:
    memory = env["memory"]
    memory["Trash"][record["repo_id"]] = dict(record)
    memory["Tombstones"][record["repo_id"]] = record["trashed_at"]


def _run(env, *, push: bool = True) -> None:
    ga.run(_ac(env["tmp"], abort_if_shrink_pct=100, abort_if_local_missing_pct=100),
           [env["tmp"]], push=push, auto_migrate=False)


def test_restore_signal_ignores_local_only_record(monkeypatch):
    record = _local_only_record()
    memory = {
        "Trash": {"RID-foo": dict(record)},
        "Tombstones": {"RID-foo": record["trashed_at"]},
        "Repositories": {},
        "Known": [],
    }
    monkeypatch.setattr(
        trash, "restore_local_record",
        lambda rec: pytest.fail("a local-only record must never be auto-restored"),
    )

    restored = ga._apply_remote_restore_signals(
        [{"id": "RID-foo", "name": "foo", "isArchived": False,
          "owner": {"login": "me"}}],
        memory,
        set(),
    )

    assert restored == []
    assert memory["Trash"] == {"RID-foo": record}
    assert memory["Tombstones"] == {"RID-foo": record["trashed_at"]}
    assert memory["Repositories"] == {}
    assert memory["Known"] == []


@pytest.mark.parametrize("push", [True, False])
def test_sync_keeps_local_only_delete(env, monkeypatch, push):
    record = _local_only_record()
    _seed(env, record)
    env["gh"] = [_repo("foo", repo_id="RID-foo")]
    monkeypatch.setattr(
        trash, "restore_local_record",
        lambda rec: pytest.fail("a local-only record must never be auto-restored"),
    )

    _run(env, push=push)

    memory = env["memory"]
    assert memory["Trash"]["RID-foo"] == record
    assert memory["Tombstones"]["RID-foo"] == record["trashed_at"]
    assert "RID-foo" not in memory["Repositories"]
    assert all(str(n).casefold() != "foo" for n in memory["Known"])
    assert env["cloned"] == []
    assert env["remote_trashed"] == []
    assert env["moved"] == []


def test_sync_keeps_local_only_delete_across_repeated_runs(env, monkeypatch):
    record = _local_only_record()
    _seed(env, record)
    env["gh"] = [_repo("foo", repo_id="RID-foo")]
    monkeypatch.setattr(
        trash, "restore_local_record",
        lambda rec: pytest.fail("a local-only record must never be auto-restored"),
    )

    _run(env)
    _run(env)

    assert env["memory"]["Trash"]["RID-foo"] == record
    assert env["memory"]["Tombstones"]["RID-foo"] == record["trashed_at"]
    assert env["cloned"] == []


def test_local_only_delete_is_not_reported_as_reappeared(env, capsys):
    _seed(env, _local_only_record())
    env["gh"] = [_repo("foo", repo_id="RID-foo")]

    _run(env, push=False)

    captured = capsys.readouterr()
    assert "曾被删除" not in captured.out + captured.err


def test_reappeared_warning_still_lists_other_tombstones(env, capsys):
    """Only the local-only ID is exempt; a real unarchive is still surfaced."""
    _seed(env, _local_only_record())
    env["memory"]["Tombstones"]["RID-bar"] = "2026-06-20T12:00:00+00:00"
    env["gh"] = [_repo("foo", repo_id="RID-foo"), _repo("bar", repo_id="RID-bar")]

    _run(env, push=False)

    out = capsys.readouterr()
    text = out.out + out.err
    assert "1 个曾被删除" in text
    assert "bar" in text
    assert "- foo" not in text
    assert env["cloned"] == []


def test_ordinary_trash_record_is_still_restored_by_remote_signal(monkeypatch):
    """Guard: the fix must not disable restore for real (renamed+archived) trash."""
    record = {
        "repo_id": "RID-foo",
        "owner": "me",
        "original_name": "foo",
        "remote_name": "zz-trash--v1--20261025-110000--abcd1234--foo",
        "trashed_at": "2026-10-25T11:00:00+00:00",
    }
    memory = {
        "Trash": {"RID-foo": dict(record)},
        "Tombstones": {"RID-foo": record["trashed_at"]},
        "Repositories": {},
        "Known": [],
    }
    calls: list[dict] = []

    def restore(rec):
        calls.append(rec)
        return True, ga.Path("/code/foo"), ""

    monkeypatch.setattr(trash, "restore_local_record", restore)

    restored = ga._apply_remote_restore_signals(
        [{"id": "RID-foo", "name": "foo", "isArchived": False,
          "owner": {"login": "me"}}],
        memory,
        set(),
    )

    assert restored == ["foo"]
    assert len(calls) == 1
    assert memory["Trash"] == {}
    assert memory["Tombstones"] == {}


@pytest.mark.parametrize("push", [True, False])
def test_local_only_delete_of_renamed_dir_never_archives_remote(env, push):
    """`rename --local-only` leaves Known keyed by the GitHub name ("foo") while
    the directory is "local-copy". A stale Known entry must still not turn the
    tombstoned, deliberately-live remote into an archive candidate."""
    record = _local_only_record()
    record["original_path"] = "/code/local-copy"
    _seed(env, record)
    env["memory"]["Known"] = ["foo", "bar", "baz"]
    env["gh"] = [_repo("foo", repo_id="RID-foo"), _repo("bar"), _repo("baz")]
    env["local"] = ["bar", "baz"]

    _run(env, push=push)

    assert env["remote_trashed"] == []
    assert "RID-foo" not in env["memory"]["PendingArchives"]
    assert env["memory"]["Trash"]["RID-foo"] == record
