"""Tavla core: GitHub issues with the Claude Code sessions working on each.

The terminal app (tui.py) imports it; `tavla json` prints the board for scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

JOBS = Path.home() / ".claude" / "jobs"
# Conversations, as <project>/<session id>.jsonl: `claude rm` keeps them, which is what makes a deleted session resumable.
TRANSCRIPTS = Path.home() / ".claude" / "projects"
# Local state and config.toml.
CONFIG = Path(os.environ.get("TAVLA_HOME", Path.home() / ".config" / "tavla"))
# The four below are set from config.toml by load_config().
# Projects whose Status field places a card, in precedence order. Non-Done items of a
# backlog project become cards even when no session or assignment points at them.
PROJECTS: list[dict] = []
# Where `create` files issues, as (owner/repo, label): the first is the default.
ISSUE_REPOS: list[tuple[str, str]] = []
# Where `new` starts background sessions.
SESSION_CWD = Path.cwd()
THEME = "tavla"
# The terminal app `attach` opens tabs in. iTerm2 and Ghostty are scripted with AppleScript (macOS);
# the others have a command that opens a tab, in ATTACH_ARGV.
TERMINALS = {"iterm2": "iTerm2", "ghostty": "Ghostty", "tmux": "tmux", "gnome-terminal": "GNOME Terminal", "konsole": "Konsole", "terminator": "Terminator", "kitty": "kitty", "wezterm": "WezTerm"}
# {script} is the shell line to run in the new tab, {title} the tab's name, {cwd} the session's directory.
ATTACH_ARGV = {
    "tmux": ["tmux", "new-window", "-n", "{title}", "sh", "-c", "{script}"],
    "gnome-terminal": ["gnome-terminal", "--tab", "--", "sh", "-c", "{script}"],
    "konsole": ["konsole", "--new-tab", "-e", "sh", "-c", "{script}"],
    "terminator": ["terminator", "--new-tab", "-x", "sh", "-c", "{script}"],
    "kitty": ["kitty", "@", "launch", "--type=tab", "--tab-title", "{title}", "sh", "-c", "{script}"],
    "wezterm": ["wezterm", "cli", "spawn", "--", "sh", "-c", "{script}"],
}


def detect_terminal(env: Mapping[str, str] = os.environ, platform: str = sys.platform) -> str:
    """The terminal tavla runs in, or "" when it is not a known one. tmux comes first: inside it the
    outer terminal's variables are still set."""
    if platform == "darwin":
        return "ghostty" if env.get("TERM_PROGRAM") == "ghostty" else "iterm2"
    seen = {"TMUX": "tmux", "KITTY_WINDOW_ID": "kitty", "WEZTERM_PANE": "wezterm", "TERMINATOR_UUID": "terminator", "KONSOLE_VERSION": "konsole", "GNOME_TERMINAL_SCREEN": "gnome-terminal"}
    return next((name for var, name in seen.items() if env.get(var)), "")


TERMINAL = detect_terminal()
# A command of your own that opens the tab, from `attach_command` in the config file; it wins over TERMINAL.
ATTACH_COMMAND: list[str] = []
PROJECT_TTL_S = 300
ASSIGNED_TTL_S = 300  # open issues assigned to me
META_TTL_S = 600  # issue/PR metadata: labels, sub-issues, linked PRs, open state
CLOSED_TTL_S = 900  # recently closed issues, which only a search shows
CLOSED_DAYS = 30
PROJECT_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  repositoryOwner(login: $owner) { ... on ProjectV2Owner { projectV2(number: $number) {
    items(first: 100, after: $cursor, query: "is:open is:issue") { pageInfo { hasNextPage endCursor }
      nodes { id status: fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
        content { __typename ... on Issue { number title url repository { nameWithOwner } } } } } } } }
}
"""
CLOSED_PROJECT_QUERY = """
query($owner: String!, $number: Int!, $filter: String!, $cursor: String) {
  repositoryOwner(login: $owner) { ... on ProjectV2Owner { projectV2(number: $number) {
    items(first: 100, after: $cursor, query: $filter) { pageInfo { hasNextPage endCursor }
      nodes { content { __typename ... on Issue { number title url closedAt repository { nameWithOwner } } } } } } } }
}
"""
STATUS_FIELD_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) { ... on ProjectV2Owner { projectV2(number: $number) { id
    field(name: "Status") { ... on ProjectV2SingleSelectField { id options { id name } } } } } }
}
"""
SET_STATUS_MUTATION = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field, value: {singleSelectOptionId: $option}}) { projectV2Item { id } }
}
"""
RECENT_S = 3 * 86_400

NEEDS = {"blocked", "needs_input", "needs-input", "waiting", "review"}
WORKING = {"working", "running", "starting", "busy"}
STATUS_COLUMNS = [("todo", "Todo"), ("progress", "In Progress"), ("blocked", "Blocked"), ("done", "Done")]
# Not-started statuses share the Todo column; a card keeps its own status for display.
# Statuses a new session may move to In Progress.
NOT_STARTED = ("Todo", "Backlog")
STATUS_BUCKET = {"none": "todo", "backlog": "todo", "todo": "todo", "progress": "progress", "blocked": "blocked", "done": "done"}
SESSION_COLUMNS = [("active", "Active"), ("idle", "Idle"), ("backlog", "No session")]
# Sessions that need you and working ones share Active; needs-you cards sort first and keep their chip.
SESSION_BUCKET = {"needs": "active", "working": "active", "idle": "idle", "backlog": "backlog"}

URL_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)")
SHORT_RE = re.compile(r"(?:^|[\s(\[])([\w.-]+)/([\w.-]+)#(\d+)\b")

Ref = tuple[str, str, int]


def load_config() -> None:
    """Set PROJECTS, ISSUE_REPOS, SESSION_CWD, THEME, TERMINAL and ATTACH_COMMAND from CONFIG/config.toml; a missing file leaves the defaults."""
    global PROJECTS, ISSUE_REPOS, SESSION_CWD, THEME, TERMINAL, ATTACH_COMMAND
    path = CONFIG / "config.toml"
    try:
        cfg = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return
    except tomllib.TOMLDecodeError as err:
        raise ValueError(f"{path}: {err}") from err
    try:
        projects = [{"owner": str(p["owner"]), "number": str(int(p["number"])), "is_backlog": bool(p.get("backlog", False))} for p in cfg.get("projects", [])]
        repos = [(str(r["repo"]), str(r.get("label") or r["repo"])) for r in cfg.get("issue_repos", [])]
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError(f"{path}: a [[projects]] entry needs owner and number, an [[issue_repos]] entry needs repo ({err!r})") from err
    bad = [r for r, _ in repos if not re.fullmatch(r"[\w.-]+/[\w.-]+", r)]
    if bad:
        raise ValueError(f"{path}: issue_repos repo must be owner/repo, got {bad[0]!r}")
    terminal = str(cfg.get("terminal") or TERMINAL).lower()
    if terminal and terminal not in TERMINALS:
        raise ValueError(f"{path}: terminal must be one of {', '.join(TERMINALS)}, got {terminal!r}")
    command = cfg.get("attach_command") or []
    if not isinstance(command, list) or not all(isinstance(a, str) for a in command) or (command and not any("{script}" in a for a in command)):
        raise ValueError(f"{path}: attach_command must be a list of strings, one of them holding {{script}}")
    PROJECTS, ISSUE_REPOS, TERMINAL, ATTACH_COMMAND = projects, repos, terminal, command
    if cfg.get("session_dir"):
        SESSION_CWD = Path(str(cfg["session_dir"])).expanduser()
    THEME = str(cfg.get("theme") or THEME)


class RateLimited(RuntimeError):
    """GitHub refused a call for rate limiting (primary budget or a secondary burst limit)."""


def run(argv: list[str], timeout: int = 60, cwd: Path | None = None) -> str:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd, check=False)
    if p.returncode != 0:
        msg = f"{' '.join(argv[:3])}: {(p.stderr or p.stdout).strip()[:200]}"
        raise RateLimited(msg) if "rate limit" in msg.lower() else RuntimeError(msg)
    return p.stdout


def graphql(query: str, variables: dict | None = None) -> dict:
    """The parsed response; partial data is kept, since gh exits non-zero when any one alias is missing."""
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in (variables or {}).items():
        argv += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    body = json.loads(p.stdout or "{}")
    # Judge only gh's own message and GraphQL's error entries: data (an issue title) may say "rate limit" too.
    messages = [p.stderr, *(f"{e.get('type', '')} {e.get('message', '')}" for e in body.get("errors") or [])]
    if any("rate limit" in m.lower() or "RATE_LIMIT" in m for m in messages):
        raise RateLimited(f"graphql: {next(m for m in messages if 'rate' in m.lower()).strip()[:200]}")
    return body


def backoff_until() -> float:
    try:
        return float(json.loads((CONFIG / "backoff.json").read_text())["until"])
    except FileNotFoundError, json.JSONDecodeError, KeyError, ValueError:
        return 0.0


def start_backoff() -> float:
    """Pause GitHub calls until the exhausted budget resets, or 90s for a secondary (burst) limit.

    The GraphQL budget is read from GraphQL's own rateLimit: the REST /rate_limit graphql bucket can
    report plenty remaining while GraphQL itself refuses every query. `gh search` spends GraphQL too."""
    until = time.time() + 90
    with suppress(Exception):  # the 90s default stands
        rl = (graphql("{ rateLimit { remaining resetAt } }").get("data") or {}).get("rateLimit") or {}
        if rl.get("remaining") == 0:
            until = max(until, parse_time_ms(rl["resetAt"]) / 1000)
    with suppress(Exception):
        search = json.loads(run(["gh", "api", "rate_limit"], timeout=20))["resources"]["search"]
        if search["remaining"] == 0:
            until = max(until, float(search["reset"]))
    CONFIG.mkdir(parents=True, exist_ok=True)
    (CONFIG / "backoff.json").write_text(json.dumps({"until": until}))
    return until


def now_ms() -> int:
    return int(time.time() * 1000)


def key_of(r: Ref) -> str:
    return f"{r[0]}/{r[1]}#{r[2]}".lower()


def parse_refs(text: str) -> list[Ref]:
    out = [(m[1], m[2], int(m[3])) for m in URL_RE.finditer(text)]
    out += [(m[1], m[2], int(m[3])) for m in SHORT_RE.finditer(text)]
    return out


def ref_from_arg(arg: str) -> Ref | None:
    refs = parse_refs(f" {arg}")
    return refs[0] if refs else None


def kind_of(state: str) -> str:
    return "needs" if state in NEEDS else "working" if state in WORKING else "idle"


def status_column_of(status: str, is_open: bool) -> str:
    if not is_open:
        return "done"
    s = status.lower()
    if s == "backlog":
        return "backlog"
    if s in ("todo", "ready"):
        return "todo"
    if s in ("in progress", "in review"):
        return "progress"
    if s == "blocked":
        return "blocked"
    if s == "done":
        return "done"
    return "none"


def parse_time_ms(value: str | None) -> int:
    if not value:
        return 0
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return 0


# --- links -------------------------------------------------------------------


def load_links() -> dict[str, list[str]]:
    try:
        return json.loads((CONFIG / "links.json").read_text())
    except FileNotFoundError, json.JSONDecodeError:
        return {}


def save_links(links: dict[str, list[str]]) -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    (CONFIG / "links.json").write_text(json.dumps(links, indent=2, sort_keys=True) + "\n")


# --- hidden issues -------------------------------------------------------------


def load_hidden() -> set[str]:
    try:
        return set(json.loads((CONFIG / "hidden.json").read_text()))
    except FileNotFoundError, json.JSONDecodeError:
        return set()


def toggle_hidden(issue_key: str) -> set[str]:
    """Hide the issue on the board, or show it again; returns the hidden keys."""
    hidden = load_hidden() ^ {issue_key}
    CONFIG.mkdir(parents=True, exist_ok=True)
    (CONFIG / "hidden.json").write_text(json.dumps(sorted(hidden), indent=2) + "\n")
    return hidden


# --- session history -----------------------------------------------------------


def load_history() -> dict[str, dict]:
    try:
        return json.loads((CONFIG / "history.json").read_text())
    except FileNotFoundError, json.JSONDecodeError:
        return {}


def save_history(history: dict[str, dict]) -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    (CONFIG / "history.json").write_text(json.dumps(history, indent=2, sort_keys=True) + "\n")


def history_record(sess: dict) -> dict:
    """What is kept of a session so that it can be listed and resumed once it is gone."""
    return {
        "id": sess["id"],
        "sessionId": sess.get("sessionId") or "",
        "name": sess.get("name") or sess["id"],
        "cwd": sess.get("cwd") or "",
        "issues": list(sess.get("issues") or []),
        "prs": list(sess.get("prs") or []),
        "result": " ".join((sess.get("result") or sess.get("detail") or "").split())[:200],
        "updatedAt": sess.get("updatedAt") or 0,
        "isInteractive": bool(sess.get("isInteractive")),
    }


def from_transcript(short_id: str, issues: list[str]) -> dict:
    """A record for a session known only by its id (a link left behind by a session deleted elsewhere):
    its session id, name and directory are read from its conversation file. Without one
    the record has no sessionId, which keeps it off the cards."""
    sess = {"id": short_id, "issues": issues}
    path = next(iter(sorted(TRANSCRIPTS.glob(f"*/{short_id}-*.jsonl"))), None) if TRANSCRIPTS.is_dir() else None
    if path is None:
        return history_record(sess)
    sess |= {"sessionId": path.stem, "updatedAt": int(path.stat().st_mtime * 1000)}
    names: dict[str, str] = {}
    with path.open(errors="replace") as f:
        for line in f:
            # Titles are rewritten throughout the file, between request lines of a megabyte: parse only the lines that can matter.
            if not any(f'"{field}"' in line for field in ("agentName", "customTitle", "aiTitle", *(() if sess.get("cwd") else ("cwd",)))):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            names |= {field: str(row[field]) for field in ("agentName", "customTitle", "aiTitle") if row.get(field)}
            if not sess.get("cwd") and isinstance(row.get("cwd"), str):
                sess["cwd"] = row["cwd"]
    sess["name"] = names.get("agentName") or names.get("customTitle") or names.get("aiTitle")
    return history_record(sess)


def update_history(sessions: list[dict], links: dict[str, list[str]], errors: list[str]) -> list[dict]:
    """Record the sessions that sit on a card, and return the recorded ones that are gone, as session rows.

    A session is gone when it is no longer listed: a background one was deleted, an interactive one
    ended. Its conversation is still on disk, so resume_session brings it back. Sessions deleted
    before they were recorded are picked up once from the manual links they left behind."""
    history = load_history()
    before = json.dumps(history, sort_keys=True)
    live = {s["id"] for s in sessions}
    for s in sessions:
        if s["issues"]:
            history[s["id"]] = history_record(s)
    for sid, keys in links.items():
        if sid not in live and sid not in history:
            try:
                history[sid] = from_transcript(sid, list(keys))
            except OSError as err:
                errors.append(f"history: {err}")
    if json.dumps(history, sort_keys=True) != before:
        try:
            save_history(history)
        except OSError as err:  # a history that can't be saved must not cost the board
            errors.append(f"history.json: {err}")
    # Without `claude agents` no interactive session is listed: that is not them having ended.
    agents_failed = any(e.startswith("claude agents:") for e in errors)
    return [
        {**r, "state": "ended" if r["isInteractive"] else "deleted", "kind": "idle", "detail": "", "intent": "", "refs": r["issues"], "isPast": True}
        for r in history.values()
        if r["id"] not in live and r.get("sessionId") and not (agents_failed and r["isInteractive"])
    ]


# --- sessions ----------------------------------------------------------------


def read_sessions(links: dict[str, list[str]], errors: list[str]) -> list[tuple[dict, list[Ref]]]:
    raw: list[tuple[dict, list[Ref]]] = []
    for d in sorted(JOBS.iterdir()) if JOBS.is_dir() else []:
        try:
            st = json.loads((d / "state.json").read_text())
        except FileNotFoundError, NotADirectoryError, json.JSONDecodeError:
            continue
        pr_hrefs = [c["href"] for c in st.get("children") or [] if c.get("kind") == "pr" and c.get("href")]
        refs = parse_refs(st.get("intent") or "") + parse_refs(" ".join(pr_hrefs))
        explicit = links.get(d.name, [])
        result = (st.get("output") or {}).get("result")
        raw.append(
            (
                {
                    "id": d.name,
                    "sessionId": st.get("sessionId") or "",
                    "name": st.get("name") or d.name,
                    "state": st.get("state") or "unknown",
                    "kind": kind_of(st.get("state") or ""),
                    "detail": st.get("detail") or "",
                    "intent": st.get("intent") or "",
                    "result": result if isinstance(result, str) else "",
                    "updatedAt": parse_time_ms(st.get("updatedAt") or st.get("createdAt")),
                    "refs": sorted({*map(key_of, refs), *explicit}),
                    "issues": list(explicit),
                    "cwd": st.get("cwd") or "",
                    "isInteractive": False,
                },
                refs,
            )
        )
    # Interactive sessions have no job dir and no `id`; they are keyed by their short session id.
    try:
        for a in json.loads(run(["claude", "agents", "--json"], timeout=30)):
            if a.get("kind") != "interactive" or not a.get("sessionId"):
                continue
            sid = a["sessionId"][:8]
            explicit = links.get(sid, [])
            state = a.get("status") or "unknown"
            raw.append(
                (
                    {
                        "id": sid,
                        "sessionId": a["sessionId"],
                        "name": a.get("name") or sid,
                        "state": state,
                        "kind": kind_of(state),
                        "detail": f"interactive in {a.get('cwd') or '?'}",
                        "intent": "",
                        "result": "",
                        "updatedAt": a.get("startedAt") or 0,
                        "refs": list(explicit),
                        "issues": list(explicit),
                        "cwd": a.get("cwd") or "",
                        "isInteractive": True,
                    },
                    [],
                )
            )
    except Exception as err:  # noqa: BLE001 - a missing CLI only costs the interactive rows
        errors.append(f"claude agents: {err}")
    return raw


def find_sessions(sessions: list[dict], needle: str) -> list[dict]:
    n = needle.lower()
    exact = [s for s in sessions if n in (s["id"], s["sessionId"], s["name"].lower())]
    return exact or [s for s in sessions if s["id"].startswith(n) or n in s["name"].lower()]


def search_text(row: dict) -> str:
    """What a filter matches on: a card's title, repo#N, status, labels and session names; a session's name, id and refs."""
    if "sessions" in row:
        names = [s["name"] for s in [*row["sessions"], *row.get("pastSessions", [])]]
        parts = [row["title"], row["key"], row["key"].split("/", 1)[1], row["status"], *(lb["name"] for lb in row.get("labels") or []), *names]
    else:
        parts = [row["name"], row["id"], *row.get("refs", [])]
    return " ".join(p for p in parts if p).lower()


def matches(row: dict, query: str) -> bool:
    """Every whitespace-separated word of the query appears somewhere in the row, ignoring case."""
    text = search_text(row)
    return all(word in text for word in query.lower().split())


# --- GitHub ------------------------------------------------------------------


def read_cache(name: str, ttl: float, force: bool, offline: bool) -> tuple[list | None, bool]:
    """(cached items, fresh enough to use): offline any age counts, force none does."""
    try:
        cached = json.loads((CONFIG / name).read_text())
        items, age = cached["items"], time.time() - cached["at"]
    except FileNotFoundError, json.JSONDecodeError, KeyError, TypeError:
        return None, False
    return items, offline or (not force and age < ttl)


def write_cache(name: str, items: list, errors: list[str]) -> None:
    try:
        CONFIG.mkdir(parents=True, exist_ok=True)
        (CONFIG / name).write_text(json.dumps({"at": time.time(), "items": items}))
    except OSError as err:  # a cache that can't be saved must not cost the fresh answer
        errors.append(f"{name}: {err}")


def load_assigned(force: bool, errors: list[str], offline: bool = False) -> list[dict]:
    """Open issues assigned to me, cached for ASSIGNED_TTL_S: `gh search` spends the GraphQL budget
    and takes over a second. Offline, or when GitHub errs, the last answer is used whatever its age."""
    cached, fresh = read_cache("assigned-cache.json", ASSIGNED_TTL_S, force, offline)
    if fresh or offline:
        return cached or []
    try:
        items = json.loads(run(["gh", "search", "issues", "--assignee", "@me", "--state", "open", "--limit", "100", "--json", "repository,number,title,url,updatedAt"]))
    except RateLimited:
        raise
    except Exception as err:  # noqa: BLE001
        errors.append(str(err))
        return cached or []
    write_cache("assigned-cache.json", items, errors)
    return items


def load_closed(force: bool, errors: list[str], offline: bool = False) -> list[dict]:
    """Issues closed in the last CLOSED_DAYS days that are assigned to me or sit in a backlog project,
    cached for CLOSED_TTL_S: one search and one page per backlog project, fetched in parallel.
    Offline, or when GitHub errs, the last answer is used whatever its age."""
    cached, fresh = read_cache("closed-cache.json", CLOSED_TTL_S, force, offline)
    if fresh or offline:
        return cached or []
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - CLOSED_DAYS * 86_400))

    def assigned() -> list[dict]:
        fields = "repository,number,title,url,closedAt"
        rows = json.loads(run(["gh", "search", "issues", "--assignee", "@me", "--state", "closed", "--closed", f">={since}", "--sort", "updated", "--limit", "100", "--json", fields]))
        return [{"key": f"{r['repository']['nameWithOwner']}#{r['number']}".lower(), "title": r["title"], "url": r["url"], "closedAt": parse_time_ms(r.get("closedAt"))} for r in rows]

    def project(p: dict) -> list[dict]:
        # The items filter knows no closed date: closing an issue updates it, so `updated` bounds the pages.
        items, cursor = [], None
        while True:
            variables = {"owner": p["owner"], "number": int(p["number"]), "filter": f"is:closed is:issue updated:>={since}", **({"cursor": cursor} if cursor else {})}
            page = ((((graphql(CLOSED_PROJECT_QUERY, variables).get("data") or {}).get("repositoryOwner") or {}).get("projectV2") or {}).get("items")) or None
            if page is None:
                raise RuntimeError(f"project {p['owner']}/{p['number']}: closed issues not readable")
            for it in page["nodes"]:
                c = it.get("content") or {}
                if c.get("__typename") == "Issue" and (c.get("closedAt") or "")[:10] >= since:
                    items.append(
                        {"key": f"{c['repository']['nameWithOwner']}#{c['number']}".lower(), "title": c.get("title") or "", "url": c.get("url") or "", "closedAt": parse_time_ms(c["closedAt"])}
                    )
            if not page["pageInfo"]["hasNextPage"]:
                return items
            cursor = page["pageInfo"]["endCursor"]

    jobs = [assigned, *(lambda p=p: project(p) for p in PROJECTS if p["is_backlog"])]
    try:
        with ThreadPoolExecutor(len(jobs)) as pool:
            lists = list(pool.map(lambda job: job(), jobs))
    except RateLimited:
        raise
    except Exception as err:  # noqa: BLE001
        errors.append(str(err))
        return cached or []
    items = list({i["key"]: i for lst in lists for i in lst}.values())
    write_cache("closed-cache.json", items, errors)
    return items


def load_projects(force: bool, errors: list[str], offline: bool = False) -> list[dict]:
    """Status is read from the project side: issue-side projectItems is empty for cross-org projects.
    Offline, the cached lists are used whatever their age."""
    cached, fresh = read_cache("projects-cache.json", PROJECT_TTL_S, force, offline)
    if fresh or offline:
        return cached or []

    def fetch(p: dict) -> list[dict] | None:
        # Status and issue identity only: 1 point per 100 items, where `gh project item-list` reads every field.
        # Open issues only: a closed issue is Done whatever its status, and pages are fetched one after another.
        items, cursor = [], None
        while True:
            variables = {"owner": p["owner"], "number": int(p["number"]), **({"cursor": cursor} if cursor else {})}
            page = ((((graphql(PROJECT_QUERY, variables).get("data") or {}).get("repositoryOwner") or {}).get("projectV2") or {}).get("items")) or None
            if page is None:
                errors.append(f"project {p['owner']}/{p['number']}: not readable")
                return None
            for it in page["nodes"]:
                c = it.get("content") or {}
                if c.get("__typename") != "Issue":
                    continue
                items.append(
                    {
                        "key": f"{c['repository']['nameWithOwner']}#{c['number']}".lower(),
                        "title": c.get("title") or "",
                        "url": c.get("url") or "",
                        "status": (it.get("status") or {}).get("name") or "",
                        "isBacklog": p["is_backlog"],
                        "project": f"{p['owner']}/{p['number']}",
                        "itemId": it.get("id") or "",
                    }
                )
            if not page["pageInfo"]["hasNextPage"]:
                return items
            cursor = page["pageInfo"]["endCursor"]

    if not PROJECTS:
        return []
    # Projects in parallel, pages within one in order: the wall time is the longest project's pages.
    with ThreadPoolExecutor(len(PROJECTS)) as pool:
        lists = list(pool.map(fetch, PROJECTS))
    items = [i for lst in lists if lst for i in lst]
    if all(lst is not None for lst in lists):
        write_cache("projects-cache.json", items, errors)
    return items


def resolve_refs(refs: list[Ref], errors: list[str], force: bool = False, offline: bool = False) -> dict[str, dict]:
    """One GraphQL call per 40 refs: an issue resolves to itself with its card metadata, a PR to its
    own state and the issues it closes. Answers are cached for META_TTL_S, so a routine refresh only
    asks about refs it has not seen lately; offline, only cached answers are used, whatever their age."""
    cache_file = CONFIG / "meta-cache.json"
    try:
        cache: dict[str, dict] = json.loads(cache_file.read_text())
    except FileNotFoundError, json.JSONDecodeError:
        cache = {}
    now = time.time()
    uniq = list({key_of(r): r for r in refs}.values())
    stale = [] if offline else [r for r in uniq if force or now - cache.get(key_of(r), {}).get("at", 0) >= META_TTL_S]
    fetched = _fetch_refs(stale, errors)
    for r in stale:
        if key_of(r) in fetched or key_of(r) not in cache:
            cache[key_of(r)] = {"at": now, "info": fetched.get(key_of(r))}
    if stale:
        CONFIG.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache))
    return {key_of(r): cache[key_of(r)]["info"] for r in uniq if (cache.get(key_of(r)) or {}).get("info")}


def _fetch_refs(uniq: list[Ref], errors: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    pr_fields = "number state url repository { nameWithOwner }"
    fields = (
        "number title body url state updatedAt repository { nameWithOwner }"
        " labels(first: 6) { nodes { name color } } subIssuesSummary { total completed }"
        " assignees(first: 3) { nodes { login } }"
        f" closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {{ nodes {{ {pr_fields} }} }}"
    )

    def to_pr(n: dict) -> dict:
        return {"key": f"{n['repository']['nameWithOwner']}#{n['number']}".lower(), "url": n["url"], "state": n["state"]}

    def to_issue(n: dict) -> dict:
        sub = n.get("subIssuesSummary") or {}
        return {
            "kind": "Issue",
            "key": f"{n['repository']['nameWithOwner']}#{n['number']}".lower(),
            "title": n["title"],
            "body": n.get("body") or "",
            "url": n["url"],
            "isOpen": n["state"] == "OPEN",
            "updatedAt": parse_time_ms(n.get("updatedAt")),
            "labels": [{"name": lb["name"], "color": lb["color"]} for lb in (n.get("labels") or {}).get("nodes") or []],
            "subIssues": {"total": sub.get("total") or 0, "completed": sub.get("completed") or 0},
            "assignees": [a["login"] for a in (n.get("assignees") or {}).get("nodes") or []],
            "prs": [to_pr(p) for p in (n.get("closedByPullRequestsReferences") or {}).get("nodes") or []],
        }

    def fetch(chunk: list[Ref]) -> None:
        body = "\n".join(
            f'r{j}: repository(owner: "{o}", name: "{r}") {{ issueOrPullRequest(number: {n}) {{'
            f" __typename ... on Issue {{ {fields} }}"
            f" ... on PullRequest {{ {pr_fields} closingIssuesReferences(first: 5) {{ nodes {{ {fields} }} }} }} }} }}"
            for j, (o, r, n) in enumerate(chunk)
        )
        try:
            data = graphql(f"query {{ {body} }}").get("data") or {}
        except RateLimited:
            raise
        except Exception as err:  # noqa: BLE001
            errors.append(f"graphql: {err}")
            return
        for j, r in enumerate(chunk):
            node = (data.get(f"r{j}") or {}).get("issueOrPullRequest")
            if not node:
                continue
            if node["__typename"] == "Issue":
                out[key_of(r)] = to_issue(node)
            else:
                out[key_of(r)] = {"kind": "PullRequest", **to_pr(node), "closes": [to_issue(n) for n in node["closingIssuesReferences"]["nodes"]]}

    # Chunks in parallel: each takes over a second, and the recently closed issues add a couple.
    chunks = [uniq[i : i + 40] for i in range(0, len(uniq), 40)]
    if chunks:
        with ThreadPoolExecutor(min(len(chunks), 4)) as pool:
            list(pool.map(fetch, chunks))
    return out


# --- board -------------------------------------------------------------------


def build_board(force: bool = False) -> dict:
    """The board. While GitHub is rate-limited (or a backoff from an earlier limit runs) it is built
    from the session files and cached GitHub data alone, without calling GitHub; sessions whose issue
    isn't cached land in Unlinked, and errors[0] says so.

    Cards marked searchOnly are finished issues that are off the board: only a search shows them.
    A card's pastSessions are the deleted or ended sessions that were on it; they place no card."""
    until = backoff_until()
    if until <= time.time():
        try:
            return _build_board(force, offline=False)
        except RateLimited:
            until = start_backoff()
    board = _build_board(False, offline=True)
    board["errors"].insert(0, f"GitHub rate limit: paused until {time.strftime('%H:%M:%S', time.localtime(until))}; showing cached GitHub data")
    board["isOffline"] = True
    return board


def _build_board(force: bool, offline: bool) -> dict:
    errors: list[str] = []
    links = load_links()
    with ThreadPoolExecutor(4) as pool:
        f_sessions = pool.submit(read_sessions, links, errors)
        f_assigned = pool.submit(load_assigned, force, errors, offline)
        f_projects = pool.submit(load_projects, force, errors, offline)
        f_closed = pool.submit(load_closed, force, errors, offline)
        raw = f_sessions.result()
        assigned = f_assigned.result()
        project_items = f_projects.result()
        closed = f_closed.result()

    issues: dict[str, dict] = {}

    def add_issue(key: str, title: str, url: str, is_open: bool, updated_at: int, is_mine: bool = False) -> None:
        prev = issues.get(key, {})
        issues[key] = {
            "title": title or prev.get("title", ""),
            "url": url or prev.get("url", ""),
            "isOpen": is_open,
            "updatedAt": max(updated_at, prev.get("updatedAt", 0)),
            "isMine": is_mine or prev.get("isMine", False),
        }

    # Closed first: an issue reopened since the closed list was cached is open in the lists below.
    for i in closed:
        add_issue(i["key"], i["title"], i["url"], False, i["closedAt"], True)
    for i in assigned:
        add_issue(f"{i['repository']['nameWithOwner']}#{i['number']}".lower(), i["title"], i["url"], True, parse_time_ms(i.get("updatedAt")), True)
    status_of: dict[str, str] = {}
    status_item: dict[str, dict] = {}  # the project item that status came from, for start_progress
    for it in project_items:
        if it["status"] and it["key"] not in status_of:
            status_of[it["key"]] = it["status"]
            if it.get("itemId"):
                status_item[it["key"]] = {"project": it["project"], "itemId": it["itemId"]}
        if it["isBacklog"] and it["status"] != "Done":
            add_issue(it["key"], it["title"], it["url"], True, 0, True)

    session_refs = [r for _, refs in raw for r in refs] + [r for keys in links.values() for k in keys if (r := ref_from_arg(k))]
    # Issues known only from search or a project need their metadata too; one pass resolves everything.
    known_refs = [r for k in issues if (r := ref_from_arg(k))]
    infos = resolve_refs(session_refs + known_refs, errors, force, offline)
    from_sessions = {key_of(r) for r in session_refs}
    meta: dict[str, dict] = {}  # full GraphQL view of each issue, for the card metadata
    for key, info in infos.items():
        for i in [info] if info["kind"] == "Issue" else info["closes"]:
            meta[i["key"]] = i
            if key in from_sessions:
                add_issue(i["key"], i["title"], i["url"], i["isOpen"], i["updatedAt"])
            elif i["key"] in issues:
                issues[i["key"]]["isOpen"] = i["isOpen"]

    sessions = []
    for sess, refs in raw:
        found = set(sess["issues"])
        prs: dict[str, dict] = {}
        for r in refs:
            info = infos.get(key_of(r))
            if not info:
                continue
            if info["kind"] == "Issue":
                found.add(info["key"])
            else:
                found.update(c["key"] for c in info["closes"])
                prs[info["key"]] = {"key": info["key"], "url": info["url"], "state": info["state"]}
        sessions.append({**sess, "issues": sorted(found), "prs": list(prs.values())})

    past = sorted(update_history(sessions, links, errors), key=lambda s: -s["updatedAt"])
    recent = now_ms() - RECENT_S * 1000
    cards = []
    for key, iss in issues.items():
        ss = sorted((s for s in sessions if key in s["issues"]), key=lambda s: -s["updatedAt"])
        kinds = {s["kind"] for s in ss}
        session_column = "needs" if "needs" in kinds else "working" if "working" in kinds else "idle" if ss else "backlog"
        status = status_of.get(key, "")
        status_column = status_column_of(status, iss["isOpen"])
        last_touch = max((s["updatedAt"] for s in ss), default=0)
        if session_column == "backlog" and not iss["isMine"]:
            continue
        # A finished issue stays on the board only while a session on it is live or recent; after that a search still finds it.
        search_only = status_column == "done" and session_column in ("idle", "backlog") and last_touch < recent
        m = meta.get(key, {})
        prs = {p["key"]: p for p in [*m.get("prs", []), *(p for s in ss for p in s["prs"])]}
        cards.append(
            {
                "key": key,
                "url": iss["url"],
                "title": iss["title"],
                "isOpen": iss["isOpen"],
                "status": status if iss["isOpen"] else "Closed",
                "statusColumn": status_column,
                "statusItem": status_item.get(key),
                "sessionColumn": session_column,
                "searchOnly": search_only,
                "updatedAt": max(iss["updatedAt"], last_touch),
                "sessions": ss,
                "pastSessions": [s for s in past if key in s["issues"]],
                "prs": list(prs.values()),
                "labels": m.get("labels", []),
                "subIssues": m.get("subIssues", {"total": 0, "completed": 0}),
                "assignees": m.get("assignees", []),
                "body": m.get("body", ""),
            }
        )
    liveness = {"needs": 0, "working": 1}
    cards.sort(key=lambda c: (liveness.get(c["sessionColumn"], 2), -c["updatedAt"]))
    # Unlinked means on no card: no issue found, or its issue has no card (e.g. GitHub couldn't be asked).
    on_card = {c["key"] for c in cards}
    unlinked = sorted(
        (s for s in sessions if not on_card.intersection(s["issues"]) and (s["kind"] != "idle" or s["updatedAt"] >= recent)),
        key=lambda s: -s["updatedAt"],
    )
    return {"cards": cards, "unlinked": unlinked, "fetchedAt": now_ms(), "errors": errors, "isOffline": False}


def columns(grouping: str) -> list[tuple[str, str]]:
    return STATUS_COLUMNS if grouping == "status" else SESSION_COLUMNS


def column_of(card: dict, grouping: str) -> str:
    return STATUS_BUCKET[card["statusColumn"]] if grouping == "status" else SESSION_BUCKET[card["sessionColumn"]]


def column_cards(cards: list[dict], col_id: str, grouping: str) -> list[dict]:
    """A column's cards in board order; by status, Backlog cards come after the other not-started ones."""
    members = [c for c in cards if column_of(c, grouping) == col_id]
    if grouping == "status":
        members.sort(key=lambda c: c["statusColumn"] == "backlog")
    return members


def badge(card: dict) -> str:
    ss = card["sessions"]
    if not ss:
        return ""
    live = sum(1 for s in ss if s["kind"] != "idle")
    mark = {"needs": "! ", "working": "~ "}.get(card["sessionColumn"], "")
    return f"{mark}{len(ss)}s" + (f"/{live} live" if live else "")


def ago(ms: int) -> str:
    m = max(0, round((now_ms() - ms) / 60_000))
    return f"{m}m" if m < 60 else f"{round(m / 60)}h" if m < 2880 else f"{round(m / 1440)}d"


def summary(board: dict, grouping: str) -> str:
    lines = []
    on_board = [c for c in board["cards"] if not c.get("searchOnly")]
    for col_id, title in columns(grouping):
        cs = column_cards(on_board, col_id, grouping)
        lines.append(f"## {title} ({len(cs)})")
        for c in cs:
            tag = badge(c) if grouping == "status" else c["status"]
            lines.append(f"- {c['key']} {c['title']}" + (f" [{tag}]" if tag else ""))
            lines += [f'  - {s["id"]} {s["state"]} "{s["name"]}" {ago(s["updatedAt"])}' for s in c["sessions"]]
    lines.append(f"## Unlinked sessions ({len(board['unlinked'])})")
    lines += [f'- {s["id"]} {s["state"]} "{s["name"]}" {ago(s["updatedAt"])}' for s in board["unlinked"]]
    if board["errors"]:
        lines += ["## Errors", *[f"- {e}" for e in board["errors"]]]
    return "\n".join(lines)


# --- actions -----------------------------------------------------------------

ITERM_SCRIPT = """
on run argv
  set cmd to item 1 of argv
  tell application "iTerm2"
    activate
    if (count of windows) = 0 then
      set s to current session of (create window with default profile)
    else
      tell current window to set t to (create tab with default profile)
      set s to current session of t
    end if
    tell s to write text cmd
  end tell
end run
"""
# Ghostty (1.3 or later) takes the command as the new shell's first input and the tab name as an override.
GHOSTTY_SCRIPT = """
on run argv
  set cmd to item 1 of argv
  set tabName to item 2 of argv
  tell application "Ghostty"
    activate
    set cfg to new surface configuration
    set initial input of cfg to cmd & linefeed
    if (count of windows) = 0 then
      set t to selected tab of (new window with configuration cfg)
    else
      set t to new tab in front window with configuration cfg
    end if
    set name override of t to tabName
  end tell
end run
"""


def attach(session_id: str, title: str = "", dry_run: bool = False, cwd: str = "") -> str:
    """Open a new tab in TERMINAL attached to the session; detaching leaves the tab's shell open.

    The tab closes itself once the session is deleted: `claude attach` returns when its session is
    removed, and the shell then exits because the job directory is gone.

    The tab first moves to the session's own directory: waking a finished session starts Claude in
    the shell's directory, which in a new tab is the home directory. The tab is named with an OSC 1
    escape from its own shell, as iTerm2 does not show a name set through AppleScript, and Claude
    Code's own terminal title is switched off so the tab keeps that name. Ghostty ignores the escape
    and gets the name as a tab name override instead.

    The other terminals (ATTACH_ARGV, or `attach_command` from the config file) run a command in the
    new tab rather than typing into its shell, so the line ends by starting a shell for the tab to
    stay open on, names the tab with OSC 2, and calls `claude` by its full path: a tab opened by a
    terminal's server process need not have the PATH tavla runs with. The return value is then the
    whole command, for `--dry-run`."""
    name, where = title or session_id, cwd or str(SESSION_CWD)
    gone = f"[ -d {shlex.quote(str(JOBS / session_id))} ] || exit"
    template = ATTACH_COMMAND or ATTACH_ARGV.get(TERMINAL)
    if template:
        claude = shlex.quote(shutil.which("claude") or "claude")
        script = f"printf '\\033]2;%s\\007' {shlex.quote(name)}; cd {shlex.quote(where)} && CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 {claude} attach {session_id}; {gone}; exec \"${{SHELL:-sh}}\""
        values = {"script": script, "title": name, "cwd": where}
        argv = [re.sub(r"\{(script|title|cwd)\}", lambda m: values[m[1]], a) for a in template]
        cmd = shlex.join(argv)
    elif TERMINAL in ("iterm2", "ghostty"):
        cmd = f"printf '\\033]1;%s\\007' {shlex.quote(name)} && cd {shlex.quote(where)} && CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 claude attach {session_id}; {gone}"
        argv = ["osascript", "-e", GHOSTTY_SCRIPT, cmd, name] if TERMINAL == "ghostty" else ["osascript", "-e", ITERM_SCRIPT, cmd]
    else:
        raise ValueError(f"tavla does not know the terminal it runs in: set terminal (one of {', '.join(TERMINALS)}) or attach_command in {CONFIG / 'config.toml'}")
    if not dry_run:
        try:
            run(argv, timeout=20)
        except FileNotFoundError as err:
            raise RuntimeError(f"{argv[0]} is not installed or not on PATH") from err
    return cmd


def terminal_label() -> str:
    return ATTACH_COMMAND[0] if ATTACH_COMMAND else TERMINALS.get(TERMINAL, "terminal")


def new_session(target: str, prompt: str = "", title: str = "") -> str:
    ref = ref_from_arg(target)
    if not ref:
        raise ValueError("expected an issue URL or owner/repo#N")
    url = target if target.startswith("https://") else f"https://github.com/{ref[0]}/{ref[1]}/issues/{ref[2]}"
    ask = prompt or "Pick up this issue: read it and its comments, then tell me where it stands and the next step."
    # Only the default prompt is shared by every session on an issue; a custom one lets Claude name the session after it.
    sid = start_background(f"{url}\n\n{ask}", "" if prompt else f"{ref[1]}#{ref[2]} {title}")
    link(sid, key_of(ref))
    return sid


def new_unlinked_session(prompt: str, name: str = "") -> str:
    """A background session on no issue; without a name Claude Code names it from the prompt."""
    if not prompt.strip():
        raise ValueError("a session on no issue needs a prompt")
    return start_background(prompt.strip(), name)


def start_background(prompt: str, name: str = "") -> str:
    argv = ["claude", "--bg", *(["--name", name.strip()[:60]] if name.strip() else []), prompt]
    out = run(argv, cwd=SESSION_CWD)
    m = re.search(r"\b[0-9a-f]{8}\b", out)
    if not m:
        raise RuntimeError(f"no session id in: {out.strip()}")
    return m[0]


def create_issue(repo: str, title: str, body: str = "", assign: bool = True) -> dict:
    """File an issue and return its card fields.

    An assigned issue is added to the assigned-issue cache, so the next refresh shows its card without
    waiting out the cache or the search index, which lags behind a new issue."""
    if not title.strip():
        raise ValueError("an issue needs a title")
    argv = ["gh", "issue", "create", "--repo", repo, "--title", title.strip(), "--body", body]
    if assign:
        argv += ["--assignee", "@me"]
    out = run(argv)
    m = URL_RE.search(out)
    if not m:
        raise RuntimeError(f"no issue URL in: {out.strip()[:200]}")
    url = m[0] if m[0].startswith("https://") else f"https://{m[0]}"
    issue = {"repository": {"nameWithOwner": f"{m[1]}/{m[2]}"}, "number": int(m[3]), "title": title.strip(), "url": url, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if assign:
        try:
            cached = json.loads((CONFIG / "assigned-cache.json").read_text())
            cached["items"] = [issue, *cached["items"]]
            (CONFIG / "assigned-cache.json").write_text(json.dumps(cached))
        except OSError, json.JSONDecodeError, KeyError, TypeError:
            pass  # no cache yet: the next refresh searches anyway
    return {"key": key_of((m[1], m[2], int(m[3]))), "url": url, "title": issue["title"]}


def start_progress(card: dict) -> bool:
    """Move a card whose status is Todo or Backlog to In Progress, on the project its status came from.

    False when there is nothing to move: another status, or no project item recorded for it. The
    cached project list is patched, so the next refresh shows the move without refetching."""
    item = card.get("statusItem")
    if card.get("status") not in NOT_STARTED or not item:
        return False
    owner, number = item["project"].split("/")
    proj = ((graphql(STATUS_FIELD_QUERY, {"owner": owner, "number": int(number)}).get("data") or {}).get("repositoryOwner") or {}).get("projectV2") or {}
    field = proj.get("field") or {}
    option = next((o for o in field.get("options") or [] if o["name"].lower() == "in progress"), None)
    if not proj.get("id") or not field.get("id") or not option:
        raise RuntimeError(f"project {item['project']} has no Status option In Progress")
    body = graphql(SET_STATUS_MUTATION, {"project": proj["id"], "item": item["itemId"], "field": field["id"], "option": option["id"]})
    if not ((body.get("data") or {}).get("updateProjectV2ItemFieldValue") or {}).get("projectV2Item"):
        raise RuntimeError(f"status not changed: {(body.get('errors') or [{}])[0].get('message', 'no answer')}")
    try:
        cached = json.loads((CONFIG / "projects-cache.json").read_text())
        for it in cached["items"]:
            if it.get("itemId") == item["itemId"]:
                it["status"] = option["name"]
        (CONFIG / "projects-cache.json").write_text(json.dumps(cached))
    except OSError, json.JSONDecodeError, KeyError, TypeError:
        pass  # no cache: the next refresh fetches the projects anyway
    return True


def resume_session(needle: str) -> dict:
    """Bring a deleted or ended session back as a background session, and return its history record.

    `claude --bg --resume` continues the conversation under the same id; it runs in the session's own
    directory, where Claude Code keeps the conversation. The issues the session was on are linked
    again, since deleting it dropped its manual links. A session that is still listed is left alone."""
    history = load_history()
    n = needle.lower()
    found = [r for r in history.values() if r.get("sessionId") and (n in (r["id"], r["sessionId"]) or n == r["name"].lower())]
    found = found or [r for r in history.values() if r.get("sessionId") and (r["id"].startswith(n) or n in r["name"].lower())]
    if len(found) != 1:
        raise ValueError(f'"{needle}" matches {", ".join(r["id"] for r in found) or "no recorded session"}; use an exact id')
    rec = found[0]
    if (JOBS / rec["id"]).is_dir():
        return rec
    cwd = Path(rec["cwd"]) if rec["cwd"] and Path(rec["cwd"]).is_dir() else SESSION_CWD
    named = ["--name", rec["name"][:60]] if rec["name"] != rec["id"] else []
    out = run(["claude", "--bg", *named, "--resume", rec["sessionId"]], cwd=cwd)
    m = re.search(r"\b[0-9a-f]{8}\b", out)
    if not m:
        raise RuntimeError(f"no session id in: {out.strip()}")
    for key in rec["issues"]:
        link(m[0], key)
    return {**rec, "id": m[0], "cwd": str(cwd)}


def stop_session(session_id: str) -> str:
    """Stop a background session; its conversation is kept and attaching resumes it."""
    return run(["claude", "stop", session_id], timeout=30).strip()


def delete_session(session_id: str) -> str:
    """Delete a background session and its worktree, and drop its manual links.

    A session no board build has recorded yet (one deleting itself) is recorded first from its job
    file and manual links, so its card still lists it for resuming.

    claude rm refuses a worktree with unpushed commits; its message, raised here, names the flag that discards them."""
    links = load_links()
    history = load_history()
    if session_id not in history and links.get(session_id):
        with suppress(OSError, json.JSONDecodeError):
            st = json.loads((JOBS / session_id / "state.json").read_text())
            when = parse_time_ms(st.get("updatedAt") or st.get("createdAt"))
            history[session_id] = history_record({"id": session_id, "sessionId": st.get("sessionId"), "name": st.get("name"), "cwd": st.get("cwd"), "issues": links[session_id], "updatedAt": when})
            save_history(history)
    unlink(session_id, None)  # before the delete: a session deleting itself does not outlive `claude rm`
    try:
        return run(["claude", "rm", session_id], timeout=60).strip()
    except Exception:
        save_links(links)
        raise


def link(session_id: str, issue_key: str) -> None:
    links = load_links()
    links[session_id] = sorted({*links.get(session_id, []), issue_key})
    save_links(links)


def unlink(session_id: str, issue_key: str | None) -> None:
    links = load_links()
    kept = [k for k in links.get(session_id, []) if issue_key and k != issue_key]
    if kept:
        links[session_id] = kept
    else:
        links.pop(session_id, None)
    save_links(links)


def resolve_session(needle: str) -> dict:
    matches = find_sessions([s for s, _ in read_sessions(load_links(), [])], needle)
    if len(matches) != 1:
        ids = ", ".join(s["id"] for s in matches) or "none"
        raise ValueError(f'"{needle}" matches {ids}; use an exact id')
    return matches[0]


def own_session() -> dict:
    """The session this process runs inside, from the environment Claude Code gives its tools."""
    job, sid = os.environ.get("CLAUDE_JOB_DIR", ""), os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not job and not sid:
        raise ValueError("--self works only inside a Claude Code session")
    return {"id": Path(job).name if job else sid[:8], "sessionId": sid, "name": "this session", "isInteractive": not job}


def main(argv: list[str] | None = None) -> int:
    try:
        load_config()
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    ap = argparse.ArgumentParser(prog="tavla", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("json", help="print the board as JSON")
    p.add_argument("--force", action="store_true", help="bypass the project status cache")
    p = sub.add_parser("dump", help="print the board as text")
    p.add_argument("--group", choices=["status", "session"], default="status")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("attach", help="attach to a session in a new terminal tab")
    p.add_argument("session")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("new", help="start a background session on an issue")
    p.add_argument("issue")
    p.add_argument("prompt", nargs="*")
    p = sub.add_parser("start", help="start a background session on no issue")
    p.add_argument("prompt", nargs="+")
    p.add_argument("--name", default="")
    p = sub.add_parser("create", help="create an issue assigned to me")
    p.add_argument("title")
    p.add_argument("--repo", default=ISSUE_REPOS[0][0] if ISSUE_REPOS else None, help="owner/repo; defaults to the first issue_repos entry in config.toml")
    p.add_argument("--body", default="")
    p = sub.add_parser("resume", help="bring a deleted session back as a background session, under the same id")
    p.add_argument("session")
    p = sub.add_parser("stop", help="stop a background session, keeping its conversation")
    p.add_argument("session")
    p = sub.add_parser("rm", help="delete a background session and its worktree")
    p.add_argument("session", nargs="?")
    p.add_argument("--self", dest="own", action="store_true", help="the session this command runs inside")
    p = sub.add_parser("link", help="link a session to an issue")
    p.add_argument("target", nargs="*", metavar="session|issue", help="<session> <issue>, or with --self only <issue>")
    p.add_argument("--self", dest="own", action="store_true", help="the session this command runs inside")
    p = sub.add_parser("unlink", help="remove manual links")
    p.add_argument("session")
    p.add_argument("issue", nargs="?")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "json":
            print(json.dumps(build_board(a.force)))
        elif a.cmd == "dump":
            print(summary(build_board(a.force), a.group))
        elif a.cmd == "attach":
            s = resolve_session(a.session)
            if s["isInteractive"]:
                raise ValueError(f"{s['id']} is an interactive session running in its own terminal; only background sessions attach")
            print(attach(s["id"], s["name"], a.dry_run, s["cwd"]))
        elif a.cmd == "new":
            print(new_session(a.issue, " ".join(a.prompt)))
        elif a.cmd == "start":
            print(new_unlinked_session(" ".join(a.prompt), a.name))
        elif a.cmd == "create":
            if not a.repo:
                raise ValueError("no --repo given and no issue_repos in config.toml")
            print(create_issue(a.repo, a.title, a.body)["url"])
        elif a.cmd == "resume":
            rec = resume_session(a.session)
            print(f'Resumed {rec["id"]} ("{rec["name"]}"); tavla attach {rec["id"]} opens it.')
        elif a.cmd in ("stop", "rm"):
            own = getattr(a, "own", False)
            if own == bool(a.session):
                raise ValueError("give a session or --self")
            s = own_session() if own else resolve_session(a.session)
            if s["isInteractive"]:
                raise ValueError(f"{s['id']} is an interactive session running in its own terminal; only background sessions stop or delete")
            print(stop_session(s["id"]) if a.cmd == "stop" else delete_session(s["id"]))
        elif a.cmd == "link":
            if len(a.target) != (1 if a.own else 2):
                raise ValueError("expected --self <issue>" if a.own else "expected <session> <issue>")
            s, ref = own_session() if a.own else resolve_session(a.target[0]), ref_from_arg(a.target[-1])
            if not ref:
                raise ValueError("expected an issue URL or owner/repo#N")
            link(s["id"], key_of(ref))
            print(f'Linked {s["id"]} ("{s["name"]}") to {key_of(ref)}.')
        elif a.cmd == "unlink":
            s, ref = resolve_session(a.session), ref_from_arg(a.issue or "")
            unlink(s["id"], key_of(ref) if ref else None)
            print(f"Removed manual links from {s['id']}. Links from its first prompt or PRs stay.")
    except (ValueError, RuntimeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
