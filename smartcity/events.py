"""Bounded log of algorithm decisions, shown in the HUD and asserted on in tests."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass

# Kinds that mean "the algorithm overrode the standard fixed timer".
OVERRIDE_KINDS = frozenset(
    {
        "LOCAL_EXTEND",
        "PREDICTIVE_EXTEND",
        "PREDICTIVE_SWITCH",
        "EARLY_SWITCH",
        "SPLIT_ADJUST",
        "THROTTLE",
    }
)


@dataclass(frozen=True)
class Event:
    t: float
    kind: str
    node: int | None
    text: str


class EventLog:
    def __init__(self, capacity: int = 3000):
        self._events: deque[Event] = deque(maxlen=capacity)
        self.counts: Counter[str] = Counter()

    def add(self, t: float, kind: str, text: str, node: int | None = None) -> None:
        self._events.append(Event(t, kind, node, text))
        self.counts[kind] += 1

    def recent(self, n: int, kinds: frozenset[str] | set[str] | None = None) -> list[Event]:
        events = [e for e in self._events if kinds is None or e.kind in kinds]
        return events[-n:]

    def since(self, t: float) -> list[Event]:
        return [e for e in self._events if e.t >= t]

    @property
    def overrides(self) -> int:
        return sum(self.counts[k] for k in OVERRIDE_KINDS)
