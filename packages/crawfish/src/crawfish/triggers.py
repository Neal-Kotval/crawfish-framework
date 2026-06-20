"""Pipeline triggers: cron + webhook (CRA-115).

A pipeline declares *how* it fires. Beyond cron (time-based polling), a webhook
trigger is a true push: an HTTP endpoint enqueues a run and carries the payload,
no polling required. Webhook secrets are held **by reference** — the name of an
environment variable, never an inline value — so secrets never enter the manifest
or any serialized description. :func:`verify_webhook` does constant-time
HMAC-SHA256 verification of inbound payloads.
"""

from __future__ import annotations

import hashlib
import hmac
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from crawfish.core.ids import new_id
from crawfish.core.types import JSONValue

__all__ = [
    "Trigger",
    "CronTrigger",
    "CronSchedule",
    "Cron",
    "IntervalTrigger",
    "IntervalSchedule",
    "parse_schedule",
    "WebhookTrigger",
    "verify_webhook",
]


class CronSchedule:
    """A minimal 5-field cron evaluator (``m h dom mon dow``).

    Supports ``*``, ``*/n`` steps, ``a,b`` lists, ``a-b`` ranges, and exact values
    — enough for the deploy/observer polling cases (``0 8 * * *``, ``*/5 * * * *``).
    Day-of-week is ``0-6`` with Sunday = 0. When both day-of-month and day-of-week
    are restricted, a tick matches if *either* matches (standard cron semantics).
    Evaluation is at minute resolution.
    """

    _RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]

    def __init__(self, expr: str) -> None:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron expression must have 5 fields, got {len(parts)}: {expr!r}")
        self.expr = expr
        self._fields = [
            self._parse_field(p, lo, hi) for p, (lo, hi) in zip(parts, self._RANGES, strict=True)
        ]
        self._dom_restricted = parts[2] != "*"
        self._dow_restricted = parts[4] != "*"

    @staticmethod
    def _parse_field(field: str, lo: int, hi: int) -> set[int]:
        allowed: set[int] = set()
        for token in field.split(","):
            step = 1
            body = token
            if "/" in token:
                body, _, step_s = token.partition("/")
                step = int(step_s)
            if body in ("*", ""):
                start, end = lo, hi
            elif "-" in body:
                start_s, _, end_s = body.partition("-")
                start, end = int(start_s), int(end_s)
            else:
                start = end = int(body)
            if not (lo <= start <= hi and lo <= end <= hi and start <= end):
                raise ValueError(f"cron field out of range: {token!r}")
            allowed.update(range(start, end + 1, step))
        return allowed

    def matches(self, dt: datetime) -> bool:
        """True if ``dt`` (truncated to the minute) satisfies the schedule."""
        minute, hour, dom, mon = self._fields[0], self._fields[1], self._fields[2], self._fields[3]
        dow = self._fields[4]
        cron_dow = (dt.weekday() + 1) % 7  # Python Mon=0 → cron Sun=0
        if self._dom_restricted and self._dow_restricted:
            day_ok = dt.day in dom or cron_dow in dow  # cron OR semantics
        else:
            day_ok = dt.day in dom and cron_dow in dow
        return dt.minute in minute and dt.hour in hour and dt.month in mon and day_ok

    def next_after(self, dt: datetime) -> datetime:
        """The first minute strictly after ``dt`` that matches (searches ≤366d)."""
        cur = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if self.matches(cur):
                return cur
            cur += timedelta(minutes=1)
        raise ValueError(f"no matching time within a year for {self.expr!r}")


class Trigger(ABC):
    """Base for anything that can fire a pipeline run (CRA-115)."""

    id: str
    kind: str

    @abstractmethod
    def describe(self) -> dict[str, JSONValue]:
        """Return a JSON-serialisable description of this trigger (CRA-115)."""
        raise NotImplementedError


class CronTrigger(Trigger):
    """Fire a run on a cron ``schedule`` (CRA-115)."""

    def __init__(self, schedule: str) -> None:
        self.id = new_id()
        self.kind = "cron"
        self.schedule = schedule

    def describe(self) -> dict[str, JSONValue]:
        """Round-trippable description: kind + schedule (CRA-115)."""
        return {"id": self.id, "kind": self.kind, "schedule": self.schedule}


class WebhookTrigger(Trigger):
    """Fire a run from an inbound HTTP POST to ``path`` (CRA-115).

    ``secret_ref`` is the *name* of an environment variable holding the shared
    secret, never the secret value itself, so it is safe to serialise.
    """

    def __init__(self, path: str, secret_ref: str | None = None) -> None:
        self.id = new_id()
        self.kind = "webhook"
        self.path = path
        self.secret_ref = secret_ref

    def describe(self) -> dict[str, JSONValue]:
        """Round-trippable description; carries the secret *reference* only (CRA-115)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "path": self.path,
            "secret_ref": self.secret_ref,
        }


# Ergonomic alias so observer/poll call sites read `poll=Cron("*/5 * * * *")`.
Cron = CronSchedule


class IntervalSchedule:
    """Fire every ``seconds`` — a fixed interval, with sub-minute resolution.

    Cron is minute-resolution and can't express "every 30 seconds"; this can. It is
    serialised as the string ``"@every <n>s"`` (see :func:`parse_schedule`), so it flows
    through the same single ``schedule`` channel that cron does. ``matches`` is always
    true (the supervisor sleeps ``next_after - now`` between cycles, so the interval is
    what governs cadence).
    """

    def __init__(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(f"interval seconds must be positive, got {seconds!r}")
        self.seconds = float(seconds)
        n: float | int = int(self.seconds) if self.seconds.is_integer() else self.seconds
        self.expr = f"@every {n}s"

    def matches(self, dt: datetime) -> bool:
        """Always true — the supervisor's inter-cycle sleep enforces the interval."""
        return True

    def next_after(self, dt: datetime) -> datetime:
        """One interval after ``dt``."""
        return dt + timedelta(seconds=self.seconds)


class IntervalTrigger(Trigger):
    """Fire a run every ``seconds`` (CRA-115). Simpler than cron for sub-minute cadence.

    ``TRIGGER = IntervalTrigger(seconds=30)`` reads far more plainly than a cron string
    — and unlike cron it can fire faster than once a minute.
    """

    def __init__(self, *, seconds: float) -> None:
        self.id = new_id()
        self.kind = "interval"
        self.seconds = float(seconds)

    @property
    def schedule(self) -> str:
        """The ``"@every <n>s"`` string ``craw deploy`` carries for this trigger."""
        return IntervalSchedule(self.seconds).expr

    def describe(self) -> dict[str, JSONValue]:
        """Round-trippable description: kind + schedule (CRA-115)."""
        return {"id": self.id, "kind": self.kind, "schedule": self.schedule}


def _parse_duration(spec: str) -> float:
    """Parse ``"30s"`` / ``"5m"`` / ``"2h"`` / bare seconds (``"90"``) to seconds."""
    spec = spec.strip()
    units = {"s": 1.0, "m": 60.0, "h": 3600.0}
    if spec and spec[-1] in units:
        return float(spec[:-1]) * units[spec[-1]]
    return float(spec)


def parse_schedule(spec: str) -> CronSchedule | IntervalSchedule:
    """Parse a schedule string into a cron or interval schedule.

    ``"@every 30s"`` (or ``5m`` / ``2h`` / bare seconds) → :class:`IntervalSchedule`;
    anything else → a 5-field :class:`CronSchedule`. Both expose ``matches`` /
    ``next_after``, so the supervisor and deploy treat them uniformly.
    """
    spec = spec.strip()
    if spec.startswith("@every"):
        return IntervalSchedule(_parse_duration(spec[len("@every") :]))
    return CronSchedule(spec)


def verify_webhook(secret: str, payload: bytes, signature: str) -> bool:
    """Verify an inbound webhook ``signature`` against ``payload`` (CRA-115).

    Computes ``HMAC-SHA256(secret, payload)`` as lowercase hex and compares it to
    ``signature`` in constant time to avoid timing oracles. The caller resolves
    ``secret`` from the trigger's ``secret_ref`` environment variable.
    """
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
