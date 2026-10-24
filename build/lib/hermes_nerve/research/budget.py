"""DRK PR10: budget governor — deterministic research budgets.

The same engine serves a 4-second question and a 120-second deep dive; depth
is a budget setting, not a different architecture. Exhausted budget fails
closed (unresolved), never fabricated.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ResearchBudget:
    max_wall_ms: int = 4000
    max_pages: int = 6
    max_searches: int = 3
    max_depth: int = 1


@dataclass
class BudgetGovernor:
    budget: ResearchBudget
    _t0: float = field(default_factory=time.monotonic, init=False)
    pages: int = 0
    searches: int = 0
    depth: int = 0

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def allow(self, kind: str) -> bool:
        if self.elapsed_ms() >= self.budget.max_wall_ms:
            return False
        return {"page": self.pages < self.budget.max_pages,
                "search": self.searches < self.budget.max_searches,
                "depth": self.depth < self.budget.max_depth}.get(kind, False)

    def spend(self, kind: str) -> None:
        if kind == "page":
            self.pages += 1
        elif kind == "search":
            self.searches += 1
        elif kind == "depth":
            self.depth += 1

    @property
    def exhausted(self) -> bool:
        return (self.elapsed_ms() >= self.budget.max_wall_ms
                or self.pages >= self.budget.max_pages
                and self.searches >= self.budget.max_searches)


BBQ_BUDGET = ResearchBudget(max_wall_ms=4000, max_pages=3, max_searches=1, max_depth=1)
