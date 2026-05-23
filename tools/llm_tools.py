"""Anthropic LLM wrapper.

Exposes a single stable internal function, `extract_json_with_llm`, used by
the document classifier, metric extractor, and company-profile synthesizer.

Reliability behavior:
- Requests strict JSON.
- Strips markdown fences defensively.
- Retries once on JSON parse failure with a stricter follow-up.
- Returns {"_error": "..."} on persistent failure rather than raising, so the
  agent can record a review task and keep going.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    # Sometimes the model wraps JSON in prose; try to find the first {/[
    if not (text.startswith("{") or text.startswith("[")):
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if m:
            text = m.group(1)
    return text


def _get_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    # Lazy import so missing optional dep doesn't break test runs.
    from anthropic import Anthropic
    return Anthropic(api_key=api_key)


def _model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")


def _call_llm(prompt: str, max_tokens: int = 4096) -> str:
    client = _get_client()
    msg = client.messages.create(
        model=_model(),
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    # Concatenate text blocks
    parts = []
    for block in msg.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def extract_json_with_llm(prompt: str, schema_name: str = "result",
                          max_tokens: int = 4096) -> dict[str, Any]:
    """Call the LLM and return parsed JSON.

    On success: returns the parsed dict.
    On persistent failure: returns {"_error": "...", "_raw": "..."} so the
    caller can record a review task and skip the result instead of crashing.
    """
    try:
        raw = _call_llm(prompt, max_tokens=max_tokens)
    except Exception as e:  # network, auth, rate limit, etc.
        return {"_error": f"LLM call failed: {e}", "_raw": ""}

    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Retry once with a stricter follow-up.
    retry_prompt = (
        prompt
        + "\n\nYour previous response was not valid JSON. Respond with ONLY the JSON object, "
        "no prose, no markdown fences."
    )
    try:
        raw2 = _call_llm(retry_prompt, max_tokens=max_tokens)
    except Exception as e:
        return {"_error": f"LLM retry failed: {e}", "_raw": raw}

    cleaned2 = _strip_fences(raw2)
    try:
        return json.loads(cleaned2)
    except json.JSONDecodeError as e:
        return {
            "_error": f"LLM produced invalid JSON for {schema_name}: {e}",
            "_raw": raw2[:500],
        }
