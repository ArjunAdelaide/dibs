"""Memory as plain text files, one folder per user (the Instinct pattern).

    data/memory/<user>/log/2026-10-03.md   raw notes from today, appended as they happen
    data/memory/<user>/profile.md          what the agent always needs to know
    data/memory/<user>/<topic>.md          venues, people, preferences ...

The chat agent only appends notes and reads. A daily job (consolidate) rewrites
the topic files: it merges notes, turns examples into general facts, dates
corrections and drops one-time details. Search is a keyword match, no database.

    python -m dibs.memory consolidate
"""

import json
import re
from datetime import datetime
from pathlib import Path

from . import config

FILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\.md$")
PROFILE_CHARS = 3000

CONSOLIDATE = """You maintain the long-term memory of a booking assistant for one user.
You get the current memory files and new raw notes. Return the updated files.

Rules:
- profile.md is short (under 250 words): who the user is, where they are based, who they go out with, how they like to book, usual group size and budget.
- Other files are topics, for example venues.md, people.md, preferences.md. Create one only when there is something to put in it.
- Each file starts with a header block: three dashes, then id, type (preference, person, venue or history) and aliases (search keywords), then three dashes.
- The body is a list of short facts. Put the date on facts that can change, for example "(stated 2026-10-03)".
- Turn repeated examples into a general fact. Replace a wrong fact with a dated correction. Remove one-time details such as codes or links.
- Never invent a fact. Use only the notes and the current files.

Reply with JSON only: {"files": {"profile.md": "...", "venues.md": "..."}}. Include every file that must exist after this update."""


def user_dir(handle: str) -> Path:
    return config.MEMORY_DIR / (re.sub(r"[^a-zA-Z0-9]+", "-", handle).strip("-") or "user")


def note(handle: str, fact: str, now: datetime) -> None:
    log = user_dir(handle) / "log" / f"{now:%Y-%m-%d}.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(f"- {now:%H:%M} {' '.join(fact.split())}\n")


def _pending_logs(handle: str) -> list[Path]:
    return sorted((user_dir(handle) / "log").glob("*.md"))


def context(handle: str) -> str:
    """Profile plus notes not yet consolidated: goes into every conversation."""
    parts = []
    profile = user_dir(handle) / "profile.md"
    if profile.exists():
        parts.append(profile.read_text()[:PROFILE_CHARS])
    recent = [line for log in _pending_logs(handle)[-3:] for line in log.read_text().splitlines()][-20:]
    if recent:
        parts.append("Recent notes:\n" + "\n".join(recent))
    return "\n\n".join(parts) or "nothing yet"


def search(handle: str, query: str, limit: int = 12) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    hits = []
    for path in sorted(user_dir(handle).rglob("*.md")):
        for line in path.read_text().splitlines():
            if line.strip() and any(w in line.lower() for w in words):
                hits.append(f"{path.relative_to(user_dir(handle))}: {line.strip()}")
    return hits[-limit:]


def consolidate(handle: str, llm) -> bool:
    """Merge pending notes into the topic files. Returns True if files changed."""
    logs = _pending_logs(handle)
    if not logs:
        return False
    base = user_dir(handle)
    files = {p.name: p.read_text() for p in sorted(base.glob("*.md"))}
    notes = "\n".join(f"## {p.stem}\n{p.read_text()}" for p in logs)
    msg = llm.chat([
        {"role": "system", "content": CONSOLIDATE},
        {"role": "user", "content": f"Current files:\n{json.dumps(files, indent=1)}\n\nNew notes:\n{notes}"},
    ], [])
    raw = re.sub(r"^```(?:json)?|```$", "", (msg.get("content") or "").strip(), flags=re.M).strip()
    new_files = json.loads(raw)["files"]
    for name, body in new_files.items():
        if FILE_NAME.match(name) and isinstance(body, str):  # stay inside the user's folder
            (base / name).write_text(body.rstrip() + "\n")
    done = base / "log" / "done"
    done.mkdir(exist_ok=True)
    for log in logs:
        log.rename(done / log.name)
    return True


def consolidate_all(llm) -> int:
    if not config.MEMORY_DIR.exists():
        return 0
    count = 0
    for folder in sorted(p for p in config.MEMORY_DIR.iterdir() if p.is_dir()):
        try:
            count += consolidate(folder.name, llm)
        except Exception as exc:  # one bad reply must not block the other users
            print(f"memory: could not consolidate {folder.name}: {type(exc).__name__}: {exc}")
    return count


if __name__ == "__main__":
    import sys

    from .llm import OpenAICompatLLM

    if sys.argv[1:] == ["consolidate"]:
        print(f"Updated memory for {consolidate_all(OpenAICompatLLM())} user(s).")
    else:
        print("Usage: python -m dibs.memory consolidate")
