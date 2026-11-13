"""Check that this machine is ready to run RoleGenie (BRIEF-027, CR-rg-tester-install).

    python first_run_check.py

Standard library only, so it runs before anything is installed. Each line is OK, MISSING (with the one thing to do)
or OPTIONAL. Exit 0 when every required item is OK, 1 otherwise. Key values are never printed. The only thing it may
create is the empty data folder.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# name -> (where to get it, required). Adzuna is only one source of the daily job scan (ruling, BRIEF-027 section 4).
KEYS = {
    "TYPESAFE_API_KEY": ("your Jev API key, from your Jev account", True),
    "ADZUNA_APP_ID": ("a free Adzuna developer app id, from developer.adzuna.com", False),
    "ADZUNA_APP_KEY": ("a free Adzuna developer app key, from developer.adzuna.com", False),
}
MIN_PYTHON = (3, 12)


def check_python():
    v = sys.version_info
    ok = v[:2] >= MIN_PYTHON
    return ok, f"Python {v.major}.{v.minor}.{v.micro}", "install Python 3.12 or newer from python.org, then recreate .venv"


def check_packages():
    req = ROOT / "requirements.txt"
    if not req.exists():
        return False, "Python packages: requirements.txt not found", "run this from the RoleGenie folder you cloned"
    missing = []
    for raw in req.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0]
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
    if missing:
        return False, f"Python packages not installed: {', '.join(missing)}", \
            "activate .venv, then: pip install -r requirements.txt"
    return True, "Python packages installed", ""


def check_tool(cmd, label, fix, note=""):
    found = shutil.which(cmd)
    return bool(found), f"{label} ({found})" + note if found else f"{label} not found", fix


def env_file_values():
    env = ROOT / ".env"
    values = {}
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def check_keys():
    rows = []
    env = ROOT / ".env"
    rows.append((env.exists(), ".env file " + ("found" if env.exists() else "missing"),
                 "copy .env.example to .env, then fill in the keys"))
    values = env_file_values()
    for key, (where, required) in KEYS.items():
        ok = bool(os.environ.get(key) or values.get(key))
        if required:
            rows.append((ok, f"{key} {'set' if ok else 'missing'}",
                         f"put {where} in .env (copy .env.example to .env if you have no .env yet)"))
        else:
            rows.append((ok, f"{key} {'set' if ok else 'missing'}" + ("" if ok else
                         f"; only needed for the daily job scan's Adzuna source: {where}"), "", "optional"))
    return rows


def data_dir():
    home = os.environ.get("ROLEGENIE_HOME")
    return Path(home).expanduser() if home else Path.home() / ".rolegenie"


def check_data_dir():
    d = data_dir()
    fix = f"set ROLEGENIE_HOME to a folder you can write to, or make {d} writable"
    try:
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=d):
            pass
    except OSError as e:
        return False, f"data folder {d} not writable ({type(e).__name__})", fix
    return True, f"data folder {d} writable", fix


def check_example_config():
    ok = (ROOT / "config.example.yaml").exists()
    return ok, "config.example.yaml " + ("found" if ok else "missing"), \
        "update your clone (git pull): the file ships with RoleGenie"


def check_workflow_setup():
    """The daily workflow's own prerequisites (BRIEF-038). They are set up AFTER install (onboard, board setup), so they
    never fail the install check: each says OK or what to do next and why it matters. Read-only."""
    d = data_dir()
    rows = [
        ((d / "config.yaml").exists(), "your search config (keywords, location) in your data folder",
         "run `python rolegenie.py search setup` to create config.yaml there and choose your job search"),
        ((d / "master.json").exists(), "approved master CV",
         "needed before the daily run: `python rolegenie.py onboard FILE...`, then `master approve`"),
        ((d / "board.json").exists(), "phone board set up",
         "optional until you want decisions on your phone: `python rolegenie.py board setup` (README, Phone board)"),
        (any(shutil.which(b) for b in ("chromium", "chromium-browser", "google-chrome", "chrome")),
         "a Chrome/Chromium browser for fetching full adverts",
         "optional: run `playwright install chromium` after installing the packages"),
        (bool(re.search(r"(?m)^mail:\s*(?:\n\s+enabled:\s*true|\{[^}\n]*enabled:\s*true)", (d / "config.yaml").read_text(encoding="utf-8")
                        if (d / "config.yaml").exists() else "")), "recruiter-mail review is on",
         "optional: add `mail: {enabled: true}` to config.yaml to queue recruiter replies for your review"),
    ]
    for ok, what, fix in rows:
        yield (ok, what if ok else f"{what} not ready ({fix})", fix, "optional")


def required_checks():
    yield check_python
    yield check_packages
    yield lambda: check_tool("claude", "Claude Code CLI 'claude'",
                             "install Claude Code (claude.com/claude-code), then run 'claude' once to log in",
                             "; login is not checked: run 'claude' once if you have not")
    yield check_keys
    yield check_data_dir
    yield check_example_config


def main() -> int:
    print("RoleGenie first-run check\n")
    all_ok = True
    for check in required_checks():
        try:
            result = check()
        except Exception as e:  # one broken item is reported, never a traceback
            result = (False, f"check failed unexpectedly ({type(e).__name__})", "report this to whoever shared RoleGenie")
        for ok, what, fix, *optional in result if isinstance(result, list) else [result]:
            if ok:
                print(f"OK       {what}")
            elif optional:
                print(f"OPTIONAL {what}")
            else:
                all_ok = False
                print(f"MISSING  {what}  -> {fix}")
    for ok, what, _fix, *_ in check_workflow_setup():
        print(f"OK       {what}" if ok else f"NEXT     {what}")
    node = shutil.which("node")
    print(f"OK       Node.js ({node})" if node else
          "OPTIONAL Node.js not found; only needed for developers running the gate (nodejs.org)")
    rclone = shutil.which("rclone")
    print(f"OK       rclone ({rclone}) for the optional backup" if rclone else
          "OPTIONAL rclone not found; only needed for the optional backup (sync: remote: in config.yaml), rclone.org")
    print("OPTIONAL phone board: set up in your browser when you want it (README, Phone board)")
    print("\nReady: every required item is OK." if all_ok else "\nNot ready: fix each MISSING line, then run this again.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
