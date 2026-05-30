"""Pytest setup — force a hermetic, offline configuration BEFORE importing src.

Env must be set before `src.config` is first imported, because Settings reads the
environment at class-definition time. pytest loads conftest before test modules,
so setting it here is sufficient.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="harness-test-")
os.environ.setdefault("HARNESS_LLM_PROVIDER", "mock")
os.environ.setdefault("HARNESS_SANDBOX", "local")
os.environ.setdefault("HARNESS_DATABASE_URL", f"sqlite:///{_TMP}/test.sqlite3")

import pytest  # noqa: E402

PY_REPO = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       "dummy-repos", "python-api-sample")
TASK = "Add request validation to the user creation endpoint"


@pytest.fixture
def override_settings():
    """Temporarily override frozen Settings fields (bypassing frozen=True)."""
    from src.config import settings

    saved = {}

    def set(**kw):
        for k, v in kw.items():
            saved[k] = getattr(settings, k)
            object.__setattr__(settings, k, v)

    yield set
    for k, v in saved.items():
        object.__setattr__(settings, k, v)
