"""Scenario definition shared by the MVP and held-out scenario modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .world import World


@dataclass(frozen=True)
class Scenario:
    id: int
    name: str
    family: str
    label: str
    build: Callable[[World], None]
