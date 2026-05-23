"""Document reading, classification, and metric extraction.

LLM-first with deterministic fallbacks: if the LLM call fails we use keyword
heuristics for classification and regex extraction for metrics, plus a
needs_review task so the human reviewer knows the AI path didn't run cleanly.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import ValidationError

from prompts import (
    DOCUMENT_TYPES,
    document_classification_prompt,
    metric_extraction_prompt,
)
from schemas import DocumentClassification, MetricObservation, SourceReference
from tools.llm_tools import extract_json_with_llm


def read_documents(paths: list[str]) -> list[dict]:
    """Read local .txt/.md files. Returns [{path, name, text}, ...]."""
    out: list[dict] = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            out.append({"path": str(path), "name": path.name, "text": "",
                        "error": "file_not_found"})
            continue
        # PDF support: optional, best-effort
        if path.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader  # optional
                reader = PdfReader(str(path))
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
            except Exception as e:
                text = ""
                out.append({"path": str(path), "name": path.name, "text": "",
                            "error": f"pdf_read_failed: {e}"})
                continue
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                out.append({"path": str(path), "name": path.name, "text": "",
                            "error": f"read_failed: {e}"})
                continue
        out.append({"path": str(path), "name": path.name, "text": text})
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_KEYWORDS = {
    "term_sheet": ["pre-money", "post-money", "valuation cap", "term sheet",
                   "series a", "series b", "series c", "investment amount",
                   "liquidation preference"],
    "financials": ["gaap", "ebitda", "income statement", "balance sheet",
                   "gross margin", "operating income", "net income", "cash flow"],
    "pitch_deck": ["market opportunity", "go-to-market", "traction",
                   "the team", "the problem", "our solution", "tam", "sam", "som"],
    "public_filing": ["10-k", "10-q", "form 10", "annual report",
                      "securities and exchange commission", "sec filing"],
    "contract": ["this agreement", "the parties", "hereinafter referred",
                 "whereas,", "in witness whereof"],
    "transcript": ["q&a", "operator:", "analyst:", "ceo:", "earnings call",
                   "moderator:"],
    "diligence_memo": ["diligence", "investment memo", "thesis",
                       "risks and mitigants", "deal notes"],
}


def _heuristic_classify(document_name: str, text: str) -> DocumentClassification:
    lowered = text.lower()
    scores: dict[str, int] = {}
    for dtype, kws in _KEYWORDS.items():
        scores[dtype] = sum(1 for kw in kws if kw in lowered)
    best = max(scores, key=scores.get) if scores else "unknown"
    if scores.get(best, 0) == 0:
        return DocumentClassification(
            document_name=document_name, document_type="unknown",
            confidence=0.2,
            reasoning="No strong keyword signal in fallback heuristic.",
        )
    conf = min(0.7, 0.3 + 0.1 * scores[best])
    return DocumentClassification(
        document_name=document_name, document_type=best, confidence=conf,
        reasoning=f"Keyword heuristic matched {scores[best]} {best} indicator(s).",
    )


def classify_document(document_text: str, document_name: str) -> DocumentClassification:
    prompt = document_classification_prompt(document_name, document_text)
    result = extract_json_with_llm(prompt, schema_name="DocumentClassification",
                                   max_tokens=512)
    if "_error" in result:
        return _heuristic_classify(document_name, document_text)
    # Force document_name to match what we passed (defend against hallucination)
    result["document_name"] = document_name
    if result.get("document_type") not in DOCUMENT_TYPES:
        result["document_type"] = "unknown"
        result["confidence"] = min(float(result.get("confidence", 0.3)), 0.3)
        result["reasoning"] = (result.get("reasoning") or "") + " (Normalized to 'unknown'.)"
    try:
        return DocumentClassification(**result)
    except ValidationError:
        return _heuristic_classify(document_name, document_text)


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(
    r"(?P<label>[A-Z][A-Za-z _/&-]+?)\s*[:\-]\s*\$\s*(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<suf>[MmBbKk]?)",
)
_PCT_RE = re.compile(r"(?P<label>[A-Z][A-Za-z _-]+?)\s*[:\-]\s*(?P<num>\d+(?:\.\d+)?)\s*%")
_COUNT_RE = re.compile(
    r"(?P<label>[A-Z][A-Za-z _-]+?)\s*[:\-]\s*(?P<num>\d{1,4}(?:,\d{3})*)\s+(?:employees?|people)",
    re.IGNORECASE,
)


def _normalize_money(num_str: str, suffix: str) -> Optional[float]:
    try:
        val = float(num_str.replace(",", ""))
    except ValueError:
        return None
    suffix = suffix.lower()
    if suffix == "k":
        val *= 1_000
    elif suffix == "m":
        val *= 1_000_000
    elif suffix == "b":
        val *= 1_000_000_000
    return val


_LABEL_TO_METRIC = {
    "revenue": "revenue",
    "gaap revenue": "revenue",
    "arr": "arr",
    "annual recurring revenue": "arr",
    "ebitda": "ebitda",
    "gross margin": "gross_margin",
    "valuation": "valuation",
    "pre-money valuation": "pre_money_valuation",
    "post-money valuation": "post_money_valuation",
    "investment amount": "funding_amount",
    "funding amount": "funding_amount",
    "headcount": "headcount",
    "team size": "headcount",
    "employees": "headcount",
    "customers": "customer_count",
    "market cap": "market_cap",
    "market capitalization": "market_cap",
}


def _label_to_metric(label: str) -> Optional[str]:
    cleaned = label.lower().strip().rstrip(":")
    for key, metric in _LABEL_TO_METRIC.items():
        if key in cleaned:
            return metric
    return None


def _regex_extract(document_name: str, document_type: str, text: str) -> list[MetricObservation]:
    out: list[MetricObservation] = []
    seen: set[tuple[str, str]] = set()

    for line in text.splitlines():
        line_s = line.strip()
        if not line_s:
            continue

        m = _MONEY_RE.search(line_s)
        if m:
            metric = _label_to_metric(m.group("label"))
            if metric:
                normalized = _normalize_money(m.group("num"), m.group("suf"))
                value_str = (
                    f"${normalized/1e6:.1f}M" if normalized and normalized >= 1e6
                    else (f"${normalized:,.0f}" if normalized is not None else m.group(0))
                )
                key = (metric, line_s)
                if key not in seen:
                    seen.add(key)
                    out.append(MetricObservation(
                        metric_name=metric, value=value_str,
                        normalized_value=normalized, unit="USD",
                        period=None, as_of_date=None,
                        source=SourceReference(
                            source_type="document", document_name=document_name,
                            quote=line_s,
                            retrieved_at=datetime.now(timezone.utc).isoformat(),
                        ),
                        confidence=0.5, observation_type="extracted",
                        review_status="needs_review",
                        notes="Fallback regex extraction — confirm period and definition.",
                    ))
                    continue

        m = _COUNT_RE.search(line_s)
        if m:
            metric = _label_to_metric(m.group("label"))
            if metric:
                try:
                    normalized = float(m.group("num").replace(",", ""))
                except ValueError:
                    normalized = None
                key = (metric, line_s)
                if key not in seen:
                    seen.add(key)
                    out.append(MetricObservation(
                        metric_name=metric, value=m.group(0).split(":", 1)[-1].strip(),
                        normalized_value=normalized, unit="count",
                        period=None, as_of_date=None,
                        source=SourceReference(
                            source_type="document", document_name=document_name,
                            quote=line_s,
                            retrieved_at=datetime.now(timezone.utc).isoformat(),
                        ),
                        confidence=0.5, observation_type="extracted",
                        review_status="needs_review",
                        notes="Fallback regex extraction — confirm period and definition.",
                    ))
                    continue

        m = _PCT_RE.search(line_s)
        if m:
            metric = _label_to_metric(m.group("label"))
            if metric:
                try:
                    normalized = float(m.group("num")) / 100.0
                except ValueError:
                    normalized = None
                key = (metric, line_s)
                if key not in seen:
                    seen.add(key)
                    out.append(MetricObservation(
                        metric_name=metric, value=f"{m.group('num')}%",
                        normalized_value=normalized, unit="percent",
                        period=None, as_of_date=None,
                        source=SourceReference(
                            source_type="document", document_name=document_name,
                            quote=line_s,
                            retrieved_at=datetime.now(timezone.utc).isoformat(),
                        ),
                        confidence=0.5, observation_type="extracted",
                        review_status="needs_review",
                        notes="Fallback regex extraction — confirm period and definition.",
                    ))
    return out


def extract_metrics_from_document(document_text: str, document_name: str,
                                  classification: DocumentClassification
                                  ) -> tuple[list[MetricObservation], Optional[str]]:
    """Extract structured metric observations.

    Returns (observations, error_or_none). When error is set, the agent should
    create an `external_research_failed`-style review task; observations may
    still be populated via the regex fallback.
    """
    prompt = metric_extraction_prompt(
        document_name, classification.document_type, document_text,
    )
    result = extract_json_with_llm(prompt, schema_name="MetricObservation[]",
                                   max_tokens=4096)

    if "_error" in result:
        fallback = _regex_extract(document_name, classification.document_type, document_text)
        return fallback, result["_error"]

    raw_observations = result.get("observations")
    if raw_observations is None and isinstance(result, list):
        raw_observations = result
    raw_observations = raw_observations or []

    observations: list[MetricObservation] = []
    now = datetime.now(timezone.utc).isoformat()
    for r in raw_observations:
        if not isinstance(r, dict):
            continue
        quote = r.get("source_quote")
        review_status = r.get("review_status", "proposed")
        if not quote:
            review_status = "needs_review"
        confidence = float(r.get("confidence", 0.5))
        if not quote:
            confidence = min(confidence, 0.4)

        try:
            obs = MetricObservation(
                metric_name=r.get("metric_name", "unknown"),
                value=str(r.get("value", "")),
                normalized_value=r.get("normalized_value"),
                unit=r.get("unit"),
                period=r.get("period"),
                as_of_date=r.get("as_of_date"),
                source=SourceReference(
                    source_type="document",
                    document_name=document_name,
                    quote=quote,
                    retrieved_at=now,
                ),
                confidence=confidence,
                observation_type="extracted",
                review_status=review_status if review_status in (
                    "proposed", "needs_review", "rejected", "accepted_candidate"
                ) else "needs_review",
                notes=r.get("notes"),
            )
            observations.append(obs)
        except ValidationError:
            # skip malformed
            continue

    if not observations:
        # Try regex as a backstop even on success-with-zero (e.g. model returned [])
        fallback = _regex_extract(document_name, classification.document_type, document_text)
        # Don't surface error string here — LLM responded, just had no hits.
        return fallback, None

    return observations, None
