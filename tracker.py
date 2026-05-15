#!/usr/bin/env python3
"""
claude-usage-live tracker
=========================
Runs `claude --usage` every ~5h, parses the output, computes the next reset
timestamp, and detects "early resets" (i.e. Anthropic resetting all users
ahead of schedule).

Writes data/usage.json to the repo and pushes.

This script is a "canary" — it doesn't expose what *Bruno* used. It only
exposes whether the system reset earlier than expected, useful as a public
signal for the rest of the Claude Code community.
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DATA_FILE = REPO_ROOT / "data" / "usage.json"            # public, NO usage %
STATE_FILE = REPO_ROOT / ".tracker_state.json"           # local-only, gitignored
EARLY_RESET_DROP_PCT = 50  # Δ% used that triggers "early reset" alarm
MAX_HISTORY = 50            # rolling window of early-reset events kept


# ──────────────────────────────────────────────────────────────────────
# ANSI / parsing helpers (copied from memory-graph api_server.py)
# ──────────────────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def parse_usage(raw: str) -> dict | None:
    clean = re.sub(r"\s+", " ", strip_ansi(raw))
    data = {}

    m = re.search(
        r"Current\s*session\s*[█▌░\s]*(\d+)\s*%\s*used\s*Rese?t?s?\s*s?\s*(.+?)(?=Current|Extra|Esc|What|$)",
        clean, re.I,
    )
    if m:
        data["session"] = {"pct": int(m.group(1)), "resets_text": m.group(2).strip()}

    m = re.search(
        r"Current\s*week\s*\(all\s*models?\)\s*[█▌░\s]*(\d+)\s*%\s*used\s*Rese?t?s?\s*(.+?)(?=Current|Extra|Esc|What|$)",
        clean, re.I,
    )
    if m:
        data["weekAll"] = {"pct": int(m.group(1)), "resets_text": m.group(2).strip()}

    m = re.search(
        r"Extra\s*usage\s*[█▌░▏\s]*(\d+)\s*%\s*used.*?Rese?t?s?\s*(.+?)(?=Esc|Last|$)",
        clean, re.I,
    )
    if m:
        data["extra"] = {"pct": int(m.group(1)), "resets_text": m.group(2).strip()}

    return data if data else None


def find_claude_bin():
    candidates = [
        str(Path.home() / ".npm-global/bin/claude"),
        str(Path.home() / ".local/bin/claude"),
        "/usr/local/bin/claude",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return shutil.which("claude")


def fetch_claude_usage():
    """Spawn `claude` via pexpect, send /usage, harvest the TUI output."""
    try:
        import pexpect
    except ImportError:
        return {"error": "pexpect not installed"}

    binpath = find_claude_bin()
    if not binpath:
        return {"error": "claude binary not found"}

    cwd = str(Path.home())
    try:
        with open(Path.home() / ".claude.json") as f:
            cfg = json.load(f)
        trusted = [p for p, v in cfg.get("projects", {}).items()
                   if isinstance(v, dict) and v.get("hasTrustDialogAccepted") and os.path.isdir(p)]
        if trusted:
            cwd = trusted[0]
    except Exception:
        pass

    buf = ""
    child = None
    try:
        child = pexpect.spawn(
            binpath, dimensions=(50, 120), encoding="utf-8", timeout=None, cwd=cwd,
            env={**os.environ, "TERM": "xterm-256color", "PWD": cwd, "HOME": str(Path.home())},
        )

        def read_avail(t=1.0):
            try:
                return child.read_nonblocking(size=100_000, timeout=t)
            except Exception:
                return ""

        trust_confirmed = False
        deadline = time.time() + 12.0
        while time.time() < deadline:
            chunk = read_avail(0.5)
            if chunk:
                buf += chunk
                if not trust_confirmed and ("trust this folder" in buf.lower() or "Enter to confirm" in buf):
                    child.send("\r")
                    trust_confirmed = True
                    time.sleep(2.5)

        child.send("/usage")
        time.sleep(0.5)
        child.send("\r")

        deadline = time.time() + 18.0
        while time.time() < deadline:
            chunk = read_avail(0.6)
            if chunk:
                buf += chunk

        try:
            child.send("\x1b")
            time.sleep(0.3)
            child.sendline("/exit")
            time.sleep(1.5)
        except Exception:
            pass
    finally:
        if child:
            try:
                child.terminate(force=True)
            except Exception:
                pass

    data = parse_usage(buf)
    if not data:
        return {"error": "no usage data parsed", "raw_preview": strip_ansi(buf)[-400:]}
    return {"ok": True, "data": data}


# ──────────────────────────────────────────────────────────────────────
# resets_text → absolute ISO timestamp
# Claude TUI prints variations: "in 4h 30m", "Mon Apr 28 09:00", "now",
# "May 19, 09:00 UTC". This best-effort converts them.
# ──────────────────────────────────────────────────────────────────────
def parse_reset_to_iso(text: str, now: dt.datetime) -> str | None:
    if not text:
        return None
    s = text.strip().lower()

    # "in 4h 30m" / "in 12h" / "in 45m"
    m = re.search(r"in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", s)
    if m and (m.group(1) or m.group(2)):
        h = int(m.group(1) or 0)
        mn = int(m.group(2) or 0)
        return (now + dt.timedelta(hours=h, minutes=mn)).isoformat()

    # "now"
    if "now" in s:
        return now.isoformat()

    # "Mon Apr 28 09:00" (current year, local time)
    m = re.search(r"([a-z]{3})\s+([a-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})", s)
    if m:
        months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                  "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        try:
            month = months[m.group(2)]
            day = int(m.group(3))
            hh = int(m.group(4))
            mm = int(m.group(5))
            year = now.year
            cand = dt.datetime(year, month, day, hh, mm, tzinfo=now.tzinfo)
            if cand < now:
                cand = cand.replace(year=year + 1)
            return cand.isoformat()
        except Exception:
            pass

    return None


# ──────────────────────────────────────────────────────────────────────
# Early reset detection
# ──────────────────────────────────────────────────────────────────────
def detect_early_reset(prev: dict, curr: dict, now_iso: str) -> tuple[bool, dict | None]:
    """If weekAll dropped >EARLY_RESET_DROP_PCT and we are NOT yet past the
    previously-recorded next_weekly_reset_at, that's an early reset."""
    if not prev or not curr:
        return False, None
    p = (prev.get("weekAll") or {}).get("pct")
    c = (curr.get("weekAll") or {}).get("pct")
    if p is None or c is None:
        return False, None
    drop = p - c
    if drop < EARLY_RESET_DROP_PCT:
        return False, None
    prev_reset = prev.get("next_weekly_reset_at")
    if not prev_reset:
        return False, None
    try:
        if dt.datetime.fromisoformat(now_iso) >= dt.datetime.fromisoformat(prev_reset):
            return False, None  # scheduled reset — not early
    except Exception:
        return False, None
    return True, {
        "detected_at": now_iso,
        "previous_pct": p,
        "current_pct": c,
        "drop": drop,
        "expected_reset_at": prev_reset,
    }


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────
def main():
    now = dt.datetime.now(dt.timezone.utc)
    now_iso = now.isoformat()

    # Load previous state (local-only, gitignored — has the pct values
    # needed for early-reset detection. Never published.)
    prev_state = {}
    if STATE_FILE.exists():
        try:
            prev_state = json.loads(STATE_FILE.read_text())
        except Exception:
            prev_state = {}

    # Load previous public data (for history continuity)
    prev_public = {}
    if DATA_FILE.exists():
        try:
            prev_public = json.loads(DATA_FILE.read_text())
        except Exception:
            prev_public = {}

    # Fetch fresh
    fetched = fetch_claude_usage()
    if "error" in fetched:
        print(f"[tracker] fetch error: {fetched['error']}", file=sys.stderr)
        # Keep public file as-is, only update last_attempt
        out = dict(prev_public)
        out["last_attempt_at"] = now_iso
        out["last_error"] = fetched["error"]
        DATA_FILE.write_text(json.dumps(out, indent=2))
        sys.exit(1)

    data = fetched["data"]
    week_pct = (data.get("weekAll") or {}).get("pct")
    sess_pct = (data.get("session") or {}).get("pct")

    # Translate reset texts → absolute ISO
    session_reset_iso = parse_reset_to_iso((data.get("session") or {}).get("resets_text", ""), now)
    week_reset_iso = parse_reset_to_iso((data.get("weekAll") or {}).get("resets_text", ""), now)

    # Early reset detection uses prev_state (which holds the actual pct)
    curr_for_detect = {
        "weekAll": data.get("weekAll"),
        "session": data.get("session"),
    }
    prev_for_detect = {
        "weekAll": {"pct": prev_state.get("weekAll_pct")} if prev_state.get("weekAll_pct") is not None else None,
        "next_weekly_reset_at": prev_state.get("weekly_reset_at"),
    }
    early, event = detect_early_reset(prev_for_detect, curr_for_detect, now_iso)

    history = list(prev_public.get("early_reset_history") or [])
    if early and event:
        history.insert(0, event)
        history = history[:MAX_HISTORY]

    # PUBLIC file — never contains usage pct values.
    out = {
        "updated_at": now_iso,
        "next_session_reset_at": session_reset_iso,
        "next_weekly_reset_at": week_reset_iso,
        "early_reset_detected": bool(early),
        "early_reset_event": event,
        "early_reset_history": history,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(out, indent=2))

    # LOCAL state file (gitignored).
    state = {
        "weekAll_pct": week_pct,
        "session_pct": sess_pct,
        "weekly_reset_at": week_reset_iso,
        "session_reset_at": session_reset_iso,
        "last_run_at": now_iso,
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))

    print(f"[tracker] wrote {DATA_FILE} (public, no pct) — early={early}")

    # Git push (if inside a git repo with remote configured)
    if (REPO_ROOT / ".git").is_dir():
        try:
            subprocess.run(["git", "add", "data/usage.json"], cwd=REPO_ROOT, check=True, capture_output=True)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT)
            if diff.returncode != 0:
                msg = f"tracker {now.strftime('%Y-%m-%d %H:%M UTC')}"
                if early:
                    msg = "🚨 EARLY RESET DETECTED · " + msg
                subprocess.run(["git", "commit", "-m", msg], cwd=REPO_ROOT, check=True, capture_output=True)
                subprocess.run(["git", "push"], cwd=REPO_ROOT, check=True, capture_output=True)
                print(f"[tracker] pushed: {msg}")
        except subprocess.CalledProcessError as e:
            print(f"[tracker] git error: {e.stderr.decode() if e.stderr else e}", file=sys.stderr)


if __name__ == "__main__":
    main()
