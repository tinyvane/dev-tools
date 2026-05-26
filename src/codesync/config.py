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

    return Config(code_roots=code_roots, auto_clone=auto_clone, db_sync=db_sync)


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
    output.detail(f"  auto_clone:  {'是' if cfg.auto_clone else '否'}")
    output.detail(f"  db_sync:     {len(cfg.db_sync)} 项")
    output.detail("旧 .ps1 未删除，请自行核对 TOML 后处理。")
    return 0
