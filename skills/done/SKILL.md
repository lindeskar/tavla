---
name: done
description: Remove this background session from the Tavla board and delete its worktree; an attached Tavla tab closes with it. Only the user runs this, when the session's work is finished.
disable-model-invocation: true
allowed-tools: Bash(tavla rm --self)
---

The user is finished with this session and wants it gone from the Tavla board.

1. This session ends inside the next step, and nothing written after it is shown. If the user is still owed anything (a result, a link, a command to run), write it now, in one short message.
2. Run exactly `tavla rm --self`, outside the Bash sandbox: it writes `~/.config/tavla/links.json` and talks to the Claude Code daemon.

If the command returns instead of ending the session, it refused. Relay its message as it is and stop:

- **Unpushed commits or uncommitted changes in the worktree:** say what is unpushed and let the user decide. Never add `--discard-unpushed` or `--force-remove-worktree`, and never push, commit or discard anything to get past the refusal.
- **Interactive session:** it is not a background session, so there is nothing to remove; the user closes it in its own terminal.

The conversation itself is kept: `claude --resume` still lists it.
