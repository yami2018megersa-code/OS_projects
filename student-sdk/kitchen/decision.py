"""What your scheduler hands back.

A :class:`Decision` says which order each cook you name should be working on,
and optionally when you want to be called again. A cook you do not name carries
on with whatever it was doing. Naming a cook with ``None`` sends it idle and
puts whatever it held back on the rail.

    d = Decision()
    d.assign(core, order)        # core and order may be ids or the objects
    d.idle(core)                 # take this cook off whatever it is doing
    d.wake_in(8)                 # call me again in 8 ticks even if nothing happens
    d.annotate(text="SJF", queue=[o.id for o in my_queue])   # drawn by the viewer
    return d

Returning a plain dict of the same shape works too::

    return {"assign": {0: 17, 1: None}, "timer": 8}
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


def _id_of(value: Any, what: str) -> int:
    """An id from an id, or from anything with an ``id`` attribute."""
    if value is None:
        raise TypeError(f"{what} cannot be None here")
    if hasattr(value, "id"):
        value = value.id
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise TypeError(f"{what} must be an id (or an object with .id), got {value!r}") from None
    if value < 0:
        raise ValueError(f"{what} must not be negative, got {value}")
    return value


class Decision:
    """The scheduler's answer at one scheduling point."""

    __slots__ = ("_assign", "_timer", "_debug")

    def __init__(self, assign: Optional[Dict[Any, Any]] = None, timer: Optional[int] = None) -> None:
        self._assign: Dict[int, Optional[int]] = {}
        self._timer: Optional[int] = None
        self._debug: Optional[Dict[str, Any]] = None
        if assign:
            for core, order in dict(assign).items():
                self.assign(core, order)
        if timer is not None:
            self.wake_in(timer)

    # -- building ----------------------------------------------------------

    def assign(self, core: Any, order: Any) -> "Decision":
        """Put ``order`` on ``core``. ``order=None`` idles the cook.

        If the cook is already on that order nothing changes and no switch is
        paid. Anything else it was holding goes back to the ready queue.
        """
        c = _id_of(core, "core")
        self._assign[c] = None if order is None else _id_of(order, "order")
        return self

    def idle(self, core: Any) -> "Decision":
        """Take this cook off whatever it is doing."""
        return self.assign(core, None)

    def wake_in(self, ticks: Any) -> "Decision":
        """Ask to be called again in ``ticks`` ticks even if nothing happens.

        This is your timer interrupt. Round robin needs it; anything that
        reacts only to arrivals and completions does not. ``0`` cancels an
        alarm you set earlier.
        """
        t = int(ticks)
        if t < 0:
            raise ValueError("wake_in expects a non-negative number of ticks")
        self._timer = t
        return self

    def cancel_alarm(self) -> "Decision":
        return self.wake_in(0)

    def annotate(
        self,
        text: Optional[str] = None,
        queue: Optional[Iterable[Any]] = None,
        tags: Optional[Dict[Any, str]] = None,
        **extra: Any,
    ) -> "Decision":
        """Attach notes for the viewer. Ignored by the engine.

        ``text`` is a free-form line about this decision; ``queue`` is the
        order you would serve the rail in, drawn as the rail's order; ``tags``
        labels individual orders ("urgent", "skipped: cannot finish"). Kept
        small - a payload over the size limit is dropped, not an error.
        """
        payload: Dict[str, Any] = dict(self._debug or {})
        if text is not None:
            payload["text"] = str(text)
        if queue is not None:
            payload["queue"] = [_id_of(o, "queue entry") for o in queue]
        if tags:
            payload["tags"] = {str(_id_of(k, "tag key")): str(v) for k, v in tags.items()}
        for key, value in extra.items():
            payload[str(key)] = value
        self._debug = payload
        return self

    # -- reading -----------------------------------------------------------

    @property
    def assignments(self) -> Dict[int, Optional[int]]:
        return dict(self._assign)

    @property
    def timer(self) -> Optional[int]:
        return self._timer

    @property
    def debug(self) -> Optional[Dict[str, Any]]:
        return self._debug

    def is_empty(self) -> bool:
        return not self._assign and self._timer is None

    def to_dict(self) -> Dict[str, Any]:
        """The shape the engine reads."""
        out: Dict[str, Any] = {"assign": dict(self._assign)}
        if self._timer is not None:
            out["timer"] = self._timer
        if self._debug:
            out["debug"] = self._debug
        return out

    def __repr__(self) -> str:
        parts = [f"assign={self._assign!r}"]
        if self._timer is not None:
            parts.append(f"timer={self._timer}")
        return f"Decision({', '.join(parts)})"


def as_decision_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Whatever ``schedule`` returned, as the dict the engine reads.

    Raises ``TypeError`` for anything that is not a Decision, a dict or None,
    so the runner can report it as a scheduler failure with a clear message.
    """
    if value is None:
        return None
    if isinstance(value, Decision):
        return value.to_dict()
    if isinstance(value, dict):
        return value
    raise TypeError(
        f"schedule() must return a Decision (or a dict, or None), got {type(value).__name__}"
    )
