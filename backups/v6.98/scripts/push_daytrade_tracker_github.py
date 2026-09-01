#!/usr/bin/env python3
"""SSH push ~/.hermes/www/daytrade-tracker → git@github.com:WT-Jerry/tw-daytrade-desk.git

Used after 07:30 report publishes local tracker data.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

OWNER = "WT-Jerry"
REPO = "tw-daytrade-desk"
SSH_REMOTE = f"git@github.com:{OWNER}/{REPO}.git"
WEB_ROOT = Path.home() / ".hermes" / "www" / "daytrade-tracker"
BRANCH = "main"


def run(cmd, cwd=None, check=True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    p = subprocess.run(
        cmd,
        cwd=str(cwd or WEB_ROOT),
        text=True,
        capture_output=True,
        env=env,
    )
    if check and p.returncode != 0:
        raise RuntimeError(
            f"cmd failed ({p.returncode}): {' '.join(cmd)}\n"
            f"stdout: {p.stdout[-800:]}\nstderr: {p.stderr[-800:]}"
        )
    return p


def ensure_repo() -> None:
    WEB_ROOT.mkdir(parents=True, exist_ok=True)
    git_dir = WEB_ROOT / ".git"
    if not git_dir.is_dir():
        run(["git", "init", "-b", BRANCH])
        run(["git", "config", "user.email", "hermes@nousresearch.com"])
        run(["git", "config", "user.name", "Hermes Agent"])
    # remote
    p = run(["git", "remote"], check=False)
    remotes = set((p.stdout or "").split())
    if "origin" not in remotes:
        run(["git", "remote", "add", "origin", SSH_REMOTE])
    else:
        run(["git", "remote", "set-url", "origin", SSH_REMOTE])


def ensure_nojekyll() -> None:
    p = WEB_ROOT / ".nojekyll"
    if not p.exists():
        p.write_text("", encoding="utf-8")


def commit_and_push(message: str | None = None) -> dict:
    ensure_repo()
    ensure_nojekyll()

    run(["git", "add", "-A"])
    st = run(["git", "status", "--porcelain"], check=False)
    porcelain = (st.stdout or "").strip()
    if not porcelain:
        # still try push in case remote behind
        push = run(["git", "push", "-u", "origin", BRANCH], check=False)
        return {
            "ok": push.returncode == 0,
            "changed": False,
            "message": "no local changes",
            "push_rc": push.returncode,
            "push_stderr": (push.stderr or "")[-400:],
            "remote": SSH_REMOTE,
            "pages": f"https://wt-jerry.github.io/{REPO}/",
        }

    msg = message or f"chore(desk): update tracker {datetime.now().strftime('%Y-%m-%d %H:%M:%S %z')}".strip()
    run(["git", "commit", "-m", msg])
    push = run(["git", "push", "-u", "origin", BRANCH], check=False)
    if push.returncode != 0:
        # first push / non-fast-forward: pull --rebase then push
        run(["git", "pull", "--rebase", "origin", BRANCH], check=False)
        push = run(["git", "push", "-u", "origin", BRANCH], check=False)
    if push.returncode != 0:
        raise RuntimeError(f"git push failed\n{push.stdout}\n{push.stderr}")

    return {
        "ok": True,
        "changed": True,
        "message": msg,
        "remote": SSH_REMOTE,
        "pages": f"https://wt-jerry.github.io/{REPO}/",
        "commit": run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip(),
    }


def main() -> int:
    try:
        info = commit_and_push()
    except Exception as e:
        print(f"PUSH_FAILED: {e}", file=sys.stderr)
        return 1
    print("PUSH_OK", info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
