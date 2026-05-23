"""Parallel Web Systems integration.

Thin wrappers around the official `parallel` Python SDK (parallel-web pkg)
so the rest of the agent depends only on stable internal function names.

Verified SDK surface (parallel-web==0.6.0):
- client = parallel.Parallel()  (reads PARALLEL_API_KEY)
- client.task_run.create(input=..., processor=..., task_spec=...) -> TaskRun
- client.task_run.result(run_id, api_timeout=...) -> TaskRunResult
  - .output: TaskRunJsonOutput | TaskRunTextOutput  (.content, .basis)
- client.search(objective=..., search_queries=[...], max_chars_total=...) -> SearchResult
  - .results: list[WebSearchResult]  (.url, .title, .excerpts, .publish_date)
- client.extract(urls=[...], objective=...) -> ExtractResponse
  - .results: list[ExtractResult]  (.url, .title, .full_content, .excerpts)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional


# Output schema for the company research Task API call.
_COMPANY_RESEARCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": {"type": ["string", "null"], "description": "1-3 sentence description of what the company does."},
        "sector": {"type": ["string", "null"], "description": "Industry sector, e.g. 'Electric Vehicles', 'AI Software'."},
        "headquarters": {"type": ["string", "null"], "description": "City and country of HQ."},
        "business_model": {"type": ["string", "null"], "description": "How the company makes money."},
        "market_position": {"type": ["string", "null"], "description": "Competitive position / notable share or rank."},
        "is_public": {"type": ["boolean", "null"], "description": "Whether the company is publicly traded."},
        "ticker": {"type": ["string", "null"], "description": "Stock ticker if public."},
        "latest_annual_revenue_usd": {"type": ["number", "null"], "description": "Most recent annual revenue in USD."},
        "latest_annual_revenue_period": {"type": ["string", "null"], "description": "The fiscal period the revenue applies to, e.g. 'FY2024'."},
        "latest_market_cap_usd": {"type": ["number", "null"], "description": "Latest market capitalization in USD if public."},
        "latest_valuation_usd": {"type": ["number", "null"], "description": "Latest reported valuation in USD if private."},
        "headcount": {"type": ["integer", "null"], "description": "Approximate employee count."},
        "major_products": {"type": "array", "items": {"type": "string"}, "description": "Notable products or product lines."},
        "recent_developments": {"type": "array", "items": {"type": "string"}, "description": "Recent (within ~12 months) notable developments."},
        "source_urls": {"type": "array", "items": {"type": "string"}, "description": "Source URLs that back the above facts."},
    },
    "required": [
        "description", "sector", "headquarters", "business_model",
        "market_position", "is_public", "major_products",
        "recent_developments", "source_urls",
    ],
}


def _get_client():
    api_key = os.environ.get("PARALLEL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "PARALLEL_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    from parallel import Parallel
    return Parallel(api_key=api_key)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Internal stable wrappers
# ---------------------------------------------------------------------------

def parallel_task_research(company_name: str, website: Optional[str]) -> dict:
    """Run a structured Task API call for company research.

    Returns a dict with:
        {"content": <dict matching _COMPANY_RESEARCH_SCHEMA>,
         "basis": [<citations>], "run_id": "...", "retrieved_at": "..."}
    Raises on hard failure; the caller wraps it in try/except.
    """
    client = _get_client()
    site_hint = f" Their website is {website}." if website else ""
    input_text = (
        f"Research the company '{company_name}'.{site_hint} "
        "Find the following facts using reliable public sources: a 1-3 sentence "
        "description of what the company does; its industry sector; headquarters "
        "city; business model; market position; whether it is publicly traded "
        "(and ticker); most recent reported annual revenue in USD and the fiscal "
        "period; latest market capitalization (if public) or latest private "
        "valuation; approximate headcount; major products; notable recent "
        "developments in the last 12 months; and the source URLs you used. "
        "If a field cannot be found from reliable sources, return null."
    )

    task_run = client.task_run.create(
        input=input_text,
        processor="base",
        task_spec={
            "output_schema": {
                "type": "json",
                "json_schema": _COMPANY_RESEARCH_SCHEMA,
            }
        },
    )
    result = client.task_run.result(task_run.run_id, api_timeout=600)

    output = result.output
    content = getattr(output, "content", None)
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            content = {"raw_text": content}

    basis = getattr(output, "basis", None) or []
    # Normalize basis to plain dicts
    basis_out: list[dict] = []
    for b in basis:
        try:
            basis_out.append(b.model_dump() if hasattr(b, "model_dump") else dict(b))
        except Exception:
            basis_out.append({"repr": str(b)})

    return {
        "content": content or {},
        "basis": basis_out,
        "run_id": task_run.run_id,
        "retrieved_at": _now_iso(),
    }


def parallel_search(query: str, objective: str) -> list[dict]:
    """Run a Search API call. Returns a list of {url, title, excerpts, publish_date}."""
    client = _get_client()
    res = client.search(
        objective=objective,
        search_queries=[query],
        max_chars_total=6000,
    )
    out: list[dict] = []
    for r in res.results or []:
        out.append({
            "url": getattr(r, "url", None),
            "title": getattr(r, "title", None),
            "excerpts": list(getattr(r, "excerpts", []) or []),
            "publish_date": getattr(r, "publish_date", None),
        })
    return out


def parallel_extract(url: str, objective: Optional[str] = None) -> dict:
    """Run an Extract API call for a single URL.

    Returns: {url, title, full_content, excerpts}.
    """
    client = _get_client()
    kwargs: dict[str, Any] = {"urls": [url]}
    if objective:
        kwargs["objective"] = objective
    res = client.extract(**kwargs)

    if not res.results:
        return {"url": url, "title": None, "full_content": None, "excerpts": []}
    r = res.results[0]
    return {
        "url": getattr(r, "url", url),
        "title": getattr(r, "title", None),
        "full_content": getattr(r, "full_content", None),
        "excerpts": list(getattr(r, "excerpts", []) or []),
    }


# ---------------------------------------------------------------------------
# High-level: research a company and return MetricObservation-shaped facts.
# ---------------------------------------------------------------------------

def _mk_source(url: Optional[str], quote: Optional[str]) -> dict:
    return {
        "source_type": "parallel_task",
        "source_name": "Parallel Task API",
        "url": url,
        "quote": quote,
        "retrieved_at": _now_iso(),
    }


def _confidence_for_field(field_name: str, has_basis: bool) -> float:
    # Time-sensitive facts get lower default confidence so they hit review.
    time_sensitive = {
        "latest_market_cap_usd", "latest_annual_revenue_usd",
        "latest_valuation_usd", "headcount", "recent_developments",
    }
    base = 0.65 if has_basis else 0.45
    if field_name in time_sensitive:
        base -= 0.1
    return round(max(0.1, min(0.9, base)), 2)


def _build_observations_from_task_content(content: dict, basis: list[dict]) -> list[dict]:
    """Turn the structured Task output into MetricObservation-shaped dicts."""
    # Build a quick lookup of citations by field name (basis items carry a
    # `field` pointer when present).
    citations_by_field: dict[str, dict] = {}
    for b in basis:
        field = b.get("field")
        if field and field not in citations_by_field:
            citations_by_field[field] = b

    observations: list[dict] = []

    field_to_metric = [
        ("latest_annual_revenue_usd", "revenue", "USD"),
        ("latest_market_cap_usd", "market_cap", "USD"),
        ("latest_valuation_usd", "valuation", "USD"),
        ("headcount", "headcount", "count"),
    ]

    for field, metric_name, unit in field_to_metric:
        val = content.get(field)
        if val is None:
            continue
        cit = citations_by_field.get(field, {})
        # Pull a URL and quote if we can.
        cit_citations = cit.get("citations") or []
        first_cit = cit_citations[0] if cit_citations else {}
        url = first_cit.get("url")
        quote = None
        excerpts = first_cit.get("excerpts") or []
        if excerpts:
            quote = excerpts[0][:300] if isinstance(excerpts[0], str) else None
        period = None
        if field == "latest_annual_revenue_usd":
            period = content.get("latest_annual_revenue_period")

        has_basis = bool(cit_citations)
        confidence = _confidence_for_field(field, has_basis)
        review_status = "needs_review" if confidence < 0.6 else "proposed"
        # Always send time-sensitive market_cap to review.
        if metric_name == "market_cap":
            review_status = "needs_review"

        normalized = float(val) if isinstance(val, (int, float)) else None
        value_str = (
            f"${normalized/1e9:.2f}B" if normalized and normalized >= 1e9
            else f"${normalized/1e6:.2f}M" if normalized and normalized >= 1e6
            else (str(int(normalized)) if normalized is not None else str(val))
        )

        observations.append({
            "metric_name": metric_name,
            "value": value_str,
            "normalized_value": normalized,
            "unit": unit,
            "period": period,
            "as_of_date": None,
            "source": _mk_source(url, quote),
            "confidence": confidence,
            "observation_type": "researched",
            "review_status": review_status,
            "notes": (
                "Time-sensitive market metric — verify against current data."
                if review_status == "needs_review" else None
            ),
        })

    return observations


def research_company_with_parallel(company_name: str, website: Optional[str]) -> dict:
    """High-level company research using the Parallel Task API.

    Returns:
        {
          "company_profile": {<CompanyProfile-compatible dict>},
          "researched_observations": [<MetricObservation-compatible dict>, ...],
          "sources": [<SourceReference-compatible dict>, ...],
          "raw_parallel_response": <dict>,
        }
    """
    raw = parallel_task_research(company_name, website)
    content = raw.get("content", {}) or {}
    basis = raw.get("basis", []) or []

    profile = {
        "company_name": company_name,
        "website": website,
        "sector": content.get("sector"),
        "description": content.get("description"),
        "headquarters": content.get("headquarters"),
        "business_model": content.get("business_model"),
        "market_position": content.get("market_position"),
    }

    researched_observations = _build_observations_from_task_content(content, basis)

    # Flat list of sources.
    sources: list[dict] = []
    for url in content.get("source_urls") or []:
        sources.append({
            "source_type": "parallel_task",
            "source_name": "Parallel Task API",
            "url": url,
            "retrieved_at": raw.get("retrieved_at"),
        })

    return {
        "company_profile": profile,
        "researched_observations": researched_observations,
        "sources": sources,
        "raw_parallel_response": {
            "content": content,
            "basis": basis,
            "run_id": raw.get("run_id"),
            "retrieved_at": raw.get("retrieved_at"),
            "recent_developments": content.get("recent_developments", []),
            "major_products": content.get("major_products", []),
        },
    }
