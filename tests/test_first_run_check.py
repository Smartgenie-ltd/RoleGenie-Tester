"""first_run_check.py: a tester's prerequisites reported OK / MISSING / OPTIONAL (BRIEF-027, CR-rg-tester-install).

Each case runs the script as a subprocess from a tmp copy holding only the script and the files under test, with
ROLEGENIE_HOME always a tmp dir and PATH restricted to a stub bin/ (claude, node; rclone only when asked for).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("TYPESAFE_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY")
FAKE = {"TYPESAFE_API_KEY": "fake-jev-7f3a91", "ADZUNA_APP_ID": "fake-id-0c2d55", "ADZUNA_APP_KEY": "fake-key-b81e40"}
WIN = os.name == "nt"


def _stub(bin_dir: Path, name: str) -> None:
    if WIN:
        (bin_dir / f"{name}.cmd").write_text("@echo off\r\n", encoding="ascii")
    else:
        p = bin_dir / name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        p.chmod(0o755)


def _clone(tmp: Path, *, env_file: str | None = None, example: bool = True, tools=("claude", "node")) -> tuple[Path, dict]:
    clone = tmp / "clone"
    clone.mkdir(parents=True)
    shutil.copy(ROOT / "first_run_check.py", clone)
    # pytest is installed wherever this suite runs; the inline comment and specifier must be stripped.
    (clone / "requirements.txt").write_text("\n# a comment line\npytest>=8.0    # CR-x: inline comment\n", encoding="utf-8")
    if example:
        (clone / "config.example.yaml").write_text("search: {}\n", encoding="utf-8")
    if env_file is not None:
        (clone / ".env").write_text(env_file, encoding="utf-8")
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True)
    for t in tools:
        _stub(bin_dir, t)
    env = {k: v for k, v in os.environ.items() if k not in KEYS}
    env["PATH"] = str(bin_dir)
    env["ROLEGENIE_HOME"] = str(tmp / "home")
    return clone, env


def _run(clone: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(clone / "first_run_check.py")], cwd=clone, env=env,
                          capture_output=True, text=True, timeout=60)


def _line(out: str, needle: str) -> str:
    hits = [l for l in out.splitlines() if needle in l]
    assert hits, f"no line with {needle!r} in:\n{out}"
    return hits[0]


def _full_env() -> str:
    return "".join(f"{k}={v}\n" for k, v in FAKE.items())


def test_no_env_file_reports_each_key_missing_with_fix(tmp_path):
    clone, env = _clone(tmp_path)
    r = _run(clone, env)
    assert r.returncode == 1
    assert "Traceback" not in r.stdout + r.stderr
    line = _line(r.stdout, "TYPESAFE_API_KEY")
    assert line.startswith("MISSING") and "missing" in line
    for k in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"):  # ruling (section 4): Adzuna keys are optional
        line = _line(r.stdout, k)
        assert line.startswith("OPTIONAL") and "missing" in line
    assert ".env.example" in r.stdout  # the fix names the template to copy


def test_only_the_jev_key_is_required(tmp_path):
    clone, env = _clone(tmp_path, env_file=f"TYPESAFE_API_KEY={FAKE['TYPESAFE_API_KEY']}\n", tools=("claude",))
    r = _run(clone, env)
    assert r.returncode == 0, r.stdout  # no Adzuna keys, no Node: still ready
    assert _line(r.stdout, "ADZUNA_APP_ID").startswith("OPTIONAL")
    assert _line(r.stdout, "Node").startswith("OPTIONAL")
    clone2, env2 = _clone(tmp_path / "b", env_file="ADZUNA_APP_ID=x\nADZUNA_APP_KEY=y\n")
    assert _run(clone2, env2).returncode == 1  # Jev key missing still fails


def test_fake_keys_set_and_never_printed(tmp_path):
    clone, env = _clone(tmp_path, env_file='TYPESAFE_API_KEY="%s"\nADZUNA_APP_ID=%s\nADZUNA_APP_KEY=\'%s\'\n'
                        % tuple(FAKE.values()))
    r = _run(clone, env)
    out = r.stdout + r.stderr
    for k in KEYS:
        line = _line(r.stdout, k)
        assert line.startswith("OK") and "set" in line
    for v in FAKE.values():
        assert v not in out
    assert r.returncode == 0, out  # every other required item is satisfied in this fixture


def test_key_from_environment_counts_as_set(tmp_path):
    clone, env = _clone(tmp_path, env_file="TYPESAFE_API_KEY=\nADZUNA_APP_ID=x\nADZUNA_APP_KEY=y\n")
    env["TYPESAFE_API_KEY"] = FAKE["TYPESAFE_API_KEY"]
    r = _run(clone, env)
    assert _line(r.stdout, "TYPESAFE_API_KEY").startswith("OK")
    assert FAKE["TYPESAFE_API_KEY"] not in r.stdout + r.stderr
    assert r.returncode == 0, r.stdout


def test_empty_key_in_env_file_is_missing(tmp_path):
    clone, env = _clone(tmp_path, env_file="TYPESAFE_API_KEY=\nADZUNA_APP_ID=x\nADZUNA_APP_KEY=y\n")
    r = _run(clone, env)
    assert _line(r.stdout, "TYPESAFE_API_KEY").startswith("MISSING")
    assert r.returncode == 1


def test_absent_data_dir_is_created_and_left_empty(tmp_path):
    clone, env = _clone(tmp_path, env_file=_full_env())
    home = Path(env["ROLEGENIE_HOME"])
    assert not home.exists()
    r = _run(clone, env)
    assert r.returncode == 0, r.stdout
    assert home.is_dir() and list(home.iterdir()) == []
    assert _line(r.stdout, str(home)).startswith("OK")


def test_unwritable_data_dir_under_a_file_is_missing(tmp_path):
    clone, env = _clone(tmp_path, env_file=_full_env())
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    env["ROLEGENIE_HOME"] = str(blocker / "home")
    r = _run(clone, env)
    assert r.returncode == 1
    line = _line(r.stdout, "data folder")
    assert line.startswith("MISSING") and "ROLEGENIE_HOME" in line
    assert "Traceback" not in r.stdout + r.stderr


@pytest.mark.skipif(WIN or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="POSIX permissions, non-root only")
def test_read_only_data_dir_is_missing(tmp_path):
    clone, env = _clone(tmp_path, env_file=_full_env())
    home = Path(env["ROLEGENIE_HOME"])
    home.mkdir()
    home.chmod(0o500)
    try:
        r = _run(clone, env)
    finally:
        home.chmod(0o700)
    assert r.returncode == 1
    assert _line(r.stdout, "data folder").startswith("MISSING")


def test_rclone_absent_is_optional_and_present_is_ok(tmp_path):
    clone, env = _clone(tmp_path, env_file=_full_env())
    r = _run(clone, env)
    assert r.returncode == 0, r.stdout
    assert "OPTIONAL rclone not found" in r.stdout  # (the tmp path itself contains "rclone")
    _stub(Path(env["PATH"]), "rclone")
    r2 = _run(clone, env)
    assert r2.returncode == 0
    assert "OK       rclone (" in r2.stdout


def test_missing_tools_and_template_are_missing_with_fixes(tmp_path):
    clone, env = _clone(tmp_path, env_file=_full_env(), example=False, tools=())
    r = _run(clone, env)
    assert r.returncode == 1
    assert _line(r.stdout, "claude").startswith("MISSING")
    assert _line(r.stdout, "Node").startswith("OPTIONAL")  # ruling: only developers running the gate need Node
    assert _line(r.stdout, "config.example.yaml").startswith("MISSING")


def test_missing_python_package_is_named(tmp_path):
    clone, env = _clone(tmp_path, env_file=_full_env())
    (clone / "requirements.txt").write_text("pytest>=8.0\nno-such-package-b027==1.0  # nope\n", encoding="utf-8")
    r = _run(clone, env)
    assert r.returncode == 1
    line = _line(r.stdout, "no-such-package-b027")
    assert line.startswith("MISSING") and "pip install -r requirements.txt" in r.stdout
    assert "pytest" not in line
