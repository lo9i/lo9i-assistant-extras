"""Tasks handed to a coding agent (Claude Code, Copilot): each runs in a worker process of its own, detached
from the MCP server, so it outlives a restart of the server or of lo9i. The server starts and follows
them through the files the worker writes (tasks.py)."""

from agent_tasks.server import build
from agent_tasks.tasks import Outcome, Task, TaskNotFoundError, TaskState, TaskStore
from agent_tasks.worker import Progress, Runner

__all__ = ["Outcome", "Progress", "Runner", "Task", "TaskNotFoundError", "TaskState", "TaskStore", "build"]
