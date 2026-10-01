"""Loading submissions, and the one move every scheduler makes."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, Iterable, List, Optional

from .decision import Decision
from .errors import SubmissionError
from .observation import Observation
from .scheduler import Scheduler


def fill_idle(decision: Decision, obs: Observation, queue: Iterable[Any]) -> List[Any]:
    """Give every idle cook the next order from ``queue`` that can start.

    ``queue`` may hold orders or ids. Returns whatever was left over, so a
    scheduler that wants to preempt with the rest can carry on from there.
    Nothing here is clever - it is written out because every scheduler starts
    with exactly this loop, and copying it is a fine way to begin.

    An order whose station has no place free is skipped and stays in the
    queue: assigning it would only be refused, and the cook would idle anyway.
    Places are counted as they are spent, so two cooks are never sent to the
    one place at the pass.
    """
    remaining = list(queue)
    free = obs.free_stations()
    for core in obs.idle_cores:
        for index, item in enumerate(remaining):
            order = obs.order(item)
            station = order.station if order is not None else None
            if station is not None and free.get(station, 0) <= 0:
                continue
            decision.assign(core, remaining.pop(index))
            if station is not None:
                free[station] -= 1
            break
        else:
            break
    return remaining


def load_scheduler(spec: str) -> Scheduler:
    """Instantiates a scheduler from a specification string.

    Accepted forms::

        path/to/scheduler.py            the only Scheduler subclass in the file
        path/to/scheduler.py:MyPolicy   a named class in a file
        package.module:MyPolicy         a named class in an importable module
        package.module                  the only Scheduler subclass in a module

    Raises :class:`SubmissionError` with an actionable message on failure.
    """
    target, class_name = _split_spec(spec)
    module = _import_module(target)
    scheduler_class = _select_class(module, class_name, spec)

    try:
        instance = scheduler_class()
    except TypeError as exc:
        raise SubmissionError(
            f"{scheduler_class.__name__} could not be constructed with no arguments: {exc}\n"
            "A submitted scheduler must be instantiable as MyPolicy()."
        ) from exc

    if type(instance).schedule is Scheduler.schedule:
        raise SubmissionError(
            f"{scheduler_class.__name__} does not implement schedule(). "
            f"A scheduler must override schedule(self, obs)."
        )
    if instance.name == "unnamed_scheduler":
        instance.name = scheduler_class.__name__
    return instance


def _split_spec(spec: str):
    """Splits ``target[:ClassName]`` on the last colon, only when what follows
    is an identifier, so ``C:\\work\\scheduler.py`` stays a path."""
    target, separator, class_name = spec.rpartition(":")
    if not separator or not class_name.isidentifier():
        return spec, None
    return target, class_name


def _import_module(target: str):
    path = Path(target)
    if path.suffix == ".py":
        if not path.exists():
            raise SubmissionError(f"no such file: {path}")
        module_name = f"_kitchen_submission_{abs(hash(str(path.resolve())))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise SubmissionError(f"cannot load {path} as a Python module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise SubmissionError(f"{path} raised while being imported: {type(exc).__name__}: {exc}") from exc
        return module
    try:
        return importlib.import_module(target)
    except ImportError as exc:
        raise SubmissionError(
            f"cannot import '{target}'. Give a .py file path or an importable "
            f"module name, optionally followed by ':ClassName'."
        ) from exc


def _select_class(module, class_name: Optional[str], spec: str):
    if class_name:
        scheduler_class = getattr(module, class_name, None)
        if scheduler_class is None:
            raise SubmissionError(f"'{class_name}' was not found in {spec}")
        if not (inspect.isclass(scheduler_class) and issubclass(scheduler_class, Scheduler)):
            raise SubmissionError(f"'{class_name}' is not a Scheduler subclass")
        return scheduler_class

    candidates = [
        value
        for value in vars(module).values()
        if inspect.isclass(value)
        and issubclass(value, Scheduler)
        and value is not Scheduler
        and value.__module__ == module.__name__
    ]
    if not candidates:
        raise SubmissionError(
            f"no Scheduler subclass found in {spec}. Define one:  class MyPolicy(Scheduler): ..."
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(c.__name__ for c in candidates))
        raise SubmissionError(
            f"{spec} defines several schedulers ({names}). "
            f"Say which one, for example '{spec}:{candidates[0].__name__}'."
        )
    return candidates[0]
