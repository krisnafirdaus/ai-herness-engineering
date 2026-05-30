"""Tracer: records a span per agent invocation (duration + token usage).

Usage::

    with tracer.span("executor", step_id="step-1", iteration=2) as span:
        result = call_llm(...)
        span.add_tokens(result.input_tokens, result.output_tokens)
        span.set_status("retry")

On ``__exit__`` the span's duration is computed and the row is written to the
``telemetry`` table (and mirrored to Langfuse when configured). Token usage is
also folded into the run's running total so the cost guardrail can trip.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from ..config import settings
from ..storage.models import Repository


class Span:
    def __init__(self, agent: str, step_id: str | None, iteration: int | None) -> None:
        self.agent = agent
        self.step_id = step_id
        self.iteration = iteration
        self.input_tokens = 0
        self.output_tokens = 0
        self.status: str | None = None
        self._t0 = time.perf_counter()

    def add_tokens(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)

    def set_status(self, status: str) -> None:
        self.status = status

    @property
    def duration_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)


class Tracer:
    """Per-run tracer. Wraps a Repository and (optionally) a Langfuse client."""

    def __init__(self, repo: Repository, run_id: str) -> None:
        self.repo = repo
        self.run_id = run_id
        self._lf = self._init_langfuse()

    def _init_langfuse(self):
        if not settings.langfuse_enabled:
            return None
        try:  # optional dependency — never let telemetry break a run
            from langfuse import Langfuse

            return Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
        except Exception:
            return None

    @contextmanager
    def span(self, agent: str, *, step_id: str | None = None,
             iteration: int | None = None) -> Iterator[Span]:
        span = Span(agent, step_id, iteration)
        try:
            yield span
        finally:
            self._persist(span)

    def _persist(self, span: Span) -> None:
        self.repo.add_telemetry(
            self.run_id, span.agent, step_id=span.step_id,
            input_tokens=span.input_tokens, output_tokens=span.output_tokens,
            duration_ms=span.duration_ms, verification_iteration=span.iteration,
            status=span.status,
        )
        total = span.input_tokens + span.output_tokens
        if total:
            self.repo.add_tokens(self.run_id, total)
        if self._lf is not None:
            try:
                self._lf.generation(
                    name=f"{span.agent}:{span.step_id or 'plan'}",
                    trace_id=self.run_id,
                    usage={"input": span.input_tokens, "output": span.output_tokens},
                    metadata={"iteration": span.iteration, "status": span.status,
                              "duration_ms": span.duration_ms},
                )
            except Exception:
                pass  # telemetry export must never affect the run outcome

    @staticmethod
    def summarize(rows: list[dict]) -> dict:
        """Aggregate telemetry rows into a compact cost/latency report."""
        by_agent: dict[str, dict] = {}
        total_in = total_out = total_ms = 0
        for r in rows:
            a = by_agent.setdefault(r["agent"], {"calls": 0, "input_tokens": 0,
                                                 "output_tokens": 0, "duration_ms": 0})
            a["calls"] += 1
            a["input_tokens"] += r["input_tokens"]
            a["output_tokens"] += r["output_tokens"]
            a["duration_ms"] += r["duration_ms"]
            total_in += r["input_tokens"]
            total_out += r["output_tokens"]
            total_ms += r["duration_ms"]
        return {
            "by_agent": by_agent,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_tokens": total_in + total_out,
            "total_duration_ms": total_ms,
        }
