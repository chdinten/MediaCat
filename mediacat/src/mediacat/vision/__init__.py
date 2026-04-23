"""Vision adapters — transcription of labels, OBI, and runout etchings.

Uses a pluggable VLM (vision-language model) to extract structured
fields from media images.  Local VLM (Ollama) is the default; API
(Anthropic vision) is the fallback.

Submodules
----------
* ``adapter``    — provider-agnostic interface, hybrid fallback
* ``prompts``    — task-specific prompt templates for label/obi/runout
* ``candidates`` — match vision output against the token reference table
"""

from __future__ import annotations
