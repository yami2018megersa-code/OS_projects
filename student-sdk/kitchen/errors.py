"""Exception types the SDK raises.

Student-facing errors carry actionable messages: the local validator exists so
that a submission fails on the student's machine with an explanation, rather
than on the marking server with a stack trace and a zero.
"""

from __future__ import annotations


class KitchenError(Exception):
    """Base class for everything this package raises."""


class SubmissionError(KitchenError):
    """A submission could not be loaded, or does not implement the interface."""


class PolicyError(KitchenError):
    """A scheduler raised while deciding.

    The runner converts these into counted failures rather than letting them
    escape into the run loop, so a crashing scheduler loses marks instead of
    taking the worker down with it.
    """

    def __init__(self, original: BaseException, tick: int) -> None:
        self.original = original
        self.tick = tick
        super().__init__(f"scheduler raised {type(original).__name__} at tick {tick}: {original}")
