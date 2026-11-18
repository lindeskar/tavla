import asyncio
import json
import re
import shlex
from datetime import UTC, datetime, timedelta

import pytest

from tavla import board as core

REAL_GRAPHQL = core.graphql  # the autouse fixture swaps in a fake

NOW = datetime.now(UTC).isoformat()
JOBS = {
    "aaaa1111": {"name": "fix flaky deploy", "state": "blocked", "intent": "https://github.com/acme/infra/issues/7 look", "updatedAt": NOW, "cwd": "/Users/me/Code/my repo"},
    "bbbb2222": {"name": "chart bump", "state": "working", "intent": "bump it", "children": [{"kind": "pr", "href": "https://github.com/acme/charts/pull/40"}], "updatedAt": NOW},
    "cccc3333": {"name": "random question", "state": "done", "intent": "what is x", "output": {"result": "x is y"}, "updatedAt": NOW},
}


def item(repo, number, title, status):
    content = {"__typename": "Issue", "number": number, "title": title, "url": f"https://github.com/{repo}/issues/{number}", "repository": {"nameWithOwner": repo}}
    return {"id": f"PVTI_{number}", "status": {"name": status}, "content": content}


PROJECTS = {
    "team": [item("acme/infra", 7, "Deploy is flaky", "Blocked")],
    "me": [item("acme/infra", 11, "Personal todo", "Todo"), item("acme/infra", 12, "Finished thing", "Done")],
    "extra": [],
}


def closed_item(repo, number, title, days_ago):
    closed_at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return {"content": {"__typename": "Issue", "number": number, "title": title, "url": f"https://github.com/{repo}/issues/{number}", "closedAt": closed_at, "repository": {"nameWithOwner": repo}}}


# The backlog project's closed items: GitHub filters on the update date only, so one closed before the window can come back.
CLOSED_PROJECTS = {"me": [closed_item("me/todo", 5, "Shipped report", 5), closed_item("me/todo", 4, "Ancient chore", 45)]}
CLOSED = {"acme/infra#20", "acme/infra#31", "me/todo#5", "me/todo#4"}
GRAPHQL_CALLS: list[str] = []
STATUS_SETS: list[dict] = []
RESUMED: list[dict] = []


def fake_run(argv, timeout=60, cwd=None):
    cmd = " ".join(argv)
    if cmd.startswith("claude agents"):
        # Real shape: background rows carry id/state, interactive rows only sessionId/status.
        return json.dumps(
            [
                {"kind": "background", "id": "aaaa1111", "sessionId": "aaaa1111-x", "name": "fix flaky deploy", "state": "blocked", "status": None},
                {"kind": "interactive", "sessionId": "eeee5555-0000-0000", "name": "side chat", "status": "idle", "cwd": "/w", "startedAt": 1},
            ]
        )
    if cmd.startswith("gh search issues") and "closed" in argv:
        GRAPHQL_CALLS.append("search closed")
        assert argv[argv.index("--closed") + 1] == f">={(datetime.now(UTC) - timedelta(days=30)).date()}"
        closed_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        return json.dumps([{"repository": {"nameWithOwner": "acme/infra"}, "number": 20, "title": "Rotated the key", "url": "https://github.com/acme/infra/issues/20", "closedAt": closed_at}])
    if cmd.startswith("gh search issues"):
        GRAPHQL_CALLS.append("search")  # gh search spends the GraphQL budget too
        return json.dumps([{"repository": {"nameWithOwner": "acme/infra"}, "number": 9, "title": "Assigned but untouched", "url": "https://github.com/acme/infra/issues/9", "updatedAt": NOW}])
    if cmd.startswith("gh issue create"):
        repo = argv[argv.index("--repo") + 1]
        return f"\nCreating issue in {repo}\n\nhttps://github.com/{repo}/issues/77\n"
    if cmd.startswith("claude --bg") and "--resume" in argv:
        RESUMED.append({"argv": argv, "cwd": cwd})
        return f"backgrounded · {argv[-1][:8]} (idle — send a prompt to start)\n"
    if cmd.startswith("claude --bg"):
        return "Started background session dddd4444\n"
    raise AssertionError(f"unexpected command: {cmd}")


def fake_graphql(query, variables=None):
    if query.strip().startswith("{ rateLimit"):
        return {"data": {"rateLimit": {"remaining": 10, "resetAt": "2030-01-01T00:00:00Z"}}}
    if "updateProjectV2ItemFieldValue" in query:
        STATUS_SETS.append(variables)
        return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["item"]}}}}
    if 'field(name: "Status")' in query:
        options = [{"id": "opt-todo", "name": "Todo"}, {"id": "opt-progress", "name": "In progress"}]
        return {"data": {"repositoryOwner": {"projectV2": {"id": f"PVT_{variables['owner']}", "field": {"id": "PVTSSF_status", "options": options}}}}}
    if "closedAt" in query and "projectV2" in query:
        GRAPHQL_CALLS.append(f"closed project {variables['owner']}")
        assert variables["filter"].startswith("is:closed is:issue updated:>=")
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": CLOSED_PROJECTS[variables["owner"]]}
        return {"data": {"repositoryOwner": {"projectV2": {"items": page}}}}
    if "projectV2" in query:
        GRAPHQL_CALLS.append(f"project {variables['owner']}")
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": PROJECTS[variables["owner"]]}
        return {"data": {"repositoryOwner": {"projectV2": {"items": page}}}}
    GRAPHQL_CALLS.append("refs")
    data = {}
    for alias, owner, repo, num in re.findall(r'(r\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\) \{ issueOrPullRequest\(number: (\d+)', query):

        def issue(n, t, r=f"{owner}/{repo}"):
            rich = n == 7
            return {
                "__typename": "Issue", "number": n, "title": t, "body": "Fails **twice** a day." if rich else "", "url": f"https://github.com/{r}/issues/{n}", "state": "CLOSED" if f"{r}#{n}" in CLOSED else "OPEN", "updatedAt": NOW,
                "repository": {"nameWithOwner": r},
                "labels": {"nodes": [{"name": "bug [p1]", "color": "d73a4a"}] if rich else []},
                "subIssuesSummary": {"total": 3 if rich else 0, "completed": 1 if rich else 0},
                "assignees": {"nodes": []},
                "closedByPullRequestsReferences": {"nodes": [{"number": 50, "state": "OPEN", "url": f"https://github.com/{r}/pull/50", "repository": {"nameWithOwner": r}}] if rich else []},
            }  # fmt: skip

        if repo == "charts":
            data[alias] = {
                "issueOrPullRequest": {
                    "__typename": "PullRequest",
                    "number": 40,
                    "state": "MERGED",
                    "url": "https://github.com/acme/charts/pull/40",
                    "repository": {"nameWithOwner": "acme/charts"},
                    "closingIssuesReferences": {"nodes": [issue(3, "Upgrade chart", "acme/charts")]},
                }
            }
        else:
            data[alias] = {"issueOrPullRequest": issue(int(num), "Deploy is flaky" if num == "7" else f"Issue {num}")}
    return {"data": data}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    for jid, st in JOBS.items():
        (jobs / jid).mkdir(parents=True)
        (jobs / jid / "state.json").write_text(json.dumps(st))
    monkeypatch.setattr(core, "JOBS", jobs)
    monkeypatch.setattr(core, "TRANSCRIPTS", tmp_path / "projects")
    monkeypatch.setattr(core, "CONFIG", tmp_path / "config")
    monkeypatch.setattr(
        core, "PROJECTS", [{"owner": "me", "number": "1", "is_backlog": True}, {"owner": "team", "number": "23", "is_backlog": False}, {"owner": "extra", "number": "2", "is_backlog": False}]
    )
    monkeypatch.setattr(core, "ISSUE_REPOS", [("me/todo", "Personal"), ("team/tracker", "Team")])
    monkeypatch.setattr(core, "SESSION_CWD", tmp_path)
    monkeypatch.setattr(core, "THEME", "tavla")
    monkeypatch.setattr(core, "TERMINAL", "iterm2")
    monkeypatch.setattr(core, "ATTACH_COMMAND", [])
    monkeypatch.setattr(core, "run", fake_run)
    monkeypatch.setattr(core, "graphql", fake_graphql)
    GRAPHQL_CALLS.clear()
    STATUS_SETS.clear()
    RESUMED.clear()


def test_routine_refresh_reads_github_metadata_from_cache():
    core.build_board()
    first = list(GRAPHQL_CALLS)
    assert first.count("refs") == 1 and {"search", "project team", "project me", "project extra"} <= set(first)
    assert first.count("search closed") == 1 and [c for c in first if c.startswith("closed project")] == ["closed project me"]  # backlog projects only
    GRAPHQL_CALLS.clear()
    core.build_board()
    assert GRAPHQL_CALLS == []  # search, projects and issue metadata all served from cache
    core.build_board(force=True)
    assert {"search", "refs", "project team"} <= set(GRAPHQL_CALLS)


def test_project_lists_ask_github_for_open_issues_only(monkeypatch):
    queries = []
    monkeypatch.setattr(core, "graphql", lambda query, variables=None: queries.append(query) or fake_graphql(query, variables))
    core.build_board()
    project_queries = [q for q in queries if "fieldValueByName" in q]
    assert len(project_queries) == 3 and all('query: "is:open is:issue"' in q for q in project_queries)


def test_rate_limit_pauses_github_calls_and_keeps_failing_fast(monkeypatch):
    def limited(argv, timeout=60, cwd=None):
        if argv[:3] == ["gh", "search", "issues"]:
            raise core.RateLimited("gh search issues: API rate limit exceeded")
        if argv[:3] == ["gh", "api", "rate_limit"]:
            return json.dumps({"resources": {"graphql": {"remaining": 10, "reset": 0}, "search": {"remaining": 0, "reset": 4_000_000_000}}})
        return fake_run(argv, timeout, cwd)

    monkeypatch.setattr(core, "run", limited)
    board = core.build_board()
    assert core.backoff_until() == 4_000_000_000  # the search budget's reset, not the 90s default
    assert board["isOffline"] and "paused until" in board["errors"][0]
    # Nothing about the sessions' issues is cached yet, so every recent session is unlinked.
    assert [s["id"] for s in board["unlinked"]] == ["aaaa1111", "bbbb2222", "cccc3333"]
    assert all(not c["sessions"] for c in board["cards"])

    calls = []
    monkeypatch.setattr(core, "run", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setattr(core, "graphql", lambda *a, **k: calls.append(a) or {})
    board = core.build_board(force=True)
    assert [c for c in calls if c[0][:1] != ["claude"]] == []  # no GitHub call while paused; the local session listing still runs
    assert board["isOffline"] and len(board["unlinked"]) == 3


def test_paused_board_links_sessions_from_cached_github_data(monkeypatch):
    online = core.build_board()
    (core.CONFIG / "backoff.json").write_text(json.dumps({"until": 4_000_000_000}))
    calls = []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or (fake_run(argv) if argv[0] == "claude" else ""))
    monkeypatch.setattr(core, "graphql", lambda *a, **k: calls.append(a) or {})
    paused = core.build_board()
    assert [c for c in calls if c[0] != "claude"] == []  # only the local session listing ran
    assert paused["isOffline"] and "cached GitHub data" in paused["errors"][0]

    def strip(b):
        return [(c["key"], [s["id"] for s in c["sessions"]]) for c in b["cards"]]

    assert strip(paused) == strip(online)
    assert [s["id"] for s in paused["unlinked"]] == [s["id"] for s in online["unlinked"]]


def test_unreachable_github_keeps_the_cached_board(monkeypatch):
    online = core.build_board()

    def no_network(argv, timeout=60, cwd=None):
        if argv[0] == "gh":
            raise RuntimeError("gh search issues: error connecting to api.github.com")
        return fake_run(argv, timeout, cwd)

    def no_graphql(query, variables=None):
        raise json.JSONDecodeError("no answer", "", 0)

    monkeypatch.setattr(core, "run", no_network)
    monkeypatch.setattr(core, "graphql", no_graphql)
    offline = core.build_board(force=True)  # every cache is asked to refresh, and none can

    def strip(b):
        return [(c["key"], c["status"], c["statusItem"], [s["id"] for s in c["sessions"]]) for c in b["cards"]]

    assert strip(offline) == strip(online)
    assert any("not readable" in e for e in offline["errors"])
    assert not core.backoff_until()  # a dead network is not a rate limit: the next refresh asks again


def test_one_unreadable_project_keeps_its_cached_statuses(monkeypatch):
    core.build_board()
    monkeypatch.setitem(PROJECTS, "me", [item("acme/infra", 11, "Personal todo", "In progress")])

    def team_down(query, variables=None):
        return {} if "projectV2" in query and variables["owner"] == "team" else fake_graphql(query, variables)

    monkeypatch.setattr(core, "graphql", team_down)
    status = {c["key"]: c["status"] for c in core.build_board(force=True)["cards"]}
    assert status["acme/infra#11"] == "In progress"  # the readable project is fresh
    assert status["acme/infra#7"] == "Blocked"  # the unreadable one keeps its last answer


def test_tui_draws_the_cached_board_before_github_answers(monkeypatch):
    import threading

    from tavla import tui

    core.build_board()  # an earlier run left its caches
    for name in ("assigned", "projects", "closed"):  # long expired, as at a morning's first start
        cache = json.loads((core.CONFIG / f"{name}-cache.json").read_text())
        (core.CONFIG / f"{name}-cache.json").write_text(json.dumps({**cache, "at": 0}))
    release = threading.Event()

    def slow_graphql(query, variables=None):
        release.wait(10)
        return fake_graphql(query, variables)

    monkeypatch.setattr(core, "graphql", slow_graphql)
    monkeypatch.setitem(PROJECTS, "team", [item("acme/infra", 7, "Deploy is flaky", "In progress")])

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(180, 40)) as pilot:
            for _ in range(100):
                if app.data["cards"]:
                    break
                await pilot.pause(0.05)
            assert str(app.query_one("#count-blocked").render()) == "1"  # the cached status, while GitHub is still out
            assert "refreshing" in str(app.query_one("#status").render())
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert str(app.query_one("#count-blocked").render()) == "0"  # the refresh replaced it
            assert "updated" in str(app.query_one("#status").render())

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_exhausted_graphql_budget_pauses_until_its_reset(monkeypatch):
    (core.CONFIG).mkdir(parents=True, exist_ok=True)

    def exhausted(query, variables=None):
        if query.strip().startswith("{ rateLimit"):
            return {"data": {"rateLimit": {"remaining": 0, "resetAt": "2096-10-06T12:00:00Z"}}}
        raise core.RateLimited("graphql: RATE_LIMIT API rate limit already exceeded")

    monkeypatch.setattr(core, "graphql", exhausted)
    assert core.build_board()["isOffline"]
    assert core.backoff_until() == core.parse_time_ms("2096-10-06T12:00:00Z") / 1000


def test_graphql_detects_rate_limits_from_errors_not_from_data(monkeypatch):
    class Done:
        def __init__(self, stdout, stderr=""):
            self.stdout, self.stderr, self.returncode = stdout, stderr, 0

    title = json.dumps({"data": {"r0": {"issueOrPullRequest": {"title": "Handle the API rate limit in the poller"}}}})
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: Done(title))
    assert REAL_GRAPHQL("query { x }")["data"]["r0"]["issueOrPullRequest"]["title"].endswith("poller")

    limited = json.dumps({"errors": [{"type": "RATE_LIMIT", "message": "API rate limit already exceeded for user ID 1."}]})
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: Done(limited))
    with pytest.raises(core.RateLimited):
        REAL_GRAPHQL("query { x }")


def test_groups_by_issue_status_with_session_badges():
    text = core.summary(core.build_board(), "status")
    # Not-started statuses (todo, backlog, none) share Todo; Blocked keeps its own column after In Progress.
    assert "## Todo (3)\n- acme/charts#3 Upgrade chart [~ 1s/1 live]" in text
    assert "- acme/infra#11 Personal todo" in text and "- acme/infra#9 Assigned but untouched" in text
    assert "## In Progress (0)\n## Blocked (1)\n- acme/infra#7 Deploy is flaky [! 1s/1 live]\n  - aaaa1111" in text
    assert "## Done (0)" in text and "Finished thing" not in text
    cards = {c["key"]: c for c in core.build_board()["cards"]}
    assert cards["acme/infra#7"]["statusColumn"] == "blocked"  # its own status survives the merge
    assert "## Unlinked sessions (1)\n- cccc3333" in text


def test_cards_carry_prs_labels_and_sub_issues():
    cards = {c["key"]: c for c in core.build_board()["cards"]}
    flaky = cards["acme/infra#7"]
    assert flaky["prs"] == [{"key": "acme/infra#50", "url": "https://github.com/acme/infra/pull/50", "state": "OPEN"}]
    assert flaky["labels"] == [{"name": "bug [p1]", "color": "d73a4a"}]
    assert flaky["subIssues"] == {"total": 3, "completed": 1}
    assert flaky["body"] == "Fails **twice** a day."
    # A session's own PR counts on the card it works on.
    assert [(p["key"], p["state"]) for p in cards["acme/charts#3"]["prs"]] == [("acme/charts#40", "MERGED")]
    # Issues known only from search are enriched too.
    assert cards["acme/infra#9"]["labels"] == [] and cards["acme/infra#9"]["prs"] == []


def test_groups_by_session_state():
    text = core.summary(core.build_board(), "session")
    # Needs-you and working share Active, needs-you first.
    assert "## Active (2)\n- acme/infra#7 Deploy is flaky [Blocked]\n  - aaaa1111 blocked" in text
    assert "\n- acme/charts#3 Upgrade chart\n  - bbbb2222 working" in text.split("## Idle")[0]
    assert "## No session (2)" in text


def test_link_moves_session_onto_issue_and_unlink_moves_it_back():
    assert core.main(["link", "cccc3333", "acme/infra#9"]) == 0
    text = core.summary(core.build_board(), "session")
    assert "## Unlinked sessions (0)" in text and "## Idle (1)\n- acme/infra#9" in text
    assert core.main(["unlink", "cccc3333"]) == 0
    assert "## Unlinked sessions (1)" in core.summary(core.build_board(), "session")


def test_new_session_names_and_links_it(monkeypatch):
    calls, cwds = [], []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or cwds.append(cwd) or fake_run(argv))
    assert core.new_session("https://github.com/acme/infra/issues/9", "", "Assigned but untouched") == "dddd4444"
    assert cwds[-1] == core.SESSION_CWD
    argv = calls[-1]
    assert argv[:4] == ["claude", "--bg", "--name", "infra#9 Assigned but untouched"]
    assert argv[4].startswith("https://github.com/acme/infra/issues/9\n\n")
    assert core.load_links() == {"dddd4444": ["acme/infra#9"]}


def test_new_session_with_a_prompt_leaves_the_name_to_claude(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or fake_run(argv))
    core.new_session("acme/infra#9", "why is the plan failing?", "Assigned but untouched")
    assert "--name" not in calls[-1]
    assert calls[-1][2] == "https://github.com/acme/infra/issues/9\n\nwhy is the plan failing?"


def test_interactive_sessions_are_listed_and_linkable_but_not_attached(capsys):
    board = core.build_board()
    assert not board["errors"]
    assert [s["id"] for s in board["cards"][0]["sessions"]] == ["aaaa1111"]  # background row not duplicated
    core.main(["link", "eeee5555", "acme/infra#9"])
    card = next(c for c in core.build_board()["cards"] if c["key"] == "acme/infra#9")
    assert [(s["id"], s["state"], s["isInteractive"]) for s in card["sessions"]] == [("eeee5555", "idle", True)]
    assert core.main(["attach", "eeee5555", "--dry-run"]) == 1
    assert "interactive session" in capsys.readouterr().err


def test_attach_opens_a_named_ghostty_tab_when_the_terminal_is_ghostty(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or "")
    monkeypatch.setattr(core, "TERMINAL", "ghostty")
    cmd = core.attach("aaaa1111", "fix flaky deploy", cwd="/Users/me/Code/my repo")
    osa = calls[-1]
    assert osa[0] == "osascript" and 'tell application "Ghostty"' in osa[2] and "iTerm2" not in osa[2]
    # The same shell line as in iTerm2, plus the tab name for the name override.
    assert osa[3:] == [cmd, "fix flaky deploy"] and "cd '/Users/me/Code/my repo' && " in cmd
    core.attach("bbbb2222")
    assert calls[-1][4] == "bbbb2222"


def test_config_picks_the_terminal_and_rejects_an_unknown_one(monkeypatch):
    core.CONFIG.mkdir(parents=True, exist_ok=True)
    (core.CONFIG / "config.toml").write_text('terminal = "Ghostty"\n')
    core.load_config()
    assert core.TERMINAL == "ghostty"
    # Without the key the detected terminal stays.
    (core.CONFIG / "config.toml").write_text("")
    core.load_config()
    assert core.TERMINAL == "ghostty"
    (core.CONFIG / "config.toml").write_text('terminal = "xterm"\n')
    with pytest.raises(ValueError, match="terminal must be one of iterm2, ghostty"):
        core.load_config()


def test_terminal_is_detected_from_the_environment_outside_macos():
    assert core.detect_terminal({"TERM_PROGRAM": "ghostty"}, "darwin") == "ghostty"
    assert core.detect_terminal({"TMUX": "/tmp/tmux-1000/default,1,0"}, "darwin") == "iterm2"
    assert core.detect_terminal({"TERMINATOR_UUID": "urn:uuid:1"}, "linux") == "terminator"
    assert core.detect_terminal({"GNOME_TERMINAL_SCREEN": "/org/gnome/Terminal/screen/1"}, "linux") == "gnome-terminal"
    # Inside tmux the outer terminal's variables are still set, and the new tab belongs in tmux.
    assert core.detect_terminal({"TERMINATOR_UUID": "urn:uuid:1", "TMUX": "/tmp/tmux-1000/default,1,0"}, "linux") == "tmux"
    assert core.detect_terminal({"TERM": "xterm-256color"}, "linux") == ""


def test_attach_runs_the_terminals_own_new_tab_command(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or "")
    monkeypatch.setattr(core.shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(core, "TERMINAL", "terminator")
    printed = core.attach("aaaa1111", "fix flaky deploy", cwd="/home/me/Code/my repo")
    # The tab runs the line itself, so it ends in a shell to stay open on, unless the session is deleted.
    script = (
        "printf '\\033]2;%s\\007' 'fix flaky deploy'; cd '/home/me/Code/my repo' && CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 /opt/bin/claude attach aaaa1111; "
        f'[ -d {core.JOBS / "aaaa1111"} ] || exit; exec "${{SHELL:-sh}}"'
    )
    assert calls[-1] == ["terminator", "--new-tab", "-x", "sh", "-c", script]
    assert printed == shlex.join(calls[-1])
    monkeypatch.setattr(core, "TERMINAL", "tmux")
    core.attach("bbbb2222", "chart {cwd} bump")
    assert calls[-1][:4] == ["tmux", "new-window", "-n", "chart {cwd} bump"] and f"cd {core.SESSION_CWD} && " in calls[-1][-1]
    # --dry-run prints the command and opens nothing.
    assert core.attach("bbbb2222", dry_run=True).startswith("tmux new-window -n bbbb2222 sh -c ") and len(calls) == 2


def test_attach_command_in_the_config_file_replaces_the_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or "")
    core.CONFIG.mkdir(parents=True, exist_ok=True)
    (core.CONFIG / "config.toml").write_text('attach_command = ["alacritty", "--title", "{title}", "--working-directory", "{cwd}", "-e", "sh", "-c", "{script}"]\n')
    core.load_config()
    core.attach("aaaa1111", "fix flaky deploy", cwd="/home/me/repo")
    assert calls[-1][:7] == ["alacritty", "--title", "fix flaky deploy", "--working-directory", "/home/me/repo", "-e", "sh"] and " attach aaaa1111; " in calls[-1][-1]
    assert core.terminal_label() == "alacritty"
    (core.CONFIG / "config.toml").write_text('attach_command = ["xterm", "-e"]\n')
    with pytest.raises(ValueError, match="attach_command must be a list of strings"):
        core.load_config()


def test_attach_says_what_to_configure_when_the_terminal_is_unknown_or_missing(monkeypatch, capsys):
    monkeypatch.setattr(core, "TERMINAL", "")
    assert core.main(["attach", "aaaa1111"]) == 1
    assert "set terminal (one of iterm2, ghostty, tmux," in capsys.readouterr().err

    def missing(argv, timeout=60, cwd=None):
        if argv[0] == "konsole":
            raise FileNotFoundError(argv[0])
        return fake_run(argv, timeout, cwd)

    monkeypatch.setattr(core, "run", missing)
    monkeypatch.setattr(core, "TERMINAL", "konsole")
    assert core.main(["attach", "aaaa1111"]) == 1
    assert "konsole is not installed or not on PATH" in capsys.readouterr().err


def test_attach_opens_an_iterm_tab_in_the_session_directory(monkeypatch):
    calls = []
    real_run = core.run
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or ("" if argv[0] == "osascript" else real_run(argv, timeout)))
    assert core.main(["attach", "aaaa1111"]) == 0
    osa = calls[-1]
    assert osa[0] == "osascript" and 'tell application "iTerm2"' in osa[2]
    # The tail closes the tab once the session is deleted: its job directory is then gone.
    assert osa[3:] == [
        f"printf '\\033]1;%s\\007' 'fix flaky deploy' && cd '/Users/me/Code/my repo' && CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 claude attach aaaa1111; [ -d {core.JOBS / 'aaaa1111'} ] || exit"
    ]
    # A session with no recorded directory starts where new sessions do.
    core.attach("bbbb2222", "chart bump")
    assert calls[-1][3] == f"printf '\\033]1;%s\\007' 'chart bump' && cd {core.SESSION_CWD} && CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 claude attach bbbb2222; [ -d {core.JOBS / 'bbbb2222'} ] || exit"


def test_tui_draws_columns_and_attaches_selected_session(monkeypatch):
    from tavla import tui

    attached = []
    monkeypatch.setattr(core, "attach", lambda sid, title="", dry_run=False, cwd="": attached.append((sid, cwd)) or "")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(180, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert str(app.query_one("#count-blocked").render()) == "1"
            blocked = app.query_one("#col-blocked")
            blocked.focus()
            blocked.index = 0
            await pilot.pause()
            assert app.query_one("#detail-body", tui.Markdown).source == "Fails **twice** a day."  # the body, not the card again
            assert str(app.query_one("#detail-link").render()) == "infra #7"

            card = app.query_one("#col-blocked CardItem")
            texts = [str(lb.render()) for lb in card.query("Label")]
            assert texts[0] == "Deploy is flaky"  # the title leads the card
            meta = next(t for t in texts if "PR" in t)
            assert "needs you" in meta and "1 PR" in meta and "☑ 1/3" in meta and "Blocked" not in meta  # the column already says it
            assert any("bug [p1]" in t for t in texts)  # label text is escaped, not read as markup
            link = card.query_one(tui.IssueLink)
            assert (str(link.render()), link.url) == ("infra #7", "https://github.com/acme/infra/issues/7")

            opened = []
            app.open_url = lambda url, new_tab=True: opened.append(url)
            # Cards mount asynchronously; click only once the layout has settled so the link is where it is drawn.
            await pilot.wait_for_scheduled_animations()
            await pilot.pause(0.1)
            await pilot.click(link)
            await pilot.pause()
            assert opened == ["https://github.com/acme/infra/issues/7"]
            assert not app.query_one("#sessions").has_focus  # the click did not select the card
            blocked.focus()
            await pilot.pause()
            await pilot.press("enter")  # card -> its sessions
            await pilot.press("enter")  # session -> attach
            await app.workers.wait_for_complete()
            await pilot.press("g")
            await pilot.pause()
            assert str(app.query_one("#count-active").render()) == "2"
            assert app.query_one("#header-active").has_class("alert")  # a card in it needs you

    asyncio.run(scenario())
    assert attached == [("aaaa1111", "/Users/me/Code/my repo")]


def test_tui_footer_clips_key_hints_in_a_narrow_terminal():
    from tavla import tui

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(60, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            footer = app.query_one(tui.Footer)
            assert footer.virtual_size.width > footer.size.width  # the hints overflow
            assert footer.scrollable_content_region.height == 1  # still drawn, no scrollbar in their place

    asyncio.run(scenario())


def test_tui_refresh_keeps_focus_and_selection():
    import copy
    import inspect

    async def settle(result):
        if inspect.isawaitable(result):
            await result

    from tavla import tui

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(180, 50)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            col = app.query_one("#col-todo")
            col.focus()
            await pilot.press("down", "down")  # second card in No status
            await pilot.pause()
            picked = app.item_key(col.highlighted_child)
            assert picked is not None and col.index == 1
            before = list(col.children)

            # A refresh that changes another column and leaves this one alone.
            data = copy.deepcopy(app.data)
            data["unlinked"][0]["name"] = "Renamed elsewhere"
            await settle(app.apply(data))
            await pilot.pause()
            assert app.focused is col and col.index == 1 and app.item_key(col.highlighted_child) == picked
            assert list(col.children) == before  # unchanged column was not redrawn
            assert app.current_key() == picked  # the detail panel stayed on the picked card

            # A refresh that changes this column keeps the same card highlighted.
            data = copy.deepcopy(data)
            for c in data["cards"]:
                if core.column_of(c, "status") == "todo":
                    c["title"] += " (edited)"
            await settle(app.apply(data))
            await pilot.pause()
            assert app.focused is col and app.item_key(col.highlighted_child) == picked
            assert app.current_key() == picked

    asyncio.run(scenario())


def test_tui_list_view_groups_rows_and_keeps_the_cursor_off_headings(monkeypatch):
    from tavla import tui

    attached = []
    monkeypatch.setattr(core, "attach", lambda sid, title="", dry_run=False, cwd="": attached.append(sid) or "")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            lv = app.query_one("#col-list")
            groups = [(r.group["title"], r.group["count"], r.has_class("alert")) for r in lv.query(tui.GroupRow)]
            assert ("Blocked", 1, True) in groups and groups[-1][0] == "Unlinked"
            assert not any(count == 0 for _, count, _ in groups)  # empty groups are left out
            lv.focus()
            lv.index = None
            await pilot.press("down")
            await pilot.pause()
            assert isinstance(lv.highlighted_child, tui.CardRow)  # the first heading is stepped over
            for _ in range(len(lv.children)):
                if app.current_key() == "card:acme/infra#7":
                    break
                await pilot.press("down")
            assert str(app.query_one("#detail-link").render()) == "infra #7"
            await pilot.press("enter", "enter")  # row -> its sessions -> attach
            await app.workers.wait_for_complete()
            await pilot.press("g")  # grouping switches within the list view
            await pilot.pause()
            assert next(r.group["title"] for r in app.query(tui.GroupRow)) == "Active"
            assert app.current_key() == "card:acme/infra#7"
            await pilot.press("v")  # and back to columns
            await pilot.pause()
            assert app.query_one("#count-active") and not app.query("#col-list")

    asyncio.run(scenario())
    assert attached == ["aaaa1111"]


def test_tui_list_view_tab_moves_between_groups_then_to_sessions():
    from tavla import tui

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            lv = app.query_one("#col-list")
            lv.focus()
            titles = [r.group["title"] for r in lv.query(tui.GroupRow)]

            def group_of_cursor():
                return next(n.group["title"] for n in reversed(lv.children[: lv.index]) if isinstance(n, tui.GroupRow))

            await pilot.pause()
            assert group_of_cursor() == titles[0]  # focusing the list highlights its first row
            seen = [titles[0]]
            for _ in titles[1:]:
                await pilot.press("tab")
                seen.append(group_of_cursor())
                assert not lv.children[lv.index].disabled  # lands on the group's first row, not its heading
            assert seen == titles
            await pilot.press("shift+tab")
            assert group_of_cursor() == titles[-2]
            await pilot.press("tab", "tab")  # past the last group: on to the sessions panel
            assert app.focused is app.query_one("#sessions")
            await pilot.press("shift+tab")  # back into the list
            assert app.focused is lv

    asyncio.run(scenario())


def test_tui_tab_to_another_column_shows_its_card_in_the_detail():
    from tavla import tui

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            cols = [lv for lv in app.query(".col ListView").results(tui.CardList) if lv.children]
            assert len(cols) > 1
            for lv in reversed(cols):  # every column already has a highlighted card, so tab moves no cursor
                lv.focus()
                lv.index = 0
                await pilot.pause()
            assert app.current is not None and app.current is tui.BoardApp.payload(cols[0].highlighted_child)
            while True:  # tab past any empty column to the next card
                await pilot.press("tab")
                await pilot.pause()
                if app.focused in cols:
                    break
            assert app.focused is not cols[0]
            assert app.current_key() == tui.BoardApp.item_key(app.focused.highlighted_child)
            sessions = app.current.get("sessions", [app.current])
            assert [r.sess["id"] for r in app.query_one("#sessions").children] == [s["id"] for s in sessions]

    asyncio.run(scenario())


def test_tui_home_and_end_jump_to_the_first_and_last_row():
    from tavla import tui

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            col = next(lv for lv in app.query(".col ListView").results(tui.CardList) if len(lv.children) > 1)
            col.focus()
            await pilot.press("end")
            assert col.index == len(col.children) - 1
            await pilot.press("home")
            assert col.index == 0
            await pilot.press("v")
            await pilot.pause()
            lv = app.query_one("#col-list")
            lv.focus()
            await pilot.press("end")
            assert lv.index == len(lv.children) - 1
            assert app.current_key() == tui.BoardApp.item_key(lv.children[-1])  # the detail follows the jump
            await pilot.press("home")
            assert lv.index == 1  # the first row, not the group heading above it

    asyncio.run(scenario())


def test_tui_enter_on_a_single_session_card_opens_its_sessions_without_attaching(monkeypatch):
    from tavla import tui

    attached = []
    monkeypatch.setattr(core, "attach", lambda sid, title="", dry_run=False, cwd="": attached.append(sid) or "")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            blocked = app.query_one("#col-blocked")
            blocked.focus()
            blocked.index = 0
            await pilot.pause()
            assert len(app.current["sessions"]) == 1
            # Enter reaching the app rather than the list (nothing focused) must not attach either.
            app.set_focus(None)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert app.focused is app.query_one("#sessions") and attached == []
            await pilot.press("enter")  # from the sessions panel it attaches
            await app.workers.wait_for_complete()
            assert attached == ["aaaa1111"]

    asyncio.run(scenario())


def test_tui_clicking_a_card_selects_it_and_keeps_the_focus_on_the_board():
    from tavla import tui

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.wait_for_scheduled_animations()
            await pilot.pause(0.1)
            blocked = app.query_one("#col-blocked")
            card = blocked.query_one(tui.CardItem)
            assert card.card["sessions"]  # a card with sessions, which enter would move to
            await pilot.click(card.query_one(".card-title"))
            await pilot.pause()
            assert app.focused is blocked and blocked.highlighted_child is card
            assert app.current_key() == tui.BoardApp.item_key(card)

            await pilot.press("v")
            await pilot.pause()
            await pilot.wait_for_scheduled_animations()
            await pilot.pause(0.1)
            lv = app.query_one("#col-list")
            row = next(r for r in lv.children if isinstance(r, tui.CardRow) and r.card["sessions"])
            await pilot.click(row)
            await pilot.pause()
            assert app.focused is lv and lv.highlighted_child is row
            assert app.current_key() == tui.BoardApp.item_key(row)

    asyncio.run(scenario())


def test_create_issue_assigns_me_and_shows_on_the_next_refresh_without_a_search(monkeypatch):
    core.build_board()  # fills the assigned-issue cache
    calls = []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or fake_run(argv))
    issue = core.create_issue("me/todo", " Write it down ", "why")
    assert issue == {"key": "me/todo#77", "url": "https://github.com/me/todo/issues/77", "title": "Write it down"}
    assert calls[-1] == ["gh", "issue", "create", "--repo", "me/todo", "--title", "Write it down", "--body", "why", "--assignee", "@me"]
    keys = [c["key"] for c in core.build_board()["cards"]]
    assert "me/todo#77" in keys and "acme/infra#9" in keys
    assert not any(a[:3] == ["gh", "search", "issues"] for a in calls)  # the cache was still fresh

    core.create_issue("team/tracker", "Team thing", assign=False)
    assert "--assignee" not in calls[-1]
    with pytest.raises(ValueError):
        core.create_issue("me/todo", "  ")


def test_tui_new_issue_defaults_to_personal_then_starts_and_attaches_a_session(monkeypatch):
    from tavla import tui

    created, started, attached = [], [], []
    monkeypatch.setattr(
        core,
        "create_issue",
        lambda repo, title, body="", assign=True: created.append((repo, title, body, assign)) or {"key": f"{repo}#77", "url": f"https://github.com/{repo}/issues/77", "title": title},
    )
    monkeypatch.setattr(core, "new_session", lambda target, prompt="", title="": started.append((target, prompt)) or "dddd4444")
    monkeypatch.setattr(core, "attach", lambda sid, title="", dry_run=False, cwd="": attached.append(sid) or "")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("i")
            await pilot.pause()
            assert isinstance(app.screen, tui.NewIssue)
            await pilot.press("enter")  # no title yet: stays open
            await pilot.pause()
            assert isinstance(app.screen, tui.NewIssue) and created == []
            await pilot.press(*"Personal one")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert created == [("me/todo", "Personal one", "", True)]
            assert isinstance(app.screen, tui.AskPrompt)
            long = "go " * 40  # wider than the box: it wraps onto more lines instead of scrolling
            await pilot.press(*long)
            await pilot.pause()
            box = app.screen.query_one("#prompt")
            assert box.region.height > 3 and box.wrapped_document.height > 1
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert started == [("https://github.com/me/todo/issues/77", long.strip())] and attached == ["dddd4444"]

            # The team repo is one pick away; board keys typed on the dialog's controls do nothing.
            await pilot.press("i")
            await pilot.pause()
            await pilot.press(*"Team one")
            await pilot.press("shift+tab")
            await pilot.pause()
            assert isinstance(app.focused, tui.RadioSet)
            await pilot.press("down", "enter", "v", "n")
            await pilot.pause()
            assert app.view == "board" and isinstance(app.screen, tui.NewIssue)
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert created[-1][0] == "team/tracker"
            await pilot.press("escape")  # no session this time
            await app.workers.wait_for_complete()
            assert len(started) == 1

    asyncio.run(scenario())


def test_start_progress_moves_a_todo_card_on_its_own_project_and_the_next_refresh_shows_it():
    cards = {c["key"]: c for c in core.build_board()["cards"]}
    todo, blocked = cards["acme/infra#11"], cards["acme/infra#7"]
    assert todo["statusItem"] == {"project": "me/1", "itemId": "PVTI_11"}
    assert core.start_progress(blocked) is False and STATUS_SETS == []  # only Todo moves
    assert core.start_progress(todo) is True
    assert STATUS_SETS == [{"project": "PVT_me", "item": "PVTI_11", "field": "PVTSSF_status", "option": "opt-progress"}]
    GRAPHQL_CALLS.clear()
    card = next(c for c in core.build_board()["cards"] if c["key"] == "acme/infra#11")
    assert (card["status"], card["statusColumn"]) == ("In progress", "progress")
    assert not any(c.startswith("project") for c in GRAPHQL_CALLS)  # from the patched cache


def test_tui_new_session_offers_in_progress_only_on_todo_cards(monkeypatch):
    from tavla import tui

    started, moved = [], []
    monkeypatch.setattr(core, "new_session", lambda target, prompt="", title="": started.append(target) or "dddd4444")
    monkeypatch.setattr(core, "start_progress", lambda card: moved.append(card["key"]) or True)

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            async def select(column, key):
                col = app.query_one(f"#col-{column}")
                col.focus()
                col.index = next(i for i, it in enumerate(col.children) if it.card["key"] == key)
                await pilot.pause()

            await select("todo", "acme/infra#11")
            await pilot.press("n")
            await pilot.pause()
            assert app.screen.query_one("#to-progress", tui.Checkbox).value is True
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert started == ["https://github.com/acme/infra/issues/11"] and moved == ["acme/infra#11"]

            await select("todo", "acme/infra#11")
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("tab", "space", "enter")  # untick it
            await app.workers.wait_for_complete()
            assert len(started) == 2 and moved == ["acme/infra#11"]

            await select("todo", "acme/infra#9")  # assigned, on no project: no status to move
            await pilot.press("n")
            await pilot.pause()
            assert not app.screen.query("#to-progress")
            await pilot.press("escape")

            await select("blocked", "acme/infra#7")
            await pilot.press("n")
            await pilot.pause()
            assert not app.screen.query("#to-progress")

    asyncio.run(scenario())


BACKLOG = [item("acme/infra", 21, "Someday thing", "Backlog"), item("acme/infra", 11, "Personal todo", "Todo"), item("acme/infra", 22, "Another someday", "Backlog")]


def test_backlog_cards_sort_after_the_other_todo_cards(monkeypatch):
    monkeypatch.setitem(PROJECTS, "me", BACKLOG)
    board = core.build_board()
    todo = [c["key"] for c in core.column_cards(board["cards"], "todo", "status")]
    assert todo[-2:] == ["acme/infra#21", "acme/infra#22"] and len(todo) == 5
    # By session state nothing is reordered.
    assert core.column_cards(board["cards"], "backlog", "session") == [c for c in board["cards"] if c["sessionColumn"] == "backlog"]
    text = core.summary(board, "status")
    assert text.index("Personal todo") < text.index("Someday thing")


def test_start_progress_moves_a_backlog_card_too(monkeypatch):
    monkeypatch.setitem(PROJECTS, "me", BACKLOG)
    card = next(c for c in core.build_board()["cards"] if c["key"] == "acme/infra#21")
    assert core.start_progress(card) is True
    assert STATUS_SETS == [{"project": "PVT_me", "item": "PVTI_21", "field": "PVTSSF_status", "option": "opt-progress"}]


def test_tui_backlog_cards_sit_dimmed_under_a_divider_at_the_bottom_of_todo(monkeypatch):
    from tavla import tui

    monkeypatch.setitem(PROJECTS, "me", BACKLOG)

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            todo = app.query_one("#col-todo")
            kinds = [type(it).__name__ for it in todo.children]
            assert kinds == ["CardItem"] * 3 + ["Divider"] + ["CardItem"] * 2
            assert [it.card["key"] for it in todo.children[4:]] == ["acme/infra#21", "acme/infra#22"]
            assert [it.has_class("is-backlog") for it in todo.children if isinstance(it, tui.CardItem)] == [False] * 3 + [True] * 2
            divider = todo.children[3]
            assert [str(lb.render()) for lb in divider.query(tui.Label)] == ["Backlog", "2"]
            assert str(app.query_one("#count-todo").render()) == "5"  # the divider is not counted
            assert todo.index is None  # a fresh column starts with no cursor, not on the card under the divider
            todo.focus()
            todo.index = 2
            await pilot.press("down")
            await pilot.pause()
            assert todo.index == 4 and app.current["key"] == "acme/infra#21"  # the cursor steps over the divider
            await pilot.press("up")
            assert todo.index == 2

            await pilot.press("slash", *"someday", "enter")  # only Backlog cards left: the divider still heads them
            await pilot.pause()
            assert [type(it).__name__ for it in todo.children] == ["Divider", "CardItem", "CardItem"]
            assert app.current["key"] == "acme/infra#21"
            await pilot.press("slash", "escape")
            await pilot.pause()

            await pilot.press("v")
            await pilot.pause()
            lv = app.query_one("#col-list")
            names = [type(it).__name__ for it in lv.children]
            at = names.index("Divider")
            assert names.count("Divider") == 1 and names[at + 1 : at + 3] == ["CardRow", "CardRow"]
            assert [it.card["key"] for it in lv.children[at + 1 : at + 3]] == ["acme/infra#21", "acme/infra#22"]
            assert all(it.has_class("is-backlog") for it in lv.children[at + 1 : at + 3])
            assert next(r.group for r in lv.query(tui.GroupRow) if r.group["title"] == "Todo")["count"] == 5

            await pilot.press("g")  # by session state Backlog is an ordinary status again
            await pilot.pause()
            assert not app.query(tui.Divider) and not app.query(".is-backlog")

    asyncio.run(scenario())


def test_tui_new_session_on_a_backlog_card_offers_in_progress_unticked(monkeypatch):
    from tavla import tui

    monkeypatch.setitem(PROJECTS, "me", BACKLOG)
    started, moved = [], []
    monkeypatch.setattr(core, "new_session", lambda target, prompt="", title="": started.append(target) or "dddd4444")
    monkeypatch.setattr(core, "start_progress", lambda card: moved.append(card["key"]) or True)

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            async def select(key):
                col = app.query_one("#col-todo")
                col.focus()
                col.index = next(i for i, it in enumerate(col.children) if app.item_key(it) == f"card:{key}")
                await pilot.pause()

            await select("acme/infra#21")
            await pilot.press("n")
            await pilot.pause()
            box = app.screen.query_one("#to-progress", tui.Checkbox)
            assert box.value is False and str(box.label) == "Move the issue from Backlog to In Progress"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert len(started) == 1 and moved == []

            await select("acme/infra#21")
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("tab", "space", "enter")  # tick it
            await app.workers.wait_for_complete()
            assert len(started) == 2 and moved == ["acme/infra#21"]

    asyncio.run(scenario())


def test_tui_new_session_attaches_only_when_ticked(monkeypatch):
    from tavla import tui

    attached = []
    monkeypatch.setattr(core, "new_session", lambda target, prompt="", title="": "dddd4444")
    monkeypatch.setattr(core, "new_unlinked_session", lambda prompt, name="": "eeee5555")
    monkeypatch.setattr(core, "attach", lambda sid, title="", dry_run=False, cwd="": attached.append(sid) or "")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            col = app.query_one("#col-blocked")
            col.focus()
            col.index = next(i for i, it in enumerate(col.children) if it.card["key"] == "acme/infra#7")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert app.screen.query_one("#attach-now", tui.Checkbox).value is False
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert attached == []

            col.focus()
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("tab", "space", "enter")
            await app.workers.wait_for_complete()
            assert attached == ["dddd4444"]

            await pilot.press("N")
            await pilot.pause()
            await pilot.press(*"what is x", "tab", "space", "enter")
            await app.workers.wait_for_complete()
            assert attached == ["dddd4444", "eeee5555"]

    asyncio.run(scenario())


def test_filter_matches_every_word_across_title_ref_status_labels_and_sessions():
    board = core.build_board()
    cards = {c["key"]: c for c in board["cards"]}
    flaky = cards["acme/infra#7"]
    for query in ("FLAKY", "infra#7", "blocked", "p1", "deploy bug", "fix flaky deploy"):
        assert core.matches(flaky, query), query
    assert not core.matches(flaky, "deploy chart")  # every word must match
    assert [k for k, c in cards.items() if core.matches(c, "chart")] == ["acme/charts#3"]  # its session is "chart bump"
    side = next(s for s in board["unlinked"] if s["name"] == "random question")
    assert core.matches(side, "question") and core.matches(side, "cccc") and not core.matches(side, "flaky")


def test_tui_slash_filters_board_and_list_then_enter_selects_and_esc_clears():
    from tavla import tui

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            def keys(column):
                return [app.item_key(it) for it in app.query_one(f"#col-{column}").children]

            all_todo = keys("todo")
            await pilot.press("slash", "escape")  # nothing selected yet: focus still lands on a list
            await pilot.pause()
            assert isinstance(app.focused, tui.ListView)
            await pilot.press("slash")
            await pilot.pause()
            assert app.focused is app.query_one("#filter")
            await pilot.press(*"personal v")  # board keys typed in the box are text, not actions
            await pilot.pause()
            assert app.view == "board"
            await pilot.press("backspace", "backspace")
            await pilot.pause()
            assert keys("todo") == ["card:acme/infra#11"] and keys("blocked") == [] and keys("unlinked") == []
            assert str(app.query_one("#count-todo").content) == "1"
            assert "1 of 6 issues" in str(app.query_one("#status").content)  # the two recently closed ones count: the filter reaches them

            await pilot.press("enter")
            await pilot.pause()
            assert app.focused is app.query_one("#col-todo") and app.current["key"] == "acme/infra#11"

            await pilot.press("v")  # the filter carries over to the list view
            await pilot.pause()
            rows = app.query_one("#col-list").children
            assert [app.item_key(r) for r in rows if not r.disabled] == ["card:acme/infra#11"]

            await pilot.press("slash", "escape")
            await pilot.pause()
            assert app.filter == "" and not app.query_one("#filter").display
            assert app.focused is app.query_one("#col-list")
            await pilot.press("v")
            await pilot.pause()
            assert keys("todo") == all_todo

    asyncio.run(scenario())


def test_recently_closed_issues_are_search_only_cards_off_the_board():
    board = core.build_board()
    cards = {c["key"]: c for c in board["cards"]}
    # Assigned to me, or in a backlog project; one closed before the 30 days is left out.
    assert {k for k, c in cards.items() if c["searchOnly"]} == {"acme/infra#20", "me/todo#5"}
    rotated = cards["acme/infra#20"]
    assert (rotated["isOpen"], rotated["status"], rotated["statusColumn"], rotated["sessions"]) == (False, "Closed", "done", [])
    assert core.matches(rotated, "rotated key") and core.matches(cards["me/todo#5"], "todo#5 closed")
    text = core.summary(board, "status")
    assert "Rotated the key" not in text and "Shipped report" not in text and "## Done (0)" in text


def test_finished_issue_with_only_an_old_session_is_search_only(tmp_path):
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    (tmp_path / "jobs" / "ffff6666").mkdir()
    (tmp_path / "jobs" / "ffff6666" / "state.json").write_text(json.dumps({"name": "tidy logs", "state": "done", "intent": "acme/infra#31 tidy", "updatedAt": old}))
    board = core.build_board()
    card = next(c for c in board["cards"] if c["key"] == "acme/infra#31")
    assert card["searchOnly"] and [s["id"] for s in card["sessions"]] == ["ffff6666"]
    assert all(s["id"] != "ffff6666" for s in board["unlinked"])


def test_tui_search_finds_recently_closed_issues_and_starts_a_session_on_one(monkeypatch):
    from tavla import tui

    started = []
    monkeypatch.setattr(core, "new_session", lambda target, prompt="", title="": started.append(target) or "dddd4444")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            def keys(column):
                return [app.item_key(it) for it in app.query_one(f"#col-{column}").children]

            assert keys("done") == [] and str(app.query_one("#count-done").content) == "0"
            await pilot.press("slash", *"rotated")
            await pilot.pause()
            assert keys("done") == ["card:acme/infra#20"] and keys("todo") == []
            await pilot.press("enter")
            await pilot.pause()
            assert app.current["key"] == "acme/infra#20"

            await pilot.press("n")
            await pilot.pause()
            assert not app.screen.query("#to-progress")  # a closed issue has no status to move
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert started == ["https://github.com/acme/infra/issues/20"]

            await pilot.press("slash", "escape")
            await pilot.pause()
            assert keys("done") == []

    asyncio.run(scenario())


def test_toggle_hidden_hides_then_shows_an_issue_and_survives_a_reload():
    assert core.load_hidden() == set()
    assert core.toggle_hidden("acme/infra#11") == {"acme/infra#11"}
    assert core.toggle_hidden("acme/infra#7") == {"acme/infra#11", "acme/infra#7"}
    assert core.toggle_hidden("acme/infra#11") == {"acme/infra#7"}
    assert core.load_hidden() == {"acme/infra#7"}


def test_tui_h_hides_a_card_from_board_and_list_and_shift_h_shows_hidden_ones():
    from tavla import tui

    async def scenario(hidden_at_start):
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            def keys(column):
                return [app.item_key(it) for it in app.query_one(f"#col-{column}").children]

            if hidden_at_start:  # a fresh start reads hidden.json and leaves hidden cards out
                assert "card:acme/infra#11" not in keys("todo") and "1 hidden" in str(app.query_one("#status").content)
                return
            todo = app.query_one("#col-todo")
            todo.focus()
            todo.index = keys("todo").index("card:acme/infra#11")
            await pilot.pause()
            assert app.current["key"] == "acme/infra#11"
            await pilot.press("h")
            await pilot.pause()
            assert "card:acme/infra#11" not in keys("todo") and core.load_hidden() == {"acme/infra#11"}
            assert app.current is None or app.current.get("key") != "acme/infra#11"  # the detail follows the highlight
            assert "1 hidden" in str(app.query_one("#status").content)

            await pilot.press("H")
            await pilot.pause()
            item = next(it for it in todo.children if app.item_key(it) == "card:acme/infra#11")
            assert item.has_class("is-hidden")
            await pilot.press("v")
            await pilot.pause()
            row = next(r for r in app.query_one("#col-list").children if app.item_key(r) == "card:acme/infra#11")
            assert row.has_class("is-hidden")
            await pilot.press("H")
            await pilot.pause()
            assert "card:acme/infra#11" not in [app.item_key(r) for r in app.query_one("#col-list").children]
            await pilot.press("v", "H")
            await pilot.pause()

            todo = app.query_one("#col-todo")
            todo.focus()
            todo.index = keys("todo").index("card:acme/infra#11")
            await pilot.pause()
            await pilot.press("h")  # unhide
            await pilot.pause()
            assert core.load_hidden() == set()
            assert not next(it for it in todo.children if app.item_key(it) == "card:acme/infra#11").has_class("is-hidden")
            await pilot.press("h", "H")  # hide again, and hidden cards go back out of view
            await pilot.pause()
            assert "card:acme/infra#11" not in keys("todo")

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_new_unlinked_session_needs_a_prompt_and_links_nothing(monkeypatch):
    calls, cwds = [], []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or cwds.append(cwd) or fake_run(argv))
    assert core.new_unlinked_session("  what is x  ") == "dddd4444"
    assert calls[-1] == ["claude", "--bg", "what is x"] and cwds[-1] == core.SESSION_CWD  # Claude Code names it
    assert core.main(["start", "what", "is", "y", "--name", "side question"]) == 0
    assert calls[-1] == ["claude", "--bg", "--name", "side question", "what is y"]
    assert core.load_links() == {}
    with pytest.raises(ValueError):
        core.new_unlinked_session("  ")


def test_tui_shift_n_and_n_off_a_card_start_an_unlinked_session(monkeypatch):
    from tavla import tui

    started, on_issue = [], []
    monkeypatch.setattr(core, "new_unlinked_session", lambda prompt, name="": started.append(prompt) or "dddd4444")
    monkeypatch.setattr(core, "new_session", lambda target, prompt="", title="": on_issue.append(target) or "dddd4444")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("N")
            await pilot.pause()
            assert isinstance(app.screen, tui.AskPrompt) and not app.screen.query("#to-progress")
            await pilot.press("enter")  # no prompt yet: stays open
            await pilot.pause()
            assert isinstance(app.screen, tui.AskPrompt) and started == []
            await pilot.press(*"what is x", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert started == ["what is x"] and not isinstance(app.screen, tui.AskPrompt)

            unlinked = app.query_one("#col-unlinked")
            unlinked.focus()
            unlinked.index = 0
            await pilot.pause()
            assert "sessions" not in app.current
            await pilot.press("n", *"another", "enter")
            await app.workers.wait_for_complete()
            assert started == ["what is x", "another"] and on_issue == []

    asyncio.run(scenario())


def test_rm_deletes_a_background_session_and_drops_its_links_and_refuses_interactive_ones(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or (f"removed {argv[2]}\n" if argv[:2] in (["claude", "rm"], ["claude", "stop"]) else fake_run(argv)))
    core.link("cccc3333", "acme/infra#9")
    assert core.main(["stop", "cccc3333"]) == 0
    assert calls[-1] == ["claude", "stop", "cccc3333"] and core.load_links() == {"cccc3333": ["acme/infra#9"]}
    assert core.main(["rm", "cccc3333"]) == 0
    assert calls[-1] == ["claude", "rm", "cccc3333"] and core.load_links() == {}
    assert core.main(["rm", "eeee5555"]) == 1
    assert "interactive session" in capsys.readouterr().err and calls[-1][:2] == ["claude", "agents"]


def test_self_flag_links_and_deletes_the_session_the_command_runs_in(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: calls.append(argv) or f"removed {argv[2]}\n")
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert core.main(["rm", "--self"]) == 1
    assert "only inside a Claude Code session" in capsys.readouterr().err
    # A background session is its job directory; no `claude agents` call is needed to find it.
    monkeypatch.setenv("CLAUDE_JOB_DIR", str(core.JOBS / "cccc3333"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ffff6666-0000-0000")
    assert core.main(["link", "--self", "acme/infra#9"]) == 0
    assert core.load_links() == {"cccc3333": ["acme/infra#9"]} and calls == []
    assert core.main(["link", "--self"]) == 1 and core.main(["rm", "cccc3333", "--self"]) == 1 and core.main(["rm"]) == 1
    assert core.main(["rm", "--self"]) == 0
    assert calls == [["claude", "rm", "cccc3333"]] and core.load_links() == {}
    # An interactive session has no job directory: it links by its short session id and is not deleted.
    monkeypatch.delenv("CLAUDE_JOB_DIR")
    assert core.main(["link", "--self", "acme/infra#9"]) == 0
    assert core.load_links() == {"ffff6666": ["acme/infra#9"]}
    assert core.main(["rm", "--self"]) == 1
    assert "interactive session" in capsys.readouterr().err and len(calls) == 1


def test_rm_keeps_the_links_when_claude_refuses_to_delete(monkeypatch, capsys):
    def refuse(argv, timeout=60, cwd=None):
        if argv[:2] == ["claude", "rm"]:
            raise RuntimeError("worktree has unpushed commits")
        return fake_run(argv)

    monkeypatch.setattr(core, "run", refuse)
    core.link("cccc3333", "acme/infra#9")
    assert core.main(["rm", "cccc3333"]) == 1
    assert "unpushed commits" in capsys.readouterr().err and core.load_links() == {"cccc3333": ["acme/infra#9"]}


def test_tui_ctrl_x_stops_or_deletes_the_selected_session(monkeypatch):
    from tavla import tui

    done = []
    monkeypatch.setattr(core, "stop_session", lambda sid: done.append(("stop", sid)) or "")
    monkeypatch.setattr(core, "delete_session", lambda sid: done.append(("delete", sid)) or "")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            unlinked = app.query_one("#col-unlinked")
            unlinked.focus()
            unlinked.index = 0
            await pilot.pause()
            await pilot.press("ctrl+x")
            await pilot.pause()
            assert isinstance(app.screen, tui.StopOrDelete)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, tui.StopOrDelete) and done == []
            await pilot.press("ctrl+x", "s")
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+x", "d")
            await app.workers.wait_for_complete()
            assert done == [("stop", "cccc3333"), ("delete", "cccc3333")]

            # A card with one session acts on it; the dialog's own keys don't reach the board.
            blocked = app.query_one("#col-blocked")
            blocked.focus()
            blocked.index = 0
            await pilot.pause()
            await pilot.press("ctrl+x")
            await pilot.pause()
            assert app.screen.sess["id"] == "aaaa1111"
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, tui.StopOrDelete)
            await pilot.press("escape")

    asyncio.run(scenario())


def test_tui_names_the_terminal_tab_while_running_and_restores_it(monkeypatch):
    import io

    from tavla import tui

    out = io.StringIO()
    seen = []
    monkeypatch.setattr(tui.BoardApp, "run", lambda self: seen.append(out.getvalue()))
    tui.run(out)
    assert seen == ["\x1b[22;1t\x1b]1;Tavla\x07"]
    assert out.getvalue().endswith("\x1b[23;1t")
    # Ghostty names a tab after the window title.
    monkeypatch.setattr(core, "TERMINAL", "ghostty")
    out = io.StringIO()
    tui.run(out)
    assert seen[-1] == "\x1b[22;2t\x1b]2;Tavla\x07" and out.getvalue().endswith("\x1b[23;2t")


def test_tui_deleting_the_selected_session_drops_it_from_the_detail_panel(monkeypatch):
    import shutil

    from tavla import tui

    def delete(sid):
        shutil.rmtree(core.JOBS / sid)
        return f"removed {sid}"

    monkeypatch.setattr(core, "delete_session", delete)

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            def panel():
                return [it.sess["id"] for it in app.query_one("#sessions").children]

            async def select(key):
                lv, i = next((lv, i) for lv in app.query(".col ListView").results(tui.CardList) for i, it in enumerate(lv.children) if app.item_key(it) == key)
                lv.focus()
                lv.index = i
                await pilot.pause()

            # An unlinked session, and a card on the board only through its session's PR.
            for key, sid in [("sess:cccc3333", "cccc3333"), ("card:acme/charts#3", "bbbb2222")]:
                await select(key)
                assert panel() == [sid]
                app.stop_or_delete(app.focused_session(), "delete")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert sid not in panel() and app.current_key() != key
                assert app.current_key() == app.item_key(app.focused.highlighted_child)  # the detail follows the cursor

    asyncio.run(scenario())


def test_tui_refresh_asked_for_mid_load_runs_after_it(monkeypatch):
    from tavla import tui

    builds = []
    real = core.build_board
    monkeypatch.setattr(core, "build_board", lambda force=False: builds.append(force) or real(force))

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            builds.clear()
            app.loading = True  # a load is under way
            app.action_refresh()
            app.action_refresh(True)
            assert builds == []
            await app.apply(app.data)  # that load lands
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert builds == [True] and not app.loading and app.refresh_queued is None

    asyncio.run(scenario())


def test_tui_new_session_refreshes_before_moving_the_issue_to_in_progress(monkeypatch):
    from tavla import tui

    order = []
    monkeypatch.setattr(core, "new_session", lambda target, prompt="", title="": "dddd4444")
    monkeypatch.setattr(core, "start_progress", lambda card: order.append("progress") or True)

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            monkeypatch.setattr(app, "action_refresh", lambda force=False: order.append("refresh"))
            card = next(c for c in app.data["cards"] if c["key"] == "acme/infra#11")
            app.start_session(card, "go", progress=True)
            await app.workers.wait_for_complete()
            assert order == ["refresh", "progress", "refresh"]

    asyncio.run(scenario())


def test_tui_prompt_box_moves_by_word_and_edits_in_external_editor(tmp_path, monkeypatch):
    import contextlib

    from tavla import tui

    editor = tmp_path / "editor.sh"
    editor.write_text('#!/bin/sh\nprintf "first line\\nsecond line\\n" > "$1"\n')
    editor.chmod(0o755)
    monkeypatch.setenv("VISUAL", str(editor))
    answers = []

    class Host(tui.App):
        def on_mount(self) -> None:
            self.push_screen(tui.AskPrompt("New background session"), answers.append)

    async def scenario():
        app = Host()
        async with app.run_test() as pilot:
            app.suspend = contextlib.nullcontext  # the headless driver cannot hand the terminal over
            box = app.screen.query_one("#prompt", tui.PromptArea)
            box.focus()
            await pilot.press(*"fix the deploy")
            await pilot.press("alt+left")
            assert box.cursor_location == (0, 8)
            await pilot.press("alt+left", "alt+shift+right")
            assert box.selected_text == "the"
            await pilot.press("alt+up")
            assert box.cursor_location == (0, 0)
            await pilot.press("ctrl+g")
            await pilot.pause()
            assert box.text == "first line\nsecond line"  # the editor's trailing newline is dropped
            assert box.cursor_location == (1, 11)
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(scenario())
    assert answers == [("first line\nsecond line", False, False)]


def test_tui_one_line_fields_move_by_word():
    from tavla import tui

    class Host(tui.App):
        def compose(self):
            yield tui.WordInput(value="owner/repo issue title")

    async def scenario():
        app = Host()
        async with app.run_test() as pilot:
            field = app.query_one(tui.WordInput)
            field.focus()
            field.cursor_position = len(field.value)
            await pilot.press("alt+left")
            assert field.cursor_position == len("owner/repo issue ")
            await pilot.press("alt+shift+left")
            assert field.selected_text == "issue "

    asyncio.run(scenario())


def test_config_file_sets_projects_issue_repos_session_dir_and_theme(monkeypatch):
    core.CONFIG.mkdir(parents=True)
    (core.CONFIG / "config.toml").write_text(
        'session_dir = "~/somewhere"\ntheme = "nord"\n\n'
        '[[projects]]\nowner = "octo"\nnumber = 4\nbacklog = true\n\n[[projects]]\nowner = "octo-org"\nnumber = 9\n\n'
        '[[issue_repos]]\nrepo = "octo/todo"\nlabel = "Mine"\n\n[[issue_repos]]\nrepo = "octo-org/tracker"\n'
    )
    core.load_config()
    assert core.PROJECTS == [{"owner": "octo", "number": "4", "is_backlog": True}, {"owner": "octo-org", "number": "9", "is_backlog": False}]
    assert core.ISSUE_REPOS == [("octo/todo", "Mine"), ("octo-org/tracker", "octo-org/tracker")]
    assert core.SESSION_CWD == core.Path.home() / "somewhere" and core.THEME == "nord"


def test_config_file_with_a_broken_entry_is_reported_with_its_path(capsys):
    core.CONFIG.mkdir(parents=True)
    (core.CONFIG / "config.toml").write_text('[[issue_repos]]\nrepo = "not-a-repo"\n')
    assert core.main(["dump"]) == 1
    err = capsys.readouterr().err
    assert "config.toml" in err and "owner/repo" in err


def test_without_a_config_the_board_shows_assigned_and_linked_issues_and_reads_no_project(monkeypatch):
    monkeypatch.setattr(core, "PROJECTS", [])
    monkeypatch.setattr(core, "ISSUE_REPOS", [])
    core.load_config()  # no config.toml: the defaults stay
    board = core.build_board(force=True)
    assert {"acme/infra#7", "acme/infra#9"} <= {c["key"] for c in board["cards"]}
    assert not [c for c in GRAPHQL_CALLS if c.startswith("project")] and not board["errors"]
    assert core.main(["create", "A title"]) == 1


def test_tui_new_issue_without_a_configured_repo_says_where_to_add_one(monkeypatch):
    from tavla import tui

    monkeypatch.setattr(core, "ISSUE_REPOS", [])

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 50)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("i")
            await pilot.pause()
            assert not isinstance(app.screen, tui.NewIssue)
            assert any("issue_repos" in str(n.message) for n in app._notifications)

    asyncio.run(scenario())


def delete_job(monkeypatch, jid, session_id):
    """Give the job a session id, then make `claude rm` remove its directory as the real one does."""
    import shutil

    state = core.JOBS / jid / "state.json"
    state.write_text(json.dumps({**json.loads(state.read_text()), "sessionId": session_id}))

    def run(argv, timeout=60, cwd=None):
        if argv[:2] == ["claude", "rm"]:
            shutil.rmtree(core.JOBS / argv[2])
            return f"removed {argv[2]}\n"
        return fake_run(argv, timeout, cwd)

    monkeypatch.setattr(core, "run", run)


def test_deleted_session_stays_on_its_card_and_resumes_under_the_same_id(monkeypatch, capsys):
    delete_job(monkeypatch, "cccc3333", "cccc3333-aaaa-bbbb")
    core.link("cccc3333", "acme/infra#9")
    card = next(c for c in core.build_board()["cards"] if c["key"] == "acme/infra#9")
    assert [s["id"] for s in card["sessions"]] == ["cccc3333"] and card["pastSessions"] == []
    core.delete_session("cccc3333")
    assert core.load_links() == {}
    board = core.build_board()
    card = next(c for c in board["cards"] if c["key"] == "acme/infra#9")
    assert card["sessions"] == [] and card["sessionColumn"] == "backlog"  # a deleted session places no card
    [past] = card["pastSessions"]
    assert (past["id"], past["name"], past["state"], past["isPast"], past["result"]) == ("cccc3333", "random question", "deleted", True, "x is y")
    assert core.matches(card, "random question")
    assert all(s["id"] != "cccc3333" for s in board["unlinked"])

    assert core.main(["resume", "cccc"]) == 0
    assert RESUMED == [{"argv": ["claude", "--bg", "--name", "random question", "--resume", "cccc3333-aaaa-bbbb"], "cwd": core.SESSION_CWD}]
    assert core.load_links() == {"cccc3333": ["acme/infra#9"]}  # back on its card once it is listed again
    assert "Resumed cccc3333" in capsys.readouterr().out
    assert core.main(["resume", "nope"]) == 1 and "no recorded session" in capsys.readouterr().err


def test_resume_leaves_a_session_that_is_still_listed_alone():
    state = core.JOBS / "aaaa1111" / "state.json"
    state.write_text(json.dumps({**JOBS["aaaa1111"], "sessionId": "aaaa1111-x"}))
    core.build_board()
    assert core.resume_session("aaaa1111")["id"] == "aaaa1111" and RESUMED == []


def test_session_deleting_itself_before_any_board_saw_it_is_still_recorded(monkeypatch):
    delete_job(monkeypatch, "cccc3333", "cccc3333-aaaa-bbbb")
    core.link("cccc3333", "acme/infra#9")
    core.delete_session("cccc3333")
    card = next(c for c in core.build_board()["cards"] if c["key"] == "acme/infra#9")
    assert [(s["id"], s["sessionId"], s["name"]) for s in card["pastSessions"]] == [("cccc3333", "cccc3333-aaaa-bbbb", "random question")]


def test_sessions_deleted_before_history_existed_are_found_through_their_links_and_transcripts(tmp_path):
    project = core.TRANSCRIPTS / "-w-notes"
    project.mkdir(parents=True)
    rows = [{"type": "agent-name", "agentName": "old digging"}, "not an object", {"type": "user", "cwd": str(tmp_path)}]
    (project / "ffff6666-1111-2222.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n{broken\n")
    core.save_links({"ffff6666": ["acme/infra#9"], "99990000": ["acme/infra#9"]})  # the second has no conversation left
    card = next(c for c in core.build_board()["cards"] if c["key"] == "acme/infra#9")
    assert [(s["id"], s["sessionId"], s["name"], s["cwd"]) for s in card["pastSessions"]] == [("ffff6666", "ffff6666-1111-2222", "old digging", str(tmp_path))]
    assert core.resume_session("ffff6666")["cwd"] == str(tmp_path) and RESUMED[0]["cwd"] == tmp_path
    (project / "ffff6666-1111-2222.jsonl").unlink()
    core.build_board()  # read once: the record, not the file, carries it from here on
    assert set(core.load_history()) >= {"ffff6666", "99990000"}


def test_ended_interactive_session_is_not_past_while_claude_agents_fails(monkeypatch):
    core.link("eeee5555", "acme/infra#9")
    core.build_board()

    def run(argv, timeout=60, cwd=None):
        if argv[:2] == ["claude", "agents"]:
            raise RuntimeError("claude: not found")
        return fake_run(argv, timeout, cwd)

    monkeypatch.setattr(core, "run", run)
    card = next(c for c in core.build_board()["cards"] if c["key"] == "acme/infra#9")
    assert card["pastSessions"] == []
    monkeypatch.setattr(core, "run", lambda argv, timeout=60, cwd=None: "[]" if argv[:2] == ["claude", "agents"] else fake_run(argv, timeout, cwd))
    card = next(c for c in core.build_board()["cards"] if c["key"] == "acme/infra#9")
    assert [(s["id"], s["state"]) for s in card["pastSessions"]] == [("eeee5555", "ended")]


def test_tui_deleted_sessions_come_last_under_a_divider_and_enter_resumes_and_attaches(monkeypatch):
    from tavla import tui

    delete_job(monkeypatch, "cccc3333", "cccc3333-aaaa-bbbb")
    core.link("cccc3333", "acme/infra#7")
    core.build_board()
    core.delete_session("cccc3333")
    attached, stopped = [], []
    monkeypatch.setattr(core, "attach", lambda sid, title="", dry_run=False, cwd="": attached.append((sid, title, cwd)) or "")
    monkeypatch.setattr(core, "stop_session", lambda sid: stopped.append(sid) or "")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            blocked = app.query_one("#col-blocked")
            blocked.focus()
            blocked.index = 0
            await pilot.pause()
            rows = list(app.query_one("#sessions").children)
            assert [type(r).__name__ for r in rows] == ["SessDetailRow", "Divider", "SessDetailRow"]
            assert rows[0].sess["id"] == "aaaa1111" and rows[2].sess["id"] == "cccc3333" and rows[2].has_class("is-past")
            assert str(app.query_one("#count-sessions").render()) == "1"  # live sessions only
            assert rows[1].region.y - rows[0].region.y == 2  # a blank line sets the divider off from the live sessions
            await pilot.press("enter", "down")  # into the sessions, over the divider
            await pilot.pause()
            assert app.query_one("#sessions").highlighted_child is rows[2]
            # Only enter acts on a deleted session.
            await pilot.press("ctrl+x")
            await pilot.pause()
            assert not isinstance(app.screen, tui.StopOrDelete) and any("enter resumes it" in str(n.message) for n in app._notifications)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert [r["argv"][-1] for r in RESUMED] == ["cccc3333-aaaa-bbbb"] and stopped == []
            assert attached == [("cccc3333", "random question", str(core.SESSION_CWD))]
            assert core.load_links()["cccc3333"] == ["acme/infra#7"]

    asyncio.run(scenario())


def test_tui_enter_on_a_card_with_only_deleted_sessions_lands_on_the_first_of_them(monkeypatch):
    from tavla import tui

    delete_job(monkeypatch, "cccc3333", "cccc3333-aaaa-bbbb")
    core.link("cccc3333", "acme/infra#9")
    core.build_board()
    core.delete_session("cccc3333")

    async def scenario():
        app = tui.BoardApp()
        async with app.run_test(size=(190, 45)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            todo = app.query_one("#col-todo")
            todo.focus()
            todo.index = next(i for i, it in enumerate(todo.children) if getattr(it, "card", {}).get("key") == "acme/infra#9")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            sessions = app.query_one("#sessions")
            assert app.focused is sessions and sessions.highlighted_child.sess["id"] == "cccc3333"
            assert sessions.children[0].region.y == sessions.region.y  # no blank line above a divider that comes first

    asyncio.run(scenario())
