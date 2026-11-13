# Tavla

Issues in. Code out.

A terminal app to manage agent sessions: GitHub issues on a kanban board, each card listing the Claude Code sessions working on it. *Tavla* is Swedish for board.

## Requirements

- macOS with [iTerm2](https://iterm2.com): attaching to a session opens a new iTerm2 tab.
- [Claude Code](https://claude.com/claude-code) as `claude` on `PATH`, a version with background sessions (`claude --bg`, `claude agents`).
- [GitHub CLI](https://cli.github.com) as `gh`, signed in with the `project` scope so it can read and set project status: `gh auth refresh -s project`.
- [uv](https://docs.astral.sh/uv/) to install it.

## Install

```sh
uv tool install git+https://github.com/lindeskar/tavla
```

`uv tool upgrade tavla` updates it and `uv tool uninstall tavla` removes it. To run from a clone instead, so that a `git pull` takes effect without reinstalling:

```sh
git clone https://github.com/lindeskar/tavla
uv tool install --editable ./tavla
```

uv puts the command in `~/.local/bin`; if that is not on `PATH`, `uv tool update-shell` adds it.

## Configure

Tavla works without configuration: the board then shows the open issues assigned to you and any issue a session links to, all under Todo. `~/.config/tavla/config.toml` adds the rest:

```toml
# Where new sessions start. Default: the directory tavla was started in.
session_dir = "~/Code/my-notes"

# "tavla" (the default) or any built-in Textual theme, e.g. "nord" or "textual-light".
theme = "tavla"

# GitHub Projects whose Status field places a card in a column, in precedence order:
# the first project that holds the issue wins. The non-Done items of a project marked
# `backlog = true` become cards even when they are not assigned to you.
[[projects]]
owner = "octocat"       # user or organization
number = 1
backlog = true

[[projects]]
owner = "octo-org"
number = 12

# Repositories the new-issue dialog (`i`) and `tavla create` file issues in; the first is the default.
[[issue_repos]]
repo = "octocat/todo"
label = "Personal"

[[issue_repos]]
repo = "octo-org/tracker"
label = "Team"
```

A project's Status options are matched by name, ignoring case: Backlog, Todo or Ready, In Progress or In Review, Blocked, and Done. Any other status counts as Todo, and a closed issue as Done.

Set `TAVLA_HOME` to keep the config file and the local state in another directory.

## Terminal app

```sh
tavla
```

While it runs, the terminal tab is named "Tavla"; the previous name comes back on exit.

| Key | Does |
|---|---|
| arrows / tab | Move between cards and columns (in the list view, tab jumps between groups) |
| `home` / `end` | Jump to the first / last card in a column, row in the list view, or session in the sessions panel |
| `/` | Search: filters the board and the list as you type. Every word must appear in an issue's title, `repo#N`, status, labels or session names (an unlinked session's name, id or refs), ignoring case. `enter` moves to the first match and keeps the filter on; `esc` in the box clears it. The status line shows how many issues match |
| `enter` | On a card: focus its sessions. On a session: attach in a new iTerm2 tab |
| `n` | New background session on the selected issue (asks for an optional prompt). On a Todo or Backlog issue the box also offers to move it to In Progress on the project its status comes from: ticked by default for Todo, unticked for Backlog. A checkbox at the bottom, unticked by default, attaches to the new session in a new iTerm2 tab. With no issue card selected (an unlinked session, or nothing) it does what `N` does |
| `N` | New background session on no issue: asks for a prompt (required), lets Claude Code name the session, and it shows under Unlinked. The same attach checkbox as `n` |
| `i` | New issue: pick one of the `issue_repos` from the config file (the first is the default), a title and an optional body; `enter` in the title or `ctrl+s` creates it. Then asks for an optional prompt to start a session on it and attach (`esc` skips) |
| `l` / `u` | Link the selected session to an issue / remove its manual links |
| `ctrl+x` | Stop or delete the selected session: `s` stops it (`claude stop`; the conversation is kept and attaching resumes it), `d` deletes it and its worktree (`claude rm`, which refuses while the worktree has unpushed commits) and drops its manual links, `esc` cancels. Acts on the highlighted row in the sessions panel, an unlinked session, or a card's only session; interactive sessions are stopped in their own terminal |
| `h` | Hide the selected issue from the board and the list, or unhide it. Kept locally in `~/.config/tavla/hidden.json` |
| `H` | Show or leave out hidden issues (left out at every start). Shown, a hidden card is dimmed and tagged `⊘ hidden`; the status line counts the hidden issues |
| `o` | Open the issue in the browser |
| `g` | Group by issue status or by session state |
| `v` | Switch between the board (columns) and a grouped list |
| `r` | Refresh now (also bypasses the project status cache) |
| `esc` | Back to the columns |
| `q` | Quit |

The footer shows the common keys and is cut off at the right edge when the terminal is narrower than they need; `ctrl+p` → Keys lists every key.

Each card shows the issue title, then `repo #number` (click it to open the issue); clicking elsewhere on a card selects it and keeps the focus on the board, then indicators: session state (`● needs you`, `◐ working`, `○ N sessions`), PRs (`⎇ N PRs`, green while any is open, purple once merged), sub-issue progress (`☑ done/total`), and the issue's labels in their GitHub colors.

Below the board, the left panel shows the highlighted issue's body as Markdown (scroll it with the mouse; links open in the browser), or an unlinked session's first prompt; the right panel lists its sessions one per line: state, name, last update, id, PRs and the start of the result.

The list view puts each issue on one line under its group's heading (same groups as the columns, empty ones left out), with Repo, Status, Sessions, PRs, Sub and Labels cells sized for a terminal about 190 columns wide; the title cell absorbs the difference. Status is left blank where the group heading already says it.

In the text boxes (prompt, issue title and body, search), `option+left` / `option+right` move by word and add `shift` to select; in the prompt and issue body, `option+up` / `option+down` jump to the start / end of the text, and `ctrl+g` opens the text in `$VISUAL` (else `$EDITOR`, else `vi`) and takes it back when the editor exits, which is also how to write a prompt with several lines, since `enter` starts the session. A GUI editor needs its wait flag, e.g. `EDITOR="code --wait"`.

A new issue is assigned to you unless you untick it in the dialog, and goes straight into the assigned-issue cache, so its card shows on the next refresh even though the search index lags. An unassigned one shows only once a session links to it.

New sessions start in `session_dir` from the config file, or in the directory `tavla` was started in.

Attaching opens a new tab in the current iTerm2 window, moves to the session's own directory (or `session_dir` when it has none recorded) and runs `claude attach <id>` there, so waking a finished session doesn't start it in your home directory. The tab is named after the session with an OSC 1 escape from its own shell (iTerm2 does not show a name set through AppleScript), and `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` stops Claude Code from overwriting that name with its own title. Detaching leaves the tab's shell open; deleting the session (`ctrl+x` `d`, `tavla rm`, `claude rm`) closes the tab, provided the iTerm2 profile closes a session when its shell exits. The first attach asks macOS for permission to control iTerm2.

## Core CLI

The app is built on `src/tavla/board.py`; `tavla` with a command runs it directly:

```sh
tavla dump [--group session] [--force]
tavla json
tavla attach <session>
tavla new <issue> [prompt...]
tavla start <prompt...> [--name name]
tavla create <title> [--repo owner/repo] [--body text]
tavla stop <session>
tavla rm <session> | --self
tavla link <session> <issue> | --self <issue>
tavla unlink <session> [issue]
```

## Claude Code plugin

The repository is also a Claude Code plugin, for use inside the sessions the board shows:

```
/plugin install tavla --marketplace lindeskar/tavla
```

| Skill | Does |
|---|---|
| `/tavla:link <issue>` | Links the session it runs in to an issue (`tavla link --self`), so the session shows on that card. Claude may also run it itself when a session starts on an issue its first prompt did not name |
| `/tavla:done` | Deletes the background session it runs in and its worktree (`tavla rm --self`), which also closes a tab Tavla attached to it. Only you can run it, and it stops when `claude rm` refuses, as it does for unpushed commits |

Both need the `tavla` command on `PATH`.

## Where the data comes from

- **Cards:** open issues assigned to `@me` (`gh search issues`, cached for 5 minutes in `~/.config/tavla/assigned-cache.json`), non-Done items of the `backlog` projects in the config file, and any issue a session links to.
- **Issue status:** the Status field of the `projects` in the config file; the first match wins. Status is read from the project side, because issue-side `projectItems` is empty for cross-org projects. It is cached for 5 minutes in `~/.config/tavla/projects-cache.json`.
- **GitHub budget:** everything except the session files spends the GraphQL rate limit (5,000 points an hour), `gh search` included. Project lists use a minimal query (1 point per 100 items, open issues only since a closed issue is Done whatever its status, the projects fetched in parallel), and issue/PR metadata is cached for 10 minutes in `meta-cache.json`, so a routine refresh only looks up what is new and otherwise calls nothing but `claude agents`; `r` bypasses all three caches. When GitHub answers with a rate limit, all GitHub calls pause until the budget resets (read from GraphQL's own `rateLimit`; the REST `/rate_limit` graphql bucket can disagree), or 90 seconds for a burst limit, and the board is built from the session files and cached GitHub data alone (any age), with the pause time in its status line: sessions whose issue isn't cached show as Unlinked.
- **Sessions:** `~/.claude/jobs/*/state.json` for background sessions (`name`, `state`, `intent` = first prompt, `children` = linked PRs, `output.result`), plus `claude agents --json` for interactive ones. The job files are Claude Code internals and may change between versions.
- **Card metadata:** labels, sub-issue progress, the issue body and closing PRs come from the same batched GraphQL calls; a card's PRs are its closing PRs plus the PRs of the sessions on it.
- **Session to issue:** manual links (`~/.config/tavla/links.json`), issue/PR URLs or `owner/repo#N` in the first prompt, and the issues each linked PR closes (one batched GraphQL call).
- **Columns by issue status:** Todo (No status, Backlog and Todo together; Backlog cards come last, under a `Backlog N` divider line and with a dimmed title, in the board and the list), In Progress, Blocked, Done. A closed issue counts as Done, and Done cards show only while a session on them is live or was touched in the last 3 days. Each card carries a session badge (`!` needs you, `~` working, `Ns/M live`), and cards needing you sort first.
- **Columns by session state:** Active (sessions that need you or are working; needs-you cards first, and the header is highlighted while any card needs you), Idle, No session.

Agent view's own filter works alongside: pasting an issue URL in `claude agents` lists the sessions whose first prompt contains it, which `new` guarantees.

## Develop

```sh
uv run pytest -q
uv run ruff format . && uv run ruff check .
```

`uv run tavla` runs the code in the clone. Issues and pull requests are welcome; a behaviour change comes with a test and an update to this file.

## License

[MIT](LICENSE)
