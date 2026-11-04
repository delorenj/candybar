"""`candybar create`: the step wizard (Textual).

The app only COLLECTS answers. It returns a `CreateResult` (spec, destination,
register flag, overwrite flag) or None when cancelled; the create command does
the writing and the registering. The review step still calls
`project.plan_write` (no write) so the person sees the exact file text and any
validation error before confirming.

Imported lazily by `candybar.cli.create`: nothing else in candybar pays for
textual.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from rich.syntax import Syntax
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.suggester import SuggestFromList
from textual.widget import Widget
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Select,
    Static,
    Switch,
    TextArea,
)

from candybar.project import (
    CANDYBARS_DIR,
    FILE_SUFFIX,
    PROJECT_FILE,
    Planned,
    Project,
    WriteError,
    load_project,
    plan_write,
)
from candybar.timezones import DEFAULT_TIMEZONE, resolve_timezone
from candybar.tui.schedule import (
    DAYS,
    PRESET_NEEDS,
    PRESETS,
    build_cron,
    describe_schedule,
    next_fires,
)
from candybar.tui.spec import (
    KIND_LABELS,
    KINDS,
    TIMEOUT_KINDS,
    Answers,
    CreateResult,
    SpecError,
    build_spec,
    format_fire,
    host_timezone,
    list_user_units,
    name_error,
    schedule_error,
    timezone_error,
)

STEP_TITLES = ("Basics", "Schedule", "Runner", "Options", "Destination", "Review")
LAST_STEP = len(STEP_TITLES)

# Answers fields that have an inline error line (`#err-<field>`).
ERROR_FIELDS = (
    "name", "tags", "preset", "schedule", "timezone", "command", "workdir", "env",
    "unit", "profile", "job_id", "target", "data", "entrypoint", "timeout", "on_failure", "dest",
)

# The container that holds each runner kind's fields.
_KIND_BOXES = {
    "exec": "box-exec",
    "systemd": "box-systemd",
    "hermes": "box-hermes",
    "n8n": "box-n8n",
    "entrypoint": "box-entrypoint",
}


class CreateApp(App[CreateResult | None]):
    TITLE = "candybar create"
    CSS = """
    #banner { height: 1; padding: 0 1; background: $primary; color: $text; text-style: bold; }
    #progress { height: 1; padding: 0 1; color: $primary; }
    #steps { height: 1fr; }
    .step { padding: 0 2; }
    .field-label { margin-top: 1; text-style: bold; }
    .hint { color: $text-muted; }
    .error { color: $error; text-style: bold; }
    .warn { color: $warning; }
    .good { color: $success; }
    .box { height: auto; }
    #sched-preview { margin-top: 1; height: auto; }
    #review-text { height: auto; margin-top: 1; }
    #nav { height: 3; padding: 0 1; align-horizontal: right; }
    #nav Button { margin-left: 1; }
    #step-error { color: $error; padding: 0 1; height: auto; }
    .row { height: auto; }
    .row Label { padding: 1 1 0 0; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+n", "next", "Next", priority=True),
        Binding("ctrl+b", "back", "Back", priority=True),
        Binding("ctrl+s", "write", "Write", priority=True),
        Binding("ctrl+r", "write_register", "Write & register", priority=True),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        project: Project | None,
        answers: Answers | None = None,
        *,
        dest_choice: str = "default",
        custom_path: str = "",
        overwrite: bool = False,
        unit_loader: Callable[[], list[str]] | None = list_user_units,
        cwd: Path | None = None,
    ) -> None:
        super().__init__()
        self.project = project
        self.a = answers or Answers()
        if not self.a.timezone or self.a.timezone == DEFAULT_TIMEZONE:
            self.a.timezone = host_timezone()
        self.dest_choice = dest_choice if project is not None else "custom"
        self.custom_path = custom_path
        self.start_overwrite = overwrite
        self.unit_loader = unit_loader
        self.cwd = cwd or Path.cwd()
        self.step = 1
        self.planned: Planned | None = None
        self.existing_names: set[str] = set()
        if project is not None:
            try:
                self.existing_names = {d.name for d in load_project(project).defs}
            except (ValueError, OSError):  # a broken sibling file must not stop the wizard
                self.existing_names = set()

    # --- layout ----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="banner")
        yield Static("", id="progress")
        with ContentSwitcher(id="steps", initial="step-1"):
            with VerticalScroll(id="step-1", classes="step"):
                yield from self._basics()
            with VerticalScroll(id="step-2", classes="step"):
                yield from self._schedule()
            with VerticalScroll(id="step-3", classes="step"):
                yield from self._runner()
            with VerticalScroll(id="step-4", classes="step"):
                yield from self._options()
            with VerticalScroll(id="step-5", classes="step"):
                yield from self._destination()
            with VerticalScroll(id="step-6", classes="step"):
                yield from self._review()
        yield Static("", id="step-error")
        with Horizontal(id="nav"):
            yield Button("Back", id="back")
            yield Button("Next", id="next", variant="primary")
            yield Button("Write", id="write", variant="success")
            yield Button("Write & register", id="write-register", variant="primary")
        yield Footer()

    @staticmethod
    def _label(text: str) -> Label:
        return Label(text, classes="field-label")

    @staticmethod
    def _err(field: str) -> Static:
        widget = Static("", id=f"err-{field}", classes="error")
        widget.display = False
        return widget

    def _basics(self) -> ComposeResult:
        a = self.a
        yield self._label("Name")
        yield Input(a.name, placeholder="nightly-backup", id="name")
        yield self._err("name")
        warn = Static("", id="warn-name", classes="warn")
        warn.display = False
        yield warn
        yield Static("Lowercase letters, digits and hyphens. This is the id everything refers to.", classes="hint")
        yield self._label("Description (optional)")
        yield Input(a.description, placeholder="what it does", id="description")
        yield self._label("Tags (optional, comma separated)")
        yield Input(a.tags, placeholder="backup, nightly", id="tags")
        yield self._err("tags")
        if self.project is not None:
            yield Static(f"Project tag {self.project.tag} is added automatically.", classes="hint")

    def _schedule(self) -> ComposeResult:
        a = self.a
        yield self._label("Preset")
        yield Select(
            [(label, value) for value, label in PRESETS],
            value="custom" if a.schedule else "daily",
            allow_blank=False,
            id="preset",
        )
        with Vertical(id="box-time", classes="box"):
            yield self._label("Time (HH:MM, 24h, in the timezone below)")
            yield Input("09:00", placeholder="09:00", id="preset-time")
        with Vertical(id="box-dow", classes="box"):
            yield self._label("Day of week")
            yield Select(
                [(day, str(i)) for i, day in enumerate(DAYS)],
                value="1",
                allow_blank=False,
                id="preset-dow",
            )
        with Vertical(id="box-dom", classes="box"):
            yield self._label("Day of month (1-31)")
            yield Input("1", placeholder="1", id="preset-dom")
        yield self._err("preset")
        yield self._label("Cron expression (5 fields, editable)")
        yield Input(a.schedule or "0 9 * * *", placeholder="m h dom mon dow", id="schedule")
        yield self._err("schedule")
        yield self._label("Timezone (IANA)")
        yield Input(a.timezone, placeholder="America/New_York", id="timezone")
        yield self._err("timezone")
        yield Static("", id="sched-preview")

    def _runner(self) -> ComposeResult:
        a = self.a
        yield self._label("Runner")
        yield Select(
            [(KIND_LABELS[k], k) for k in KINDS],
            value=a.kind or "exec",
            allow_blank=False,
            id="kind",
        )
        with Vertical(id="box-exec", classes="box"):
            yield self._label("Command line")
            yield Input(a.command, placeholder="./scripts/backup.sh --quiet", id="command")
            yield self._err("command")
            yield self._label("Working directory (optional)")
            yield Input(
                a.workdir,
                placeholder="defaults to the project root in project files, else your home",
                id="workdir",
            )
            yield self._err("workdir")
            yield self._label("Environment, one KEY=VAL per line (optional, non-secret)")
            yield TextArea("\n".join(a.env), id="env", tab_behavior="focus", compact=True)
            yield self._err("env")
        with Vertical(id="box-systemd", classes="box"):
            yield self._label("User service unit")
            yield Input(a.unit, placeholder="cache-prune.service", id="unit")
            yield self._err("unit")
            yield Static("", id="unit-hint", classes="hint")
        with Vertical(id="box-hermes", classes="box"):
            yield self._label("Hermes profile")
            yield Input(a.profile, placeholder="33god-pm", id="profile")
            yield self._err("profile")
            yield self._label("Hermes job id (hex id, not the name)")
            yield Input(a.job_id, placeholder="a1b2c3d4e5f6", id="job_id")
            yield self._err("job_id")
            yield Static(
                "The Hermes job must be pinned scheduler=external:\n"
                "hermes cron edit <id> --scheduler external",
                classes="hint",
            )
        with Vertical(id="box-n8n", classes="box"):
            yield self._label("Webhook path or full URL")
            yield Input(a.target, placeholder="candybar/nightly-digest  or  https://n8n.delo.sh/webhook/...", id="target")
            yield self._err("target")
            yield self._label("Static data (optional JSON object)")
            yield Input(a.data, placeholder='{"lane": "ops"}', id="data")
            yield self._err("data")
            yield Static(
                "The workflow must have no Schedule Trigger: candybar is its only clock.",
                classes="hint",
            )
        with Vertical(id="box-entrypoint", classes="box"):
            yield self._label("Script path")
            yield Input(a.entrypoint, placeholder="./run.sh", id="entrypoint")
            yield self._err("entrypoint")
            yield Static("Relative paths resolve against the project root. The file must exist.", classes="hint")
        with Vertical(id="box-timeout", classes="box"):
            yield self._label("Timeout in seconds (optional)")
            yield Input(a.timeout, placeholder="default", id="timeout", type="text")
            yield self._err("timeout")
        with Vertical(id="box-on_failure", classes="box"):
            yield self._label("On failure, run this command (optional)")
            yield Input(a.on_failure, placeholder="ntfy-publish --topic alerts", id="on_failure")
            yield self._err("on_failure")

    def _options(self) -> ComposeResult:
        a = self.a
        yield self._label("Noise")
        with Horizontal(classes="row"):
            yield Switch(a.noise, id="noise")
            yield Label("Mark this candybar as noisy (kept out of the default views)")
        yield self._label("Enabled")
        with Horizontal(classes="row"):
            yield Switch(a.enabled, id="enabled")
            yield Label("Dispatch it on schedule once registered")
        yield Static(
            "Jobs with side effects: register disabled first, check one manual run, "
            "then `candybar resume NAME`.",
            classes="hint",
        )

    def _destination(self) -> ComposeResult:
        yield self._label("Where to write it")
        with RadioSet(id="dest"):
            if self.project is not None:
                yield RadioButton(
                    f"{CANDYBARS_DIR}/<name>{FILE_SUFFIX}  (one file for this candybar)",
                    id="dest-default",
                    value=self.dest_choice == "default",
                )
                yield RadioButton(
                    f"Append to {PROJECT_FILE}  (one file, many candybars)",
                    id="dest-project-file",
                    value=self.dest_choice == "project-file",
                )
            yield RadioButton(
                "Custom path" + ("" if self.project else "  (not inside a project)"),
                id="dest-custom",
                value=self.dest_choice == "custom",
            )
        yield self._label("Custom path")
        yield Input(self.custom_path, placeholder="./<name>.candybar.toml", id="dest-path")
        yield self._label("Overwrite if the file exists")
        with Horizontal(classes="row"):
            yield Switch(self.start_overwrite, id="overwrite")
            yield Label("Only applies to a one-candybar file")
        yield Static("", id="dest-abs", classes="good")
        yield self._err("dest")

    def _review(self) -> ComposeResult:
        yield Static("", id="review-path", classes="good")
        review_error = Static("", id="review-error", classes="error")
        review_error.display = False
        yield review_error
        yield Static("", id="review-text")

    # --- lifecycle ---------------------------------------------------------------

    def on_mount(self) -> None:
        self._sync_kind()
        self._sync_preset()
        self._sync_cron()
        self._refresh_preview()
        self._refresh_name()
        self._refresh_dest()
        self._go(1)
        if self.unit_loader is not None:
            self._load_units()

    @work(thread=True, exclusive=True, group="units")
    def _load_units(self) -> None:
        loader = self.unit_loader
        if loader is None:
            return
        try:
            units = loader()
        except Exception:  # noqa: BLE001 - suggestions are best-effort, never fatal
            units = []
        self.call_from_thread(self._set_units, units)

    def _set_units(self, units: list[str]) -> None:
        field = self.query_one("#unit", Input)
        hint = self.query_one("#unit-hint", Static)
        if units:
            field.suggester = SuggestFromList(units, case_sensitive=False)
            sample = ", ".join(units[:4])
            hint.update(f"{len(units)} user services found (type to complete): {sample}, ...")
        else:
            hint.update("Name of a user service, for example cache-prune.service.")

    # --- reading the widgets -------------------------------------------------------

    def _text(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value.strip()

    def _select(self, widget_id: str) -> str:
        return str(self.query_one(f"#{widget_id}", Select).value)

    def answers(self) -> Answers:
        kind = self._select("kind")
        env = self.query_one("#env", TextArea).text.splitlines()
        return Answers(
            name=self._text("name"),
            description=self._text("description"),
            tags=self._text("tags"),
            schedule=self._text("schedule"),
            timezone=self._text("timezone"),
            kind=kind,
            command=self._text("command"),
            workdir=self._text("workdir"),
            env=env,
            unit=self._text("unit"),
            profile=self._text("profile"),
            job_id=self._text("job_id"),
            target=self._text("target"),
            data=self._text("data"),
            entrypoint=self._text("entrypoint"),
            timeout=self._text("timeout") if kind in TIMEOUT_KINDS else "",
            on_failure=self._text("on_failure") if kind != "entrypoint" else "",
            noise=self.query_one("#noise", Switch).value,
            enabled=self.query_one("#enabled", Switch).value,
        )

    # --- inline errors -------------------------------------------------------------

    def _set_err(self, field: str, message: str | None) -> None:
        try:
            widget = self.query_one(f"#err-{field}", Static)
        except NoMatches:
            return
        widget.update(escape(message) if message else "")
        widget.display = bool(message)

    def _clear_errors(self, step: int) -> None:
        for widget in self.query(f"#step-{step} .error"):
            widget.update("")
            widget.display = False
        self.query_one("#step-error", Static).update("")

    def _show_errors(self, errors: dict[str, str]) -> None:
        stray = []
        for field, message in errors.items():
            if field in ERROR_FIELDS:
                self._set_err(field, message)
            else:
                stray.append(message)
        self.query_one("#step-error", Static).update(escape("; ".join(stray)) if stray else "")

    # --- live reactions ------------------------------------------------------------

    def _refresh_name(self) -> None:
        name = self._text("name")
        self._set_err("name", name_error(name) if name else None)
        warn = self.query_one("#warn-name", Static)
        clash = bool(name) and name in self.existing_names
        warn.update(escape(f"This project already defines {name!r}; writing it will be refused.") if clash else "")
        warn.display = clash

    def _sync_kind(self) -> None:
        kind = self._select("kind")
        for k, box in _KIND_BOXES.items():
            self.query_one(f"#{box}").display = k == kind
        self.query_one("#box-timeout").display = kind in TIMEOUT_KINDS
        self.query_one("#box-on_failure").display = kind != "entrypoint"

    def _sync_preset(self) -> None:
        needs = PRESET_NEEDS[self._select("preset")]
        self.query_one("#box-time").display = "time" in needs
        self.query_one("#box-dow").display = "dow" in needs
        self.query_one("#box-dom").display = "dom" in needs

    def _sync_cron(self) -> None:
        preset = self._select("preset")
        if preset == "custom":
            self._set_err("preset", None)
            return
        try:
            cron = build_cron(
                preset,
                time=self._text("preset-time"),
                dow=self._select("preset-dow"),
                dom=self._text("preset-dom"),
            )
        except ValueError as exc:
            self._set_err("preset", str(exc))
            return
        self._set_err("preset", None)
        field = self.query_one("#schedule", Input)
        if field.value != cron:
            field.value = cron

    def _refresh_preview(self) -> None:
        cron = self._text("schedule")
        tz_name = self._text("timezone")
        problem = schedule_error(cron)
        tz_problem = timezone_error(tz_name)
        self._set_err("schedule", problem)
        self._set_err("timezone", tz_problem)
        preview = self.query_one("#sched-preview", Static)
        if problem or tz_problem:
            preview.update("")
            return
        lines = [f"[b]{escape(describe_schedule(cron))}[/b]  [dim]({escape(tz_name)})[/dim]"]
        try:
            fires = next_fires(cron, tz_name, count=5, now=datetime.now(UTC))
        except (ValueError, KeyError) as exc:
            preview.update(f"[red]{escape(str(exc))}[/red]")
            return
        zone = resolve_timezone(tz_name)
        if fires:
            lines.append("Next fires:")
            lines.extend(f"  {escape(format_fire(f, zone))}" for f in fires)
        else:
            lines.append("[yellow]This expression never fires.[/yellow]")
        preview.update("\n".join(lines))

    def _dest_choice(self) -> str:
        pressed = self.query_one("#dest", RadioSet).pressed_button
        if pressed is None or pressed.id is None:
            return "custom" if self.project is None else "default"
        return {"dest-default": "default", "dest-project-file": "project-file"}.get(pressed.id, "custom")

    def _dest_path(self) -> Path:
        name = self._text("name") or "<name>"
        choice = self._dest_choice()
        if choice == "default" and self.project is not None:
            return self.project.default_file_for(name)
        if choice == "project-file" and self.project is not None:
            return self.project.project_file
        raw = self._text("dest-path")
        if not raw:
            return self.cwd / f"{name}{FILE_SUFFIX}"
        path = Path(raw).expanduser()
        return path if path.is_absolute() else self.cwd / path

    def _refresh_dest(self) -> None:
        try:
            path = self._dest_path()
        except (ValueError, OSError):
            return
        choice = self._dest_choice()
        self.query_one("#dest-abs", Static).update(escape(f"-> {path}"))
        self.query_one("#dest-path", Input).disabled = choice != "custom"

    @on(Input.Changed)
    def _input_changed(self, event: Input.Changed) -> None:
        field = event.input.id
        if field == "name":
            self._refresh_name()
            self._refresh_dest()
        elif field in ("preset-time", "preset-dom"):
            self._sync_cron()
        elif field in ("schedule", "timezone"):
            self._refresh_preview()
        elif field == "dest-path":
            self._refresh_dest()

    @on(Select.Changed)
    def _select_changed(self, event: Select.Changed) -> None:
        field = event.select.id
        if field == "kind":
            self._sync_kind()
        elif field == "preset":
            self._sync_preset()
            self._sync_cron()
        elif field == "preset-dow":
            self._sync_cron()

    @on(RadioSet.Changed)
    def _radio_changed(self, event: RadioSet.Changed) -> None:
        self._refresh_dest()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.step < LAST_STEP and self._is_last_field(event.input):
            self.action_next()
        else:
            self.screen.focus_next()

    def _shown(self, widget: Widget) -> bool:
        return all(node.display for node in widget.ancestors_with_self if isinstance(node, Widget))

    def _is_last_field(self, widget: Input) -> bool:
        fields = [w for w in self.query(f"#step-{self.step} Input") if self._shown(w) and not w.disabled]
        return bool(fields) and fields[-1] is widget

    # --- validation ------------------------------------------------------------------

    def _validate(self, step: int) -> dict[str, str]:
        errors: dict[str, str] = {}
        if step == 1:
            problem = name_error(self._text("name"))
            if problem:
                errors["name"] = problem
        elif step == 2:
            preset_err = self._preset_problem()
            if preset_err:
                errors["preset"] = preset_err
            problem = schedule_error(self._text("schedule"))
            if problem:
                errors["schedule"] = problem
            tz_problem = timezone_error(self._text("timezone"))
            if tz_problem:
                errors["timezone"] = tz_problem
        elif step == 3:
            errors.update(self._runner_problems())
        elif step == 5:
            errors.update(self._dest_problems())
        return errors

    def _preset_problem(self) -> str | None:
        preset = self._select("preset")
        if preset == "custom":
            return None
        try:
            build_cron(
                preset,
                time=self._text("preset-time"),
                dow=self._select("preset-dow"),
                dom=self._text("preset-dom"),
            )
        except ValueError as exc:
            return str(exc)
        return None

    def _runner_problems(self) -> dict[str, str]:
        a = self.answers()
        try:
            spec = build_spec(
                Answers(**{**a.__dict__, "name": a.name or "probe", "schedule": "* * * * *"})
            )
        except SpecError as exc:
            return {exc.field: str(exc)}
        entry = spec.get("entrypoint")
        if entry:
            base = self.project.root if self.project is not None else self.cwd
            path = Path(entry).expanduser()
            path = path if path.is_absolute() else base / path
            if not path.is_file():
                return {"entrypoint": f"not an existing file: {path}"}
        return {}

    def _dest_problems(self) -> dict[str, str]:
        if self._dest_choice() == "custom" and not self._text("dest-path") and not self._text("name"):
            return {"dest": "enter a path"}
        try:
            self._plan()
        except SpecError as exc:
            return {"dest": str(exc)}
        except WriteError as exc:
            return {"dest": str(exc)}
        return {}

    def _plan(self) -> Planned:
        spec = build_spec(self.answers())
        return plan_write(
            spec,
            self._dest_path(),
            project=self.project,
            overwrite=self.query_one("#overwrite", Switch).value,
        )

    # --- navigation ------------------------------------------------------------------

    def _go(self, step: int) -> None:
        self.step = step
        self.query_one("#steps", ContentSwitcher).current = f"step-{step}"
        self.query_one("#banner", Static).update(f" Step {step}/{LAST_STEP}: {STEP_TITLES[step - 1]}")
        bar = "".join("#" if i < step else "-" for i in range(LAST_STEP))
        self.query_one("#progress", Static).update(f" [{bar}] {step}/{LAST_STEP}")
        self.query_one("#step-error", Static).update("")
        self.query_one("#back", Button).disabled = step == 1
        review = step == LAST_STEP
        self.query_one("#next", Button).display = not review
        self.query_one("#write", Button).display = review
        self.query_one("#write-register", Button).display = review
        if step == 5:
            self._refresh_dest()
        if review:
            self._render_review()
        else:
            focusables = [
                w for w in self.query(f"#step-{step} Input, #step-{step} Select")
                if self._shown(w) and not w.disabled
            ]
            if focusables:
                focusables[0].focus()

    def _render_review(self) -> None:
        text = self.query_one("#review-text", Static)
        path = self.query_one("#review-path", Static)
        error = self.query_one("#review-error", Static)
        self.planned = None
        try:
            self.planned = self._plan()
        except (SpecError, WriteError) as exc:
            error.update(escape(str(exc)))
            error.display = True
            path.update("")
            text.update("")
        else:
            error.update("")
            error.display = False
            verb = "Append to" if self.planned.appended else "Write"
            path.update(escape(f"{verb} {self.planned.path}"))
            text.update(Syntax(self.planned.text, "toml", theme="ansi_dark", word_wrap=True, background_color="default"))
        ok = self.planned is not None
        self.query_one("#write", Button).disabled = not ok
        self.query_one("#write-register", Button).disabled = not ok

    def action_next(self) -> None:
        if self.step >= LAST_STEP:
            return
        self._clear_errors(self.step)
        errors = self._validate(self.step)
        if errors:
            self._show_errors(errors)
            self.bell()
            return
        self._go(self.step + 1)

    def action_back(self) -> None:
        if self.step > 1:
            self._go(self.step - 1)

    def _finish(self, register: bool) -> None:
        if self.step != LAST_STEP:
            self.notify("Finish the earlier steps first (ctrl+n).", severity="warning")
            return
        if self.planned is None:
            self.bell()
            return
        self.exit(
            CreateResult(
                spec=build_spec(self.answers()),
                dest=self.planned.path,
                register=register,
                overwrite=self.query_one("#overwrite", Switch).value,
            )
        )

    def action_write(self) -> None:
        self._finish(False)

    def action_write_register(self) -> None:
        self._finish(True)

    def action_cancel(self) -> None:
        self.exit(None)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        event.stop()
        match event.button.id:
            case "next":
                self.action_next()
            case "back":
                self.action_back()
            case "write":
                self._finish(False)
            case "write-register":
                self._finish(True)


def run_create_wizard(
    project: Project | None,
    answers: Answers | None = None,
    *,
    dest_choice: str = "default",
    custom_path: str = "",
    overwrite: bool = False,
) -> CreateResult | None:
    """Run the wizard on the real terminal; the result, or None if cancelled."""
    app = CreateApp(
        project, answers, dest_choice=dest_choice, custom_path=custom_path, overwrite=overwrite
    )
    return app.run()


__all__ = ["STEP_TITLES", "CreateApp", "run_create_wizard"]
