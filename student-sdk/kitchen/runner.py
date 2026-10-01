"""Playing runs and evaluating over seed sets.

The failure policy is the engine's: a scheduler that raises, returns nonsense
or overruns its deadline is reported to the engine as having done so, and the
engine counts it, ignores the decision and carries on - or forfeits the run if
it keeps happening. Nothing here decides a penalty.

Evaluation of a Python scheduler parallelises across *processes*: a Python
scheduler holds the interpreter lock, so threads would serialise. Each worker
re-imports the submission for every run, so no state survives between runs.
"""

from __future__ import annotations

import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Union

from . import _engine as _native
from .decision import as_decision_dict
from .env import KitchenEnv
from .helpers import load_scheduler
from .scheduler import Scheduler

SchedulerSpec = Union[Scheduler, str]


def baseline_names() -> tuple:
    """The reference schedulers, asked of the engine rather than listed here."""
    return tuple(_native.baseline_names())


def describe_baseline(name: str) -> str:
    return _native.describe_baseline(name)


def baseline_aliases() -> dict:
    """The textbook name of each reference scheduler, as ``{alias: name}``.

    The references are named after brigade roles, but a student reading a
    scheduling chapter has ``sjf`` in front of them, so the textbook names go
    on working wherever a scheduler is named.
    """
    return dict(_native.baseline_aliases())


def canonical_baseline(name: str) -> str:
    """The kitchen name a reference answers to: ``sjf`` -> ``maitre_d``.

    A kitchen name, or a name that is not a reference at all, comes back
    unchanged.
    """
    return _native.canonical_baseline(name)


def is_baseline(spec: Any) -> bool:
    return isinstance(spec, str) and canonical_baseline(spec) in baseline_names()


def is_offered(spec: str) -> bool:
    """Whether a typed name is a scheduler this package can run: a path, a
    module, or a reference scheduler."""
    if any(mark in spec for mark in ("/", "\\", ".", ":")):
        return True
    return canonical_baseline(spec) in baseline_names()


@dataclass
class RunOutcome:
    """The result of one run."""

    result: Dict[str, Any]

    @property
    def score(self) -> float:
        return float(self.result["score"]["total"])

    @property
    def served(self) -> int:
        return int(self.result["metrics"]["served"])

    @property
    def abandoned(self) -> int:
        return int(self.result["metrics"]["abandoned"])

    @property
    def replay_path(self) -> Optional[str]:
        return self.result.get("replay_path")

    def summary(self) -> str:
        m = self.result["metrics"]
        return (
            f"{self.result['policy']} seed {self.result['seed']}: score {self.score:.1f}, "
            f"{m['served']} served, {m['abandoned']} abandoned, "
            f"{m['context_switches']} switches ({self.result['ticks']} ticks, "
            f"{self.result['terminal_reason']})"
        )


def _resolve(spec: SchedulerSpec) -> Optional[Scheduler]:
    if isinstance(spec, Scheduler):
        return spec
    if is_baseline(spec):
        return None
    return load_scheduler(spec)


def play(
    scheduler: SchedulerSpec,
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
    run_id: Optional[str] = None,
) -> RunOutcome:
    """Runs one service and returns its record.

    ``scheduler`` may be a live :class:`Scheduler`, a ``"file.py[:Class]"``
    specification, or the name of a reference scheduler such as ``"srtf"``.
    """
    controller = _resolve(scheduler)
    if controller is None:
        result = _native.play_baseline(
            scheduler,
            seed=seed,
            config_path=config_path,
            cores=cores,
            switch_cost=switch_cost,
            max_ticks=max_ticks,
            known_durations=known_durations,
            record_replay=record_replay,
            run_id=run_id,
        )
        return RunOutcome(result)

    env = KitchenEnv(
        seed,
        config_path=config_path,
        cores=cores,
        switch_cost=switch_cost,
        max_ticks=max_ticks,
        known_durations=known_durations,
        record_replay=record_replay,
        record_debug=record_debug,
        per_decision_timeout_ms=per_decision_timeout_ms,
        enforce_deadline=enforce_deadline,
        policy_name=controller.name,
        policy_version=str(controller.version),
        run_id=run_id,
    )
    try:
        try:
            controller.reset(env.seed)
        except BaseException as exc:  # noqa: BLE001 - a bad reset is a failure, not a crash
            traceback.print_exc()
            _ = exc
        obs = env.observation()
        while not env.finished:
            started = time.perf_counter()
            error: Optional[str] = None
            payload = None
            try:
                payload = as_decision_dict(controller.schedule(obs))
            except BaseException as exc:  # noqa: BLE001 - contain everything a submission does
                error = f"{type(exc).__name__}: {exc}"
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            obs, _done = env.step(payload, elapsed_ms, error)
        result = env.result()
        try:
            controller.on_run_end(result)
        except BaseException:  # noqa: BLE001
            pass
        return RunOutcome(result)
    finally:
        env.close()


# ---------------------------------------------------------------------------
# Seed sets
# ---------------------------------------------------------------------------


def _worker(spec: str, seed: int, options: Dict[str, Any]) -> Dict[str, Any]:
    """One run in a worker process. The submission is imported afresh."""
    try:
        return play(spec, seed, **options).result
    except Exception as exc:  # noqa: BLE001 - the row must come back whatever happened
        return {
            "run_id": f"{spec}-{seed}",
            "policy": spec,
            "policy_version": "?",
            "seed": seed,
            "ticks": 0,
            "terminal_reason": "forfeit",
            "forfeited": True,
            "score": {"completion": 0, "response": 0, "turnaround": 0, "slowdown": 0,
                      "switching": 0, "fairness": 0, "total": 0.0},
            "metrics": {},
            "platform_error": f"{type(exc).__name__}: {exc}",
            "failures": [traceback.format_exc()[-1500:]],
        }


def evaluate(
    scheduler: SchedulerSpec,
    seeds: Iterable[int] = range(1000, 1020),
    *,
    config_path: Optional[str] = None,
    cores: Optional[int] = None,
    switch_cost: Optional[int] = None,
    max_ticks: Optional[int] = None,
    known_durations: Optional[bool] = None,
    workers: Optional[int] = None,
    replay_dir: Optional[str] = None,
    per_decision_timeout_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """Evaluates a scheduler over a seed set and returns a report.

    A reference scheduler runs natively, in parallel, inside the engine. A
    Python scheduler runs in a process pool. Both produce the same report
    shape, summarised by the engine's own aggregator.
    """
    seeds = [int(s) for s in seeds]
    started = time.perf_counter()
    if is_baseline(scheduler):
        report = _native.evaluate_baseline(
            scheduler,
            seeds,
            config_path=config_path,
            cores=cores,
            switch_cost=switch_cost,
            max_ticks=max_ticks,
            known_durations=known_durations,
            workers=workers,
            replay_dir=replay_dir,
        )
        return report

    if isinstance(scheduler, Scheduler):
        raise TypeError(
            "evaluate() needs a specification string (a .py path or module) for a Python "
            "scheduler, because runs are played in worker processes"
        )
    spec = str(scheduler)
    # Load once here, so a broken submission is one error rather than N.
    controller = load_scheduler(spec)

    options: Dict[str, Any] = {
        "config_path": config_path,
        "cores": cores,
        "switch_cost": switch_cost,
        "max_ticks": max_ticks,
        "known_durations": known_durations,
        "per_decision_timeout_ms": per_decision_timeout_ms,
        "record_debug": False,
    }
    if workers is None:
        workers = max(1, min(len(seeds), os.cpu_count() or 1))
    workers = max(1, workers)

    results: List[Dict[str, Any]] = []
    if workers == 1 or len(seeds) == 1:
        for seed in seeds:
            run_options = dict(options)
            if replay_dir:
                run_options["record_replay"] = os.path.join(replay_dir, f"{controller.name}-{seed}.rep")
            results.append(_worker(spec, seed, run_options))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for seed in seeds:
                run_options = dict(options)
                if replay_dir:
                    run_options["record_replay"] = os.path.join(replay_dir, f"{controller.name}-{seed}.rep")
                futures[pool.submit(_worker, spec, seed, run_options)] = seed
            by_seed: Dict[int, Dict[str, Any]] = {}
            for future in as_completed(futures):
                by_seed[futures[future]] = future.result()
            results = [by_seed[s] for s in seeds]

    wall_ms = (time.perf_counter() - started) * 1000.0
    complete = [r for r in results if not r.get("platform_error")]
    summary = _native.summarise_results(complete) if complete else {}
    config = _native.load_config(config_path, cores, switch_cost, max_ticks, known_durations)
    return {
        "policy": controller.name,
        "policy_version": str(controller.version),
        "engine_version": _native.ENGINE_VERSION,
        "config_hash": config["config_hash"],
        "config_name": config["kitchen"]["name"],
        "seeds": seeds,
        "workers": workers,
        "wall_time_ms": wall_ms,
        "summary": summary,
        "results": results,
    }
