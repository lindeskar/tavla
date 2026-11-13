# CLAUDE.md

`tavla`: a Textual terminal app showing GitHub issues on a kanban board, each card listing the Claude Code sessions working on it. `README.md` is the user manual (config file, keys, data sources, caches); read it before changing behaviour.

## Layout

- `src/tavla/board.py`: the core, with no UI. GitHub and `claude` calls, caches, the session-to-issue linking, `build_board()`, the `config.toml` loader (`load_config`), and the `tavla <command>` CLI (`main`).
- `src/tavla/tui.py`: the Textual app (`BoardApp`), its widgets, modals and key bindings. GitHub and `claude` calls go through `board`; the TUI only runs `$EDITOR` and `open` itself.
- `src/tavla/__main__.py`: entry point. No args opens the TUI, any args go to `board.main`.
- `tests/test_core.py`: core and TUI tests in one file.

## Commands

- `uv run pytest -q`: the whole suite, about 30 s. Run it before every commit.
- `uv run pytest -q -k <words>`: a subset while iterating.
- `uv run ruff format . && uv run ruff check .`: both must be clean before a commit. The config is `[tool.ruff]` in `pyproject.toml` (200 columns); a block the formatter mangles gets `# fmt: skip`, as the fake issue in the tests does.
- `uv run tavla dump` / `uv run tavla json`: build the real board once and print it, read-only. Other CLI commands (`new`, `start`, `create`, `stop`, `rm`, `link`, `unlink`) change real GitHub issues, sessions or local link state: don't run them to check a change.

- Under the Bash sandbox `~/.config/tavla` is not writable, so a build that refreshes a cache fails with `PermissionError` on `meta-cache.json`. Copy the directory to a scratch dir and point `TAVLA_HOME` at it: that also keeps timing and before/after comparisons off the real caches.

## Invariants

- Every subprocess goes through `board.run` and every GraphQL call through `board.graphql`. Both raise `RateLimited`, which drives the backoff, and they are the seams the tests fake.
- GitHub calls share one GraphQL budget of 5,000 points an hour, `gh search` included. Batch new lookups into the existing aliased queries and the TTL caches; never add a call per card or per refresh.
- The project lists hold open issues only (`items(query: "is:open is:issue")`): nothing may read a closed issue's status or `statusItem` from them. Pages of one project are fetched one after another at about 1.5 s per 100 items, so a filter that drops pages is the lever on a cold refresh.
- A warm refresh costs only the `claude agents --json` call; the board-building Python is negligible. Measure before optimizing it.
- `~/.claude/jobs/*/state.json` and `claude agents --json` are Claude Code internals that change between versions: read fields defensively, so a missing field leaves a blank rather than raising.
- Local state lives under `CONFIG` (`TAVLA_HOME`, default `~/.config/tavla/`): `config.toml`, `links.json` and `hidden.json` are user data, the `*-cache.json` files are disposable.
- No owner, repository, project number or local path is written into the code, the tests or the docs: they come from `config.toml` (`PROJECTS`, `ISSUE_REPOS`, `SESSION_CWD`, `THEME`), the tests use made-up names (`acme/infra`, `me/todo`), and the docs use `octocat`. With no config file every feature either works or says which key to add.

## Tests

- The autouse `env` fixture points `JOBS` and `CONFIG` at `tmp_path`, sets the config globals to made-up projects and repos, and swaps `core.run` and `core.graphql` for `fake_run` and `fake_graphql`. `fake_run` raises on any command it doesn't know, so a new `gh` or `claude` invocation needs a branch there.
- TUI tests drive `tui.BoardApp().run_test(size=...)` with the pilot. Wait for `app.workers.wait_for_complete()` before asserting, and settle the layout (`pilot.wait_for_scheduled_animations()`, then a short `pilot.pause`) before clicking.
- Patch side effects (`core.attach`, `app.open_url`, the external editor) with `monkeypatch`; no test opens iTerm2, a browser or a real session.
- A behaviour change or a bug fix comes with a test named for the behaviour (`test_tui_<what the user sees>`).

## Changes

- A change to a key, a dialog, a card indicator or a data source updates `README.md` in the same commit: the key table and "Where the data comes from" are the spec.
- The TUI can't be seen from here. Say in the report that a visual change (layout, colours, theme) is checked only by tests and the user should look at it.
- With an editable install (`uv tool install --editable`), a commit on `main` is live in the user's next `tavla`, and a change to `pyproject.toml` needs the install repeated with `--reinstall`; say so when making one.
- The runtime dependency is pinned exactly in `pyproject.toml` and locked against pypi.org in `uv.lock`; change both with `uv add` / `uv lock`.

## Git

- Commit directly on `main` and push every commit right after making it; nothing is left committed but unpushed.
- release-please cuts releases from the commit types: `feat` and `fix` (and `!` for a breaking change) open or update the release PR, which bumps `version` in `pyproject.toml` and `uv.lock` and writes `CHANGELOG.md`; never edit those by hand. Config is in `.github/release-please-config.json`.
- Messages: `<type>: <what the user now sees or can do>`, one line, the behaviour rather than the mechanism (`fix: attached iTerm tabs are named with an OSC 1 escape, since iTerm2 does not show a name set through AppleScript`).
