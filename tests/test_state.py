from __future__ import annotations

import json

import pytest

from codesync import paths, state


def test_load_state_migrates_legacy(tmp_path, monkeypatch):
    f = tmp_path / "known-repos.json"
    f.write_text('{"Known":["foo"],"Tombstones":{"bar":"ts"}}', encoding="utf-8")
    monkeypatch.setattr(paths, "known_repos_file", lambda: f)
    loaded = state.load_state()
    assert loaded["Known"] == ["foo"]
    assert loaded["SchemaVersion"] == state.STATE_SCHEMA_VERSION
    assert loaded["Repositories"] == {}


def test_update_state_is_atomic_and_preserves_fields(tmp_path, monkeypatch):
    f = tmp_path / "known-repos.json"
    monkeypatch.setattr(paths, "known_repos_file", lambda: f)
    monkeypatch.setattr(paths, "ensure_config_dir", lambda: tmp_path)
    state.update_state(lambda s: s["Trash"].update({"RID": {"name": "foo"}}))
    parsed = json.loads(f.read_text(encoding="utf-8"))
    assert parsed["Trash"]["RID"]["name"] == "foo"
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_state_fails_closed(tmp_path, monkeypatch):
    f = tmp_path / "known-repos.json"
    f.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(paths, "known_repos_file", lambda: f)
    with pytest.raises(ValueError):
        state.load_state()


def test_future_protocol_state_fails_closed(tmp_path, monkeypatch):
    f = tmp_path / "known-repos.json"
    f.write_text('{"SchemaVersion":999,"TrashProtocolVersion":999}', encoding="utf-8")
    monkeypatch.setattr(paths, "known_repos_file", lambda: f)
    with pytest.raises(ValueError):
        state.load_state()
