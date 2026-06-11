"""Real LLM providers: one minimal completion each (key-gated, ~cents).

Set HARNESS_TEST_LLM_FULL=1 to additionally run a complete harness run
against the Python dummy repo with the real provider (more tokens).
"""
import os

import pytest

from conftest import PY_REPO, TASK

pytestmark = [pytest.mark.integration, pytest.mark.llm]

_ANTHROPIC = bool(os.environ.get("ANTHROPIC_API_KEY"))
_OPENAI = bool(os.environ.get("OPENAI_API_KEY"))


@pytest.mark.skipif(not _ANTHROPIC, reason="ANTHROPIC_API_KEY not set")
def test_anthropic_completion_reports_usage():
    from src.llm.client import AnthropicClient

    resp = AnthropicClient().complete(
        "You answer with exactly one word.", "Say: harness", max_tokens=16)
    assert resp.text.strip()
    assert resp.input_tokens > 0 and resp.output_tokens > 0


@pytest.mark.skipif(not _OPENAI, reason="OPENAI_API_KEY not set")
def test_openai_completion_reports_usage():
    from src.llm.client import OpenAIClient

    resp = OpenAIClient().complete(
        "You answer with exactly one word.", "Say: harness", max_tokens=16)
    assert resp.text.strip()
    assert resp.input_tokens > 0 and resp.output_tokens > 0


@pytest.mark.skipif(os.environ.get("HARNESS_TEST_LLM_FULL") != "1"
                    or not _ANTHROPIC,
                    reason="set HARNESS_TEST_LLM_FULL=1 + ANTHROPIC_API_KEY")
def test_full_run_with_real_provider(fresh_db, override_settings):
    override_settings(llm_provider="anthropic", sandbox="local")
    from src.orchestrator.state_machine import StateMachine
    from src.storage.models import Repository

    repo = Repository()
    run = repo.create_run(PY_REPO, TASK, "harness/llm-integration")
    result = StateMachine(repo).drive(run.run_id)
    assert result.status in ("COMPLETED", "ROLLED_BACK")
    assert result.tokens_used > 0      # real usage accounted
