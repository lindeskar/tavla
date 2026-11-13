---
name: link
description: Link this Claude Code session to a GitHub issue on the Tavla board, so the session shows on that issue's card. Use when the user says to link, attach or file this session under an issue, or when the session starts working on an issue that was not named in its first prompt.
argument-hint: <issue URL or owner/repo#N>
allowed-tools: Bash(tavla link --self *)
---

Link this session to the issue in `$ARGUMENTS`:

```sh
tavla link --self <issue URL or owner/repo#N>
```

Run it outside the Bash sandbox: it writes `~/.config/tavla/links.json`. It makes no GitHub call and changes nothing on the issue.

- With no argument, link the one issue this session is plainly working on; if there are several candidates or none, ask.
- A session can be linked to several issues: run the command once per issue.
- Tavla already links a session from an issue reference in its first prompt and from a pull request that closes the issue. Don't link again for those.
- To undo a link: `tavla unlink <session id> <issue>`, where the id is the one the link command printed.
