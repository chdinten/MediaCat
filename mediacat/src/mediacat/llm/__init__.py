"""LLM adapters — provider-agnostic interface for text intelligence.

Used **only** for:
* Comparison of token revisions
* Anomaly detection in ingested data
* Text generation (descriptions, summaries)
* Language translation (delegated from storage.translation)

LLMs never generate executable code or make autonomous data changes.

Submodules
----------
* ``adapter``   — provider-agnostic interface and factory
* ``ollama``    — local Ollama backend
* ``api``       — Anthropic / OpenAI API backend
* ``tasks``     — task-specific prompt templates and validators
* ``safety``    — prompt injection mitigations
"""

from __future__ import annotations
