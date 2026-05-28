from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from codesync import output, paths


# ---------- schema ----------

@dataclass
class AutoCloneConfig:
    owner: str
    target: str
    skip: list[str] = field(default_factory=list)
    skip_confirmation: bool = False
    abort_if_shrink_pct: int = 20
    # v2.2.8+: also auto-clone repos you forked from others. Default True since most
    # personal users treat forks as "mine" (either keeping a copy or deep-modifying);
    # set false to skip forks (pre-v2.2.8 behavior). Forks counted under your owner;
    # archived forks still skipped via the existing archive filter.
    include_forks: bool = True


@dataclass
class PublishConfig:
    """Controls `codesync sync`'s auto-publish step (v2.3.0+).
    See src/codesync/publish.py for the orphan-publishing flow."""
    skip: list[str] = field(default_factory=list)        # dir names to never publish
    skip_confirmation: bool = False                      # true → no 5-sec countdown


@dataclass
class CommitConfig:
    """Controls `codesync sync`'s auto-commit step (v2.4.0+).
    When enabled, dirty repos get `git add -A` + commit before push, so local
    working-tree changes sync without a manual commit. Default skip includes
    'dev-tools' (the codesync source repo) since its history is curated/released."""
    enabled: bool = True
    skip: list[str] = field(default_factory=lambda: ["dev-tools"])


@dataclass
class DbSyncTarget:
    name: str
    container: str
    database: str
    user: str
    password: str
    dump_file: str


@dataclass
class Config:
    code_roots: list[str] = field(default_factory=list)
    auto_clone: AutoCloneConfig | None = None
    publish: PublishConfig | None = None
    commit: CommitConfig | None = None
    db_sync: list[DbSyncTarget] = field(default_factory=list)

    @property
    def code_roots_expanded(self) -> list[Path]:
        return [Path(paths.expand(r)) for r in self.code_roots]


# ---------- load ----------

CONFIG_TEMPLATE = """\
# codesync config — auto-generated.
# Edit and re-run `codesync sync`.
#
# code_roots: directories that hold git repos (scanned recursively, one level deep).
code_roots = [
    "~/SyncRepos",
    # "~/code",
    # "D:/projects",
]

# Optional: GitHub repo auto-sync (clone missing / rm local archived / -Push archives missing-local).
# Delete this whole table if you don't want it.
# [auto_clone]
# owner               = "your-github-username"
# target              = "~/SyncRepos"
# skip                = []
# skip_confirmation   = false
# abort_if_shrink_pct = 20
# include_forks       = true   # also clone repos you forked from others (set false to skip forks)

# Optional: auto-publish orphan dirs (mkdir but no .git, or .git but no origin).
# Delete this whole table if you don't want auto-publish. Defaults are sensible
# if you keep it but leave both fields default.
# [publish]
# skip              = []        # dir names to never publish (e.g., ["experiments", "tmp"])
# skip_confirmation = false      # true → no 5-sec countdown before creating GitHub repos

# Optional: auto-commit dirty repos during sync (before push), so working-tree
# changes sync without a manual commit. Default ON. skip lists repos whose history
# you craft by hand (the codesync repo 'dev-tools' is skipped by default).
# [commit]
# enabled = true
# skip    = ["dev-tools"]

# Optional: Docker MySQL cross-PC sync via Dropbox.
# `codesync sync`          restores newer dump from Dropbox.
# `codesync sync --push`   dumps current DB to Dropbox.
# Add one [[db_sync]] block per database.
# [[db_sync]]
# name      = "example"
# container = "example-mysql-dev"
# database  = "example_db"
# user      = "example_user"
# password  = "dev_pwd"
# dump_file = "~/Dropbox/db-sync/example.sql"
"""


def config_file_path() -> str:
    return str(paths.config_file())


def write_template_if_missing() -> bool:
    f = paths.config_file()
    if f.exists():
        return False
    paths.ensure_config_dir()
    f.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return True


def is_template_unedited() -> bool:
    """True iff config.toml exists and contents match CONFIG_TEMPLATE byte-for-byte —
    i.e. an old-style "auto-generated, please edit" template the user never touched.

    The CLI uses this to re-trigger the first-run wizard for v2.2.5-era users who
    saw the old "edit and rerun" message and got stuck with the template on disk:
    file exists, so v2.2.6's "no config → wizard" check misses it.

    Exact-match check is intentional. Any edit (even a comment, even whitespace)
    is treated as "user has touched this, don't overwrite or re-prompt".
    """
    f = paths.config_file()
    if not f.exists():
        return False
    try:
        return f.read_text(encoding="utf-8") == CONFIG_TEMPLATE
    except OSError:
        return False


def load() -> Config:
    f = paths.config_file()
    if not f.exists():
        write_template_if_missing()
        output.warn(f"配置文件不存在，已生成模板: {f}")
        output.warn("请编辑后重新运行 `codesync sync`。")
        raise SystemExit(1)

    raw = tomllib.loads(f.read_text(encoding="utf-8"))

    code_roots = list(raw.get("code_roots") or [])

    ac_raw = raw.get("auto_clone")
    auto_clone = None
    if ac_raw:
        auto_clone = AutoCloneConfig(
            owner=ac_raw["owner"],
            target=ac_raw["target"],
            skip=list(ac_raw.get("skip") or []),
            skip_confirmation=bool(ac_raw.get("skip_confirmation", False)),
            abort_if_shrink_pct=int(ac_raw.get("abort_if_shrink_pct", 20)),
            include_forks=bool(ac_raw.get("include_forks", True)),
        )

    pub_raw = raw.get("publish")
    publish = None
    if pub_raw:
        publish = PublishConfig(
            skip=list(pub_raw.get("skip") or []),
            skip_confirmation=bool(pub_raw.get("skip_confirmation", False)),
        )

    # [commit]: absent → defaults (enabled=True, skip=["dev-tools"]). Present →
    # read enabled (default True) and skip (absent key → ["dev-tools"]; explicit
    # [] respected so the user can opt to auto-commit everything).
    commit_raw = raw.get("commit")
    if commit_raw is None:
        commit = CommitConfig()
    else:
        skip_val = commit_raw.get("skip")
        commit = CommitConfig(
            enabled=bool(commit_raw.get("enabled", True)),
            skip=list(skip_val) if skip_val is not None else ["dev-tools"],
        )

    db_sync = []
    for d in raw.get("db_sync") or []:
        db_sync.append(DbSyncTarget(
            name=d["name"],
            container=d["container"],
            database=d["database"],
            user=d["user"],
            password=d["password"],
            dump_file=d["dump_file"],
        ))

    return Config(
        code_roots=code_roots,
        auto_clone=auto_clone,
        publish=publish,
        commit=commit,
        db_sync=db_sync,
    )


# ---------- one-shot migration from V1 config.local.ps1 ----------

# Old format example:
#   $CodeRoots = @("C:\Users\yiwang\SyncRepos")
#   $AutoClone = @{ Owner = 'x'; Target = '...'; Skip = @(); SkipConfirmation = $false; AbortIfShrinkPct = 20 }
#   $DbSyncTargets = @( @{ Name = ...; Container = ...; ... } )

_PS_STRING = r"""['"]([^'"]*)['"]"""
_PS_BOOL = r"\$(true|false)"
_PS_INT = r"(\d+)"


def _ps_strings(text: str) -> list[str]:
    return re.findall(_PS_STRING, text)


def _ps_hash_field(block: str, name: str) -> str | None:
    m = re.search(rf"{name}\s*=\s*{_PS_STRING}", block, re.IGNORECASE)
    return m.group(1) if m else None


def _ps_hash_bool(block: str, name: str) -> bool | None:
    m = re.search(rf"{name}\s*=\s*{_PS_BOOL}", block, re.IGNORECASE)
    return m.group(1) == "true" if m else None


def _ps_hash_int(block: str, name: str) -> int | None:
    m = re.search(rf"{name}\s*=\s*{_PS_INT}", block, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _strip_ps_comments(text: str) -> str:
    # Strip `#` comments line-by-line. PS comment syntax matches Python's enough for this.
    out_lines = []
    for line in text.splitlines():
        i = line.find("#")
        out_lines.append(line if i < 0 else line[:i])
    return "\n".join(out_lines)


def _extract_block(text: str, var: str) -> str | None:
    """Find `$Var = @{ ... }` or `$Var = @( ... )` and return inner content."""
    m = re.search(rf"\${var}\s*=\s*@[\({{]", text)
    if not m:
        return None
    start = m.end() - 1
    open_ch = text[start]
    close_ch = ")" if open_ch == "(" else "}"
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return None


def parse_v1_ps1(text: str) -> Config:
    text = _strip_ps_comments(text)
    cfg = Config()

    cr_block = _extract_block(text, "CodeRoots")
    if cr_block is not None:
        cfg.code_roots = _ps_strings(cr_block)

    ac_block = _extract_block(text, "AutoClone")
    if ac_block:
        owner = _ps_hash_field(ac_block, "Owner")
        target = _ps_hash_field(ac_block, "Target")
        if owner and target:
            skip_block = _extract_block(ac_block, "")  # not used; manually grab Skip
            skip_match = re.search(r"Skip\s*=\s*@\(([^)]*)\)", ac_block, re.IGNORECASE)
            skip_items = _ps_strings(skip_match.group(1)) if skip_match else []
            cfg.auto_clone = AutoCloneConfig(
                owner=owner,
                target=target,
                skip=skip_items,
                skip_confirmation=_ps_hash_bool(ac_block, "SkipConfirmation") or False,
                abort_if_shrink_pct=_ps_hash_int(ac_block, "AbortIfShrinkPct") or 20,
            )

    db_block = _extract_block(text, "DbSyncTargets")
    if db_block:
        # Find each @{...} entry inside.
        depth = 0
        i = 0
        starts = []
        while i < len(db_block):
            if db_block[i:i+2] == "@{":
                if depth == 0:
                    starts.append(i + 2)
                depth += 1
                i += 2
                continue
            if db_block[i] == "{":
                depth += 1
            elif db_block[i] == "}":
                depth -= 1
                if depth == 0 and starts:
                    inner = db_block[starts[-1]:i]
                    starts.pop()
                    name = _ps_hash_field(inner, "Name")
                    container = _ps_hash_field(inner, "Container")
                    database = _ps_hash_field(inner, "Database")
                    user = _ps_hash_field(inner, "User")
                    password = _ps_hash_field(inner, "Password")
                    dump_file = _ps_hash_field(inner, "DumpFile")
                    if all([name, container, database, user, password, dump_file]):
                        cfg.db_sync.append(DbSyncTarget(
                            name=name, container=container, database=database,
                            user=user, password=password, dump_file=dump_file,
                        ))
            i += 1

    return cfg


def _toml_str(s: str) -> str:
    """Quote a string for TOML. Prefer literal (single-quote) to avoid escape headaches
    on Windows paths (`\\U` would be a Unicode escape in basic strings). Fall back to
    basic string if the value contains characters a literal string can't carry: the
    single quote itself, or line breaks.
    """
    if "'" not in s and "\n" not in s and "\r" not in s:
        return f"'{s}'"
    escaped = (s.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r"))
    return f'"{escaped}"'


def _to_toml(cfg: Config) -> str:
    lines: list[str] = []
    lines.append("# codesync config — migrated from V1 config.local.ps1.\n")

    lines.append("code_roots = [")
    for r in cfg.code_roots:
        lines.append(f"    {_toml_str(r)},")
    lines.append("]\n")

    if cfg.auto_clone:
        ac = cfg.auto_clone
        lines.append("[auto_clone]")
        lines.append(f"owner               = {_toml_str(ac.owner)}")
        lines.append(f"target              = {_toml_str(ac.target)}")
        skip_str = ", ".join(_toml_str(s) for s in ac.skip)
        lines.append(f"skip                = [{skip_str}]")
        lines.append(f"skip_confirmation   = {'true' if ac.skip_confirmation else 'false'}")
        lines.append(f"abort_if_shrink_pct = {ac.abort_if_shrink_pct}")
        lines.append(f"include_forks       = {'true' if ac.include_forks else 'false'}")
        lines.append("")

    if cfg.publish:
        p = cfg.publish
        lines.append("[publish]")
        publish_skip_str = ", ".join(_toml_str(s) for s in p.skip)
        lines.append(f"skip              = [{publish_skip_str}]")
        lines.append(f"skip_confirmation = {'true' if p.skip_confirmation else 'false'}")
        lines.append("")

    if cfg.commit:
        c = cfg.commit
        lines.append("[commit]")
        lines.append(f"enabled = {'true' if c.enabled else 'false'}")
        commit_skip_str = ", ".join(_toml_str(s) for s in c.skip)
        lines.append(f"skip    = [{commit_skip_str}]")
        lines.append("")

    for t in cfg.db_sync:
        lines.append("[[db_sync]]")
        lines.append(f"name      = {_toml_str(t.name)}")
        lines.append(f"container = {_toml_str(t.container)}")
        lines.append(f"database  = {_toml_str(t.database)}")
        lines.append(f"user      = {_toml_str(t.user)}")
        lines.append(f"password  = {_toml_str(t.password)}")
        lines.append(f"dump_file = {_toml_str(t.dump_file)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _is_codesync_self_dir(path_str: str) -> bool:
    """True if a code_roots entry points at the codesync/dev-tools repo itself.

    V1 sometimes had ~/dev-tools listed as a code_root because users hand-edited
    the config to include "the directory I'm running from". In V2 the tool is
    pip-installed and updates via `codesync --update`, so the source repo no
    longer needs to be in code_roots (and being there is at best a no-op,
    at worst clutter that shows up in status output).

    Definitive markers: V1's `sync.ps1` or V2's `src/codesync/__init__.py`.
    Anything missing both is treated as a normal repo dir (don't second-guess).
    """
    p = Path(paths.expand(path_str))
    if not p.is_dir():
        return False
    if (p / "sync.ps1").is_file():
        return True
    if (p / "src" / "codesync" / "__init__.py").is_file():
        return True
    return False


def filter_codesync_self_dirs(roots: list[str]) -> tuple[list[str], list[str]]:
    """Split code_roots into (kept, dropped). Dropped entries point at the
    codesync source repo itself — see _is_codesync_self_dir."""
    kept: list[str] = []
    dropped: list[str] = []
    for r in roots:
        (dropped if _is_codesync_self_dir(r) else kept).append(r)
    return kept, dropped


def migrate_from_ps1() -> int:
    """Find old V1 config.local.ps1 (search likely locations) and write a fresh config.toml."""
    candidates: list[Path] = []
    # Repo path used during V1 era — most users had it under ~/dev-tools or ~/SyncRepos/dev-tools.
    for parent in (Path.home(), Path.home() / "SyncRepos", Path.home() / "code"):
        candidates.append(parent / "dev-tools" / "config.local.ps1")
    # Plus: cwd, if user runs from inside the V1 repo checkout.
    candidates.append(Path.cwd() / "config.local.ps1")

    src: Path | None = None
    for c in candidates:
        if c.exists():
            src = c
            break

    if src is None:
        output.err("找不到 V1 config.local.ps1。已检查:")
        for c in candidates:
            output.detail(f"  - {c}")
        output.detail("请把旧文件放到 ~/dev-tools/config.local.ps1 后重试，或手动编辑 codesync 的 TOML。")
        return 2

    output.section(f"读取 V1 配置: {src}")
    cfg = parse_v1_ps1(src.read_text(encoding="utf-8", errors="replace"))

    cfg.code_roots, dropped_roots = filter_codesync_self_dirs(cfg.code_roots)

    out = paths.config_file()
    if out.exists():
        backup = out.with_suffix(".toml.bak")
        out.rename(backup)
        output.warn(f"现有 config.toml 已备份到 {backup}")

    paths.ensure_config_dir()
    out.write_text(_to_toml(cfg), encoding="utf-8")

    output.section("已生成")
    output.good(f"{out}")
    output.detail(f"  code_roots:  {len(cfg.code_roots)} 个")
    if dropped_roots:
        output.detail(f"  (跳过 {len(dropped_roots)} 个指向 codesync 源码本身的 root，V2 通过 --update 升级，不需要它在 code_roots 里:)")
        for r in dropped_roots:
            output.detail(f"    - {r}")
    output.detail(f"  auto_clone:  {'是' if cfg.auto_clone else '否'}")
    output.detail(f"  db_sync:     {len(cfg.db_sync)} 项")
    output.detail("旧 .ps1 未删除，请自行核对 TOML 后处理。")
    return 0
