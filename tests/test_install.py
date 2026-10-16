"""Installer behavior with a fake pipx. These tests do not touch the real tplay."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install"
INSTALL_PS1 = ROOT / "install.ps1"
REPO_URL = "git+https://github.com/parthakk07/tplay.git"
SETTINGS = '{"volume": 17, "keep": "yes"}\n'


def _extract_checker(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "# BEGIN TPLAY_PIPX_CHECK")
    end = next(i for i, line in enumerate(lines) if line.strip() == "# END TPLAY_PIPX_CHECK")
    cleaned = []
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if stripped in {"PY", "'@", "@'"} or line == ")":
            continue
        if stripped.startswith("TPLAY_PIPX_CHECK=") or stripped.startswith("$TplayPipxCheck"):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip() + "\n"


def _write_exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _snap(path: Path):
    if not os.path.lexists(path):
        return None
    info = path.lstat()
    return (info.st_ino, info.st_mtime_ns, info.st_size)


@pytest.fixture(autouse=True)
def user_tplay_untouched():
    paths = [
        Path.home() / ".local/bin/tplay",
        Path.home() / ".local/share/pipx/venvs/tplay/bin/tplay",
    ]
    before = [(path, _snap(path)) for path in paths]
    yield
    after = [(path, _snap(path)) for path in paths]
    assert after == before


def _stubs(home: Path) -> Path:
    stub = home / "stub"
    stub.mkdir()
    python = shutil_which("python3")
    os.symlink(python, stub / "python3")
    _write_exe(
        stub / "sudo",
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$TPLAY_SUDO_LOG\"\nexit 0\n",
    )
    _write_exe(stub / "git", "#!/bin/sh\nexit 0\n")
    _write_exe(stub / "mpv", "#!/bin/sh\nexit 0\n")
    _write_exe(
        stub / "pipx",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$TPLAY_PIPX_LOG"
if [ "$1" = "list" ]; then
  if [ -n "$TPLAY_PIPX_LIST_FAIL" ]; then
    echo "pipx list exploded" >&2
    exit 1
  fi
  cat "$TPLAY_PIPX_LIST" || exit 1
  exit 0
fi
if [ "$1" = "environment" ]; then
  case "$3" in
    PIPX_BIN_DIR) printf '%s\\n' "$TPLAY_BIN_DIR" ;;
    PIPX_LOCAL_VENVS) printf '%s\\n' "$TPLAY_VENVS" ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [ "$1" = "ensurepath" ] || [ "$1" = "--version" ]; then
  exit 0
fi
if [ "$1" = "install" ]; then
  if [ -n "$TPLAY_PIPX_INSTALL_CODE" ] && [ "$TPLAY_PIPX_FAIL_CLEAN" = "1" ]; then
    echo "pipx could not reach the repo" >&2
    exit "$TPLAY_PIPX_INSTALL_CODE"
  fi
  mkdir -p "$TPLAY_BIN_DIR"
  if [ "$TPLAY_PIPX_DAMAGE" = "1" ]; then
    if [ -n "$TPLAY_VENV" ]; then
      rm -f "$TPLAY_VENV/MARKER"
      printf '%s\\n' '#!/bin/sh' 'echo BROKEN' 'exit 1' > "$TPLAY_VENV/bin/tplay"
      chmod +x "$TPLAY_VENV/bin/tplay"
    fi
    printf '%s\\n' '#!/bin/sh' 'echo BROKEN' 'exit 1' > "$TPLAY_BIN_DIR/tplay"
    chmod +x "$TPLAY_BIN_DIR/tplay"
  elif [ "$TPLAY_HELP_FAIL" = "1" ]; then
    printf '%s\\n' '#!/bin/sh' 'exit 1' > "$TPLAY_BIN_DIR/tplay"
    chmod +x "$TPLAY_BIN_DIR/tplay"
  else
    cat > "$TPLAY_BIN_DIR/tplay" << EOF
#!/bin/sh
if [ "\\$1" = "--help" ]; then
  echo NEW
  exit 0
fi
echo launched > "\\$TPLAY_LAUNCH_FILE"
exit 0
EOF
    chmod +x "$TPLAY_BIN_DIR/tplay"
  fi
  if [ -n "$TPLAY_PIPX_INSTALL_CODE" ]; then
    exit "$TPLAY_PIPX_INSTALL_CODE"
  fi
  exit 0
fi
exit 0
""",
    )
    return stub


def shutil_which(name: str) -> str:
    import shutil

    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} is not on PATH")
    return found


def _tools(home: Path) -> Path:
    tools = home / "tools"
    tools.mkdir()
    for name in ("uname", "mktemp", "cat", "cp", "mv", "rm", "sed", "dirname", "basename", "chmod", "mkdir"):
        target = shutil_which(name)
        os.symlink(target, tools / name)
    return tools


def _env(home: Path, stub: Path, extra: dict[str, str] | None = None, tools: Path | None = None) -> dict[str, str]:
    path = f"{stub}{os.pathsep}{tools}" if tools is not None else f"{stub}{os.pathsep}/usr/bin{os.pathsep}/bin"
    env = {
        "HOME": str(home),
        "PATH": path,
        "TPLAY_PIPX_LOG": str(home / "pipx.log"),
        "TPLAY_SUDO_LOG": str(home / "sudo.log"),
        "TPLAY_LAUNCH_FILE": str(home / "launched.txt"),
        "TPLAY_BIN_DIR": str(home / ".local/bin"),
        "TPLAY_VENVS": str(home / ".local/share/pipx/venvs"),
        "TPLAY_VENV": str(home / ".local/share/pipx/venvs/tplay"),
        "TPLAY_PIPX_LIST": str(home / "pipx-list.json"),
        "TMPDIR": str(home / "tmp"),
        "LANG": "C",
    }
    (home / "tmp").mkdir()
    if extra:
        env.update(extra)
    return env


def _settings(home: Path) -> Path:
    path = home / ".config/tplay/state.json"
    path.parent.mkdir(parents=True)
    path.write_text(SETTINGS, encoding="utf-8")
    return path


def _empty_list(home: Path) -> None:
    (home / "pipx-list.json").write_text('{"venvs": {}}\n', encoding="utf-8")


def _managed(home: Path) -> Path:
    venv = home / ".local/share/pipx/venvs/tplay"
    binary = venv / "bin/tplay"
    _write_exe(
        binary,
        "#!/bin/sh\nif [ \"$1\" = \"--help\" ]; then echo OLD; exit 0; fi\n"
        "echo launched-old > \"$TPLAY_LAUNCH_FILE\"\nexit 0\n",
    )
    (venv / "MARKER").write_text("old-marker\n", encoding="utf-8")
    shim = home / ".local/bin/tplay"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.symlink_to(binary)
    payload = {
        "venvs": {
            "tplay": {
                "metadata": {
                    "main_package": {
                        "package": "tplay",
                        "apps": ["tplay"],
                        "app_paths": [{"__Path__": str(binary), "__type__": "Path"}],
                    }
                }
            }
        }
    }
    (home / "pipx-list.json").write_text(json.dumps(payload), encoding="utf-8")
    return shim


def _run(home: Path, env: dict[str, str], mode: str = "file") -> subprocess.CompletedProcess[str]:
    bash = shutil_which("bash")
    if mode == "curl":
        command = [bash, "-c", INSTALL.read_text(encoding="utf-8")]
    else:
        command = [bash, str(INSTALL)]
    return subprocess.run(command, cwd=home, env=env, text=True, capture_output=True, check=False)


def _pipx_log(home: Path) -> str:
    path = home / "pipx.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _help(binary: Path, home: Path) -> str:
    env = {"TPLAY_LAUNCH_FILE": str(home / "help-launch.txt"), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([str(binary), "--help"], env=env, text=True, capture_output=True, check=False)
    return result.stdout


def test_checkers_in_the_shell_and_windows_scripts_match() -> None:
    assert _extract_checker(INSTALL) == _extract_checker(INSTALL_PS1)


def test_checker_classifies_a_pipx_symlink_unknown_and_copies(tmp_path: Path) -> None:
    checker = _extract_checker(INSTALL)
    home = tmp_path
    binary = home / "venvs/tplay/bin/tplay"
    _write_exe(binary, "#!/bin/sh\nexit 0\n")
    shim = home / "bin/tplay"
    shim.parent.mkdir()
    shim.symlink_to(binary)
    listed = {
        "venvs": {
            "tplay": {
                "metadata": {
                    "main_package": {
                        "package": "tplay",
                        "apps": ["tplay"],
                        "app_paths": [{"__Path__": str(binary), "__type__": "Path"}],
                    }
                }
            }
        }
    }
    fields = _check(checker, str(shim), str(home / "bin"), str(home / "venvs"), listed)
    assert fields[:3] == ["managed", str(shim), str(home / "venvs/tplay")]

    other = home / "opt/tplay"
    _write_exe(other, "#!/bin/sh\nexit 0\n")
    fields = _check(checker, str(other), str(home / "bin"), str(home / "venvs"), {"venvs": {}})
    assert fields == ["unknown", str(other), "not-listed"]

    copied = home / "bin/tplay.exe"
    _write_exe(copied, f"launcher {home / 'venvs/tplay'}\n")
    fields = _check(checker, str(copied), str(home / "bin"), str(home / "venvs"), listed)
    assert fields[0] == "managed"
    assert fields[2] == str(home / "venvs/tplay")

    second = {
        "venvs": {
            "tplay": listed["venvs"]["tplay"],
            "other": {
                "metadata": {
                    "main_package": {
                        "package": "tplay",
                        "apps": ["tplay"],
                        "app_paths": [str(binary)],
                    }
                }
            },
        }
    }
    fields = _check(checker, str(binary), str(home / "bin"), str(home / "venvs"), second)
    assert fields[:3] == ["unclear", str(binary), "ambiguous"]

    loose = home / "loose/tplay"
    _write_exe(loose, "#!/bin/sh\nexit 0\n")
    odd = {
        "venvs": {
            "tplay": {
                "metadata": {"main_package": {"package": "tplay", "apps": ["tplay"], "app_paths": [str(loose)]}}
            }
        }
    }
    fields = _check(checker, str(loose), str(home / "bin"), str(home / "venvs"), odd)
    assert fields[:3] == ["unclear", str(loose), "no-venv-dir"]


def _check(checker: str, command: str, bin_dir: str, venvs: str, payload: dict) -> list[str]:
    result = subprocess.run(
        ["python3", "-c", checker, command, bin_dir, venvs],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n").split("\t")


def test_fresh_install_from_the_repo_launches_the_new_command(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    _empty_list(tmp_path)
    result = _run(tmp_path, _env(tmp_path, stub))
    assert result.returncode == 0, result.stderr
    assert "Installing tplay..." in result.stderr
    assert "Installed tplay." in result.stderr
    assert (tmp_path / "launched.txt").read_text(encoding="utf-8") == "launched\n"
    log = _pipx_log(tmp_path)
    assert f"install --force --editable {ROOT}" in log
    assert settings.read_text(encoding="utf-8") == SETTINGS
    assert not (tmp_path / "sudo.log").exists()


def test_curl_style_install_uses_the_git_repo(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    _empty_list(tmp_path)
    result = _run(tmp_path, _env(tmp_path, stub), mode="curl")
    assert result.returncode == 0, result.stderr
    assert "Installed tplay." in result.stderr
    log = _pipx_log(tmp_path)
    assert f"install --force {REPO_URL}" in log
    assert "--editable" not in log
    assert settings.read_text(encoding="utf-8") == SETTINGS


def test_fresh_install_failure_does_not_launch_or_claim_success(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    _empty_list(tmp_path)
    env = _env(tmp_path, stub, {"TPLAY_PIPX_INSTALL_CODE": "7", "TPLAY_PIPX_FAIL_CLEAN": "1"})
    result = _run(tmp_path, env)
    assert result.returncode == 7
    assert "Could not install tplay." in result.stderr
    assert "Installed tplay." not in result.stderr
    assert "tplay is installed. Open a new terminal" not in result.stderr
    assert not (tmp_path / "launched.txt").exists()
    assert not (tmp_path / ".local/bin/tplay").exists()
    assert settings.read_text(encoding="utf-8") == SETTINGS


def test_update_of_a_pipx_install_launches_only_the_new_copy(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    shim = _managed(tmp_path)
    result = _run(tmp_path, _env(tmp_path, stub), mode="curl")
    assert result.returncode == 0, result.stderr
    assert "Updating the pipx installation of tplay..." in result.stderr
    assert "Updated tplay." in result.stderr
    assert (tmp_path / "launched.txt").read_text(encoding="utf-8") == "launched\n"
    assert "launched-old" not in (tmp_path / "launched.txt").read_text(encoding="utf-8")
    assert f"install --force {REPO_URL}" in _pipx_log(tmp_path)
    assert settings.read_text(encoding="utf-8") == SETTINGS
    assert shim.exists()


def test_unknown_install_is_left_unchanged(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    _empty_list(tmp_path)
    binary = tmp_path / "opt/tplay"
    _write_exe(binary, "#!/bin/sh\necho OTHER\nexit 0\n")
    before = binary.read_bytes()
    env = _env(tmp_path, stub)
    env["PATH"] = f"{binary.parent}{os.pathsep}{env['PATH']}"
    result = _run(tmp_path, env)
    assert result.returncode != 0
    assert f"tplay is already installed at: {binary}" in result.stderr
    assert "not managed by pipx" in result.stderr
    assert "left unchanged" in result.stderr
    assert "Next steps:" in result.stderr
    assert "Installed tplay." not in result.stderr
    assert "Updated tplay." not in result.stderr
    assert binary.read_bytes() == before
    assert not (tmp_path / "launched.txt").exists()
    assert "install " not in _pipx_log(tmp_path)
    assert settings.read_text(encoding="utf-8") == SETTINGS


def test_pipx_list_failure_changes_nothing(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    binary = tmp_path / "opt/tplay"
    _write_exe(binary, "#!/bin/sh\necho OTHER\nexit 0\n")
    before = binary.read_bytes()
    env = _env(tmp_path, stub, {"TPLAY_PIPX_LIST_FAIL": "1"})
    env["PATH"] = f"{binary.parent}{os.pathsep}{env['PATH']}"
    result = _run(tmp_path, env)
    assert result.returncode != 0
    assert "pipx list failed" in result.stderr
    assert "pipx list exploded" in result.stderr
    assert "Nothing was changed" in result.stderr
    assert binary.read_bytes() == before
    assert not (tmp_path / "launched.txt").exists()
    assert "install " not in _pipx_log(tmp_path)
    assert settings.read_text(encoding="utf-8") == SETTINGS


def test_a_different_pipx_copy_is_not_replaced(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    shim = _managed(tmp_path)
    shim.unlink()
    other = tmp_path / "opt/tplay"
    _write_exe(other, "#!/bin/sh\necho OTHER\nexit 0\n")
    venv_binary = tmp_path / ".local/share/pipx/venvs/tplay/bin/tplay"
    before_other = other.read_bytes()
    before_venv = venv_binary.read_bytes()
    env = _env(tmp_path, stub)
    env["PATH"] = f"{other.parent}{os.pathsep}{env['PATH']}"
    result = _run(tmp_path, env)
    assert result.returncode != 0
    assert f"tplay on PATH is: {other}" in result.stderr
    assert "Neither copy was changed" in result.stderr
    assert str(venv_binary) in result.stderr
    assert other.read_bytes() == before_other
    assert venv_binary.read_bytes() == before_venv
    assert "install " not in _pipx_log(tmp_path)
    assert settings.read_text(encoding="utf-8") == SETTINGS


def test_a_venv_outside_pipx_home_is_not_deleted(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    evil = tmp_path / "evil/venvs/tplay/bin/tplay"
    _write_exe(evil, "#!/bin/sh\necho EVIL\nexit 0\n")
    before = evil.read_bytes()
    payload = {
        "venvs": {
            "tplay": {
                "metadata": {
                    "main_package": {
                        "package": "tplay",
                        "apps": ["tplay"],
                        "app_paths": [str(evil)],
                    }
                }
            }
        }
    }
    (tmp_path / "pipx-list.json").write_text(json.dumps(payload), encoding="utf-8")
    env = _env(tmp_path, stub)
    env["PATH"] = f"{evil.parent}{os.pathsep}{env['PATH']}"
    result = _run(tmp_path, env)
    assert result.returncode != 0, result.stderr
    assert "outside-pipx-home" in result.stderr
    assert "Nothing was changed" in result.stderr
    assert evil.read_bytes() == before
    assert evil.parent.is_dir()
    assert "install " not in _pipx_log(tmp_path)
    assert settings.read_text(encoding="utf-8") == SETTINGS


def test_failed_update_restores_the_previous_pipx_install(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    shim = _managed(tmp_path)
    env = _env(tmp_path, stub, {"TPLAY_PIPX_DAMAGE": "1", "TPLAY_PIPX_INSTALL_CODE": "4"})
    result = _run(tmp_path, env, mode="curl")
    assert result.returncode == 4
    assert "Updating tplay failed" in result.stderr
    assert "previous installation is still in place" in result.stderr
    assert "Updated tplay." not in result.stderr
    assert "Installed tplay." not in result.stderr
    assert not (tmp_path / "launched.txt").exists()
    assert _help(shim, tmp_path) == "OLD\n"
    marker = tmp_path / ".local/share/pipx/venvs/tplay/MARKER"
    assert marker.read_text(encoding="utf-8") == "old-marker\n"
    assert settings.read_text(encoding="utf-8") == SETTINGS


def test_failed_update_check_restores_the_previous_pipx_install(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    settings = _settings(tmp_path)
    shim = _managed(tmp_path)
    env = _env(tmp_path, stub, {"TPLAY_HELP_FAIL": "1"})
    result = _run(tmp_path, env, mode="curl")
    assert result.returncode == 1
    assert "failed its check" in result.stderr
    assert "previous installation was restored" in result.stderr
    assert "Updated tplay." not in result.stderr
    assert not (tmp_path / "launched.txt").exists()
    assert _help(shim, tmp_path) == "OLD\n"
    assert settings.read_text(encoding="utf-8") == SETTINGS


def test_missing_mpv_still_installs_and_launches(tmp_path: Path) -> None:
    stub = _stubs(tmp_path)
    (stub / "mpv").unlink()
    _write_exe(stub / "apt-get", "#!/bin/sh\nexit 0\n")
    settings = _settings(tmp_path)
    _empty_list(tmp_path)
    result = _run(tmp_path, _env(tmp_path, stub, tools=_tools(tmp_path)))
    assert result.returncode == 0, result.stderr
    assert "mpv is not installed" in result.stderr
    assert "sudo apt install mpv" in result.stderr
    assert "Installed tplay." in result.stderr
    assert (tmp_path / "launched.txt").read_text(encoding="utf-8") == "launched\n"
    sudo_log = (tmp_path / "sudo.log").read_text(encoding="utf-8")
    assert "mpv" in sudo_log
    assert "pipx" not in sudo_log
    assert settings.read_text(encoding="utf-8") == SETTINGS
