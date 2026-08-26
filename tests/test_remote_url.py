from __future__ import annotations

import pytest

from codesync.remote_url import GitHubRemote, normalize, parse_github_remote


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/n.git",
        "https://github.com/o/n",
        "http://github.com/o/n",
        "git@github.com:o/n.git",
        "ssh://git@github.com/o/n.git",
        "ssh://git@github.com:22/o/n.git",
        "ssh://git@ssh.github.com:443/o/n.git",
        "ssh://git@ssh.github.com/o/n.git",
        "git://github.com/o/n.git",
        "https://ghfast.top/https://github.com/o/n.git",
        "https://gh-proxy.com/https://github.com/o/n",
        "https://github.com/o/n.git/",
        "https://github.com/o/n/",
    ],
)
def test_parse_supported_github_remote_forms(url: str) -> None:
    assert parse_github_remote(url) == GitHubRemote("o", "n")


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/o/n.git",
        "git@gitea.example:o/n.git",
        "https://notgithub.com/o/n.git",
        "https://github.com.evil.tld/o/n.git",
        "https://evil.tld/https://github.com.evil.tld/o/n.git",
    ],
)
def test_rejects_non_github_and_confusable_hosts(url: str) -> None:
    assert parse_github_remote(url) is None


def test_normalize_protocols_and_proxy_to_one_identity() -> None:
    urls = [
        "https://github.com/Owner/Repo.git",
        "git@github.com:owner/repo.git",
        "ssh://git@ssh.github.com:443/OWNER/REPO.git",
        "https://ghfast.top/https://github.com/owner/repo",
    ]
    assert {normalize(url) for url in urls} == {"github.com/owner/repo"}


def test_mixed_case_host_owner_and_name_are_normalized() -> None:
    parsed = parse_github_remote("SSH://git@SSH.GitHub.Com:443/Owner/Repo.GIT/")
    assert parsed == GitHubRemote("Owner", "Repo")
    assert normalize("SSH://git@SSH.GitHub.Com:443/Owner/Repo.GIT/") == "github.com/owner/repo"
