import pytest

from lo9i_factory import db, settings
from lo9i_factory.errors import ValidationError
from lo9i_factory.models import ROLES


def test_the_defaults(conn):
    s = settings.load(conn)
    assert set(s.models) == set(ROLES) and set(s.prompts) == set(ROLES)
    assert all(m.split(":")[0] in ("anthropic", "openai") for m in s.models.values())
    assert {m.split(":")[0] for m in s.models.values()} == {"anthropic", "openai"}  # A team on both.
    assert all(len(p) > 100 for p in s.prompts.values())
    assert (s.max_rounds, s.max_fix_loops, s.max_parallel, s.full_test_minutes) == (3, 2, 2, 5.0)
    assert s.max_usd > 0
    assert (s.agent, s.run_mode) == ("claude-code", "docker")
    assert s.editor_url("/work/repo") == "vscode://file//work/repo"
    assert s.repos == {} and s.prices == {} and s.changed == []


def test_changes_are_saved(tmp_path):
    path = str(tmp_path / "factory.db")
    settings.update(
        db.connect(path),
        {"models.judge": "openai:gpt-5", "max_rounds": 5, "agent": "codex", "run_mode": "local", "max_usd": 2},
    )
    s = settings.load(db.connect(path))
    assert (s.models["judge"], s.max_rounds, s.agent, s.run_mode, s.max_usd) == (
        "openai:gpt-5",
        5,
        "codex",
        "local",
        2.0,
    )
    assert s.changed == ["agent", "max_rounds", "max_usd", "models.judge", "run_mode"]


def test_none_or_the_default_resets_a_setting(conn):
    settings.update(conn, {"prompts.tester": "Only run the fast tests.", "max_rounds": 4, "full_test_minutes": 10})
    assert settings.load(conn).prompts["tester"] == "Only run the fast tests."

    s = settings.update(conn, {"prompts.tester": None, "max_rounds": 3, "full_test_minutes": 5})
    assert s.prompts["tester"] == settings.default_prompt("tester") and s.max_rounds == 3
    assert s.changed == []
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_each_repo_has_its_test_command_and_base_branch(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    s = settings.update(conn, {"repos.~/code/app/": {"test_command": "uv run pytest", "base_branch": "main"}})
    repo = str(tmp_path / "code" / "app")
    assert s.repo(repo) == settings.Repo("uv run pytest", "main")
    assert s.repo("/elsewhere") == settings.Repo()
    assert s.changed == []  # Repos have no default to differ from.

    assert settings.update(conn, {f"repos.{repo}": None}).repos == {}


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"colour": "red"}, "no setting 'colour'; settings: models.architect"),
        ({"models.manager": "openai:gpt-5"}, "no setting 'models.manager'"),
        ({"models.architect": "mistral:large"}, "provider one of anthropic, openai, copilot"),
        ({"models.architect": "openai:"}, "must be provider:model"),
        ({"prompts.judge": "  "}, "the prompt's text"),
        ({"max_rounds": 0}, "max_rounds must be a whole number from 1"),
        ({"max_rounds": True}, "whole number"),
        ({"max_rounds": 2.5}, "whole number"),
        ({"max_fix_loops": -1}, "from 0"),
        ({"max_usd": 0}, "more than 0"),
        ({"max_usd": "5"}, "max_usd must be a number"),
        ({"agent": "aider"}, "agent must be one of claude-code, codex"),
        ({"run_mode": "vm"}, "run_mode must be one of docker, local"),
        ({"editor": "cursor://"}, "with {path}"),
        ({"repos.code/app": {"test_command": "make"}}, "must be an absolute path"),
        ({"repos./code/app": {"lint": "make lint"}}, "test_command and/or base_branch"),
        ({"repos./code/app": {"test_command": 3}}, "are text"),
        ({"prices.copilot:gpt-5": {"input": 1, "output": 2}}, "Copilot counts requests"),
        ({"prices.openai": {"input": 1, "output": 2}}, "anthropic:<model> or openai:<model>"),
        ({"prices.openai:gpt-9": {"input": 1}}, "must be an object with input and output"),
        ({"prices.openai:gpt-9": {"input": 1, "output": -2}}, "prices.openai:gpt-9.output must be a number from 0"),
    ],
)
def test_bad_values_say_what_they_take(conn, changes, error):
    with pytest.raises(ValidationError) as e:
        settings.update(conn, changes)
    assert error in str(e.value)


def test_one_bad_value_changes_nothing(conn):
    with pytest.raises(ValidationError):
        settings.update(conn, {"max_rounds": 6, "agent": "aider"})
    assert settings.load(conn).max_rounds == 3


def test_a_models_price_takes_the_input_price_for_its_cache(conn):
    s = settings.update(conn, {"prices.openai:gpt-9": {"input": 2, "output": 8, "cache_read": 0.2}})
    assert s.prices == {"openai:gpt-9": {"input": 2.0, "output": 8.0, "cache_read": 0.2, "cache_write": 2.0}}
    assert s.changed == []
    assert settings.update(conn, {"prices.openai:gpt-9": None}).prices == {}


def test_editors_other_than_vs_code(conn):
    s = settings.update(conn, {"editor": "cursor://file/{path}"})
    assert s.editor_url("/w/factory-1") == "cursor://file//w/factory-1"
