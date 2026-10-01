"""Student SDK for Short Order, the kitchen scheduling platform.

Write a scheduler by subclassing :class:`Scheduler` and implementing
``schedule``, which is called at every scheduling point and decides which
order each cook should be working on::

    from kitchen import Scheduler, Decision, fill_idle

    class MyPolicy(Scheduler):
        def schedule(self, obs):
            d = Decision()
            fill_idle(d, obs, obs.ready)      # first come, first served
            return d

Then run it::

    python -m kitchen.cli play my_scheduler.py

Everything authoritative - the tick, the context switch, the patience rule,
the metrics, the score, the failure policy - lives in the compiled engine.
This package is a convenience layer over it, so a run here obeys exactly the
rules the marking server applies.
"""

from __future__ import annotations

# The compiled engine first: everything else depends on it, and a missing
# extension should produce a build instruction rather than a cascade of
# confusing import errors from the modules that use it.
try:
    from . import _engine
except ImportError as exc:  # pragma: no cover - only when the wheel is unbuilt
    raise ImportError(
        "The compiled kitchen engine (kitchen._engine) is not importable.\n"
        "Build it with:  maturin develop --release\n"
        "or use the student distribution, which includes it."
    ) from exc

from ._engine import ENGINE_VERSION, load_config, read_replay
from .decision import Decision
from .env import KitchenEnv
from .errors import KitchenError, PolicyError, SubmissionError
from .helpers import fill_idle, load_scheduler
from .observation import Core, Observation, Order, Reason, Step
from .runner import (
    RunOutcome,
    baseline_aliases,
    baseline_names,
    canonical_baseline,
    describe_baseline,
    evaluate,
    play,
)
from .scheduler import FunctionScheduler, Scheduler
from .validate import PUBLIC_SEEDS, validate

__version__ = ENGINE_VERSION

__all__ = [
    "Core",
    "Decision",
    "ENGINE_VERSION",
    "FunctionScheduler",
    "KitchenEnv",
    "KitchenError",
    "Observation",
    "Order",
    "PUBLIC_SEEDS",
    "PolicyError",
    "Reason",
    "RunOutcome",
    "Scheduler",
    "Step",
    "SubmissionError",
    "__version__",
    "baseline_aliases",
    "baseline_names",
    "canonical_baseline",
    "describe_baseline",
    "evaluate",
    "fill_idle",
    "load_config",
    "load_scheduler",
    "play",
    "read_replay",
    "validate",
]
