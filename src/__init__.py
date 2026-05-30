"""AI agent harness — autonomous multi-file refactoring runner.

Package layout::

    src.orchestrator   state machine + Planner / Executor / Verifier stages
    src.sandbox        Docker / local isolated execution + file tools
    src.storage        persistent run/step/error/telemetry state (SQLite/PG)
    src.telemetry      span + token-usage tracing (SQLite store, optional Langfuse)
    src.git            repo clone / branch / diff / rollback
    src.llm            provider-agnostic LLM client (anthropic | openai | mock)
    src.api            FastAPI control plane (enqueue + inspect runs)
    src.worker         queue consumer / crash-recovery resumer
    src.main           CLI entrypoint
"""

__version__ = "0.1.0"
