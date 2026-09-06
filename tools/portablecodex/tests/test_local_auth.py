"""Execute the shipped helper in Windows PowerShell against isolated fake homes.

No credentials or keyring are read: .age fixtures are deliberately plain test bytes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from portablecodex import portable, proc

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows junctions and leases")


def _run(tmp_path: Path, body: str) -> None:
    helper = Path(portable.__file__).with_name("local_auth.ps1")
    script = rf"""
$ErrorActionPreference = 'Stop'
. '{portable._powershell_literal(str(helper))}'
$root = '{portable._powershell_literal(str(tmp_path / 'portable'))}'
$testBase = '{portable._powershell_literal(str(tmp_path / 'pc-two'))}'
$testOriginalBase = '{portable._powershell_literal(str(tmp_path / 'pc-one'))}'
$testMachine = 'machine-two'
foreach ($p in @($root, "$root\home", "$root\backups", "$root\manifests", $testBase, $testOriginalBase)) {{
    [void][IO.Directory]::CreateDirectory($p)
}}
function Get-PortableLocalAuthBase([string]$Root) {{ return $testBase }}
function Get-CimInstance {{ return @() }}
function Get-ItemProperty {{ return @{{ MachineGuid = $testMachine }} }}
Write-PortableAuthMarker "$root\manifests\portable.json" @{{
    schema_version = 1; root = $root; origin_machine_id = 'machine-one'
    source_home = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex')
}}
function Assert-Equal($Actual, $Expected) {{
    if ($Actual -cne $Expected) {{ throw "Assertion failed: expected [$Expected], got [$Actual]" }}
}}
function Assert-Fails([scriptblock]$Action, [string]$Pattern) {{
    try {{ & $Action }} catch {{
        if ($_ -notmatch $Pattern) {{ throw }}
        return
    }}
    throw "Expected failure: $Pattern"
}}
{body}
"""
    result = proc.run(
        [str(Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
         "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_two_pc_credentials_survive_roundtrip_and_restore_original(tmp_path):
    _run(tmp_path, r"""
[void][IO.Directory]::CreateDirectory("$root\home\secrets")
[IO.File]::WriteAllText("$root\home\secrets\codex_auth.age", 'original-PC-encrypted')
$lease = Open-PortableLocalAuth $root 'test-volume'
$secondTarget = Get-PortableAuthTarget "$root\home\secrets"
Assert-Equal ([IO.File]::ReadAllText("$root\backups\legacy-encrypted-secrets\codex_auth.age")) 'original-PC-encrypted'
Assert-Equal ([IO.File]::Exists("$secondTarget\codex_auth.age")) $false
[IO.File]::WriteAllText("$secondTarget\codex_auth.age", 'second-PC-encrypted')
$another = Open-PortableLocalAuth $root 'test-volume'
$another.Dispose()
$lease.Dispose()
$secondBase = $testBase
$testBase = $testOriginalBase
$testMachine = 'machine-one'
$lease = Open-PortableLocalAuth $root 'test-volume'
$firstTarget = Get-PortableAuthTarget "$root\home\secrets"
Assert-Equal ([IO.File]::ReadAllText("$firstTarget\codex_auth.age")) 'original-PC-encrypted'
Assert-Equal ([IO.File]::ReadAllText("$secondTarget\codex_auth.age")) 'second-PC-encrypted'
[IO.File]::WriteAllText("$firstTarget\codex_auth.age", 'refreshed-original')
$lease.Dispose()
$testBase = $secondBase
$testMachine = 'machine-two'
$lease = Open-PortableLocalAuth $root 'test-volume'
Assert-Equal ([IO.File]::ReadAllText("$root\home\secrets\codex_auth.age")) 'second-PC-encrypted'
$lease.Dispose()
$testBase = $testOriginalBase
$testMachine = 'machine-one'
$lease = Open-PortableLocalAuth $root 'test-volume'
Assert-Equal ([IO.File]::ReadAllText("$root\home\secrets\codex_auth.age")) 'refreshed-original'
$lease.Dispose()
""")


def test_live_lease_and_legacy_process_prevent_switch(tmp_path):
    _run(tmp_path, r"""
$lease = Open-PortableLocalAuth $root 'test-volume'
$oldTarget = Get-PortableAuthTarget "$root\home\secrets"
$testBase = $testOriginalBase
Assert-Fails { Open-PortableLocalAuth $root 'test-volume' } 'Another codexv'
Assert-Equal (Get-PortableAuthTarget "$root\home\secrets") $oldTarget
$lease.Dispose()
function Get-CimInstance { return @(@{Name='codex.exe'; ProcessId=123; ExecutablePath="$root\bin\codex.exe"}) }
Assert-Fails { Open-PortableLocalAuth $root 'test-volume' } 'PID 123'
Assert-Equal (Get-PortableAuthTarget "$root\home\secrets") $oldTarget
""")


def test_unmanaged_junction_and_invalid_registration_fail_closed(tmp_path):
    _run(tmp_path, r"""
[void](New-Item -ItemType Junction -Path "$root\home\secrets" -Target $testBase)
Assert-Fails { Open-PortableLocalAuth $root 'test-volume' } 'unmanaged secrets link'
[IO.Directory]::Delete("$root\home\secrets", $false)
$lease = Open-PortableLocalAuth $root 'test-volume'
$lease.Dispose()
[IO.File]::WriteAllText("$root\manifests\local-auth.json", '{"schema_version":99}')
Assert-Fails { Open-PortableLocalAuth $root 'test-volume' } 'Invalid local auth'
""")


def test_retry_after_archive_without_junction_and_no_overwrite(tmp_path):
    _run(tmp_path, r"""
$lease = Open-PortableLocalAuth $root 'test-volume'
$lease.Dispose()
[IO.Directory]::Delete("$root\home\secrets", $false)
[void][IO.Directory]::CreateDirectory("$root\backups\legacy-encrypted-secrets")
[IO.File]::WriteAllText("$root\backups\legacy-encrypted-secrets\codex_auth.age", 'preserved')
$lease = Open-PortableLocalAuth $root 'test-volume'
Assert-Equal ([IO.File]::Exists("$root\home\secrets\codex_auth.age")) $false
$lease.Dispose()
[IO.Directory]::Delete("$root\home\secrets", $false)
[void][IO.Directory]::CreateDirectory("$root\home\secrets")
[IO.File]::WriteAllText("$root\home\secrets\codex_auth.age", 'unexpected')
Assert-Fails { Open-PortableLocalAuth $root 'test-volume' } 'backup already exists'
Assert-Equal ([IO.File]::ReadAllText("$root\home\secrets\codex_auth.age")) 'unexpected'
Assert-Equal ([IO.File]::ReadAllText("$root\backups\legacy-encrypted-secrets\codex_auth.age")) 'preserved'
""")


def test_credential_base_requires_local_drive_and_no_external_links(tmp_path):
    _run(tmp_path, r"""
# The fixture normally overrides this Windows helper. Reload production checks.
. 'HELPER_PATH'
Assert-Fails { Get-PortableLocalAuthBase $testBase } 'outside the portable volume'
function Get-PortableLocalAuthBase([string]$Root) { return $testBase }
[void](New-Item -ItemType Junction -Path "$testBase\PortableCodex" -Target $testOriginalBase)
Assert-Fails { Open-PortableLocalAuth $root 'test-volume' } 'ordinary auth directory'
""".replace("HELPER_PATH", portable._powershell_literal(
        str(Path(portable.__file__).with_name("local_auth.ps1")))))


def test_launcher_passes_c_option_without_parameter_binding(tmp_path):
    layout = portable.PortableLayout.from_root(tmp_path)
    portable._write_launcher(layout, "test-volume", None)
    launcher = layout.launcher.read_text(encoding="utf-8-sig")
    prefix = launcher.split("$ErrorActionPreference", 1)[0]
    assert "param(" not in prefix
    _run(tmp_path, """
$result = & { PREFIX; $CodexArgs | ConvertTo-Json -Compress } -c 'key="value"' login status
Assert-Equal $result '["-c","key=\\"value\\"","login","status"]'
""".replace("PREFIX", prefix))
