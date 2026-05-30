"""HTTP control plane (FastAPI). Enqueues runs and exposes run/step/telemetry
state. The actual driving happens in worker processes (or a background task in
single-node mode) — see ``src.worker``.
"""
