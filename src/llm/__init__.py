"""Provider-agnostic LLM client (``anthropic`` | ``openai`` | ``mock``).

The orchestrator only depends on :func:`get_client` and the small
:class:`~src.llm.client.LLMResponse` shape, so swapping providers — or running
fully offline with the deterministic ``mock`` — never touches agent code.
"""
from .client import LLMResponse, get_client

__all__ = ["LLMResponse", "get_client"]
