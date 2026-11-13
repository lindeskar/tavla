"""Tavla in the terminal: GitHub issues as cards, each with the Claude Code sessions on it."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import ClassVar

from textual import events, on, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Checkbox, Footer, Input, Label, Link, ListItem, ListView, Markdown, RadioButton, RadioSet, Rule, Static, TextArea

from . import board as core

REFRESH_S = 60
COL_MIN = 36  # five board columns at this width fit a 190-wide terminal
GLYPH = {"needs": "●", "working": "◐", "idle": "○"}
SESSION_COLOR = {"needs": "$warning", "working": "$accent", "idle": "$text-muted"}
MERGED = "#a371f7"
TAVLA = Theme(
    name="tavla",
    primary="#71bc6d",
    secondary="#90dc93",
    accent="#d68573",
    warning="#d3a968",
    error="#d6737d",
    success="#90dc93",
    foreground="#e4e4e8",
    background="#111116",
    surface="#1a1a21",
    panel="#2a2a33",
)


def fit(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def label_chip(name: str, color: str) -> str:
    try:
        r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        r = g = b = 128
        color = "808080"
    fg = "#1f2328" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"
    return f"[{fg} on #{color}] {escape(name)} [/]"


def session_chip(card: dict) -> str:
    ss = card["sessions"]
    if not ss:
        return ""
    col = card["sessionColumn"]
    text = {"needs": "needs you", "working": "working"}.get(col, plural(len(ss), "session"))
    extra = f" [$text-muted]· {plural(len(ss), 'session')}[/]" if col in ("needs", "working") and len(ss) > 1 else ""
    return f"[{SESSION_COLOR[col]}]{GLYPH[col]} {text}[/]{extra}"


def pr_color(state: str) -> str:
    return "$success" if state == "OPEN" else MERGED if state == "MERGED" else "$text-muted"


def pr_chip(prs: list[dict]) -> str:
    if not prs:
        return ""
    open_ = sum(p["state"] == "OPEN" for p in prs)
    merged = sum(p["state"] == "MERGED" for p in prs)
    color = pr_color("OPEN" if open_ else "MERGED" if merged else "CLOSED")
    detail = f" ({open_} open)" if open_ and open_ != len(prs) else ""
    return f"[{color}]⎇ {plural(len(prs), 'PR')}{detail}[/]"


def card_meta(card: dict, grouping: str) -> str:
    sub = card.get("subIssues") or {}
    parts = [
        session_chip(card),
        pr_chip(card.get("prs") or []),
        f"☑ {sub['completed']}/{sub['total']}" if sub.get("total") else "",
        f"[$text-muted]{escape(card['status'])}[/]" if grouping == "session" and card["status"] else "",
        "[$text-muted]⊘ hidden[/]" if card.get("hidden") else "",
    ]
    return "  ".join(p for p in parts if p)


def labels_line(card: dict) -> str:
    return " ".join(label_chip(lb["name"], lb["color"]) for lb in card.get("labels") or [])


def ref_parts(card: dict) -> tuple[str, str]:
    repo, num = card["key"].split("/", 1)[1].split("#")
    icon = "[$success]◉[/]" if card["isOpen"] else f"[{MERGED}]✓[/]"
    return icon, f"{repo} #{num}"


class IssueLink(Link):
    """The repo and number line: Link opens the issue, and the click stops here so it does not select the card."""

    can_focus = False

    def on_click(self, event: events.Click) -> None:
        event.stop()


class Columns(HorizontalScroll):
    """Holds the columns; tab moves between the lists inside, never to this container.

    Columns share the width while each gets its minimum; below that they keep the minimum and the board scrolls.
    """

    can_focus = False

    def fit(self) -> None:
        cols = sum(1 for c in self.children if c.has_class("col") and not c.has_class("list"))
        self.set_class(cols * (COL_MIN + 1) - 1 > self.size.width, "narrow")

    def on_resize(self) -> None:
        self.fit()


def is_backlog(card: dict, grouping: str) -> bool:
    return grouping == "status" and card.get("statusColumn") == "backlog"


def with_divider(cards: list[dict], grouping: str) -> list[dict]:
    """The cards of one column or group, with a divider row ahead of its Backlog cards (which come last)."""
    first = next((i for i, c in enumerate(cards) if is_backlog(c, grouping)), None)
    if first is None:
        return cards
    return [*cards[:first], {"divider": "Backlog", "count": len(cards) - first}, *cards[first:]]


class Divider(ListItem):
    """The line between a column's Todo and Backlog cards; disabled so the cursor steps over it."""

    def __init__(self, row: dict) -> None:
        super().__init__(
            Horizontal(Label(row["divider"], classes="divider-title"), Label(str(row["count"]), classes="divider-count"), Rule(line_style="solid")),
            classes="divider",
            disabled=True,
        )


class CardItem(ListItem):
    def __init__(self, card: dict, grouping: str) -> None:
        self.card = card
        icon, ref = ref_parts(card)
        rows = [
            Label(escape(card["title"]), classes="card-title"),
            Horizontal(Label(icon, classes="card-icon"), IssueLink(ref, url=card["url"], tooltip="Open on GitHub"), classes="card-ref"),
        ]
        if meta := card_meta(card, grouping):
            rows.append(Label(meta, classes="card-meta"))
        if labels := labels_line(card):
            rows.append(Label(labels, classes="card-labels"))
        classes = ["card"] + ["has-session"] * bool(card["sessions"]) + ["is-hidden"] * bool(card.get("hidden")) + ["is-backlog"] * is_backlog(card, grouping)
        super().__init__(*rows, classes=" ".join(classes))


class SessItem(ListItem):
    """A compact session card in the Unlinked column."""

    def __init__(self, sess: dict) -> None:
        self.sess = sess
        kind = sess["kind"]
        state = f"[{SESSION_COLOR[kind]}]{GLYPH[kind]} {escape(sess['state'])}[/]"
        info = f"{state}  [$text-muted]{sess['id']} · {core.ago(sess['updatedAt'])}[/]"
        super().__init__(Label(escape(sess["name"]), classes="card-title"), Label(info, classes="card-meta"), classes="card has-session")


class SessDetailRow(ListItem):
    """A session on one line in the bottom panel, its cells lined up with SESS_HEADER."""

    def __init__(self, sess: dict) -> None:
        self.sess = sess
        kind = sess["kind"]
        prs = "  ".join(f"[{pr_color(p['state'])}]⎇ {escape(p['key'].split('/', 1)[1])}[/]" for p in sess.get("prs") or [])
        super().__init__(
            Horizontal(
                Label(f"[{SESSION_COLOR[kind]}]{GLYPH[kind]} {escape(sess['state'])}[/]", classes="s-state"),
                Label(escape(sess["name"]), classes="s-name"),
                Label(core.ago(sess["updatedAt"]), classes="s-ago"),
                Label(sess["id"], classes="s-id"),
                Label(prs, classes="s-prs"),
                Label(escape(" ".join((sess["result"] or sess["detail"]).split())), classes="s-result"),
            ),
            classes="row",
        )


def row_status(card: dict, grouping: str, group_title: str) -> str:
    return "" if grouping == "status" and card["status"] == group_title else card["status"] or ""


class GroupRow(ListItem):
    """A group's heading in the list view; disabled so the cursor steps over it."""

    def __init__(self, group: dict) -> None:
        self.group = group
        super().__init__(
            Horizontal(Label(group["title"], classes="col-title"), Label(str(group["count"]), classes="col-count"), classes="col-header"),
            classes="group-row alert" if group["alert"] else "group-row",
            disabled=True,
        )


class CardRow(ListItem):
    """An issue on one line, its cells lined up with the list's column header."""

    def __init__(self, card: dict, grouping: str, group_title: str) -> None:
        self.card = card
        icon, ref = ref_parts(card)
        sub = card.get("subIssues") or {}
        super().__init__(
            Horizontal(
                Label(icon, classes="c-icon"),
                Label(escape(card["title"]), classes="c-title"),
                IssueLink(ref, url=card["url"], tooltip="Open on GitHub", classes="c-ref"),
                Label(escape(row_status(card, grouping, group_title)), classes="c-status"),
                Label(session_chip(card), classes="c-sessions"),
                Label(pr_chip(card.get("prs") or []), classes="c-prs"),
                Label(f"☑ {sub['completed']}/{sub['total']}" if sub.get("total") else "", classes="c-sub"),
                Label(labels_line(card), classes="c-labels"),
            ),
            classes=" ".join(["row"] + ["is-hidden"] * bool(card.get("hidden")) + ["is-backlog"] * is_backlog(card, grouping)),
        )


class SessRow(ListItem):
    """An unlinked session on one line, in the same cells as an issue row."""

    def __init__(self, sess: dict) -> None:
        self.sess = sess
        kind = sess["kind"]
        super().__init__(
            Horizontal(
                Label(f"[{SESSION_COLOR[kind]}]{GLYPH[kind]}[/]", classes="c-icon"),
                Label(escape(sess["name"]), classes="c-title"),
                Label(sess["id"], classes="c-ref"),
                Label(f"{core.ago(sess['updatedAt'])} ago", classes="c-status"),
                Label(f"[{SESSION_COLOR[kind]}]{escape(sess['state'])}[/]", classes="c-sessions"),
                Label("", classes="c-prs"),
                Label("", classes="c-sub"),
                Label(escape(", ".join(sess["refs"])), classes="c-labels muted"),
            ),
            classes="row",
        )


class CardList(ListView):
    """Home and end move the highlight to the first and last row, where ListView's only scroll.

    ListView steps over disabled items (headings, dividers), except on the first move from no highlight; this one always does.

    A click on an issue card only highlights it; ListView would also select it, which moves the focus to its sessions.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("home", "first", "First", show=False),
        Binding("end", "last", "Last", show=False),
    ]

    def _first_enabled(self, order: range) -> int | None:
        return next((i for i in order if not self._nodes[i].disabled), None)

    def action_first(self) -> None:
        index = self._first_enabled(range(len(self._nodes)))
        if index is not None:
            self.index = index

    def action_last(self) -> None:
        index = self._first_enabled(range(len(self._nodes) - 1, -1, -1))
        if index is not None:
            self.index = index

    def action_cursor_down(self) -> None:
        if self.index is None:
            self.index = self._first_enabled(range(len(self._nodes)))
        else:
            super().action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.index is None:
            self.index = self._first_enabled(range(len(self._nodes) - 1, -1, -1))
        else:
            super().action_cursor_up()

    def _on_list_item__child_clicked(self, event: ListItem._ChildClicked) -> None:
        if not isinstance(event.item, (CardItem, CardRow)):
            return  # ListView's own handler runs next and selects it
        event.prevent_default()
        event.stop()
        self.focus()
        self.index = self._nodes.index(event.item)


class GroupedList(CardList):
    """Tab and shift+tab jump between groups, like moving between the board's columns, and leave the list past either end."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "group_step(1)", "Next group", show=False),
        Binding("shift+tab", "group_step(-1)", "Previous group", show=False),
    ]

    def action_group_step(self, step: int) -> None:
        starts = [i + 1 for i, node in enumerate(self._nodes) if isinstance(node, GroupRow)]
        if self.index is None:
            target = 0 if step > 0 else len(starts) - 1
        else:
            target = max(n for n, start in enumerate(starts) if start <= self.index) + step
        if 0 <= target < len(starts):
            self.index = starts[target]
        elif step > 0:
            self.screen.focus_next()
        else:
            self.screen.focus_previous()


LIST_HEADER = ["", "Issue", "Repo", "Status", "Sessions", "PRs", "Sub", "Labels"]
LIST_CELLS = ["c-icon", "c-title", "c-ref", "c-status", "c-sessions", "c-prs", "c-sub", "c-labels"]
SESS_HEADER = ["State", "Session", "Updated", "Id", "PRs", "Result"]
SESS_CELLS = ["s-state", "s-name", "s-ago", "s-id", "s-prs", "s-result"]


class WordInput(Input):
    """A one-line field where option+arrows move by word, as in macOS text fields."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("alt+left", "cursor_left_word", show=False),
        Binding("alt+right", "cursor_right_word", show=False),
        Binding("alt+shift+left", "cursor_left_word(True)", show=False),
        Binding("alt+shift+right", "cursor_right_word(True)", show=False),
    ]


class PromptArea(TextArea):
    """A text box where option+arrows move by word (up/down: to the start/end of the text) and ctrl+g edits it in $VISUAL/$EDITOR."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("alt+left", "cursor_word_left", show=False),
        Binding("alt+right", "cursor_word_right", show=False),
        Binding("alt+shift+left", "cursor_word_left(True)", show=False),
        Binding("alt+shift+right", "cursor_word_right(True)", show=False),
        Binding("alt+up", "text_start", show=False),
        Binding("alt+down", "text_end", show=False),
        Binding("alt+shift+up", "text_start(True)", show=False),
        Binding("alt+shift+down", "text_end(True)", show=False),
        Binding("ctrl+g", "external_editor", "Edit in editor", priority=True),
    ]

    def action_text_start(self, select: bool = False) -> None:
        self.move_cursor((0, 0), select=select)

    def action_text_end(self, select: bool = False) -> None:
        self.move_cursor(self.document.end, select=select)

    def action_external_editor(self) -> None:
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
        with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="prompt-", delete=False) as f:
            f.write(self.text)
        path = Path(f.name)
        try:
            with self.app.suspend():
                done = subprocess.run([*editor, str(path)], check=False)
            if done.returncode != 0:
                self.notify(f"{editor[0]} exited with {done.returncode}; the text is unchanged.", severity="warning")
                return
            text = path.read_text().removesuffix("\n")  # editors end the file with a newline
        except (OSError, SuspendNotSupported) as e:
            self.notify(f"Could not run {editor[0]}: {e}", severity="error")
            return
        finally:
            path.unlink(missing_ok=True)
        self.replace(text, (0, 0), self.document.end)  # an edit, not a reload, so ctrl+z undoes it
        self.move_cursor(self.document.end)
        self.focus()


class FilterInput(WordInput):
    """The search box: typing filters the board live, enter goes to the matches, esc clears it and closes."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "clear", "Clear filter", show=False)]

    def show(self, on: bool) -> None:
        # Hidden, it must not take the focus Textual hands out at startup or on tab.
        self.display = self.can_focus = on

    def action_clear(self) -> None:
        self.value = ""
        self.show(False)
        self.app.action_back()
        if self.app.focused is None or self.app.focused is self:
            self.screen.focus_next(ListView)


class Ask(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str, placeholder: str = "") -> None:
        super().__init__()
        self.question, self.placeholder = question, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="ask"):
            yield Label(self.question)
            yield WordInput(placeholder=self.placeholder)

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AskPrompt(ModalScreen[tuple[str, bool, bool] | None]):
    """A session prompt: the box wraps and grows with the text, so a long prompt stays readable; enter submits.

    With progress_from, the issue's status, the box also offers to move the issue from it to In Progress
    (ticked by default for Todo, unticked for any other status), and with offer_attach to attach to the new session (unticked by default); the answer is
    (prompt, move it, attach). With required, an empty prompt keeps the box open."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel"), Binding("enter", "submit", "Start", priority=True)]

    def __init__(self, question: str, progress_from: str = "", required: bool = False, offer_attach: bool = False) -> None:
        super().__init__()
        self.question, self.progress_from, self.required, self.offer_attach = question, progress_from, required, offer_attach

    def compose(self) -> ComposeResult:
        placeholder = "prompt" if self.required else "prompt (enter for the default pick-up prompt)"
        with Vertical(id="ask"):
            yield Label(f"{self.question}  [$text-muted]enter starts · ctrl+g editor · esc cancels[/]")
            yield PromptArea(id="prompt", placeholder=placeholder, soft_wrap=True, show_line_numbers=False)
            if self.progress_from:
                yield Checkbox(f"Move the issue from {self.progress_from} to In Progress", value=self.progress_from == "Todo", id="to-progress")
            if self.offer_attach:
                yield Checkbox("Attach to the session (new iTerm tab)", value=False, id="attach-now")

    def action_submit(self) -> None:
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if self.required and not prompt:
            self.notify("A session on no issue needs a prompt.", severity="error")
            return
        progress = bool(self.progress_from) and self.query_one("#to-progress", Checkbox).value
        attach = self.offer_attach and self.query_one("#attach-now", Checkbox).value
        self.dismiss((prompt, progress, attach))

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewIssue(ModalScreen[dict | None]):
    """Repo, title, optional body and assignment for a new issue; enter in the title or ctrl+s creates it."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "create", "Create", priority=True)]
    AUTO_FOCUS = "#issue-title"

    def compose(self) -> ComposeResult:
        with Vertical(id="ask"):
            yield Label("New issue  [$text-muted]enter or ctrl+s creates · ctrl+g in the body opens an editor · esc cancels[/]")
            with RadioSet(id="issue-repo"):
                for i, (repo, label) in enumerate(core.ISSUE_REPOS):
                    yield RadioButton(f"{label}  [$text-muted]{repo}[/]", value=i == 0)
            yield WordInput(placeholder="title", id="issue-title")
            yield PromptArea(id="issue-body", placeholder="body (optional)")
            yield Checkbox("Assign to me, so it shows on the board", value=True, id="issue-assign")

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.action_create()

    def action_create(self) -> None:
        title = self.query_one("#issue-title", Input).value.strip()
        if not title:
            self.notify("An issue needs a title.", severity="error")
            self.query_one("#issue-title", Input).focus()
            return
        repo = core.ISSUE_REPOS[self.query_one("#issue-repo", RadioSet).pressed_index][0]
        body = self.query_one("#issue-body", TextArea).text
        self.dismiss({"repo": repo, "title": title, "body": body, "assign": self.query_one("#issue-assign", Checkbox).value})

    def action_cancel(self) -> None:
        self.dismiss(None)


class StopOrDelete(ModalScreen[str | None]):
    """Stop (s) or delete (d) a background session; the dialog is the confirmation."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel"), Binding("s", "pick('stop')", "Stop"), Binding("d", "pick('delete')", "Delete")]

    def __init__(self, sess: dict) -> None:
        super().__init__()
        self.sess = sess

    def compose(self) -> ComposeResult:
        s = self.sess
        with Vertical(id="ask"):
            name = f"  [b]{escape(s['name'])}[/]" if s["name"] != s["id"] else ""
            yield Label(f"Session {s['id']}{name}  [{SESSION_COLOR[s['kind']]}]{escape(s['state'])}[/]", classes="title")
            yield Label("[b]s[/] stop: ends the process, keeps the conversation; attach resumes it")
            yield Label("[b]d[/] delete: removes the session and its worktree (refused while it has unpushed commits)")
            yield Label("[$text-muted]esc cancels[/]", classes="hint")

    def action_pick(self, choice: str) -> None:
        self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(None)


BOARD_ACTIONS = {"search", "attach", "new", "new_unlinked", "new_issue", "open", "link", "unlink", "stop_delete", "hide", "show_hidden", "group", "view", "refresh", "back"}


CARD_CSS = """
    height: auto; padding: 0 1; margin-bottom: 0;
    background: $surface; border: round $panel-lighten-2;
"""


class BoardApp(App):
    TITLE = "Tavla"
    CSS = f"""
    * {{ scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1; }}
    Footer {{ scrollbar-size: 0 0; }}
    #status {{ height: 1; padding: 0 1; color: $text-muted; }}
    #filter {{ height: 1; border: none; padding: 0 1; margin-bottom: 1; background: $boost; }}
    #filter:focus {{ background: $accent 20%; }}
    #columns {{ height: 1fr; }}
    .col {{ width: 1fr; min-width: {COL_MIN}; height: 1fr; background: $boost; border: round $panel; margin: 0 1 0 0; }}
    .col:last-child {{ margin: 0; }}
    #columns.narrow .col {{ width: {COL_MIN}; }}
    .col:focus-within {{ border: round $accent; }}
    .col-header {{ height: 1; padding: 0 1; margin-bottom: 1; }}
    .col-title {{ width: auto; text-style: bold; }}
    .col-count {{ width: auto; margin-left: 1; padding: 0 1; background: $panel; color: $text-muted; }}
    .col-header.alert .col-title {{ color: $warning; }}
    .col-header.alert .col-count {{ background: $warning 30%; color: $warning; }}
    ListView.cards {{ height: 1fr; background: transparent; padding: 0 1; }}
    ListView.cards > ListItem.card {{ {CARD_CSS} }}
    ListView.cards > ListItem.card.-highlight {{ background: $surface-lighten-1; }}
    ListView.cards:focus > ListItem.card.-highlight {{ background: $surface-lighten-1; border: round $accent; }}
    ListView.cards > ListItem.card.has-session {{ background: $primary 18%; border: round $primary 60%; }}
    ListView.cards > ListItem.card.has-session.-highlight {{ background: $primary 28%; }}
    ListView.cards:focus > ListItem.card.has-session.-highlight {{ background: $primary 28%; border: round $accent; }}
    .card-title {{ width: 1fr; text-style: bold; margin-bottom: 1; }}
    .card-ref {{ height: 1; }}
    .card-icon {{ width: auto; margin-right: 1; }}
    .card-ref Link {{ width: auto; color: $text-muted; text-style: none; }}
    .card-ref Link:hover {{ color: $accent; text-style: underline; }}
    .card-meta, .card-labels, .card-body {{ width: 1fr; margin-top: 1; }}
    ListView.cards > ListItem.is-hidden {{ text-opacity: 50%; }}
    ListView.cards > ListItem.divider {{ height: 1; padding: 0 1; margin-bottom: 1; background: transparent; }}
    .col.list ListView.cards > ListItem.divider {{ margin-bottom: 0; }}
    .divider Horizontal {{ height: 1; }}
    .divider-title {{ width: auto; color: $text-muted; text-style: bold; }}
    .divider-count {{ width: auto; margin: 0 1; color: $text-muted; }}
    .divider Rule {{ width: 1fr; height: 1; margin: 0; color: $panel-lighten-2; }}
    .card.is-backlog .card-title {{ color: $text-muted; text-style: none; }}
    .row.is-backlog .c-title {{ color: $text-muted; }}
    .col.list {{ width: 1fr; margin: 0; }}
    .list-head {{ height: 1; padding: 0 2; margin-bottom: 1; color: $text-muted; text-style: bold; }}
    ListView.cards > ListItem.row {{ height: 1; padding: 0 1; background: transparent; }}
    ListView.cards > ListItem.row.-highlight {{ background: $surface-lighten-1; }}
    ListView.cards:focus > ListItem.row.-highlight {{ background: $accent 30%; }}
    ListView.cards > ListItem.group-row {{ height: 1; margin-top: 1; padding: 0 1; background: transparent; }}
    ListView.cards > ListItem.group-row:first-child {{ margin-top: 0; }}
    ListItem.group-row .col-header {{ margin: 0; padding: 0; }}
    .group-row.alert .col-title {{ color: $warning; }}
    .group-row.alert .col-count {{ background: $warning 30%; color: $warning; }}
    .row Horizontal, .list-head {{ height: 1; }}
    .row Label, .row Link, .list-head Label {{ height: 1; margin-right: 1; text-wrap: nowrap; text-overflow: ellipsis; }}
    .c-icon {{ width: 1; }}
    .c-title {{ width: 1fr; min-width: 20; }}
    .c-ref {{ width: 24; }}
    .row Link.c-ref {{ color: $text-muted; text-style: none; }}
    .row Link.c-ref:hover {{ color: $accent; text-style: underline; }}
    .c-status {{ width: 11; }}
    .row .c-ref, .row .c-status, .row .muted {{ color: $text-muted; }}
    .c-sessions {{ width: 24; }}
    .c-prs {{ width: 16; }}
    .c-sub {{ width: 7; }}
    .c-labels {{ width: 30; margin-right: 0; }}
    .s-state {{ width: 12; }}
    .s-name {{ width: 3fr; min-width: 16; }}
    .s-ago {{ width: 7; }}
    .s-id {{ width: 8; }}
    .s-prs {{ width: 18; }}
    .s-result {{ width: 2fr; margin-right: 0; }}
    .row .s-ago, .row .s-id, .row .s-result {{ color: $text-muted; }}
    #detail {{ height: 18; margin-top: 1; }}
    #detail-issue {{ width: 40%; min-width: 40; margin: 0 1 0 0; background: $boost; border: round $panel; }}
    #detail-issue .col-header {{ margin-bottom: 0; }}
    #detail-body-scroll {{ height: 1fr; }}
    #detail-body {{ padding: 0 1; background: transparent; }}
    #detail-sessions {{ width: 1fr; height: 1fr; background: $boost; border: round $panel; }}
    #detail-sessions:focus-within {{ border: round $accent; }}
    #ask {{ width: 80; height: auto; padding: 1 2; border: round $accent; background: $surface; }}
    Ask, AskPrompt, NewIssue, StopOrDelete {{ align: center middle; }}
    #prompt {{ height: auto; min-height: 3; max-height: 12; }}
    #ask RadioSet, #ask Checkbox {{ width: 1fr; margin-top: 1; }}
    #ask TextArea {{ height: 8; }}
    StopOrDelete #ask Label {{ width: 1fr; }}
    StopOrDelete #ask .title {{ margin-bottom: 1; }}
    StopOrDelete #ask .hint {{ margin-top: 1; }}
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "attach", "Attach", show=True, priority=False),
        Binding("slash", "search", "Search"),
        Binding("n", "new", "New session"),
        Binding("N", "new_unlinked", "New unlinked session", show=False),
        Binding("i", "new_issue", "New issue"),
        Binding("o", "open", "Open issue"),
        Binding("l", "link", "Link session"),
        Binding("u", "unlink", "Unlink", show=False),
        Binding("ctrl+x", "stop_delete", "Stop/delete", show=False),
        Binding("h", "hide", "Hide/unhide"),
        Binding("H", "show_hidden", "Show hidden", show=False),
        Binding("g", "group", "Grouping"),
        Binding("v", "view", "View"),
        Binding("r", "refresh(True)", "Refresh", show=False),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.grouping = "status"
        self.view = "board"
        self.filter = ""
        self.hidden = core.load_hidden()  # issue keys hidden from the board, kept in hidden.json
        self.show_hidden = False
        self.data: dict = {"cards": [], "unlinked": [], "fetchedAt": 0, "errors": []}
        self.current: dict | None = None  # the highlighted card or unlinked session
        self.selection_list: ListView | None = None  # the board list current was picked in
        self.loading = False
        self.refresh_queued: bool | None = None  # a refresh asked for mid-load, and whether it forces
        self.signatures: dict[str, str] = {}  # what each list last drew, so an unchanged one is left alone
        self.body_shown: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="status")
        filter_box = FilterInput(placeholder="filter: every word must match a title, repo #N, status, label or session name", id="filter")
        filter_box.show(False)
        yield filter_box
        yield Columns(id="columns")
        with Horizontal(id="detail"):
            with Vertical(id="detail-issue"):
                yield Horizontal(Label("", id="detail-icon", classes="card-icon"), IssueLink("", id="detail-link"), classes="col-header card-ref")
                body_scroll = VerticalScroll(id="detail-body-scroll")
                body_scroll.can_focus = False  # scrolls with the mouse; tab stays between the columns and the sessions
                with body_scroll:
                    yield Markdown("", id="detail-body")
            with Vertical(id="detail-sessions"):
                yield Horizontal(Label("Sessions", classes="col-title"), Label("0", id="count-sessions", classes="col-count"), classes="col-header")
                yield Horizontal(*(Label(t, classes=c) for t, c in zip(SESS_HEADER, SESS_CELLS)), classes="list-head")
                yield CardList(id="sessions", classes="cards")
        yield Footer(compact=True)

    async def on_mount(self) -> None:
        self.register_theme(TAVLA)
        self.theme = core.THEME if core.THEME in self.available_themes else "tavla"
        await self.build_columns()
        self.action_refresh()
        self.set_interval(REFRESH_S, self.action_refresh)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Letters pressed on a dialog's radio set or checkbox bubble up to the app; only its own keys apply there.
        return not (isinstance(self.screen, ModalScreen) and action in BOARD_ACTIONS)

    # --- data ------------------------------------------------------------------

    def action_refresh(self, force: bool = False) -> None:
        if self.loading:
            # The load under way may have read the session files before the change this refresh is for.
            self.refresh_queued = bool(self.refresh_queued) or force
            return
        self.loading = True
        self.update_status()
        self.load(force)

    # Its own group: exclusive cancels the group, and a start or delete worker asks for this refresh mid-run.
    @work(thread=True, exclusive=True, group="load")
    def load(self, force: bool) -> None:
        try:
            data = core.build_board(force)
        except Exception as err:  # noqa: BLE001
            data = {**self.data, "errors": [str(err)]}
        self.call_from_thread(self.apply, data)

    async def apply(self, data: dict) -> None:
        self.data = data
        self.loading = False
        await self.fill_columns()
        self.update_status()
        if self.refresh_queued is not None:
            force, self.refresh_queued = self.refresh_queued, None
            self.action_refresh(force)

    def update_status(self) -> None:
        d = self.data
        state = "refreshing…" if self.loading else f"updated {core.ago(d['fetchedAt'])} ago" if d["fetchedAt"] else "loading…"
        group = "issue status" if self.grouping == "status" else "session state"
        group = f"{self.view} view  ·  grouped by {group}"
        if self.filter:
            shown = sum(1 for c in self.visible(d["cards"]) if self.show_hidden or c["key"] not in self.hidden)
            group += f"  ·  filter [$accent]{escape(self.filter)}[/]: {shown} of {plural(len(d['cards']), 'issue')}"
        if n_hidden := sum(1 for c in d["cards"] if c["key"] in self.hidden):
            group += f"  ·  {n_hidden} hidden ({'shown, ' if self.show_hidden else ''}H toggles)"
        errors = f"  ·  [$error]{len(d['errors'])} error(s): {escape(fit(d['errors'][0], 90))}[/]" if d["errors"] else ""
        self.query_one("#status", Static).update(f"{group}  ·  {state}{errors}")

    # --- layout ----------------------------------------------------------------

    def column_specs(self) -> list[tuple[str, str]]:
        return [*core.columns(self.grouping), ("unlinked", "Unlinked")]

    async def build_columns(self) -> None:
        box = self.query_one("#columns", Columns)
        await box.remove_children()
        self.signatures = {k: v for k, v in self.signatures.items() if k == "sessions"}
        if self.view == "list":
            head = Horizontal(*(Label(t, classes=c) for t, c in zip(LIST_HEADER, LIST_CELLS)), classes="list-head")
            await box.mount(Vertical(head, GroupedList(id="col-list", classes="cards"), classes="col list"))
        else:
            await box.mount_all(
                Vertical(
                    Horizontal(Label(title, classes="col-title"), Label("0", id=f"count-{col_id}", classes="col-count"), id=f"header-{col_id}", classes="col-header"),
                    CardList(id=f"col-{col_id}", classes="cards"),
                    classes="col",
                )
                for col_id, title in self.column_specs()
            )
        box.fit()

    async def replace_items(self, lv: ListView, rows: list[dict], make, keep: str | None) -> None:
        """Redraw a list only when its rows changed, keeping the highlighted row by key and the focus where it was."""
        signature = json.dumps(rows, sort_keys=True, default=str) + self.grouping
        if self.signatures.get(lv.id or "") == signature:
            return
        self.signatures[lv.id or ""] = signature
        previous = self.item_key(lv.highlighted_child) if lv.highlighted_child else None
        old_index = lv.index
        items = [make(r) for r in rows]
        with self.batch_update():
            await lv.clear()
            await lv.extend(items)
            has_keep = any(self.item_key(it) == keep for it in items)
            if has_keep and keep is not None:
                self.selection_list = lv
            want = keep if has_keep else previous
            match = next((i for i, it in enumerate(items) if want is not None and self.item_key(it) == want), None)
            # The list view always has a cursor, from its first row on; a board column has none until the person moves into it.
            if match is None and want is None and items and isinstance(items[0], GroupRow):
                match = 0
            if match is None and old_index is not None and items:
                match = min(old_index, len(items) - 1)
            # Group headings are disabled: land on the nearest row instead.
            if match is not None and items[match].disabled:
                match = next((i for i in [*range(match, len(items)), *range(match, -1, -1)] if not items[i].disabled), None)
            # Restoring the row is not the person moving: it must not re-point the detail panel.
            with lv.prevent(ListView.Highlighted):
                lv.index = match

    def visible(self, rows: list[dict]) -> list[dict]:
        return [r for r in rows if core.matches(r, self.filter)] if self.filter else rows

    def groups(self) -> list[tuple[str, str, list[dict]]]:
        cards = [{**c, "hidden": True} if c["key"] in self.hidden else c for c in self.visible(self.data["cards"])]
        if not self.show_hidden:
            cards = [c for c in cards if not c.get("hidden")]
        out = [(col_id, title, core.column_cards(cards, col_id, self.grouping)) for col_id, title in core.columns(self.grouping)]
        return [*out, ("unlinked", "Unlinked", self.visible(self.data["unlinked"]))]

    @staticmethod
    def group_alert(col_id: str, rows: list[dict]) -> bool:
        return (col_id == "blocked" and bool(rows)) or any(r.get("sessionColumn") == "needs" for r in rows)

    async def fill_list(self, keep: str | None) -> None:
        rows: list[dict] = []
        for col_id, title, members in self.groups():
            if members:
                rows.append({"group": col_id, "title": title, "count": len(members), "alert": self.group_alert(col_id, members)})
                rows.extend(m if "divider" in m else {**m, "groupTitle": title} for m in with_divider(members, self.grouping))

        def make(r: dict) -> ListItem:
            if "group" in r:
                return GroupRow(r)
            if "divider" in r:
                return Divider(r)
            if "sessions" in r:
                return CardRow(r, self.grouping, r["groupTitle"])
            return SessRow(r)

        await self.replace_items(self.query_one("#col-list", ListView), rows, make, keep)
        await self.show_detail()

    async def fill_columns(self) -> None:
        keep = self.current_key()
        if self.view == "list":
            await self.fill_list(keep)
            return
        for col_id, _, rows in self.groups():
            lv = self.query_one(f"#col-{col_id}", ListView)
            if col_id == "unlinked":
                await self.replace_items(lv, rows, SessItem, keep)
            else:
                await self.replace_items(lv, with_divider(rows, self.grouping), lambda c: Divider(c) if "divider" in c else CardItem(c, self.grouping), keep)
            self.query_one(f"#count-{col_id}", Label).update(str(len(rows)))
            self.query_one(f"#header-{col_id}", Horizontal).set_class(self.group_alert(col_id, rows), "alert")
        await self.show_detail()

    @staticmethod
    def item_key(item: ListItem | None) -> str | None:
        if isinstance(item, (CardItem, CardRow)):
            return f"card:{item.card['key']}"
        if isinstance(item, (SessItem, SessRow, SessDetailRow)):
            return f"sess:{item.sess['id']}"
        return None

    @staticmethod
    def payload(item: ListItem | None) -> dict | None:
        """The card or unlinked session a board row stands for."""
        if isinstance(item, (CardItem, CardRow)):
            return item.card
        if isinstance(item, (SessItem, SessRow)):
            return item.sess
        return None

    def reread_current(self) -> dict | None:
        """The selection as the latest data has it; once it is gone, the row its list's cursor moved to."""
        cur = self.current
        if not cur:
            return None
        if "sessions" in cur:
            fresh = next((c for c in self.data["cards"] if c["key"] == cur["key"]), None)
        else:
            fresh = next((s for s in self.data["unlinked"] if s["id"] == cur["id"]), None)
        if fresh is not None:
            return fresh
        lv = self.selection_list
        return self.payload(lv.highlighted_child) if lv is not None and lv.is_attached else None

    def current_key(self) -> str | None:
        if not self.current:
            return None
        return f"card:{self.current['key']}" if "sessions" in self.current else f"sess:{self.current['id']}"

    async def show_detail(self) -> None:
        """The issue body of the highlighted card, or an unlinked session's first prompt, beside its sessions."""
        icon = self.query_one("#detail-icon", Label)
        link = self.query_one("#detail-link", IssueLink)
        # Re-read the selection so the detail follows a refresh, and drops a card or session that left the board.
        cur = self.current = self.reread_current()
        if cur and "sessions" in cur:
            ic, ref = ref_parts(cur)
            icon.update(ic)
            link.text, link.url = ref, cur["url"]
            body = cur.get("body") or "*No description.*"
            sessions = cur["sessions"]
        elif cur:
            icon.update(f"[{SESSION_COLOR[cur['kind']]}]{GLYPH[cur['kind']]}[/]")
            link.text, link.url = f"unlinked session {cur['id']}", ""
            body = cur.get("intent") or "*No prompt recorded.*"
            sessions = [cur]
        else:
            icon.update("")
            link.text, link.url = "Select a card", ""
            body = ""
            sessions = []
        if body != self.body_shown:
            self.body_shown = body
            await self.query_one("#detail-body", Markdown).update(body)
            self.query_one("#detail-body-scroll", VerticalScroll).scroll_home(animate=False)
        self.query_one("#count-sessions", Label).update(str(len(sessions)))
        await self.replace_items(self.query_one("#sessions", ListView), sessions, SessDetailRow, None)

    # --- events ----------------------------------------------------------------

    @on(ListView.Highlighted)
    async def highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "sessions" or event.item is None:
            return
        self.current, self.selection_list = self.payload(event.item), event.list_view
        await self.show_detail()

    @on(events.DescendantFocus)
    async def list_focused(self, event: events.DescendantFocus) -> None:
        """Tab into a list whose cursor already sits on a row fires no Highlighted: point the detail at that row."""
        lv = event.widget
        if not isinstance(lv, ListView) or lv.id == "sessions" or lv.highlighted_child is None or lv.highlighted_child.disabled:
            return
        if self.item_key(lv.highlighted_child) != self.current_key():
            self.current, self.selection_list = self.payload(lv.highlighted_child), lv
            await self.show_detail()

    @on(ListView.Selected)
    def selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, (SessItem, SessRow, SessDetailRow)):
            self.attach(event.item.sess)
        elif isinstance(event.item, (CardItem, CardRow)):
            self.open_sessions(event.item.card)

    @on(Input.Changed, "#filter")
    async def filter_changed(self, event: Input.Changed) -> None:
        self.filter = event.value.strip()
        await self.fill_columns()
        self.update_status()

    @on(Input.Submitted, "#filter")
    async def filter_submitted(self) -> None:
        """Enter in the box moves to the first match, keeping the filter on."""
        for lv in self.query(".col ListView").results(ListView):
            first = next((i for i, it in enumerate(lv.children) if not it.disabled), None)
            if first is None:
                continue
            if lv.index is None or lv.highlighted_child is None or lv.highlighted_child.disabled:
                lv.index = first
            lv.focus()
            self.current, self.selection_list = self.payload(lv.highlighted_child), lv
            await self.show_detail()
            return
        self.notify("Nothing matches the filter.")

    def action_search(self) -> None:
        box = self.query_one("#filter", FilterInput)
        box.show(True)
        box.focus()

    def open_sessions(self, card: dict) -> None:
        sessions = self.query_one("#sessions", ListView)
        if card["sessions"]:
            sessions.index = 0
            sessions.focus()
        else:
            self.notify("No sessions on this issue yet; press n to start one.")

    def focused_session(self) -> dict | None:
        lv = self.query_one("#sessions", ListView)
        if lv.has_focus and isinstance(lv.highlighted_child, SessDetailRow):
            return lv.highlighted_child.sess
        if self.current and "sessions" not in self.current:
            return self.current
        if self.current and len(self.current["sessions"]) == 1:
            return self.current["sessions"][0]
        return None

    # --- actions ---------------------------------------------------------------

    def action_attach(self) -> None:
        lv = self.query_one("#sessions", ListView)
        if lv.has_focus and isinstance(lv.highlighted_child, SessDetailRow):
            self.attach(lv.highlighted_child.sess)
        elif self.current and "sessions" in self.current:
            self.open_sessions(self.current)  # a card always goes to its sessions, even when it has only one
        elif self.current:
            self.attach(self.current)

    @work(thread=True)
    def attach(self, sess: dict) -> None:
        if sess.get("isInteractive"):
            self.call_from_thread(self.notify, f"{sess['name']} is interactive and already runs in its own terminal.", severity="warning")
            return
        try:
            core.attach(sess["id"], sess["name"], cwd=sess.get("cwd") or "")
            self.call_from_thread(self.notify, f"Attached {sess['id']} in a new iTerm2 tab")
        except Exception as err:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Attach failed: {err}", severity="error")

    def action_back(self) -> None:
        cur = self.current_key()
        for lv in self.query(".col ListView").results(ListView):
            if any(self.item_key(it) == cur for it in lv.children):
                lv.focus()
                return

    async def action_group(self) -> None:
        self.grouping = "session" if self.grouping == "status" else "status"
        await self.build_columns()
        await self.fill_columns()
        self.update_status()

    async def action_view(self) -> None:
        self.view = "list" if self.view == "board" else "board"
        await self.build_columns()
        await self.fill_columns()
        self.update_status()
        self.action_back()

    async def action_hide(self) -> None:
        card = self.current if self.current and "sessions" in self.current else None
        if not card:
            self.notify("Select an issue card first.")
            return
        self.hidden = core.toggle_hidden(card["key"])
        hiding = card["key"] in self.hidden
        await self.refill_hidden()
        self.notify((f"Hid {card['key']}" + ("" if self.show_hidden else "; H shows hidden issues")) if hiding else f"Unhid {card['key']}")

    async def action_show_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        await self.refill_hidden()

    async def refill_hidden(self) -> None:
        await self.fill_columns()
        # A card that just went hidden left its list: follow the highlight to the row that took its place.
        lv = self.focused
        if not self.show_hidden and self.current and self.current.get("key") in self.hidden and isinstance(lv, ListView) and lv.id != "sessions":
            self.current, self.selection_list = self.payload(lv.highlighted_child), lv
            await self.show_detail()
        self.update_status()

    def action_open(self) -> None:
        if self.current and self.current.get("url"):
            subprocess.run(["open", self.current["url"]], check=False)

    def action_new(self) -> None:
        card = self.current if self.current and "sessions" in self.current else None
        if not card:
            self.action_new_unlinked()
            return

        def go(answer: tuple[str, bool, bool] | None) -> None:
            if answer is not None:
                self.start_session(card, answer[0], attach=answer[2], progress=answer[1])

        progress_from = card["status"] if card.get("status") in core.NOT_STARTED and card.get("statusItem") else ""
        self.push_screen(AskPrompt(f"New background session on {card['key']}", progress_from, offer_attach=True), go)

    @work(thread=True)
    def start_session(self, card: dict, prompt: str, attach: bool = False, progress: bool = False) -> None:
        try:
            sid = core.new_session(card["url"], prompt, card["title"])
            self.call_from_thread(self.notify, f"Started {sid} on {card['key']}")
        except Exception as err:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Could not start: {err}", severity="error")
            return
        self.call_from_thread(self.action_refresh)
        if progress:
            try:
                core.start_progress(card)
                self.call_from_thread(self.action_refresh)
            except Exception as err:  # noqa: BLE001
                self.call_from_thread(self.notify, f"Started, but could not move {card['key']} to In Progress: {err}", severity="error")
        if attach:
            try:
                core.attach(sid, card["title"])
            except Exception as err:  # noqa: BLE001
                self.call_from_thread(self.notify, f"Could not attach: {err}", severity="error")

    def action_new_unlinked(self) -> None:
        def go(answer: tuple[str, bool, bool] | None) -> None:
            if answer is not None:
                self.start_unlinked(answer[0], attach=answer[2])

        self.push_screen(AskPrompt("New background session on no issue", required=True, offer_attach=True), go)

    @work(thread=True)
    def start_unlinked(self, prompt: str, attach: bool = False) -> None:
        try:
            sid = core.new_unlinked_session(prompt)
            self.call_from_thread(self.notify, f"Started {sid}")
        except Exception as err:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Could not start: {err}", severity="error")
            return
        self.call_from_thread(self.action_refresh)
        if attach:
            try:
                core.attach(sid)
            except Exception as err:  # noqa: BLE001
                self.call_from_thread(self.notify, f"Could not attach: {err}", severity="error")

    def action_new_issue(self) -> None:
        if not core.ISSUE_REPOS:
            self.notify(f"No repository to file issues in: add an [[issue_repos]] entry to {core.CONFIG / 'config.toml'}.", severity="warning", timeout=10)
            return

        def go(spec: dict | None) -> None:
            if spec:
                self.create_issue(spec)

        self.push_screen(NewIssue(), go)

    @work(thread=True)
    def create_issue(self, spec: dict) -> None:
        try:
            issue = core.create_issue(spec["repo"], spec["title"], spec["body"], spec["assign"])
        except Exception as err:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Could not create the issue: {err}", severity="error")
            return
        self.call_from_thread(self.issue_created, issue)

    def issue_created(self, issue: dict) -> None:
        self.action_refresh()

        def go(answer: tuple[str, bool, bool] | None) -> None:
            if answer is not None:
                self.start_session(issue, answer[0], attach=True)

        question = f"Created {issue['key']}. Start a session on it and attach?  [$text-muted]esc skips[/]"
        self.push_screen(AskPrompt(question), go)

    def action_link(self) -> None:
        sess = self.focused_session()
        if not sess:
            self.notify("Select a session first.")
            return

        def go(target: str | None) -> None:
            ref = core.ref_from_arg(target or "")
            if target and not ref:
                self.notify("Expected an issue URL or owner/repo#N", severity="error")
            elif ref:
                core.link(sess["id"], core.key_of(ref))
                self.notify(f"Linked {sess['id']} to {core.key_of(ref)}")
                self.action_refresh()

        self.push_screen(Ask(f"Link {sess['id']} ({sess['name']}) to issue", "https://github.com/owner/repo/issues/N or owner/repo#N"), go)

    def action_unlink(self) -> None:
        sess = self.focused_session()
        if not sess:
            self.notify("Select a session first.")
            return
        if not core.load_links().get(sess["id"]):
            self.notify("No manual links on this session; links from its first prompt or PRs can't be removed here.")
            return
        core.unlink(sess["id"], None)
        self.notify(f"Removed manual links from {sess['id']}")
        self.action_refresh()

    def action_stop_delete(self) -> None:
        sess = self.focused_session()
        if not sess:
            self.notify("Select a session first: on a card with several, pick one in the sessions panel.")
            return
        if sess.get("isInteractive"):
            self.notify(f"{sess['name']} is interactive; stop it in its own terminal.", severity="warning")
            return

        def go(choice: str | None) -> None:
            if choice:
                self.stop_or_delete(sess, choice)

        self.push_screen(StopOrDelete(sess), go)

    @work(thread=True)
    def stop_or_delete(self, sess: dict, choice: str) -> None:
        try:
            if choice == "stop":
                core.stop_session(sess["id"])
                self.call_from_thread(self.notify, f"Stopped {sess['id']}; attach resumes it")
            else:
                core.delete_session(sess["id"])
                self.call_from_thread(self.notify, f"Deleted {sess['id']}")
        except Exception as err:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Could not {choice} {sess['id']}: {err}", severity="error", timeout=15)
            return
        self.call_from_thread(self.action_refresh)


def run(out=sys.stdout) -> None:
    """Name the terminal tab after the board while it runs, then restore the previous name.

    The xterm title stack (CSI 22/23) saves and restores the name; OSC 1 sets iTerm2's tab title."""
    try:
        core.load_config()
    except ValueError as err:
        sys.exit(f"error: {err}")
    out.write(f"\x1b[22;1t\x1b]1;{BoardApp.TITLE}\x07")
    out.flush()
    try:
        BoardApp().run()
    finally:
        out.write("\x1b[23;1t")
        out.flush()


if __name__ == "__main__":
    run()
