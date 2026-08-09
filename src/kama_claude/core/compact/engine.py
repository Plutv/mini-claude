from __future__ import annotations

import logging
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kama_claude.core.compact.budget import (
    ReductionStats,
    budget_tool_results,
    estimate_message_tokens,
    snip_stale_tool_results,
    tool_pairs_balanced,
)

if TYPE_CHECKING:
    from kama_claude.core.compact.compactor import Compactor
    from kama_claude.core.context import ExecutionContext
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.llm.types import UsageStats


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextPolicy:
    context_window_tokens: int = 200_000
    tool_result_limit: int = 8_000
    tool_result_keep: int = 4_000
    budget_threshold: float = 0.50
    snip_threshold: float = 0.60
    microcompact_threshold: float = 0.70
    hot_cache_override: float = 0.80
    full_compact_threshold: float = 0.85
    cache_hot_seconds: float = 300.0
    keep_recent_tool_results: int = 3
    microcompact_every_steps: int = 4


@dataclass(frozen=True)
class ContextMaintenance:
    utilization: float
    budgeted_results: int = 0
    snipped_results: int = 0
    microcompacted_results: int = 0
    removed_chars: int = 0
    fully_compacted: bool = False
    full_compaction_failed: bool = False

    @property
    def changed(self) -> bool:
        return bool(
            self.budgeted_results
            or self.snipped_results
            or self.microcompacted_results
            or self.fully_compacted
        )


class ContextEngine:
    """Cache-aware four-tier context maintenance for one Agent run."""

    def __init__(
        self,
        compactor: Compactor | None,
        policy: ContextPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._compactor = compactor
        self._policy = policy
        self._clock = clock
        self._last_usage_pct = 0.0
        self._last_api_call_at: float | None = None

    def record_usage(self, usage: UsageStats | None) -> None:
        if usage is not None:
            self._last_usage_pct = max(0.0, usage.context_pct)
        self._last_api_call_at = self._clock()

    def tool_artifact_threshold(self) -> int:
        base = self._policy.tool_result_limit
        if self._last_usage_pct >= 0.70:
            return base
        if self._last_usage_pct >= 0.50:
            return max(base, base * 2)
        return max(32 * 1024, base * 4)

    def _utilization(self, messages: list[dict[str, Any]]) -> float:
        estimate = estimate_message_tokens(messages) / max(
            1, self._policy.context_window_tokens
        )
        return min(1.0, max(self._last_usage_pct, estimate))

    async def prepare(
        self,
        context: ExecutionContext,
        provider: LLMProvider,
    ) -> ContextMaintenance:
        """Maintain a balanced history immediately before an LLM request.

        Work happens on a deep copy. If full LLM compaction fails, the original
        context is restored byte-for-byte rather than leaving half-compacted
        history behind.
        """
        original = deepcopy(context.messages)
        if not tool_pairs_balanced(original):
            logger.warning(
                "context maintenance skipped: unbalanced tool history run_id=%s",
                context.run_id,
            )
            return ContextMaintenance(utilization=self._utilization(original))

        utilization = self._utilization(original)
        working = original
        budgeted = ReductionStats()
        snipped = ReductionStats()
        micro = ReductionStats()

        if utilization >= self._policy.budget_threshold:
            working, budgeted = budget_tool_results(
                working,
                limit=self._policy.tool_result_limit,
                keep=self._policy.tool_result_keep,
            )

        now = self._clock()
        cache_hot = (
            self._last_api_call_at is not None
            and now - self._last_api_call_at < self._policy.cache_hot_seconds
        )
        may_rewrite_hot_prefix = utilization >= self._policy.hot_cache_override

        if (
            utilization >= self._policy.snip_threshold
            and (not cache_hot or may_rewrite_hot_prefix)
        ):
            working, snipped = snip_stale_tool_results(
                working,
                keep_recent=self._policy.keep_recent_tool_results,
            )

        idle = (
            self._last_api_call_at is not None
            and now - self._last_api_call_at >= self._policy.cache_hot_seconds
        )
        multi_round = (
            self._policy.microcompact_every_steps > 0
            and context.step > 0
            and context.step % self._policy.microcompact_every_steps == 0
        )
        if (
            utilization >= self._policy.microcompact_threshold
            and (idle or multi_round)
        ):
            working, micro = snip_stale_tool_results(
                working,
                keep_recent=self._policy.keep_recent_tool_results,
                aggressive=True,
            )

        wants_full = (
            self._compactor is not None
            and self._policy.full_compact_threshold > 0
            and utilization >= self._policy.full_compact_threshold
        )
        if wants_full:
            context.messages = working
            try:
                result = await self._compactor.compact(
                    context,
                    provider,
                    continue_run=True,
                )
            except Exception:
                logger.exception(
                    "full compaction failed; restoring original context run_id=%s",
                    context.run_id,
                )
                result = None
            if result is None:
                context.messages = original
                return ContextMaintenance(
                    utilization=utilization,
                    full_compaction_failed=True,
                )
            return ContextMaintenance(
                utilization=utilization,
                budgeted_results=budgeted.changed_results,
                snipped_results=snipped.changed_results,
                microcompacted_results=micro.changed_results,
                removed_chars=(
                    budgeted.removed_chars
                    + snipped.removed_chars
                    + micro.removed_chars
                ),
                fully_compacted=True,
            )

        context.messages = working
        return ContextMaintenance(
            utilization=utilization,
            budgeted_results=budgeted.changed_results,
            snipped_results=snipped.changed_results,
            microcompacted_results=micro.changed_results,
            removed_chars=(
                budgeted.removed_chars + snipped.removed_chars + micro.removed_chars
            ),
        )
