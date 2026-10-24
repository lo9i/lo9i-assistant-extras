"""The engine (SPEC §3): the debate a stage runs and the judge's decision
(debate.py), in the messages of schemas.py, read by the team as thread.py
writes it. Every call goes through the cost meter (meter.py)."""

from .debate import Brief, StoppedError, debate, next_step, start

__all__ = ["Brief", "StoppedError", "debate", "next_step", "start"]
