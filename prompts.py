"""Structured prompts for the AI Deal Room Agent.

All prompts request strict JSON output (no markdown fences). The LLM wrapper
strips fences defensively and validates against Pydantic.
"""
from __future__ import annotations


DOCUMENT_TYPES = [
    "pitch_deck",
    "term_sheet",
    "financials",
    "diligence_memo",
    "contract",
    "transcript",
    "public_filing",
    "unknown",
]


def document_classification_prompt(document_name: str, document_text: str) -> str:
    snippet = document_text[:4000]
    types_list = ", ".join(DOCUMENT_TYPES)
    return f"""You are classifying a deal-room document for an investment team.

Return STRICT JSON only. No markdown fences. No commentary. The JSON must match this schema:
{{
  "document_name": "<echo the provided name>",
  "document_type": "<one of: {types_list}>",
  "confidence": <float between 0.0 and 1.0>,
  "reasoning": "<one or two sentences explaining the classification>"
}}

Document name: {document_name}

Document content (first 4000 chars):
---
{snippet}
---

Choose the single best document_type. If unclear, use "unknown" with low confidence."""


def metric_extraction_prompt(document_name: str, document_type: str, document_text: str) -> str:
    snippet = document_text[:8000]
    return f"""You are extracting structured financial and operational metrics from a deal-room document.

Return STRICT JSON only. No markdown fences. No commentary. The output must be a JSON object of the form:
{{ "observations": [<observation>, ...] }}

Each observation must match this schema EXACTLY:
{{
  "metric_name": "<canonical snake_case name, e.g. revenue, arr, gross_margin, ebitda, valuation, funding_amount, headcount, customer_count, growth_rate, market_share, market_cap, pre_money_valuation, post_money_valuation>",
  "value": "<the value as it appears in the document, e.g. '$22M', '64%', '140 employees'>",
  "normalized_value": <float or null, e.g. 22000000 for $22M, 0.64 for 64%>,
  "unit": "<USD, percent, count, or null>",
  "period": "<e.g. FY2024, Q3 2024, 2023, or null>",
  "as_of_date": "<YYYY-MM-DD or null>",
  "source_quote": "<exact verbatim quote from the document>",
  "confidence": <float between 0.0 and 1.0>,
  "review_status": "<proposed | needs_review>",
  "notes": "<optional clarifying note, or null>"
}}

Rules:
- Extract only metrics that are directly supported by the document. Do NOT invent values.
- Always include source_quote — copy the relevant sentence verbatim.
- If the metric definition is ambiguous (e.g. "revenue" that could be ARR vs GAAP), set review_status="needs_review" and explain in notes.
- If the value lacks a clear period, set review_status="needs_review".
- Use proposed for clean, source-backed values.
- normalized_value: convert "$22M" -> 22000000, "64%" -> 0.64, "140 employees" -> 140. Use null if not numeric.
- If no metrics are present, return {{"observations": []}}.

Document name: {document_name}
Document type: {document_type}

Document content (first 8000 chars):
---
{snippet}
---"""


def company_profile_research_prompt(company_name: str, website: str | None,
                                     parallel_findings: str) -> str:
    return f"""You are synthesizing a company profile from external web research.

Return STRICT JSON only. No markdown fences. No commentary. Match this schema:
{{
  "company_name": "{company_name}",
  "website": <string or null>,
  "sector": <string or null>,
  "description": <string or null>,
  "headquarters": <string or null>,
  "business_model": <string or null>,
  "market_position": <string or null>
}}

Rules:
- Only fill a field if the research below supports it. Use null otherwise.
- Keep description to 1-3 sentences.
- Do not invent unknown fields.

Company name: {company_name}
Provided website: {website or "<none>"}

External research findings:
---
{parallel_findings}
---"""
