"""Project-owned slash commands.

Projects declare their commands over the hub API; the hub registers them
with Discord and routes invocations to the declaring project's callback.
The hub knows command *shape* and routing — never what a command means.
See docs/decisions/0003-project-owned-slash-commands.md.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

_TYPE_MAP: dict[str, type] = {
    "integer": int,
    "string": str,
    "boolean": bool,
    "number": float,
}


@dataclass
class CommandSpec:
    name: str
    description: str
    options: list[dict[str, Any]] = field(default_factory=list)


class CommandNameTakenError(RuntimeError):
    """Raised when a command name is already owned by another project."""


class CommandStore:
    """Persisted mapping of callback_url -> declared commands."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._by_project: dict[str, list[CommandSpec]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for url, specs in data.items():
            self._by_project[url] = [CommandSpec(**s) for s in specs]
        logger.info("loaded commands for %d project(s)", len(self._by_project))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {url: [asdict(s) for s in specs] for url, specs in self._by_project.items()}
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(self._path)

    def owner_of(self, name: str) -> str | None:
        for url, specs in self._by_project.items():
            if any(s.name == name for s in specs):
                return url
        return None

    def validate_project_commands(self, callback_url: str, specs: list[CommandSpec]) -> None:
        """Check ownership without changing the currently published routes."""
        names = [spec.name for spec in specs]
        if len(names) != len(set(names)):
            raise ValueError("duplicate command names in one declaration")
        for url, existing in self._by_project.items():
            if url == callback_url:
                continue
            clash = {s.name for s in existing} & {s.name for s in specs}
            if clash:
                raise CommandNameTakenError(
                    f"command name(s) already owned by {url}: {', '.join(sorted(clash))}"
                )

    def set_project_commands(self, callback_url: str, specs: list[CommandSpec]) -> None:
        """Replace a project's set after validation and successful Discord sync."""
        self.validate_project_commands(callback_url, specs)
        previous = self._by_project
        self._by_project = {**previous, callback_url: specs}
        try:
            self._save()
        except Exception:
            self._by_project = previous
            raise

    def all(self) -> dict[str, list[CommandSpec]]:
        return dict(self._by_project)


def build_command(
    spec: CommandSpec,
    invoke: Callable[[CommandSpec, discord.Interaction, dict[str, Any]], Awaitable[None]],
    autocomplete: Callable[
        [CommandSpec, discord.Interaction, str, Any], Awaitable[list[dict[str, Any]]]
    ]
    | None = None,
) -> app_commands.Command:
    """Build a real Discord app command from a declared spec.

    Options become real typed parameters (via a constructed signature), so
    Discord renders them natively — ranges, types, and all.
    """
    params = [
        inspect.Parameter(
            "interaction",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=discord.Interaction,
        )
    ]
    for opt in spec.options:
        ann: Any = _TYPE_MAP.get(opt.get("type", "string"), str)
        min_v, max_v = opt.get("min_value"), opt.get("max_value")
        if min_v is not None or max_v is not None:
            ann = app_commands.Range[ann, min_v, max_v]  # type: ignore[valid-type]
        if opt.get("required", False):
            default = inspect.Parameter.empty
        else:
            ann = ann | None
            default = None
        params.append(
            inspect.Parameter(
                opt["name"],
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=ann,
                default=default,
            )
        )

    async def callback(interaction: discord.Interaction, **kwargs: Any) -> None:
        await invoke(spec, interaction, kwargs)

    callback.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    described = app_commands.describe(
        **{o["name"]: o.get("description", "") for o in spec.options}
    )(callback)
    if autocomplete is not None:
        handlers = {}
        for opt in spec.options:
            if opt.get("autocomplete"):
                handlers[opt["name"]] = _make_autocomplete(spec, opt["name"], autocomplete)
        if handlers:
            described = app_commands.autocomplete(**handlers)(described)
    return app_commands.Command(name=spec.name, description=spec.description, callback=described)


def _make_autocomplete(
    spec: CommandSpec,
    option_name: str,
    autocomplete: Callable[
        [CommandSpec, discord.Interaction, str, Any], Awaitable[list[dict[str, Any]]]
    ],
) -> Callable[[discord.Interaction, Any], Awaitable[list[app_commands.Choice]]]:
    """One autocomplete handler per option, routed to the owning project."""

    async def complete(interaction: discord.Interaction, current: Any) -> list[app_commands.Choice]:
        try:
            raw = await autocomplete(spec, interaction, option_name, current)
        except Exception:
            logger.exception("autocomplete for /%s option %s failed", spec.name, option_name)
            return []
        choices = []
        for c in raw[:25]:
            try:
                choices.append(app_commands.Choice(name=str(c["name"])[:100], value=c["value"]))
            except (KeyError, TypeError):
                logger.warning("bad autocomplete choice from /%s: %r", spec.name, c)
        return choices

    return complete
