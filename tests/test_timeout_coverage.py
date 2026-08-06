from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


SRC = Path(__file__).parents[1] / "src" / "codesync"
ALLOWED = Counter({
    ("proc.py", "run"): 1,
    ("auth.py", "run"): 1,
    ("updater.py", "Popen"): 1,
})


def test_raw_subprocess_calls_are_limited_to_reviewed_exceptions():
    found: Counter[tuple[str, str]] = Counter()
    violations: list[str] = []

    for path in SRC.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = {"subprocess"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess":
                        aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                imported = {alias.name for alias in node.names}
                banned = imported & {"run", "Popen"}
                if banned:
                    violations.append(
                        f"{path.name}:{node.lineno}: from subprocess import {', '.join(sorted(banned))}"
                    )
            elif (isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in aliases
                  and node.func.attr in {"run", "Popen"}):
                key = (path.name, node.func.attr)
                found[key] += 1
                if key not in ALLOWED:
                    violations.append(f"{path.name}:{node.lineno}: subprocess.{node.func.attr}")

    assert not violations, "raw subprocess calls must use codesync.proc:\n" + "\n".join(violations)
    assert found == ALLOWED

    auth_tree = ast.parse((SRC / "auth.py").read_text(encoding="utf-8"))
    auth_raw = [
        node for node in ast.walk(auth_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]
    command = auth_raw[0].args[0]
    assert isinstance(command, ast.List)
    assert [elt.value for elt in command.elts[:3]] == ["gh", "auth", "login"]
