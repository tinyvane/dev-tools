"""Run upgrade recovery in Windows PowerShell with isolated files and fake PATH.

No installed binary, user registry, credentials, or live registration is changed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from portablecodex import portable, proc

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell")


def _run(tmp_path: Path, body: str) -> None:
    helper = Path(portable.__file__).with_name("upgrade.ps1")
    script = rf"""
$ErrorActionPreference = 'Stop'
. '{portable._powershell_literal(str(helper))}'
$root = '{portable._powershell_literal(str(tmp_path / 'portable'))}'
foreach ($part in @('', 'manifests', 'backups', 'bin')) {{
    [void][IO.Directory]::CreateDirectory((Join-Path $root $part))
}}
$manifest = Join-Path $root 'manifests\portable.json'
$executable = Join-Path $root 'bin\codex.exe'
[IO.File]::WriteAllText($executable, 'fake binary')
$record = @{{ schema_version=1; root=$root; volume_unique_id='test-volume'
    mode='dual'; status='complete'; expected_cli_version='0.153.4'
    untouched=@{{ nested=@(1, 2, 3) }} }}
[IO.File]::WriteAllText($manifest, ($record | ConvertTo-Json -Depth 100))
$script:installed = '0.154.0'
$script:signatureStatus = 'Valid'
$script:signer = 'OpenAI OpCo, LLC'
$script:signatureCalls = 0
$script:userPath = 'C:\local;C:\keep'
$script:pathWrites = 0
function Get-PortableCliVersion([string]$Executable) {{ return $script:installed }}
function Get-AuthenticodeSignature {{
    $script:signatureCalls++
    $cert = [pscustomobject]@{{}}
    $cert | Add-Member ScriptMethod GetNameInfo {{ return $script:signer }}
    return [pscustomobject]@{{ Status=$script:signatureStatus; SignerCertificate=$cert }}
}}
function Get-PortableUserPath {{ return $script:userPath }}
function Set-PortableUserPath([string]$Value) {{ $script:userPath=$Value; $script:pathWrites++ }}
function Assert-Equal($Actual, $Expected) {{
    if ($Actual -cne $Expected) {{ throw "Expected [$Expected], got [$Actual]" }}
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


def test_signed_upgrade_is_atomic_preserves_metadata_and_is_idempotent(tmp_path):
    _run(tmp_path, r"""
$original = [IO.File]::ReadAllText($manifest)
Sync-PortableCliVersion $root 'test-volume' $executable
$after = Read-PortableRegistration $root 'test-volume'
Assert-Equal $after.expected_cli_version '0.154.0'
Assert-Equal ($after.untouched.nested -join ',') '1,2,3'
$backups = @(Get-ChildItem -LiteralPath "$root\backups")
Assert-Equal $backups.Count 1
Assert-Equal ([IO.File]::ReadAllText($backups[0].FullName)) $original
Assert-Equal @(Get-ChildItem "$manifest.*.tmp").Count 0
Sync-PortableCliVersion $root 'test-volume' $executable
Assert-Equal $script:signatureCalls 1
Assert-Equal @(Get-ChildItem -LiteralPath "$root\backups").Count 1
# A second future upgrade is accepted without regenerating the launcher.
$script:installed = '0.155.0'
Sync-PortableCliVersion $root 'test-volume' $executable
Assert-Equal (Read-PortableRegistration $root 'test-volume').expected_cli_version '0.155.0'
""")


@pytest.mark.parametrize("setup,pattern", [
    ("$script:installed='0.153.0'", "downgrade refused"),
    ("$script:signatureStatus='HashMismatch'", "valid OpenAI signature"),
    ("$script:signer='Other Publisher'", "valid OpenAI signature"),
    ("$record.expected_cli_version='broken'", "invalid expected version"),
    ("$record.volume_unique_id='another-volume'", "Invalid portable registration"),
    ("$record.status='data-ready'", "incomplete"),
])
def test_rejected_upgrade_preserves_registration(tmp_path, setup, pattern):
    _run(tmp_path, rf"""
{setup}
[IO.File]::WriteAllText($manifest, ($record | ConvertTo-Json -Depth 100))
$before = [IO.File]::ReadAllText($manifest)
Assert-Fails {{ Sync-PortableCliVersion $root 'test-volume' $executable }} '{pattern}'
Assert-Equal ([IO.File]::ReadAllText($manifest)) $before
Assert-Equal @(Get-ChildItem -LiteralPath "$root\backups").Count 0
""")


def test_binary_changed_during_signature_check_is_rejected(tmp_path):
    _run(tmp_path, r"""
function Assert-PortablePublisher([string]$Executable) {
    [IO.File]::WriteAllText($Executable, 'concurrently replaced binary')
}
$before = [IO.File]::ReadAllText($manifest)
Assert-Fails { Sync-PortableCliVersion $root 'test-volume' $executable } 'changed during upgrade'
Assert-Equal ([IO.File]::ReadAllText($manifest)) $before
""")


def test_signature_check_loads_builtin_module_despite_inherited_module_path(tmp_path):
    _run(tmp_path, r"""
Remove-Item Function:Get-AuthenticodeSignature
$env:PSModulePath = Join-Path $root 'missing-modules'
# The fixture is intentionally not a signed executable. The actual Windows
# cmdlet must load and return the signature rejection, not an autoload error.
Assert-Fails { Assert-PortablePublisher $executable } 'valid OpenAI signature'
""")


def test_dual_path_repair_preserves_other_entries_and_later_changes(tmp_path):
    _run(tmp_path, r"""
$script:userPath = '"' + "$root\bin\" + '";C:\local;;C:\keep;' + "$root\bin-extra"
Repair-PortableUserPath $root 'test-volume'
Assert-Equal $script:userPath ('C:\local;;C:\keep;' + "$root\bin-extra")
Repair-PortableUserPath $root 'test-volume'
Assert-Equal $script:pathWrites 1
# Simulate an updater reintroducing V bin and an unrelated tool adding an entry.
$script:userPath = "$root\bin;" + $script:userPath + ';C:\new-tool'
Repair-PortableUserPath $root 'test-volume'
Assert-Equal $script:userPath ('C:\local;;C:\keep;' + "$root\bin-extra;C:\new-tool")
Assert-Equal $script:pathWrites 2
$record.mode = 'exclusive'
[IO.File]::WriteAllText($manifest, ($record | ConvertTo-Json -Depth 100))
$script:userPath = "$root\bin;C:\local"
Repair-PortableUserPath $root 'test-volume'
Assert-Equal $script:userPath "$root\bin;C:\local"
""")


def test_generated_launcher_upgrade_cleanup_preserves_arguments_environment_and_exit(tmp_path):
    layout = portable.PortableLayout.from_root(tmp_path)
    portable._write_launcher(layout, "test-volume", "0.153.4")
    script = layout.launcher.read_text(encoding="utf-8-sig")
    # Execute the real launcher orchestration with isolated stubs for volume,
    # authentication and binary. A fake CLI models the self-updater adding PATH.
    tail = script.split("$authLease = Open-PortableLocalAuth", 1)[1]
    body = r"""
function Open-PortableLocalAuth {
    $lease = [pscustomobject]@{}
    $lease | Add-Member ScriptMethod Dispose { }
    return $lease
}
$actualRoot=$root; $actualVolume='test-volume'
$portableHome="$root\home"; $portableSqlite="$root\sqlite"; $portableBin="$root\bin"
$fakeCli = Join-Path $root 'fake-cli.ps1'
[IO.File]::WriteAllText($fakeCli, @'
if (($args -join '|') -cne '-c|x=y|--version') { throw 'arguments changed' }
if ($env:CODEX_HOME -ne "$root\home" -or $env:CODEX_SQLITE_HOME -ne "$root\sqlite" -or
    $env:CODEX_INSTALL_DIR -ne "$root\bin") { throw 'wrong environment' }
$global:userPath = "$root\bin;C:\local;C:\new-tool"
$global:LASTEXITCODE=7
'@)
$portableExe=$fakeCli
$CodexArgs=@('-c', 'x=y', '--version')
"""
    # Replace only the final process exit so the test can assert the resulting code.
    tail = tail.replace("exit $codexExitCode", "Assert-Equal $codexExitCode 7")
    _run(tmp_path, body + "\n$authLease = Open-PortableLocalAuth" + tail + r"""
Assert-Equal $script:userPath 'C:\local;C:\new-tool'
""")
