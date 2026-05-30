"""Persistent state store for runs, steps, error states and telemetry spans.

Default backend is SQLite (zero-setup, single file). The same schema and SQL is
portable to Postgres for production — see ``HARNESS_DATABASE_URL`` and the notes
in ``db.py``. Persistence is what makes the harness *resumable*: the worker
reconstructs a run purely from these tables, never from process memory.
"""
