from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import codesync.github_auto as ga
from codesync import delete, journal, proc, trash
from codesync import config, state
from tests.test_delete import _repo as _git_repo
from tests.test_github_auto import _ac, _repo, harness  # noqa: F401
from tests.test_trash import _memory_with_trash, _stub_remote_restore, _stub_state, _trash_entry


def _prefix(repo_id: str) -> str:
    return "deletions/" + hashlib.sha256(repo_id.encode("utf-8")).hexdigest()[:16] + "-"


def _entry(repo_id: str, original: str, *, owner: str = "me",
           remote_name: str | None = None, deleted_at: str = "2026-10-23T06:32:01+00:00") -> dict:
    return {
        "schema": 1,
        "repo_id": repo_id,
        "owner": owner,
        "original_name": original,
        "remote_name": (f"zz-trash--v1--20261023-063201--abcd1234--{original}"
                        if remote_name is None else remote_name),
        "deleted_at": deleted_at,
        "deleted_by": "pc",
    }


def _publish(backend, entry: dict) -> str:
    path = journal.deletion_path(entry["repo_id"], entry["deleted_at"])
    backend.put(entry["owner"], path, entry)
    return path


def _trash_record(repo_id: str = "RID-gone", original: str = "gone", **extra) -> dict:
    record = {
        "repo_id": repo_id,
        "owner": "me",
        "original_name": original,
        "remote_name": f"zz-trash--v1--20261023-063201--abcd1234--{original}",
        "trashed_at": "2026-10-23T06:32:01+00:00",
    }
    record.update(extra)
    return record


def _run(env, *, push: bool = True) -> None:
    ga.run(_ac(env["tmp"], abort_if_shrink_pct=100, abort_if_local_missing_pct=100),
           [env["tmp"]], push=push, auto_migrate=False)


# ---------- journal module: paths and publishing ----------

def test_deletion_path_is_keyed_by_hashed_repo_id_and_event_time():
    path = journal.deletion_path("R_kgDOPvTZiw", "2026-10-23T06:32:01+00:00")
    assert path == _prefix("R_kgDOPvTZiw") + "20261023-063201.json"
    assert journal.entry_prefix("R_kgDOPvTZiw") == _prefix("R_kgDOPvTZiw")


def test_deletion_path_normalizes_offset_to_utc():
    path = journal.deletion_path("RID", "2026-10-23T14:32:01+08:00")
    assert path.endswith("-20261023-063201.json")


def test_publish_deletion_writes_validated_entry(journal_backend):
    assert journal.publish_deletion(_trash_record()) is True
    paths = journal_backend.paths()
    assert paths == [_prefix("RID-gone") + "20261023-063201.json"]
    payload = journal_backend.files[("me", paths[0])]
    assert payload["schema"] == 1
    assert payload["repo_id"] == "RID-gone"
    assert payload["owner"] == "me"
    assert payload["original_name"] == "gone"
    assert payload["remote_name"].startswith("zz-trash--v1--")
    assert payload["deleted_at"] == "2026-10-23T06:32:01+00:00"
    assert isinstance(payload["deleted_by"], str)


def test_publish_deletion_is_idempotent_per_repository_id(journal_backend):
    assert journal.publish_deletion(_trash_record()) is True
    later = _trash_record(trashed_at="2026-10-24T01:00:00+00:00")
    assert journal.publish_deletion(later) is True
    assert len(journal_backend.paths()) == 1


@pytest.mark.parametrize("record", [
    _trash_record(local_only=True),
    _trash_record(repo_id=""),
    _trash_record(owner=""),
])
def test_publish_deletion_skips_records_without_cross_machine_meaning(journal_backend, record):
    assert journal.publish_deletion(record) is False
    assert journal_backend.paths() == []


def test_publish_deletion_never_raises_when_journal_unavailable(journal_backend):
    journal_backend.unavailable = True
    assert journal.publish_deletion(_trash_record()) is False


def test_forget_removes_every_event_for_the_repository_id(journal_backend):
    _publish(journal_backend, _entry("RID-a", "a"))
    _publish(journal_backend, _entry("RID-a", "a", deleted_at="2026-10-24T00:00:00+00:00"))
    other = _publish(journal_backend, _entry("RID-b", "b"))
    assert journal.forget("me", "RID-a") is True
    assert journal_backend.paths() == [other]


def test_forget_reports_failure_without_raising(journal_backend):
    _publish(journal_backend, _entry("RID-a", "a"))
    journal_backend.unavailable = True
    assert journal.forget("me", "RID-a") is False


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(schema=2),
    lambda e: e.update(repo_id=""),
    lambda e: e.update(original_name="../escape"),
    lambda e: e.update(original_name=".."),
    lambda e: e.update(remote_name="../../outside"),
    lambda e: e.update(remote_name="not-a-trash-name"),
    lambda e: e.update(remote_name="zz-trash--v1--x/../../y"),
])
def test_read_entry_rejects_malformed_or_unsafe_entries(journal_backend, mutate):
    entry = _entry("RID-a", "a")
    mutate(entry)
    path = journal.deletion_path("RID-a", "2026-10-23T06:32:01+00:00")
    journal_backend.put("me", path, entry)
    assert journal.read_entry("me", path) is None


def test_read_entry_rejects_entry_stored_under_another_ids_path(journal_backend):
    path = journal.deletion_path("RID-other", "2026-10-23T06:32:01+00:00")
    journal_backend.put("me", path, _entry("RID-a", "a"))
    assert journal.read_entry("me", path) is None


def test_read_entry_accepts_empty_remote_name(journal_backend):
    path = _publish(journal_backend, _entry("RID-a", "a", remote_name=""))
    assert journal.read_entry("me", path)["remote_name"] == ""


# ---------- sync: applying journal entries ----------

@pytest.fixture
def env(request):
    # Reuse the github_auto sync harness without shadowing its imported name.
    return request.getfixturevalue("harness")


def _local_dir(env, name: str) -> Path:
    path = env["tmp"] / name
    path.mkdir(exist_ok=True)
    return path


def test_journal_entry_moves_local_copy_after_remote_was_hard_deleted(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep", "ESMP"]
    _local_dir(env, "keep")
    esmp = _local_dir(env, "ESMP")
    env["memory"]["Known"] = ["ESMP", "keep"]
    env["memory"]["Repositories"] = {
        "RID-esmp": {"name": "ESMP", "path": str(esmp), "owner": "me"},
        "RID-keep": {"name": "keep", "path": str(env["tmp"] / "keep"), "owner": "me"},
    }
    path = _publish(journal_backend, _entry("RID-esmp", "ESMP"))

    _run(env)

    assert env["moved"] == ["ESMP"]
    memory = env["memory"]
    assert memory["Trash"]["RID-esmp"]["original_name"] == "ESMP"
    assert "RID-esmp" in memory["Tombstones"]
    assert "RID-esmp" not in memory["Repositories"]
    assert "ESMP" not in memory["Known"]
    assert path in memory["JournalSeen"]


def test_journal_move_also_happens_in_no_push_mode_without_remote_writes(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep", "ESMP"]
    _local_dir(env, "keep")
    esmp = _local_dir(env, "ESMP")
    env["memory"]["Repositories"] = {
        "RID-esmp": {"name": "ESMP", "path": str(esmp), "owner": "me"},
    }
    _publish(journal_backend, _entry("RID-esmp", "ESMP"))
    before = list(journal_backend.paths())

    _run(env, push=False)

    assert env["moved"] == ["ESMP"]
    assert journal_backend.paths() == before
    assert not [op for op, _ in journal_backend.calls if op in {"create", "delete"}]


def test_journal_never_moves_a_repository_id_that_is_still_active(env, journal_backend):
    env["gh"] = [_repo("foo", repo_id="RID-foo")]
    env["local"] = ["foo"]
    foo = _local_dir(env, "foo")
    env["memory"]["Known"] = ["foo"]
    env["memory"]["Repositories"] = {
        "RID-foo": {"name": "foo", "path": str(foo), "owner": "me"},
    }
    path = _publish(journal_backend, _entry("RID-foo", "foo"))

    _run(env)

    assert env["moved"] == []
    assert path not in env["memory"]["JournalSeen"]


def test_journal_skip_config_wins(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep", "ESMP"]
    _local_dir(env, "keep")
    esmp = _local_dir(env, "ESMP")
    env["memory"]["Repositories"] = {
        "RID-esmp": {"name": "ESMP", "path": str(esmp), "owner": "me"},
    }
    path = _publish(journal_backend, _entry("RID-esmp", "ESMP"))

    ga.run(_ac(env["tmp"], abort_if_shrink_pct=100, skip=["ESMP"]),
           [env["tmp"]], push=True, auto_migrate=False)

    assert env["moved"] == []
    assert path not in env["memory"]["JournalSeen"]


def test_journal_is_ignored_while_local_scan_is_degraded(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep", "ESMP"]
    env["scan_degraded"] = True
    esmp = _local_dir(env, "ESMP")
    env["memory"]["Repositories"] = {
        "RID-esmp": {"name": "ESMP", "path": str(esmp), "owner": "me"},
    }
    path = _publish(journal_backend, _entry("RID-esmp", "ESMP"))

    _run(env)

    assert env["moved"] == []
    assert path not in env["memory"]["JournalSeen"]


def test_journal_entry_without_local_copy_is_marked_seen(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep"]
    _local_dir(env, "keep")
    path = _publish(journal_backend, _entry("RID-never-cloned", "elsewhere"))

    _run(env)

    assert env["moved"] == []
    assert path in env["memory"]["JournalSeen"]


def test_seen_journal_entries_are_not_read_again(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep"]
    _local_dir(env, "keep")
    path = _publish(journal_backend, _entry("RID-old", "old"))
    env["memory"]["JournalSeen"] = {path: "2026-10-23T07:00:00+00:00"}

    _run(env)

    assert path not in journal_backend.reads()


def test_journal_name_fallback_moves_unmapped_local_copy(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep", "ESMP"]
    _local_dir(env, "keep")
    _local_dir(env, "ESMP")
    _publish(journal_backend, _entry("RID-esmp", "ESMP"))

    _run(env)

    assert env["moved"] == ["ESMP"]


def test_journal_name_fallback_refuses_when_same_name_exists_with_another_id(env, journal_backend):
    env["gh"] = [_repo("foo", repo_id="RID-new")]
    env["local"] = ["foo"]
    _local_dir(env, "foo")
    path = _publish(journal_backend, _entry("RID-old", "foo"))

    _run(env)

    assert env["moved"] == []
    assert path not in env["memory"]["JournalSeen"]


def test_journal_name_fallback_ignores_path_mapped_to_another_id(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep", "foo"]
    _local_dir(env, "keep")
    foo = _local_dir(env, "foo")
    env["memory"]["Repositories"] = {
        "RID-newer": {"name": "foo", "path": str(foo), "owner": "me"},
    }
    path = _publish(journal_backend, _entry("RID-old", "foo"))

    _run(env)

    assert env["moved"] == []
    assert path in env["memory"]["JournalSeen"]


def test_journal_entry_for_another_owner_is_ignored(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep", "foo"]
    _local_dir(env, "keep")
    _local_dir(env, "foo")
    path = journal.deletion_path("RID-foo", "2026-10-23T06:32:01+00:00")
    journal_backend.put("me", path, _entry("RID-foo", "foo", owner="someone-else"))

    _run(env)

    assert env["moved"] == []


def test_unsafe_journal_entry_never_moves_anything(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep", "foo"]
    _local_dir(env, "keep")
    foo = _local_dir(env, "foo")
    env["memory"]["Repositories"] = {
        "RID-foo": {"name": "foo", "path": str(foo), "owner": "me"},
    }
    path = journal.deletion_path("RID-foo", "2026-10-23T06:32:01+00:00")
    journal_backend.put("me", path, _entry("RID-foo", "foo", remote_name="../../escape"))

    _run(env)

    assert env["moved"] == []


def test_already_trashed_repository_id_is_marked_seen(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep"]
    _local_dir(env, "keep")
    env["memory"]["Trash"]["RID-done"] = _trash_record("RID-done", "done")
    path = _publish(journal_backend, _entry("RID-done", "done"))

    _run(env)

    assert env["moved"] == []
    assert path in env["memory"]["JournalSeen"]


def test_journal_outage_does_not_block_the_rest_of_sync(env, journal_backend, capsys):
    env["gh"] = [_repo("new")]
    journal_backend.unavailable = True

    _run(env)

    assert env["cloned"] == ["new"]
    assert env["moved"] == []
    assert "删除日志" in capsys.readouterr().out


def test_journal_repository_is_never_cloned_or_tracked(env, journal_backend):
    env["gh"] = [_repo("keep"), _repo(journal.JOURNAL_REPO)]
    env["local"] = ["keep"]
    _local_dir(env, "keep")

    _run(env)

    assert journal.JOURNAL_REPO not in env["clone_attempts"]
    assert journal.JOURNAL_REPO not in env["memory"]["Known"]
    assert all(r["name"] != journal.JOURNAL_REPO
               for r in env["memory"]["Repositories"].values())


# ---------- sync: publishing (backfill) ----------

def test_sync_backfills_trash_records_into_the_journal(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep"]
    _local_dir(env, "keep")
    env["memory"]["Trash"]["RID-gone"] = _trash_record()

    _run(env)

    assert [p for p in journal_backend.paths() if p.startswith(_prefix("RID-gone"))]


def test_no_push_sync_does_not_backfill(env, journal_backend):
    env["gh"] = [_repo("keep")]
    env["local"] = ["keep"]
    _local_dir(env, "keep")
    env["memory"]["Trash"]["RID-gone"] = _trash_record()

    _run(env, push=False)

    assert journal_backend.paths() == []


def test_backfill_skips_local_only_and_still_active_records(env, journal_backend):
    env["gh"] = [_repo("keep"), _repo("alive", repo_id="RID-alive")]
    env["local"] = ["keep"]
    _local_dir(env, "keep")
    env["memory"]["Trash"]["RID-lo"] = _trash_record("RID-lo", "lo", local_only=True)
    env["memory"]["Trash"]["RID-alive"] = _trash_record("RID-alive", "alive")
    env["memory"]["Tombstones"]["RID-alive"] = "2026-10-23T06:32:01+00:00"

    _run(env)

    assert journal_backend.paths() == []


def test_archive_on_local_delete_is_published_in_the_same_run(env, journal_backend):
    env["gh"] = [_repo("r1"), _repo("r2")]
    env["local"] = ["r1"]
    env["memory"]["Known"] = ["r1", "r2"]
    env["memory"]["Repositories"] = {
        f"RID-{n}": {"name": n, "path": str(env["tmp"] / n), "owner": "me"}
        for n in ("r1", "r2")
    }

    _run(env)

    assert env["remote_trashed"] == ["r2"]
    assert [p for p in journal_backend.paths() if p.startswith(_prefix("RID-r2"))]


# ---------- delete / restore / purge ----------

@pytest.fixture
def delete_harness(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    memory = state.default_state()
    monkeypatch.setattr(config, "load", lambda: config.Config(code_roots=[str(root)]))
    monkeypatch.setattr(state, "load_state", lambda: memory)
    monkeypatch.setattr(delete, "_push_before_trash", lambda repo: (True, ""))
    monkeypatch.setattr(
        trash, "get_remote_identity",
        lambda owner, name: ("ok", trash.RepoIdentity("RID-1", owner, name), ""),
    )
    monkeypatch.setattr(
        trash, "trash_remote",
        lambda ident: (True, {
            "repo_id": ident.repo_id,
            "owner": ident.owner,
            "original_name": ident.name,
            "remote_name": "zz-trash--v1--20260620-120000--abc--" + ident.name,
            "trashed_at": "2026-06-20T12:00:00+00:00",
        }, ""),
    )

    def update(mutator):
        mutator(memory)
        return memory
    monkeypatch.setattr(state, "update_state", update)
    return root, memory


def test_delete_publishes_journal_entry(delete_harness, journal_backend):
    root, memory = delete_harness
    _git_repo(root / "foo")
    assert delete.delete_repo("foo", yes=True) == 0
    assert [p for p in journal_backend.paths() if p.startswith(_prefix("RID-1"))]


def test_delete_local_only_publishes_nothing(delete_harness, journal_backend):
    root, memory = delete_harness
    _git_repo(root / "foo")
    assert delete.delete_repo("foo", yes=True, local_only=True) == 0
    assert journal_backend.paths() == []


def test_delete_succeeds_with_warning_when_journal_unavailable(
    delete_harness, journal_backend, capsys,
):
    root, memory = delete_harness
    _git_repo(root / "foo")
    journal_backend.unavailable = True
    assert delete.delete_repo("foo", yes=True) == 0
    assert "删除日志" in capsys.readouterr().out
    assert "RID-1" in memory["Trash"]


def test_restore_forgets_journal_entries(tmp_path, monkeypatch, journal_backend):
    _, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    _stub_remote_restore(monkeypatch, record)
    _publish(journal_backend, _entry("RID-x", "foo"))

    assert trash.restore_trash("foo", [tmp_path]) == 0
    assert journal_backend.paths() == []


def test_restore_still_succeeds_when_journal_forget_fails(tmp_path, monkeypatch, journal_backend):
    _, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    _stub_remote_restore(monkeypatch, record)
    journal_backend.unavailable = True

    assert trash.restore_trash("foo", [tmp_path]) == 0
    assert memory["Trash"] == {}


def _stub_purge(monkeypatch, record, *, identity_status="ok"):
    gh_calls = []

    def identity(owner, name):
        if identity_status != "ok":
            return identity_status, None, "HTTP 404: Not Found"
        return "ok", trash.RepoIdentity(record["repo_id"], owner, name), ""

    def gh(args):
        gh_calls.append(list(args))
        return True, ""

    removed = []
    monkeypatch.setattr(trash, "get_remote_identity", identity)
    monkeypatch.setattr(trash, "_gh", gh)
    monkeypatch.setattr(trash.git_ops, "rmtree_repo",
                        lambda path: (removed.append(path) or True, ""))
    return gh_calls, removed


def test_purge_publishes_journal_entry_before_remote_delete(tmp_path, monkeypatch, journal_backend):
    _, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    order = []
    real_create = journal_backend.create_json

    def create(owner, path, payload, message):
        order.append("journal")
        return real_create(owner, path, payload, message)

    journal_backend.create_json = create
    gh_calls, removed = _stub_purge(monkeypatch, record)
    real_gh = trash._gh
    monkeypatch.setattr(trash, "_gh", lambda args: (order.append("gh-delete"), real_gh(args))[1])

    assert trash.purge_trash("foo", [tmp_path], yes=True) == 0
    assert order == ["journal", "gh-delete"]
    assert [p for p in journal_backend.paths() if p.startswith(_prefix("RID-x"))]
    assert removed


def test_purge_refuses_when_journal_unavailable(tmp_path, monkeypatch, journal_backend):
    source, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    gh_calls, removed = _stub_purge(monkeypatch, record)
    journal_backend.unavailable = True

    assert trash.purge_trash("foo", [tmp_path], yes=True) == 1
    assert not [c for c in gh_calls if c[:2] == ["repo", "delete"]]
    assert removed == [] and source.exists()
    assert "RID-x" in memory["Trash"]


def test_purge_local_only_record_never_deletes_the_live_remote(tmp_path, monkeypatch, journal_backend):
    source, record = _trash_entry(tmp_path)
    record["local_only"] = True
    record["remote_name"] = "foo"
    (source / trash.MANIFEST).write_text(json.dumps(record), encoding="utf-8")
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    gh_calls, removed = _stub_purge(monkeypatch, record)

    assert trash.purge_trash("foo", [tmp_path], yes=True) == 0
    assert not [c for c in gh_calls if c[:2] == ["repo", "delete"]]
    assert removed
    assert journal_backend.paths() == []


def test_purge_after_remote_already_hard_deleted_cleans_local_trash(
    tmp_path, monkeypatch, journal_backend,
):
    _, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    gh_calls, removed = _stub_purge(monkeypatch, record, identity_status="not_found")

    assert trash.purge_trash("foo", [tmp_path], yes=True) == 0
    assert not [c for c in gh_calls if c[:2] == ["repo", "delete"]]
    assert removed
    assert [p for p in journal_backend.paths() if p.startswith(_prefix("RID-x"))]


def test_purge_still_fails_closed_when_remote_identity_unavailable(
    tmp_path, monkeypatch, journal_backend,
):
    source, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    gh_calls, removed = _stub_purge(monkeypatch, record, identity_status="unavailable")

    assert trash.purge_trash("foo", [tmp_path], yes=True) == 1
    assert removed == [] and source.exists()


# ---------- GhBackend: gh api contract ----------

def _cp(cmd, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)


def _gh_stub(monkeypatch, handler):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return handler(list(cmd))

    monkeypatch.setattr(journal.proc, "run", fake_run)
    return calls


_TREE = "repos/me/codesync-journal/git/trees/HEAD?recursive=1"


def test_gh_list_paths_returns_blob_paths(monkeypatch):
    body = json.dumps({"truncated": False, "tree": [
        {"path": "README.md", "type": "blob"},
        {"path": "deletions", "type": "tree"},
        {"path": "deletions/abc-20261023-063201.json", "type": "blob"},
    ]})
    _gh_stub(monkeypatch, lambda cmd: _cp(cmd, stdout=body))
    assert journal.GhBackend().list_paths("me") == [
        "README.md", "deletions/abc-20261023-063201.json",
    ]


@pytest.mark.parametrize("stderr", [
    "gh: Not Found (HTTP 404)",
    "gh: Git Repository is empty. (HTTP 409)",
])
def test_gh_list_paths_missing_or_empty_journal_is_empty(monkeypatch, stderr):
    calls = _gh_stub(monkeypatch, lambda cmd: _cp(cmd, rc=1, stderr=stderr))
    assert journal.GhBackend().list_paths("me") == []
    assert calls[0][:2] == ["gh", "api"] and _TREE in calls[0]


@pytest.mark.parametrize("result", [
    dict(rc=1, stderr="error connecting to api.github.com"),
    dict(rc=1, stderr="gh: Resource not accessible by integration (HTTP 403)"),
    dict(rc=proc.TIMEOUT_RC, stderr="timeout"),
    dict(rc=0, stdout="not json"),
    dict(rc=0, stdout=json.dumps({"truncated": True, "tree": []})),
])
def test_gh_list_paths_uncertainty_raises(monkeypatch, result):
    _gh_stub(monkeypatch, lambda cmd: _cp(cmd, **result))
    with pytest.raises(journal.JournalUnavailable):
        journal.GhBackend().list_paths("me")


def test_gh_read_json_decodes_contents(monkeypatch):
    payload = {"schema": 1, "repo_id": "RID"}
    content = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    calls = _gh_stub(monkeypatch, lambda cmd: _cp(
        cmd, stdout=json.dumps({"encoding": "base64", "content": content})))
    assert journal.GhBackend().read_json("me", "deletions/x.json") == payload
    assert "repos/me/codesync-journal/contents/deletions/x.json" in calls[0]


def _put_content(cmd: list[str]) -> dict:
    field = next(a for a in cmd if a.startswith("content="))
    return json.loads(base64.b64decode(field[len("content="):]).decode("utf-8"))


def test_gh_create_json_puts_base64_payload(monkeypatch):
    calls = _gh_stub(monkeypatch, lambda cmd: _cp(cmd, stdout="{}"))
    assert journal.GhBackend().create_json("me", "deletions/x.json", {"a": 1}, "msg") is True
    put = calls[0]
    assert put[:2] == ["gh", "api"]
    assert "PUT" in put
    assert "repos/me/codesync-journal/contents/deletions/x.json" in put
    assert _put_content(put) == {"a": 1}


def test_gh_create_json_existing_file_returns_false(monkeypatch):
    _gh_stub(monkeypatch, lambda cmd: _cp(
        cmd, rc=1, stderr='gh: Invalid request.\n\n"sha" wasn\'t supplied. (HTTP 422)'))
    assert journal.GhBackend().create_json("me", "deletions/x.json", {"a": 1}, "msg") is False


def test_gh_create_json_creates_private_journal_repo_when_missing(monkeypatch):
    state = {"puts": 0}

    def handler(cmd):
        if cmd[:3] == ["gh", "repo", "create"]:
            return _cp(cmd)
        state["puts"] += 1
        if state["puts"] == 1:
            return _cp(cmd, rc=1, stderr="gh: Not Found (HTTP 404)")
        return _cp(cmd, stdout="{}")

    calls = _gh_stub(monkeypatch, handler)
    assert journal.GhBackend().create_json("me", "deletions/x.json", {"a": 1}, "msg") is True
    create = next(c for c in calls if c[:3] == ["gh", "repo", "create"])
    assert "me/codesync-journal" in create
    assert "--private" in create


def test_gh_create_json_uncertainty_raises(monkeypatch):
    _gh_stub(monkeypatch, lambda cmd: _cp(cmd, rc=1, stderr="error connecting to api.github.com"))
    with pytest.raises(journal.JournalUnavailable):
        journal.GhBackend().create_json("me", "deletions/x.json", {"a": 1}, "msg")


def test_publish_deletion_without_trashed_at_still_writes_a_readable_entry(journal_backend):
    record = _trash_record()
    del record["trashed_at"]
    assert journal.publish_deletion(record) is True
    [path] = journal_backend.paths()
    assert journal.read_entry("me", path) is not None


# ---------- adversarial review regressions ----------

def test_stale_id_mapping_never_moves_path_owned_by_active_repo(env, journal_backend):
    env["gh"] = [_repo("foo", repo_id="RID-new")]
    env["local"] = ["foo"]
    foo = _local_dir(env, "foo")
    env["memory"]["Repositories"] = {
        "RID-old": {"name": "foo", "path": str(foo), "owner": "me"},
        "RID-new": {"name": "foo", "path": str(foo), "owner": "me"},
    }
    path = _publish(journal_backend, _entry("RID-old", "foo"))

    _run(env)

    assert env["moved"] == []
    assert path not in env["memory"]["JournalSeen"]


def test_excluded_fork_ignores_journal_entry(env, journal_backend):
    fork = _repo("forky", repo_id="RID-fork", archived=True)
    fork["isFork"] = True
    env["gh"] = [_repo("keep"), fork]
    env["local"] = ["keep", "forky"]
    _local_dir(env, "keep")
    forky = _local_dir(env, "forky")
    env["memory"]["Repositories"] = {
        "RID-fork": {"name": "forky", "path": str(forky), "owner": "me"},
    }
    _publish(journal_backend, _entry("RID-fork", "forky"))

    ga.run(_ac(env["tmp"], abort_if_shrink_pct=100, include_forks=False),
           [env["tmp"]], push=True, auto_migrate=False)

    assert env["moved"] == []


def test_gh_create_json_validation_422_is_not_existing_file(monkeypatch):
    _gh_stub(monkeypatch, lambda cmd: _cp(
        cmd, rc=1, stderr="gh: Validation Failed (HTTP 422)"))
    with pytest.raises(journal.JournalUnavailable):
        journal.GhBackend().create_json("me", "deletions/x.json", {"a": 1}, "msg")


def test_fresh_deletion_writes_new_event_despite_stale_one(journal_backend):
    _publish(journal_backend, _entry("RID-gone", "gone", deleted_at="2026-01-01T00:00:00+00:00"))
    assert journal.publish_deletion(_trash_record(), fresh=True) is True
    assert len([p for p in journal_backend.paths() if p.startswith(_prefix("RID-gone"))]) == 2


def test_delete_after_failed_restore_cleanup_publishes_new_event(delete_harness, journal_backend):
    root, memory = delete_harness
    _git_repo(root / "foo")
    stale = _publish(journal_backend, _entry("RID-1", "foo", deleted_at="2026-01-01T00:00:00+00:00"))
    assert delete.delete_repo("foo", yes=True) == 0
    events = [p for p in journal_backend.paths() if p.startswith(_prefix("RID-1"))]
    assert stale in events and len(events) == 2


def test_plain_archived_repo_publishes_consumable_entry(journal_backend):
    record = _trash_record(remote_name="foo")
    assert journal.publish_deletion(record) is True
    [path] = journal_backend.paths()
    entry = journal.read_entry("me", path)
    assert entry is not None and entry["remote_name"] == ""


def test_purge_replaces_unconsumable_entry_before_remote_delete(tmp_path, monkeypatch, journal_backend):
    _, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    bad_path = journal.deletion_path("RID-x", "2026-01-01T00:00:00+00:00")
    journal_backend.put("me", bad_path, _entry("RID-x", "foo", remote_name="../bad"))
    gh_calls, removed = _stub_purge(monkeypatch, record)

    assert trash.purge_trash("foo", [tmp_path], yes=True) == 0
    valid = [p for p in journal_backend.paths()
             if p.startswith(_prefix("RID-x")) and journal.read_entry("me", p) is not None]
    assert valid
    assert [c for c in gh_calls if c[:2] == ["repo", "delete"]]


def test_purge_refuses_when_same_path_entry_is_unconsumable(tmp_path, monkeypatch, journal_backend):
    source, record = _trash_entry(tmp_path)
    memory = _memory_with_trash(record)
    _stub_state(monkeypatch, memory)
    bad_path = journal.deletion_path("RID-x", record["trashed_at"])
    journal_backend.put("me", bad_path, _entry("RID-x", "foo", remote_name="../bad"))
    gh_calls, removed = _stub_purge(monkeypatch, record)

    assert trash.purge_trash("foo", [tmp_path], yes=True) == 1
    assert not [c for c in gh_calls if c[:2] == ["repo", "delete"]]
    assert source.exists()
