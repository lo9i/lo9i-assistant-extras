"""Plugins share no code, so each one carries its own copy of what its lo9i protocol needs (README.md). This
test tells when two copies of the same code differ: a fix made in one copy and not the others, or a protocol
change that reached only some plugins.

Each group lists the copies of one file. A top-level definition (a function, a class, a constant) that several
copies have must be the same in all of them, docstrings, comments and layout aside; one that only some copies
have counts as a difference too. A difference that's meant to be there goes in ACCEPTED, with why; an accepted
difference that's gone fails too, so the list stays true.

Run from the repository's root (the pre-commit hook does): uv run --no-project --with pytest pytest tests
"""

import ast
import difflib
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The copies of each shared file, by group.
SHARED = {
    "channel client": [
        "communications/chat/telegram/src/lo9i_telegram/daemon.py",
        "communications/chat/slack/src/lo9i_slack/daemon.py",
        "communications/chat/whatsapp/src/lo9i_whatsapp/daemon.py",
    ],
    "channel runner": [
        "communications/chat/telegram/src/lo9i_telegram/runner.py",
        "communications/chat/slack/src/lo9i_slack/runner.py",
        "communications/chat/whatsapp/src/lo9i_whatsapp/runner.py",
    ],
    "agent task folder": [
        "assistant/agents/claude-code/src/claude_code_tasks/task_folder.py",
        "assistant/agents/copilot/src/copilot_tasks/task_folder.py",
    ],
}

# Differences meant to be there: (group, definition) and why.
_NO_MESSAGES = (
    "Copilot's worker takes no messages while it works (lo9i's docs/agent-plugins.md: inbox/ is optional), "
    "so its task folder has no inbox."
)
ACCEPTED = {
    ("agent task folder", "Kind"): _NO_MESSAGES,
    ("agent task folder", "Message"): _NO_MESSAGES,
    ("agent task folder", "Inbox"): _NO_MESSAGES,
    ("agent task folder", "_message"): _NO_MESSAGES,
    ("agent task folder", "Runner"): _NO_MESSAGES,
    ("agent task folder", "run"): _NO_MESSAGES,
    ("agent task folder", "Measured"): (
        "Copilot's CLI doesn't say how full its context is (lo9i's docs/agent-plugins.md: context.json is optional), "
        "so its worker has none to write while it works."
    ),
}


@dataclass(frozen=True)
class Definition:
    shape: str  # Its syntax tree without docstrings, so only what it does is compared.
    source: str


def definitions(path: Path) -> dict[str, Definition]:
    """The file's top-level functions, classes and constants, by name."""
    text = path.read_text()
    found: dict[str, Definition] = {}
    for node in ast.parse(text).body:
        name = _name(node)
        if name is not None:
            source = ast.get_source_segment(text, node) or ""
            found[name] = Definition(ast.dump(_without_docstrings(node)), source)
    return found


def differences(group: str, paths: list[Path]) -> dict[str, str]:
    """Each definition that isn't the same in every copy, with what tells the copies apart."""
    copies = {path: definitions(path) for path in paths}
    names = dict.fromkeys(name for found in copies.values() for name in found)
    found = {name: _difference(name, copies) for name in names}
    return {name: why for name, why in found.items() if why}


def _difference(name: str, copies: dict[Path, dict[str, Definition]]) -> str:
    missing = [_short(path) for path, found in copies.items() if name not in found]
    if missing:
        return f"{name} isn't in {', '.join(missing)}"
    first, *others = copies.items()
    changed = [(path, found[name]) for path, found in others if found[name].shape != first[1][name].shape]
    if not changed:
        return ""
    path, definition = changed[0]
    diff = difflib.unified_diff(
        first[1][name].source.splitlines(), definition.source.splitlines(), _short(first[0]), _short(path), lineterm=""
    )
    return f"{name} differs:\n" + "\n".join(diff)


def _name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name
    target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
    return target.id if isinstance(target, ast.Name) else None


def _without_docstrings(node: ast.AST) -> ast.AST:
    for inner in ast.walk(node):
        body = getattr(inner, "body", None)
        if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and body and _is_text(body[0]):
            inner.body = body[1:] or [ast.Pass()]
    return node


def _is_text(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _short(path: Path) -> str:
    """The plugin and file, as in assistant/agents/copilot: task_folder.py."""
    relative = path.relative_to(ROOT)
    return f"{'/'.join(relative.parts[:3])}: {relative.name}"


@pytest.mark.parametrize("group", SHARED)
def test_the_copies_of_shared_code_are_the_same(group: str):
    paths = [ROOT / path for path in SHARED[group]]
    assert all(path.is_file() for path in paths), f"{group}: a copy listed in SHARED is gone"
    unexpected = {name: why for name, why in differences(group, paths).items() if (group, name) not in ACCEPTED}
    assert not unexpected, f"The copies of the {group} differ. Make them the same, or accept it in ACCEPTED:\n\n" + (
        "\n\n".join(unexpected.values())
    )


def test_every_accepted_difference_is_still_there():
    """So an accepted difference that was made the same again leaves the list."""
    found = {(group, name) for group, files in SHARED.items() for name in differences(group, [ROOT / f for f in files])}
    assert set(ACCEPTED) <= found, f"These no longer differ; take them out of ACCEPTED: {set(ACCEPTED) - found}"


# The pages' script that takes the web app's theme (lo9i's core/mcp/install.py), in each plugin with a page: the
# same file everywhere, since it isn't Python whose definitions could be compared.
THEME_SCRIPTS = [
    "connectivity/remote/hosts/src/hosts/web/static/theme.js",
    "apps/finance/expenses/src/expenses/web/static/theme.js",
    "apps/media/watchlist/src/watchlist/web/static/theme.js",
    "software/factory/factory/src/lo9i_factory/web/static/theme.js",
]


def test_every_page_takes_the_web_apps_theme_with_the_same_script():
    first, *others = [(ROOT / path).read_text() for path in THEME_SCRIPTS]
    assert all(other == first for other in others), "The pages' theme.js copies differ: make them the same."
    pages = {str(p.relative_to(ROOT)).split("/src/")[0] for p in ROOT.glob("*/*/*/src/*/web/templates/base.html")}
    assert pages == {path.split("/src/")[0] for path in THEME_SCRIPTS}, "A page without theme.js, or one gone."
