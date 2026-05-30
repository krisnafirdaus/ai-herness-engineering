"""Observability: per-agent spans, token usage and verification-loop breakdown.

Spans are ALWAYS persisted to the ``telemetry`` table (queryable offline via
``python -m src.main traces``). If Langfuse credentials are present, each span is
additionally exported as a Langfuse generation/span for visual tracing.
"""
