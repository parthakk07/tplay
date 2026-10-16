# One command: install anything missing, then open tplay.
# Windows PowerShell:
#   irm https://raw.githubusercontent.com/parthakk07/tplay/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user;$env:USERPROFILE\.local\bin"
}

function Add-UserPath([string]$Dir) {
    if (-not (Test-Path $Dir)) { return }
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($user) { $parts = $user -split ";" }
    $already = $false
    foreach ($part in $parts) {
        if ($part.TrimEnd("\") -ieq $Dir.TrimEnd("\")) { $already = $true }
    }
    if (-not $already) {
        if ($user) { $user = "$user;$Dir" } else { $user = $Dir }
        [Environment]::SetEnvironmentVariable("Path", $user, "User")
    }
    $env:Path = "$Dir;$env:Path"
}

function Install-Winget([string]$Id) {
    & winget install --exact --id $Id --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -eq 0) { return }
    & winget install --exact --id $Id --accept-package-agreements --accept-source-agreements --disable-interactivity
}

function Test-PythonOk([string[]]$Command) {
    if (-not (Test-Command $Command[0])) { return $false }
    $prefix = @()
    if ($Command.Length -gt 1) { $prefix = $Command[1..($Command.Length - 1)] }
    & $Command[0] @prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    return $LASTEXITCODE -eq 0
}

if (-not (Test-Command "winget")) {
    throw "winget is not installed. Install App Installer from the Microsoft Store, then run this again."
}

$script:Python = @()
if (Test-PythonOk @("py", "-3")) {
    $script:Python = @("py", "-3")
} elseif (Test-PythonOk @("python")) {
    $script:Python = @("python")
} else {
    Install-Winget "Python.Python.3.12"
    Update-SessionPath
    if (Test-PythonOk @("py", "-3")) {
        $script:Python = @("py", "-3")
    } elseif (Test-PythonOk @("python")) {
        $script:Python = @("python")
    } else {
        throw "Python 3.11 or newer is still missing. Install it, open a new terminal, and run this again."
    }
}

if (-not (Test-Command "git")) {
    Install-Winget "Git.Git"
    Update-SessionPath
    if (-not (Test-Command "git")) {
        throw "git is still missing. Install Git, open a new terminal, and run this again."
    }
}

if (-not (Test-Command "mpv")) {
    try {
        Install-Winget "shinchiro.mpv"
    } catch {
        Write-Host "Could not install mpv automatically."
    }
    Update-SessionPath
    $mpvRoots = @(
        "$env:ProgramFiles\mpv",
        "${env:ProgramFiles(x86)}\mpv",
        "$env:LOCALAPPDATA\Programs\mpv",
        "$env:LOCALAPPDATA\mpv",
        "$env:USERPROFILE\scoop\apps\mpv\current",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Packages"
    )
    $mpvExe = Get-ChildItem -Path $mpvRoots -Filter mpv.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($mpvExe) { Add-UserPath $mpvExe.DirectoryName }
    if (-not (Test-Command "mpv")) {
        Write-Host "mpv is not installed. tplay will still open and show this command:"
        Write-Host "  winget install --exact --id shinchiro.mpv"
    }
}

function Invoke-Py([string[]]$PyArgs) {
    $prefix = @()
    if ($script:Python.Length -gt 1) { $prefix = $script:Python[1..($script:Python.Length - 1)] }
    & $script:Python[0] @prefix @PyArgs
    if ($LASTEXITCODE -ne 0) { throw "Python command failed: $($PyArgs -join ' ')" }
}

if (-not (Test-Command "tplay")) {
    Invoke-Py @("-m", "pip", "install", "--user", "pipx")
    Invoke-Py @("-m", "pipx", "ensurepath")
    Update-SessionPath
    $here = $PSScriptRoot
    if ($here -and (Test-Path (Join-Path $here "pyproject.toml"))) {
        Invoke-Py @("-m", "pipx", "install", "--force", "--editable", $here)
    } else {
        Invoke-Py @("-m", "pipx", "install", "--force", "git+https://github.com/parthakk07/tplay.git")
    }
    Update-SessionPath
}

if (-not (Test-Command "tplay")) {
    throw "tplay is installed. Open a new terminal and type: tplay"
}

& tplay
if ($LASTEXITCODE) { exit $LASTEXITCODE }
