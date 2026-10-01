"""A run of the kitchen, one scheduling point at a time.

Thin wrapper over the compiled engine's ``KitchenEnv``: it turns the engine's
dictionaries into :class:`~kitchen.observation.Observation` objects and your
:class:`~kitchen.decision.Decision` back into the dictionary the engine reads.
Nothing authoritative lives here.

    with KitchenEnv(seed=1234) as env:
        obs = env.observation()
        while not env.finished:
            obs, done = env.step(my_scheduler.schedule(obs))
        result = env.result()
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from . import _engine as _native
from .decision import as_decision_dict
from .observation import Observation


class KitchenEnv:
    def __init__(
        self,
        seed: Optional[int] = None,
        *,
        config_path: Optional[str] = None,
        cores: Optional[int] = None,
        switch_cost: Optional[int] = None,
        max_ticks: Optional[int] = None,
        known_durations: Optional[bool] = None,
        record_replay: Optional[str] = None,
        record_debug: bool = True,
        per_decision_timeout_ms: Optional[float] = None,
        enforce_deadline: bool = True,
        policy_name: str = "scheduler",
        policy_version: str = "1",
        run_id: Optional[str] = None,
    ) -> None:
        self._env = _native.KitchenEnv(
            seed=seed,
            config_path=config_path,
            cores=cores,
            switch_cost=switch_cost,
            max_ticks=max_ticks,
            known_durations=known_durations,
            record_replay=record_replay,
            record_debug=record_debug,
            per_decision_timeout_ms=per_decision_timeout_ms,
            enforce_deadline=enforce_deadline,
            policy_name=policy_name,
            policy_version=policy_version,
            run_id=run_id,
        )
        self._closed = False

    def observation(self) -> Observation:
        return Observation(self._env.observation())

    def step(
        self,
        decision: Any = None,
        elapsed_ms: float = 0.0,
        error: Optional[str] = None,
    ) -> Tuple[Observation, bool]:
        """Submits a decision and advances to the next scheduling point.

        ``decision`` may be a :class:`Decision`, a dict, or ``None``. Pass
        ``error`` to report that the scheduler raised; the decision is then
        ignored and the failure counted.
        """
        payload = None
        if error is None:
            try:
                payload = as_decision_dict(decision)
            except TypeError as exc:
                error = str(exc)
        raw, done = self._env.step(payload, float(elapsed_ms), error)
        return Observation(raw), done

    @property
    def finished(self) -> bool:
        return bool(self._env.finished)

    @property
    def tick(self) -> int:
        return int(self._env.tick)

    @property
    def seed(self) -> int:
        return int(self._env.seed)

    @property
    def cores(self) -> int:
        return int(self._env.cores)

    @property
    def max_ticks(self) -> int:
        return int(self._env.max_ticks)

    @property
    def config_hash(self) -> str:
        return str(self._env.config_hash)

    @property
    def engine_version(self) -> str:
        return str(self._env.engine_version)

    def config(self) -> Dict[str, Any]:
        return self._env.config()

    def workload(self):
        """Every order of this run, arrived or not. Staff tooling; a scheduler
        that reads this is cheating and the marker's does not."""
        return self._env.workload()

    def result(self) -> Dict[str, Any]:
        """Finishes the run if needed, writes the replay, returns the record."""
        return self._env.result()

    def close(self) -> None:
        if not self._closed:
            self._env.close()
            self._closed = True

    def __enter__(self) -> "KitchenEnv":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return repr(self._env)
