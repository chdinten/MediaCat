# Section 7 — LLM adapters

LLMs are **advisory only**: they verify, summarise and propose; they never
change catalogue data on their own.

## Text LLM

`app.state.llm` is a `HybridLlm`: the local Ollama backend first
(`OLLAMA_TEXT_MODEL`, or `llm.text_model` in `config/app.yaml`, defaulting to
the vision model `qwen2.5vl:32b`), with the Anthropic API as fallback when
configured. Untrusted text (web pages, Wikipedia extracts) is wrapped with
`mediacat.llm.safety.build_prompt` before it reaches the model.

Current uses:

| Use | Function | Output |
|---|---|---|
| Is this Wikipedia article about this music artist? | `artist_bio.llm_confirms_artist` | JSON verdict: `music_artist`, `same_artist` |
| Biography from the official homepage | `artist_bio.llm_summarise_homepage` | Summary from facts on the page only, or `NONE` |

A cold model load takes over a minute; a verification call about 12–15 s.
The artist page shows a "Research in progress" banner with a timer meanwhile.

See the source and inline documentation in `src/mediacat/llm/`. Full API
reference is generated via `make docs`.
