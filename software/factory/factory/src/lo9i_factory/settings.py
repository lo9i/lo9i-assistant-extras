"""The factory's settings (SPEC §4): defaults in code, the user's changes in
the settings table by key. A key set back to its default, or to None, loses
its row, so `changed` lists exactly what the user changed and "reset to
default" is deleting it.

Keys:

- models.<role>: provider:model for each role and the judge.
- prompts.<role>: each role's prompt; the defaults are in prompts/.
- max_rounds, max_fix_loops, max_usd, max_parallel: the limits.
- agent: the coding agent, claude-code or codex.
- run_mode: docker or local (a task may override it).
- editor: a URL that opens a folder, {path} standing for it.
- full_test_minutes: run the full tests after the build too when their last
  run took less (they always run before the final checkpoint).
- repos.<path>: a repository's test_command and base_branch, no default.
- prices.<provider:model>: USD per million tokens for a model of a direct API
  (input, output, and optionally cache_read and cache_write), in place of
  the price table's (prices.py); a model in neither can't be used.
"""

import json
import os
import sqlite3
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

from .errors import ValidationError
from .models import AGENTS, PROVIDERS, ROLES, RUN_MODES
from .repo import now

DEFAULT_MODELS = {
    "architect": "anthropic:claude-opus-5-5",
    "implementer": "anthropic:claude-sonnet-5-5",
    "reviewer": "openai:gpt-5",
    "tester": "anthropic:claude-sonnet-5-5",
    "security": "openai:gpt-5",
    "judge": "anthropic:claude-opus-5-5",
}
# The editor link's default: VS Code.
VSCODE = "vscode://file/{path}"
REPO_KEYS = ("test_command", "base_branch")
PRICE_KEYS = ("input", "output", "cache_read", "cache_write")


@cache
def default_prompt(role: str) -> str:
    return resources.files(__package__).joinpath("prompts", f"{role}.md").read_text().strip()


def defaults() -> dict[str, Any]:
    return {
        **{f"models.{role}": model for role, model in DEFAULT_MODELS.items()},
        **{f"prompts.{role}": default_prompt(role) for role in ROLES},
        "max_rounds": 3,
        "max_fix_loops": 2,
        "max_usd": 10.0,
        "max_parallel": 2,
        "agent": "claude-code",
        "run_mode": "docker",
        "editor": VSCODE,
        "full_test_minutes": 5.0,
    }


@dataclass(frozen=True)
class Repo:
    """A repository's settings."""

    test_command: str = ""
    # The branch tasks start from; "" for the one checked out when they start.
    base_branch: str = ""


@dataclass(frozen=True)
class Settings:
    # provider:model by role.
    models: dict[str, str]
    prompts: dict[str, str]
    # Debate rounds in a stage, and build/review loops, before the judge must decide.
    max_rounds: int
    max_fix_loops: int
    # A task pauses when it has spent this much.
    max_usd: float
    max_parallel: int
    agent: str
    run_mode: str
    editor: str
    full_test_minutes: float
    # By absolute path.
    repos: dict[str, Repo]
    # By provider:model, the user's prices (PRICE_KEYS) over the price table's.
    prices: dict[str, dict[str, float]]
    # The keys the user changed from their defaults (repos and prices aren't defaults).
    changed: list[str]

    def repo(self, path: str) -> Repo:
        return self.repos.get(path, Repo())

    def editor_url(self, path: str) -> str:
        return self.editor.replace("{path}", path)


def repo_path(path: str) -> str:
    """A repository's path as tasks and settings keep it: absolute, ~ expanded."""
    full = os.path.normpath(os.path.expanduser(path.strip()))
    if not os.path.isabs(full):
        raise ValidationError(f"the repository must be an absolute path, got {path!r}")
    return full


def load(conn: sqlite3.Connection) -> Settings:
    base = defaults()
    values, repos, prices = dict(base), {}, {}
    for row in conn.execute("SELECT key, value FROM settings"):
        key, value = row["key"], json.loads(row["value"])
        if key.startswith("repos."):
            repos[key.removeprefix("repos.")] = Repo(**value)
        elif key.startswith("prices."):
            prices[key.removeprefix("prices.")] = value
        elif key in base:  # A key an older version had is left alone.
            values[key] = value
    return Settings(
        models={role: values[f"models.{role}"] for role in ROLES},
        prompts={role: values[f"prompts.{role}"] for role in ROLES},
        max_rounds=values["max_rounds"],
        max_fix_loops=values["max_fix_loops"],
        max_usd=values["max_usd"],
        max_parallel=values["max_parallel"],
        agent=values["agent"],
        run_mode=values["run_mode"],
        editor=values["editor"],
        full_test_minutes=values["full_test_minutes"],
        repos=dict(sorted(repos.items())),
        prices=dict(sorted(prices.items())),
        changed=sorted(k for k in base if values[k] != base[k]),
    )


def update(conn: sqlite3.Connection, changes: dict[str, Any]) -> Settings:
    """Change settings by key; None sets one back to its default, or removes
    a repo's. Checked all first, so one bad value changes nothing."""
    base = defaults()
    checked = {_key(key): _check(_key(key), value, base) for key, value in changes.items()}
    with conn:
        for key, value in checked.items():
            if value is None or value == base.get(key):
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, json.dumps(value), now()),
                )
    return load(conn)


def _key(key: str) -> str:
    """Repos by the same path whichever way it was written."""
    return f"repos.{repo_path(key.removeprefix('repos.'))}" if key.startswith("repos.") else key


def _check(key: str, value: Any, base: dict[str, Any]) -> Any:
    """The value to save for `key`. Raises ValidationError naming what it takes."""
    if not key.startswith(("repos.", "prices.")) and key not in base:
        known = ", ".join([*base, "repos.<path>", "prices.<provider:model>"])
        raise ValidationError(f"no setting {key!r}; settings: {known}")
    if value is None:
        return None
    if key.startswith("models."):
        return _model(key, value)
    if key.startswith("prompts."):
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{key} must be the prompt's text; None resets it to the default")
        return value.strip()
    if key.startswith("repos."):
        return _repo(key, value)
    if key.startswith("prices."):
        return _price(key, value)
    match key:
        case "max_rounds" | "max_parallel":
            return _number(key, value, minimum=1, whole=True)
        case "max_fix_loops":
            return _number(key, value, minimum=0, whole=True)
        case "max_usd":
            if _number(key, value, minimum=0) == 0:
                raise ValidationError("max_usd must be more than 0")
            return float(value)
        case "full_test_minutes":
            return float(_number(key, value, minimum=0))
        case "agent":
            return _choice(key, value, AGENTS)
        case "run_mode":
            return _choice(key, value, RUN_MODES)
        case "editor":
            if not isinstance(value, str) or "{path}" not in value:
                raise ValidationError(f"editor must be a URL with {{path}} for the folder, like {VSCODE}")
            return value.strip()
    raise AssertionError(key)


def _model(key: str, value: Any) -> str:
    provider, _, model = str(value).strip().partition(":")
    if provider not in PROVIDERS or not model.strip():
        raise ValidationError(f"{key} must be provider:model, provider one of {', '.join(PROVIDERS)}; got {value!r}")
    return f"{provider}:{model.strip()}"


def _number(key: str, value: Any, minimum: int, whole: bool = False) -> int | float:
    kinds = (int,) if whole else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or value < minimum:
        raise ValidationError(f"{key} must be a {'whole ' if whole else ''}number from {minimum}, got {value!r}")
    return value


def _choice(key: str, value: Any, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ValidationError(f"{key} must be one of {', '.join(choices)}, got {value!r}")
    return value


def _repo(key: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not set(value) <= set(REPO_KEYS):
        raise ValidationError(f"{key} must be an object with {' and/or '.join(REPO_KEYS)}")
    if not all(isinstance(v, str) for v in value.values()):
        raise ValidationError(f"{key}: {' and '.join(REPO_KEYS)} are text")
    return {k: value.get(k, "").strip() for k in REPO_KEYS}


def _price(key: str, value: Any) -> dict[str, float]:
    """A model's price. The cache's default to the input price, which never
    under-counts what a task spent."""
    provider, _, model = key.removeprefix("prices.").partition(":")
    if provider not in ("anthropic", "openai") or not model:
        raise ValidationError(f"{key}: prices are for anthropic:<model> or openai:<model>; Copilot counts requests")
    if not isinstance(value, dict) or not {"input", "output"} <= set(value) <= set(PRICE_KEYS):
        raise ValidationError(
            f"{key} must be an object with input and output, and optionally cache_read and cache_write"
        )
    for k, v in value.items():
        _number(f"{key}.{k}", v, minimum=0)
    return {k: float(value.get(k, value["input"])) for k in PRICE_KEYS}
