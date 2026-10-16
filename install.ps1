# Install tplay, or update it when the command on PATH is the pipx copy.
# Windows PowerShell:
#   irm https://raw.githubusercontent.com/parthakk07/tplay/main/install.ps1 | iex
# A copy this installer does not recognize is left in place.
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

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

# BEGIN TPLAY_PIPX_CHECK
$TplayPipxCheck = @'
import json
import os
import sys


def emit(kind, *fields):
    print("\t".join([kind, *fields]))


def resolve(path):
    if not path:
        return ""
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.abspath(path)


def norm(path):
    value = resolve(path)
    if os.name == "nt":
        return os.path.normcase(value)
    return value


def app_paths(main):
    found = []
    for item in main.get("app_paths") or []:
        if isinstance(item, str) and item:
            found.append(item)
        elif isinstance(item, dict):
            value = item.get("__Path__") or ""
            if value:
                found.append(value)
    return found


def venv_from_app(path):
    parent = os.path.dirname(resolve(path))
    if os.path.basename(parent).lower() in {"bin", "scripts"}:
        return os.path.dirname(parent)
    return ""


def basename_is_tplay(path):
    return os.path.basename(path).lower() in {"tplay", "tplay.exe"}


def inside(child, parent):
    if not child or not parent:
        return False
    child = norm(child)
    parent = norm(parent)
    if child == parent:
        return True
    return child.startswith(parent.rstrip("\\/") + os.sep)


def mentions(path, needle):
    if not path or not needle or not os.path.isfile(path):
        return False
    variants = {needle, needle.replace("/", "\\"), needle.replace("\\", "/")}
    try:
        with open(path, "rb") as handle:
            data = handle.read(8_000_000)
    except OSError:
        return False
    for item in variants:
        if not item:
            continue
        if item.encode("utf-8", "surrogateescape") in data:
            return True
        if item.encode("utf-16le", "surrogateescape") in data:
            return True
    return False


def is_tplay_env(name, env):
    main = ((env or {}).get("metadata") or {}).get("main_package") or {}
    apps = [str(item).lower() for item in (main.get("apps") or [])]
    package = str(main.get("package") or "").lower()
    if name.lower() == "tplay":
        return True
    if "tplay" in apps or "tplay.exe" in apps:
        return True
    return package == "tplay"


def main():
    if len(sys.argv) != 4:
        emit("unclear", "", "bad-args")
        return
    command, bin_dir, venvs_root = sys.argv[1:]
    if not command:
        emit("absent")
        return
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        emit("unclear", command, "invalid-json")
        return
    if not isinstance(data, dict):
        emit("unclear", command, "invalid-json")
        return
    venvs = data.get("venvs")
    if not isinstance(venvs, dict):
        emit("unclear", command, "invalid-json")
        return
    candidates = []
    for name, env in venvs.items():
        if not isinstance(name, str) or not is_tplay_env(name, env if isinstance(env, dict) else {}):
            continue
        main = ((env or {}).get("metadata") or {}).get("main_package") or {}
        if not isinstance(main, dict):
            main = {}
        paths = app_paths(main)
        venv_dir = ""
        for path in paths:
            venv_dir = venv_from_app(path)
            if venv_dir:
                break
        if not venv_dir and venvs_root:
            guess = os.path.join(venvs_root, name)
            owns_guess = inside(command, guess) or any(inside(path, guess) for path in paths)
            if os.path.isdir(guess) and owns_guess:
                venv_dir = guess
        candidates.append((paths, venv_dir))
    if not candidates:
        emit("unknown", command, "not-listed")
        return
    matches = []
    for paths, venv_dir in candidates:
        matched = any(norm(command) == norm(path) for path in paths)
        if not matched and venv_dir and inside(command, venv_dir) and basename_is_tplay(command):
            matched = True
        copy_in_bin = (
            bin_dir
            and venv_dir
            and norm(os.path.dirname(os.path.abspath(command))) == norm(bin_dir)
            and basename_is_tplay(command)
        )
        if not matched and copy_in_bin and mentions(command, venv_dir):
            matched = True
        if matched:
            matches.append(venv_dir)
    if len(matches) > 1:
        emit("unclear", command, "ambiguous")
        return
    if len(matches) == 1:
        venv_dir = matches[0]
        if not venv_dir or not os.path.isdir(venv_dir):
            emit("unclear", command, "no-venv-dir")
            return
        emit("managed", command, venv_dir)
        return
    shown = ""
    for paths, venv_dir in candidates:
        if paths:
            shown = paths[0]
            break
        if venv_dir:
            shown = venv_dir
            break
    emit("unclear", command, "different-path", shown)


if __name__ == "__main__":
    main()
'@
# END TPLAY_PIPX_CHECK

$RepoUrl = "git+https://github.com/parthakk07/tplay.git"

function Get-PythonPrefix {
    if ($script:Python.Length -gt 1) {
        return @($script:Python[1..($script:Python.Length - 1)])
    }
    return @()
}

function Invoke-PipxCapture([string[]]$PipxArgs) {
    $stdout = New-TemporaryFile
    $stderr = New-TemporaryFile
    if (Test-Command "pipx") {
        & pipx @PipxArgs > "$stdout" 2> "$stderr"
    } else {
        $prefix = @(Get-PythonPrefix)
        & $script:Python[0] @prefix @("-m", "pipx") @PipxArgs > "$stdout" 2> "$stderr"
    }
    $code = $LASTEXITCODE
    $outText = ""
    $errText = ""
    if (Test-Path "$stdout") { $outText = [string](Get-Content -Raw -ErrorAction SilentlyContinue "$stdout") }
    if (Test-Path "$stderr") { $errText = [string](Get-Content -Raw -ErrorAction SilentlyContinue "$stderr") }
    Remove-Item -Force "$stdout", "$stderr" -ErrorAction SilentlyContinue
    return @{ Code = $code; Out = $outText; Err = $errText }
}

function ConvertTo-RealPath([string]$Path) {
    if (-not $Path) { return "" }
    $prefix = @(Get-PythonPrefix)
    $resolved = & $script:Python[0] @prefix -c "import os,sys; print(os.path.realpath(sys.argv[1]))" $Path
    if ($LASTEXITCODE -ne 0) { return "" }
    return ([string]$resolved).Trim()
}

function Test-VenvTargetSafe([string]$Venv, [string]$VenvsRoot) {
    if (-not $Venv -or -not $VenvsRoot) { return $false }
    $expected = ConvertTo-RealPath (Join-Path $VenvsRoot "tplay")
    $actual = ConvertTo-RealPath $Venv
    return [bool]($expected -and $actual -and ($expected -ieq $actual))
}

function Test-ShimSafe([string]$Shim, [string]$BinDir) {
    if (-not $Shim -or -not $BinDir) { return $false }
    $name = [IO.Path]::GetFileName($Shim).ToLowerInvariant()
    if ($name -ne "tplay" -and $name -ne "tplay.exe") { return $false }
    $shimDir = ConvertTo-RealPath ([IO.Path]::GetDirectoryName($Shim))
    $expected = ConvertTo-RealPath $BinDir
    return [bool]($shimDir -and $expected -and ($shimDir -ieq $expected))
}

function Show-PipxResult($Result) {
    if ($Result.Out) { Write-Host $Result.Out }
    if ($Result.Err) { Write-Host $Result.Err }
}

function Stop-Install([string]$Message) {
    Write-Host $Message
    throw "tplay was not changed."
}

function Get-LeaveAloneMessage([string]$Command, [string]$Reason, [string]$Extra) {
    $settings = "Settings in ~/.config/tplay were not changed."
    switch ($Reason) {
        "not-listed" {
            return @"
tplay is already installed at: $Command
That copy is not managed by pipx, so it was left unchanged.
$settings

Next steps:
  Uninstall that copy the same way it was installed.
  Open a new terminal and make sure the tplay command is gone.
  Run this installer again.
"@
        }
        "different-path" {
            $other = if ($Extra) { $Extra } else { "unknown" }
            return @"
tplay on PATH is: $Command
pipx has a different tplay at: $other
Neither copy was changed. $settings

Next steps:
  This installer only updates the tplay command that pipx exposes.
  Remove the other tplay from PATH, or uninstall it the way it was installed.
  Run this installer again.
"@
        }
        "pipx-missing" {
            return @"
tplay is already installed at: $Command
pipx is not available, so this installer cannot tell whether pipx owns that copy.
Nothing was changed. $settings

Next steps:
  Install pipx, run 'pipx list', and then run this installer again.
  If tplay came from another tool, uninstall that copy first.
"@
        }
        "list-failed" {
            return @"
tplay is already installed at: $Command
pipx list failed, so this installer cannot tell whether pipx owns that copy.
Nothing was changed. $settings

Next steps:
  Fix 'pipx list', then run this installer again.
  If tplay came from another tool, uninstall that copy first.
"@
        }
        default {
            return @"
tplay is already installed at: $Command
This installer could not tell whether it is safe to update ($Reason).
Nothing was changed. $settings

Next steps:
  Run 'pipx list' and then run this installer again.
  If tplay came from another tool, uninstall that copy first.
"@
        }
    }
}

function Backup-ManagedTplay([string]$Venv, [string]$BinDir) {
    $backup = Join-Path ([IO.Path]::GetTempPath()) ("tplay-install-" + [guid]::NewGuid().ToString("n"))
    New-Item -ItemType Directory -Path $backup | Out-Null
    Copy-Item -Path $Venv -Destination (Join-Path $backup "venv") -Recurse
    if (-not $BinDir) { return $backup }
    foreach ($name in @("tplay", "tplay.exe")) {
        $shim = Join-Path $BinDir $name
        if ((Test-Path $shim) -and (Test-ShimSafe $shim $BinDir)) {
            Copy-Item -Path $shim -Destination (Join-Path $backup "shim-$name") -Force
            Set-Content -Path (Join-Path $backup "shim-$name.path") -Value $shim -NoNewline
        }
    }
    return $backup
}

function Restore-ManagedTplay([string]$Venv, [string]$VenvsRoot, [string]$BinDir, [string]$Backup) {
    $saved = Join-Path $Backup "venv"
    if (-not (Test-Path $saved)) {
        throw "Could not restore tplay because the backup is missing: $Backup"
    }
    if (-not (Test-VenvTargetSafe $Venv $VenvsRoot)) {
        throw "Refusing to restore $Venv because it is outside the pipx venv directory. Backup: $Backup"
    }
    $broken = "$Venv.broken-update"
    if (Test-Path $broken) { Remove-Item -Recurse -Force $broken }
    if (Test-Path $Venv) {
        Move-Item -Path $Venv -Destination $broken
    }
    try {
        Move-Item -Path $saved -Destination $Venv
        if (Test-Path $broken) { Remove-Item -Recurse -Force $broken }
    } catch {
        if ((Test-Path $broken) -and -not (Test-Path $Venv)) {
            Move-Item -Path $broken -Destination $Venv
        }
        throw "Could not restore the previous tplay install. The backup is in ${Backup}. $_"
    }
    foreach ($name in @("tplay", "tplay.exe")) {
        $pathFile = Join-Path $Backup "shim-$name.path"
        $savedShim = Join-Path $Backup "shim-$name"
        if (-not (Test-Path $pathFile) -or -not (Test-Path $savedShim)) { continue }
        $shim = (Get-Content -Raw $pathFile).Trim()
        if (-not (Test-ShimSafe $shim $BinDir)) {
            throw "The previous tplay command was not put back at $shim. Backup: $Backup"
        }
        if (Test-Path $shim) { Remove-Item -Force $shim }
        Copy-Item -Path $savedShim -Destination $shim -Force
    }
}

function Get-TplayOwnership {
    $found = Get-Command tplay -ErrorAction SilentlyContinue
    if (-not $found) { return "absent" }
    $command = $found.Source
    $probe = Invoke-PipxCapture @("--version")
    if ($probe.Code -ne 0) {
        return "unclear`t$command`tpipx-missing"
    }
    $listed = Invoke-PipxCapture @("list", "--json")
    if ($listed.Code -ne 0) {
        Write-Host "pipx list failed, so tplay was not changed:"
        Show-PipxResult $listed
        return "unclear`t$command`tlist-failed"
    }
    $binDir = ((Invoke-PipxCapture @("environment", "--value", "PIPX_BIN_DIR")).Out).Trim()
    $venvsRoot = ((Invoke-PipxCapture @("environment", "--value", "PIPX_LOCAL_VENVS")).Out).Trim()
    $script:TplayBinDir = $binDir
    $script:TplayVenvsRoot = $venvsRoot
    $stdout = New-TemporaryFile
    $stderr = New-TemporaryFile
    $prefix = @(Get-PythonPrefix)
    $listed.Out | & $script:Python[0] @prefix @("-c", $TplayPipxCheck, $command, $binDir, $venvsRoot) > "$stdout" 2> "$stderr"
    $code = $LASTEXITCODE
    $line = [string](Get-Content -Raw -ErrorAction SilentlyContinue "$stdout")
    Remove-Item -Force "$stdout", "$stderr" -ErrorAction SilentlyContinue
    if ($code -ne 0 -or -not $line.Trim()) {
        return "unclear`t$command`tcheck-failed"
    }
    return $line.Trim()
}

$pipxReady = Invoke-PipxCapture @("--version")
if ($pipxReady.Code -ne 0) {
    Invoke-Py @("-m", "pip", "install", "--user", "pipx")
    Invoke-Py @("-m", "pipx", "ensurepath")
    Update-SessionPath
}

$here = $PSScriptRoot
$mode = "git"
$spec = $RepoUrl
if ($here -and (Test-Path (Join-Path $here "pyproject.toml"))) {
    $mode = "editable"
    $spec = $here
}

$ownership = Get-TplayOwnership
$parts = @($ownership -split "`t", 4)
$kind = $parts[0]
$ownedCommand = if ($parts.Length -gt 1) { $parts[1] } else { "" }
$field3 = if ($parts.Length -gt 2) { $parts[2] } else { "" }
$field4 = if ($parts.Length -gt 3) { $parts[3] } else { "" }

function Install-TplayNow([string]$Mode, [string]$Spec) {
    Write-Host "Installing tplay..."
    if ($Mode -eq "editable") {
        $installed = Invoke-PipxCapture @("install", "--force", "--editable", $Spec)
    } else {
        $installed = Invoke-PipxCapture @("install", "--force", $Spec)
    }
    Show-PipxResult $installed
    if ($installed.Code -ne 0) {
        Write-Host "Could not install tplay."
        throw "Could not install tplay."
    }
    Invoke-PipxCapture @("ensurepath") | Out-Null
    Update-SessionPath
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $found = Get-Command tplay -ErrorAction SilentlyContinue
    if (-not $found) {
        Write-Host "Could not install tplay. The tplay command is still missing."
        throw "Could not install tplay. The tplay command is still missing."
    }
    & $found.Source --help > $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "tplay was installed but failed its check, so it was not started."
        throw "tplay was installed but failed its check, so it was not started."
    }
    Write-Host "Installed tplay."
    & $found.Source
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}

function Update-TplayNow([string]$Mode, [string]$Spec, [string]$Venv, [string]$Command) {
    Write-Host "Updating the pipx installation of tplay..."
    $binDir = $script:TplayBinDir
    $venvsRoot = $script:TplayVenvsRoot
    if (-not (Test-VenvTargetSafe $Venv $venvsRoot) -or -not (Test-Path $Venv)) {
        Stop-Install (Get-LeaveAloneMessage $Command "outside-pipx-home" $Venv)
    }
    $backup = Backup-ManagedTplay $Venv $binDir
    if ($Mode -eq "editable") {
        $updated = Invoke-PipxCapture @("install", "--force", "--editable", $Spec)
    } else {
        $updated = Invoke-PipxCapture @("install", "--force", $Spec)
    }
    Show-PipxResult $updated
    if ($updated.Code -ne 0) {
        Write-Host "Updating tplay failed. The previous installation is still in place."
        Restore-ManagedTplay $Venv $venvsRoot $binDir $backup
        Remove-Item -Recurse -Force $backup -ErrorAction SilentlyContinue
        throw "Updating tplay failed."
    }
    & $Command --help > $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "The updated tplay failed its check. The previous installation was restored."
        Restore-ManagedTplay $Venv $venvsRoot $binDir $backup
        Remove-Item -Recurse -Force $backup -ErrorAction SilentlyContinue
        throw "The updated tplay failed its check."
    }
    Remove-Item -Recurse -Force $backup -ErrorAction SilentlyContinue
    Write-Host "Updated tplay."
    & $Command
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}

if ($kind -eq "absent") {
    Install-TplayNow $mode $spec
} elseif ($kind -eq "managed") {
    Update-TplayNow $mode $spec $field3 $ownedCommand
} elseif ($kind -eq "unknown" -or $kind -eq "unclear") {
    Stop-Install (Get-LeaveAloneMessage $ownedCommand $field3 $field4)
} else {
    Stop-Install "Could not decide how tplay is installed. Nothing was changed."
}
