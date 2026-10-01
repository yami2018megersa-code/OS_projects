"""What your scheduler is shown.

The engine hands over a plain dictionary at every scheduling point; this
module wraps it in small typed objects so that ``obs.ready``, ``order.time_left``
and ``core.is_idle`` read the way they sound. Nothing here computes anything
the engine did not already report, except :meth:`Observation.estimate_remaining`,
which is a convenience you are welcome to replace.

Everything is in ticks. ``obs.time`` is now.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class Step:
    """One step of a recipe: ``work`` needs a cook, ``wait`` is the oven."""

    __slots__ = ("label", "kind", "station", "duration", "remaining", "done")

    def __init__(self, data: Dict[str, Any]) -> None:
        self.label: str = data["label"]
        self.kind: str = data["kind"]
        #: The station this step needs a place at. ``None`` for a wait step,
        #: which needs no cook and so can hold nothing.
        self.station: Optional[str] = data.get("station")
        #: ``None`` when durations are hidden and the step is not finished.
        self.duration: Optional[int] = data.get("duration")
        self.remaining: Optional[int] = data.get("remaining")
        self.done: bool = bool(data.get("done", False))

    @property
    def is_work(self) -> bool:
        return self.kind == "work"

    @property
    def is_wait(self) -> bool:
        return self.kind == "wait"

    def __repr__(self) -> str:
        left = "?" if self.remaining is None else self.remaining
        return f"Step({self.label}, {self.kind}, remaining={left})"


class Order:
    """A process. Live orders are ``ready``, ``running`` or ``blocked``."""

    __slots__ = (
        "id", "recipe", "priority", "arrival", "deadline", "time_left", "state", "core",
        "step", "station", "steps", "work_remaining", "work_done", "waited", "started",
        "preemptions", "dispatches",
    )

    def __init__(self, data: Dict[str, Any]) -> None:
        self.id: int = data["id"]
        self.recipe: str = data["recipe"]
        #: 1 regular, 2 hurried, 3 VIP. The engine does nothing with it.
        self.priority: int = data["priority"]
        self.arrival: int = data["arrival"]
        #: The tick the customer walks out.
        self.deadline: int = data["deadline"]
        #: ``deadline - now``.
        self.time_left: int = data["time_left"]
        self.state: str = data["state"]
        #: The cook holding it, if running.
        self.core: Optional[int] = data.get("core")
        #: Index of the current step in ``steps``.
        self.step: int = data["step"]
        #: The station the step it is on right now needs. Assigning this order
        #: is refused unless that station has a place free - see
        #: :meth:`Observation.can_start`.
        self.station: Optional[str] = data.get("station")
        self.steps: List[Step] = [Step(s) for s in data["steps"]]
        #: Ticks of work still needed, or ``None`` when durations are hidden.
        self.work_remaining: Optional[int] = data.get("work_remaining")
        self.work_done: int = data["work_done"]
        #: Ticks spent on the rail so far.
        self.waited: int = data["waited"]
        #: The tick a cook first worked on it, if one has.
        self.started: Optional[int] = data.get("started")
        self.preemptions: int = data["preemptions"]
        self.dispatches: int = data["dispatches"]

    @property
    def is_ready(self) -> bool:
        return self.state == "ready"

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    @property
    def is_blocked(self) -> bool:
        return self.state == "blocked"

    @property
    def has_started(self) -> bool:
        return self.started is not None

    @property
    def current_step(self) -> Optional[Step]:
        return self.steps[self.step] if self.step < len(self.steps) else None

    @property
    def steps_left(self) -> List[Step]:
        return self.steps[self.step:]

    @property
    def work_until_wait(self) -> Optional[int]:
        """Ticks of work before the next oven step (or the end), if known."""
        total = 0
        for step in self.steps_left:
            if step.is_wait:
                break
            if step.remaining is None:
                return None
            total += step.remaining
        return total

    def __repr__(self) -> str:
        left = "?" if self.work_remaining is None else self.work_remaining
        return (
            f"Order({self.id}, {self.recipe}, {self.state}, work_remaining={left}, "
            f"time_left={self.time_left})"
        )


class Core:
    """A cook."""

    __slots__ = ("id", "state", "order", "station", "switch_remaining", "running_for")

    def __init__(self, data: Dict[str, Any]) -> None:
        self.id: int = data["id"]
        #: ``idle``, ``switching`` or ``working``.
        self.state: str = data["state"]
        self.order: Optional[int] = data.get("order")
        #: The station this cook is holding a place at. Taking its order away
        #: hands that place back.
        self.station: Optional[str] = data.get("station")
        #: Ticks of context switch still to pay.
        self.switch_remaining: int = data["switch_remaining"]
        #: Consecutive ticks of work on the current order.
        self.running_for: int = data["running_for"]

    @property
    def is_idle(self) -> bool:
        return self.state == "idle"

    @property
    def is_switching(self) -> bool:
        return self.state == "switching"

    @property
    def is_working(self) -> bool:
        return self.state == "working"

    @property
    def is_busy(self) -> bool:
        return self.order is not None

    def __repr__(self) -> str:
        return f"Core({self.id}, {self.state}, order={self.order})"


class Station:
    """A place in the kitchen that only so many cooks fit at.

    A work step names the station it happens at, and a cook holds one of that
    station's places for as long as it holds the order. That makes a station a
    lock with :attr:`capacity` permits: two idle cooks and one free place at
    the pass is one dispatch, not two.
    """

    __slots__ = ("name", "label", "capacity", "busy", "free", "cores")

    def __init__(self, data: Dict[str, Any]) -> None:
        self.name: str = data["name"]
        #: What the viewer calls it.
        self.label: str = data.get("label", data["name"])
        #: How many cooks fit at once.
        self.capacity: int = data["capacity"]
        #: How many are there now.
        self.busy: int = data["busy"]
        #: ``capacity - busy``. Above zero means an order needing this station
        #: can be started.
        self.free: int = data["free"]
        #: The cooks standing at it.
        self.cores: List[int] = list(data.get("cores", []))

    @property
    def is_full(self) -> bool:
        return self.free <= 0

    def __repr__(self) -> str:
        return f"Station({self.name}, {self.busy}/{self.capacity})"


class Reason:
    """Why you are being called: the events since your last decision."""

    __slots__ = ("kinds", "arrived", "unblocked", "blocked", "served", "abandoned",
                 "bumped", "timer")

    def __init__(self, data: Dict[str, Any]) -> None:
        #: "start", "arrived", "blocked", "unblocked", "served", "abandoned", "timer".
        self.kinds: List[str] = list(data.get("kinds", []))
        self.arrived: List[int] = list(data.get("arrived", []))
        self.unblocked: List[int] = list(data.get("unblocked", []))
        self.blocked: List[int] = list(data.get("blocked", []))
        self.served: List[int] = list(data.get("served", []))
        self.abandoned: List[int] = list(data.get("abandoned", []))
        #: Orders sent back to the rail because the station their next step
        #: needed was full. Their cook is idle and they are ready again.
        self.bumped: List[int] = list(data.get("bumped", []))
        self.timer: bool = bool(data.get("timer", False))

    def __contains__(self, kind: str) -> bool:
        return kind in self.kinds

    @property
    def is_start(self) -> bool:
        return "start" in self.kinds

    def __repr__(self) -> str:
        return f"Reason({', '.join(self.kinds)})"


class Kitchen:
    #: No `name`. The profile a run is on is not something a scheduler gets to
    #: see: a scheduler that can name the workload can look up what to do
    #: instead of working it out, and the point of the exercise is the second
    #: one. Everything here is a property you can act on.
    __slots__ = ("cores", "switch_cost", "max_ticks", "tick_ms", "known_durations",
                 "slowdown_bound")

    def __init__(self, data: Dict[str, Any]) -> None:
        self.cores: int = data["cores"]
        #: Ticks a cook loses every time it takes on an order.
        self.switch_cost: int = data["switch_cost"]
        self.max_ticks: int = data["max_ticks"]
        self.tick_ms: int = data["tick_ms"]
        #: Whether step durations are shown to you.
        self.known_durations: bool = bool(data["known_durations"])
        #: The floor the slowdown component divides by: an order needing less
        #: time than this is scored as though it needed this much, so a
        #: one-tick espresso that waited ten ticks is not called a ten-fold
        #: delay. You are marked on `turnaround / max(the order's whole span,
        #: this)`, and you are told it rather than left to guess.
        self.slowdown_bound: float = float(data.get("slowdown_bound", 10.0))


class Stats:
    __slots__ = ("arrived", "live", "served", "abandoned", "switches", "switch_ticks",
                 "work_ticks", "idle_ticks", "preemptions", "station_conflicts",
                 "station_bumps")

    def __init__(self, data: Dict[str, Any]) -> None:
        for name in self.__slots__:
            setattr(self, name, data.get(name, 0))


class HistoryEntry:
    """A recently served order, with its real durations."""

    __slots__ = ("id", "recipe", "priority", "total_work", "turnaround", "steps")

    def __init__(self, data: Dict[str, Any]) -> None:
        self.id: int = data["id"]
        self.recipe: str = data["recipe"]
        self.priority: int = data["priority"]
        self.total_work: int = data["total_work"]
        self.turnaround: int = data["turnaround"]
        #: ``(label, kind, duration)`` per step.
        self.steps: List[Any] = [(s["label"], s["kind"], s["duration"]) for s in data["steps"]]


class Recipe:
    """A menu entry: the shape of an order, and its ranges when they are shown."""

    __slots__ = ("name", "priority", "patience", "steps", "stations")

    def __init__(self, data: Dict[str, Any]) -> None:
        self.name: str = data["name"]
        self.priority: int = data["priority"]
        #: ``[min, max]`` ticks, or ``None`` when hidden.
        self.patience: Optional[List[int]] = data.get("patience")
        #: ``(label, kind, [min, max] or None)`` per step.
        self.steps: List[Any] = [(s["label"], s["kind"], s.get("ticks")) for s in data["steps"]]
        #: The station each step happens at, ``None`` for waits.
        self.stations: List[Optional[str]] = [s.get("station") for s in data["steps"]]

    @property
    def expected_work(self) -> Optional[float]:
        """Mean total work from the menu's ranges, when they are shown."""
        total = 0.0
        for _, kind, ticks in self.steps:
            if kind != "work":
                continue
            if ticks is None:
                return None
            total += (ticks[0] + ticks[1]) / 2.0
        return total


class Observation:
    """The kitchen at a scheduling point."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.raw: Dict[str, Any] = data
        self.time: int = data["time"]
        self.kitchen = Kitchen(data["kitchen"])
        self.reason = Reason(data["reason"])
        self.cores: List[Core] = [Core(c) for c in data["cores"]]
        #: Every station, with how much of it is spoken for.
        self.stations: List[Station] = [Station(s) for s in data.get("stations", [])]
        self._stations: Dict[str, Station] = {s.name: s for s in self.stations}
        #: Every live order, in arrival order.
        self.orders: List[Order] = [Order(o) for o in data["orders"]]
        self._by_id: Dict[int, Order] = {o.id: o for o in self.orders}
        self.stats = Stats(data.get("stats", {}))
        self.history: List[HistoryEntry] = [HistoryEntry(h) for h in data.get("history", [])]
        self.recipes: Dict[str, Recipe] = {r["name"]: Recipe(r) for r in data.get("recipes", [])}

    # -- lookups -------------------------------------------------------------

    def order(self, order_id: Any) -> Optional[Order]:
        if hasattr(order_id, "id"):
            order_id = order_id.id
        return self._by_id.get(order_id)

    def core(self, core_id: Any) -> Optional[Core]:
        if hasattr(core_id, "id"):
            core_id = core_id.id
        for core in self.cores:
            if core.id == core_id:
                return core
        return None

    def station(self, name: Any) -> Optional[Station]:
        if hasattr(name, "name"):
            name = name.name
        return self._stations.get(name)

    @property
    def ready(self) -> List[Order]:
        """Orders waiting for a cook, in arrival order."""
        return [o for o in self.orders if o.is_ready]

    @property
    def runnable(self) -> List[Order]:
        """The ready orders that could actually start right now.

        A ready order whose station is full cannot be dispatched: the engine
        refuses the assignment and the cook stays idle. This is ``ready``
        with those removed, in arrival order.
        """
        return [o for o in self.ready if self.can_start(o)]

    def can_start(self, order: Any) -> bool:
        """Whether this order's station has a place free.

        Says nothing about whether a cook is free, and nothing about the rest
        of the decision you are building - if you are dispatching two orders to
        the same station, count the places yourself with :meth:`free_stations`.
        """
        o = self.order(order) if not isinstance(order, Order) else order
        if o is None or o.station is None:
            return o is not None
        station = self._stations.get(o.station)
        return station is None or station.free > 0

    def free_stations(self) -> Dict[str, int]:
        """Places free at each station, as a plain dict you can spend.

        The usual shape of a decision::

            free = obs.free_stations()
            for core in obs.idle_cores:
                for order in queue:
                    if order.station is None or free.get(order.station, 0) > 0:
                        decision.assign(core, order)
                        if order.station:
                            free[order.station] -= 1
                        break

        Taking an order off a cook in the same decision hands its place back,
        so add one for ``core.station`` when you do that.
        """
        return {s.name: s.free for s in self.stations}

    @property
    def running(self) -> List[Order]:
        return [o for o in self.orders if o.is_running]

    @property
    def blocked(self) -> List[Order]:
        """Orders in the oven. Nobody can be assigned to these."""
        return [o for o in self.orders if o.is_blocked]

    @property
    def idle_cores(self) -> List[Core]:
        return [c for c in self.cores if c.is_idle]

    @property
    def working_cores(self) -> List[Core]:
        return [c for c in self.cores if c.is_working]

    @property
    def busy_cores(self) -> List[Core]:
        return [c for c in self.cores if c.is_busy]

    def order_on(self, core: Any) -> Optional[Order]:
        """The order a cook is holding, if any."""
        c = self.core(core)
        return None if c is None or c.order is None else self.order(c.order)

    # -- estimation ----------------------------------------------------------

    def estimate_remaining(self, order: Order) -> float:
        """Ticks of work an order still needs, as well as it can be known.

        The true figure when durations are shown. Otherwise the mean total
        work of served orders of the same recipe, less the work already done -
        and with no history to go on, a flat guess per remaining work step.
        This is exactly what the reference schedulers use, so beating them in
        ``blind`` mode means writing a better estimate than this.
        """
        if order.work_remaining is not None:
            return float(order.work_remaining)
        same = [h for h in self.history if h.recipe == order.recipe]
        if not same:
            steps_left = max(1, sum(1 for s in order.steps_left if s.is_work))
            return 8.0 * steps_left
        mean_total = sum(h.total_work for h in same) / len(same)
        return max(1.0, mean_total - order.work_done)

    def __repr__(self) -> str:
        return (
            f"Observation(t={self.time}, ready={len(self.ready)}, running={len(self.running)}, "
            f"blocked={len(self.blocked)}, idle_cores={len(self.idle_cores)}, why={self.reason.kinds})"
        )
