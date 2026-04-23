# ADR-0002 — Hybrid vision + LLM deployment (local default, API fallback)

- **Status**: Accepted
- **Date**: 2026-04-16

## Context

The system needs vision transcription of labels, OBI cards, and runout
etchings, plus LLM assistance for comparison, anomaly detection,
translation, and text generation. Three deployment models were on the
table: API-only, local-only, and hybrid.

## Decision

Hybrid. A provider-agnostic adapter routes calls through a local backend
first (Ollama for text LLMs, a local VLM such as LLaVA or Qwen2-VL for
vision) and falls back to a remote API on error, timeout, confidence
threshold miss, or explicit per-task override. Both paths go through the
same request shape; the caller never sees the backend.

The specific adapters are:

| Concern    | Primary (local)             | Fallback (API)                |
|------------|-----------------------------|-------------------------------|
| LLM text   | Ollama (configurable model) | Anthropic Messages API        |
| Vision     | Local VLM via Ollama/vLLM   | Anthropic vision / OpenAI     |
| OCR        | Tesseract (already local)   | Cloud OCR (optional)          |
| Translate  | Local LLM                   | API LLM                       |

## Consequences

- Sensitive data (user images, provenance records) stays on-host by
  default; falling back to an API is a policy-controlled, logged event.
- Two adapter implementations per concern must be maintained. Adapter
  selection is a config switch, tested in CI against both paths.
- Cost is bounded but not zero: GPU memory is required for the local
  VLM. A CPU-only fallback model is documented in the runbook.
- Observability captures which backend served each call so cost
  attribution and quality regression analysis are possible.

## Open questions

- Exact local VLM model is deferred to Section 8. The adapter interface
  is stable regardless.
- Whether to allow a per-tenant override is deferred to multi-tenant
  work (not in v1).
