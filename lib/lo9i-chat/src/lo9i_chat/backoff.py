"""How long to wait before reconnecting to the daemon, longer after each failure in a row."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Backoff:
    first: float = 1.0
    most: float = 30.0
    # Failures in a row before giving up; None keeps trying.
    attempts: int | None = None

    def delay(self, failures: int) -> float:
        """Seconds to wait after the `failures`-th failure in a row (counted from 1)."""
        return min(self.first * 2 ** max(failures - 1, 0), self.most)

    def gives_up(self, failures: int) -> bool:
        return self.attempts is not None and failures >= self.attempts
