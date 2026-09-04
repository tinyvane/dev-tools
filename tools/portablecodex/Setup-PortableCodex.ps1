param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$OnboardArgs
)

$ErrorActionPreference = 'Stop'

$pythonCommand = $null
$pythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCommand = 'py'
    $pythonArgs = @('-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCommand = 'python'
} else {
    throw 'Python >= 3.11 is required. Install Python and rerun this script.'
}

$version = (& $pythonCommand @pythonArgs --version 2>&1 | Out-String).Trim()
if ($version -notmatch '^Python 3\.(1[1-9]|[2-9][0-9])(?:\.|$)') {
    throw "Python >= 3.11 is required; found: $version"
}

Write-Host '==> Installing PortableCodex from the dev-tools portable subproject'
& $pythonCommand @pythonArgs -m pip install --user --upgrade $PSScriptRoot
if ($LASTEXITCODE -ne 0) {
    throw "PortableCodex installation failed with exit code $LASTEXITCODE"
}

$userBase = (& $pythonCommand @pythonArgs -m site --user-base 2>&1 | Out-String).Trim()
$scriptsDirectory = Join-Path $userBase 'Scripts'
if (-not (Test-Path -LiteralPath $scriptsDirectory -PathType Container)) {
    throw "Python user Scripts directory is missing: $scriptsDirectory"
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$userParts = @($userPath -split ';' | Where-Object { $_ })
$pathPresent = $userParts | Where-Object {
    $_.TrimEnd('\') -ieq $scriptsDirectory.TrimEnd('\')
}
if (-not $pathPresent) {
    $newUserPath = if ($userPath) {
        "$scriptsDirectory;$userPath"
    } else {
        $scriptsDirectory
    }
    [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
    Write-Host "==> Added to User PATH: $scriptsDirectory"
}
if (-not (($env:Path -split ';') | Where-Object {
    $_.TrimEnd('\') -ieq $scriptsDirectory.TrimEnd('\')
})) {
    $env:Path = "$scriptsDirectory;$env:Path"
}

$portableCodex = Join-Path $scriptsDirectory 'portablecodex.exe'
if (-not (Test-Path -LiteralPath $portableCodex -PathType Leaf)) {
    throw "portablecodex.exe was not installed at: $portableCodex"
}

Write-Host '==> Starting guided onboarding'
& $portableCodex onboard @OnboardArgs
exit $LASTEXITCODE
