"""Conflict detection, derived metric computation, and profile merging."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

from schemas import (
    CompanyProfile,
    ComputedMetric,
    MetricObservation,
    ReviewTask,
)


# ---------------------------------------------------------------------------
# Profile merge
# ---------------------------------------------------------------------------

def merge_company_profile(
    user_input: dict,
    document_extracted_profile: Optional[CompanyProfile],
    parallel_profile: Optional[dict],
) -> tuple[CompanyProfile, list[ReviewTask]]:
    """Merge profile fields with explicit priority.

    Priority:
      1. user-provided company_name/website (always wins)
      2. parallel_profile fields (reliable web sources)
      3. document_extracted_profile fields
    Disagreements create a verify_metric review task — never silent overwrite.
    """
    review_tasks: list[ReviewTask] = []
    parallel_profile = parallel_profile or {}
    doc_profile = (document_extracted_profile.model_dump()
                   if document_extracted_profile else {})

    company_name = (user_input.get("company_name")
                    or parallel_profile.get("company_name")
                    or doc_profile.get("company_name") or "Unknown")
    website = (user_input.get("website")
               or parallel_profile.get("website")
               or doc_profile.get("website"))

    merged: dict = {"company_name": company_name, "website": website}

    for field in ("sector", "description", "headquarters",
                  "business_model", "market_position"):
        p_val = parallel_profile.get(field)
        d_val = doc_profile.get(field)
        if p_val and d_val and p_val.strip().lower() != d_val.strip().lower():
            review_tasks.append(ReviewTask(
                task_type="verify_metric",
                title=f"Conflicting {field} from document vs web research",
                description=(
                    f"Document says '{d_val}'. Parallel web research says "
                    f"'{p_val}'. Choose the canonical value before publishing."
                ),
                related_metrics=[field],
                severity="medium",
            ))
            merged[field] = p_val  # prefer Parallel, but flag for review
        else:
            merged[field] = p_val or d_val

    return CompanyProfile(**merged), review_tasks


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

_NUMERIC_TOLERANCE = 0.05  # 5 percent


def _materially_different(a: float, b: float, tol: float = _NUMERIC_TOLERANCE) -> bool:
    if a == 0 and b == 0:
        return False
    base = max(abs(a), abs(b))
    return abs(a - b) / base > tol


def detect_conflicts(observations: list[MetricObservation]) -> list[ReviewTask]:
    """Detect cross-observation conflicts and ambiguity."""
    tasks: list[ReviewTask] = []

    # 1. same metric + same period + materially different normalized value
    by_metric_period: dict[tuple[str, str], list[MetricObservation]] = defaultdict(list)
    for obs in observations:
        if obs.normalized_value is None:
            continue
        key = (obs.metric_name, obs.period or "")
        by_metric_period[key].append(obs)

    for (metric, period), group in by_metric_period.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if _materially_different(a.normalized_value, b.normalized_value):
                    period_label = period or "unspecified period"
                    tasks.append(ReviewTask(
                        task_type="resolve_conflict",
                        title=f"Conflict in {metric} for {period_label}",
                        description=(
                            f"{a.value} from "
                            f"{a.source.document_name or a.source.source_name or a.source.source_type}"
                            f" vs {b.value} from "
                            f"{b.source.document_name or b.source.source_name or b.source.source_type}."
                            " Resolve before publishing this metric."
                        ),
                        related_metrics=[metric],
                        severity="high",
                    ))

    # 2. ARR vs revenue ambiguity (same or overlapping period)
    revenue_periods = {(o.period or ""): o for o in observations if o.metric_name == "revenue"}
    arr_periods = {(o.period or ""): o for o in observations if o.metric_name == "arr"}
    for period, arr_obs in arr_periods.items():
        rev_obs = revenue_periods.get(period)
        if rev_obs is None and revenue_periods:
            # any revenue at all is enough to ask
            rev_obs = next(iter(revenue_periods.values()))
        if rev_obs is not None:
            tasks.append(ReviewTask(
                task_type="clarify_definition",
                title=f"ARR vs GAAP revenue ambiguity ({period or 'unspecified period'})",
                description=(
                    f"ARR observed as {arr_obs.value} (source: "
                    f"{arr_obs.source.document_name or arr_obs.source.source_type}) "
                    f"and GAAP revenue observed as {rev_obs.value} (source: "
                    f"{rev_obs.source.document_name or rev_obs.source.source_type}). "
                    "These are different metrics; confirm which one to use for "
                    "valuation multiples and growth analytics."
                ),
                related_metrics=["arr", "revenue"],
                severity="high",
            ))

    # 3. market cap (current) vs valuation references in documents
    has_market_cap = any(o.metric_name == "market_cap" for o in observations)
    valuation_obs = [o for o in observations
                     if o.metric_name in ("valuation", "pre_money_valuation",
                                          "post_money_valuation")]
    if has_market_cap and valuation_obs:
        tasks.append(ReviewTask(
            task_type="clarify_definition",
            title="Current market cap vs historical valuation references",
            description=(
                "Public-market market_cap from web research and "
                f"{len(valuation_obs)} document valuation reference(s) have "
                "different as-of dates. Treat them as different metrics and "
                "confirm which figure is appropriate for the deal context."
            ),
            related_metrics=["market_cap", "valuation"],
            severity="medium",
        ))

    return tasks


# ---------------------------------------------------------------------------
# Computed metrics
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"(?:FY\s*)?(20\d{2})")


def _extract_year(period: Optional[str]) -> Optional[int]:
    if not period:
        return None
    m = _YEAR_RE.search(period)
    return int(m.group(1)) if m else None


def _pick_unambiguous(observations: list[MetricObservation], metric: str,
                      period_year: int) -> Optional[tuple[int, MetricObservation]]:
    """Return (index, obs) for a single matching observation or None if ambiguous."""
    candidates: list[tuple[int, MetricObservation]] = []
    for i, obs in enumerate(observations):
        if obs.metric_name != metric:
            continue
        y = _extract_year(obs.period)
        if y == period_year and obs.normalized_value is not None:
            candidates.append((i, obs))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Prefer review_status=proposed
        proposed = [c for c in candidates if c[1].review_status == "proposed"]
        if len(proposed) == 1:
            return proposed[0]
    return None


def compute_derived_metrics(
    observations: list[MetricObservation],
) -> tuple[list[ComputedMetric], list[ReviewTask]]:
    computed: list[ComputedMetric] = []
    tasks: list[ReviewTask] = []

    # YoY revenue growth — prefer GAAP revenue.
    years = sorted({_extract_year(o.period) for o in observations
                    if o.metric_name == "revenue" and _extract_year(o.period)})
    if len(years) >= 2:
        prev_year, latest_year = years[-2], years[-1]
        prev = _pick_unambiguous(observations, "revenue", prev_year)
        cur = _pick_unambiguous(observations, "revenue", latest_year)
        if prev and cur:
            pi, p_obs = prev
            ci, c_obs = cur
            if p_obs.normalized_value and p_obs.normalized_value > 0:
                growth = (c_obs.normalized_value - p_obs.normalized_value) / p_obs.normalized_value
                computed.append(ComputedMetric(
                    metric_name="yoy_revenue_growth",
                    value=f"{growth * 100:.1f}%",
                    formula=f"(revenue_{latest_year} - revenue_{prev_year}) / revenue_{prev_year}",
                    input_metric_names=["revenue"],
                    input_observation_indexes=[pi, ci],
                    confidence=min(p_obs.confidence, c_obs.confidence),
                    notes=f"Computed from GAAP revenue {prev_year} -> {latest_year}.",
                ))
        elif (any(o.metric_name == "revenue" for o in observations)
              and any(o.metric_name == "arr" for o in observations)):
            tasks.append(ReviewTask(
                task_type="clarify_definition",
                title="YoY revenue growth not computed",
                description=(
                    "Cannot compute YoY growth: revenue and ARR are mixed across "
                    "periods. Pick one definition and rerun the computation."
                ),
                related_metrics=["revenue", "arr", "yoy_revenue_growth"],
                severity="medium",
            ))

    # valuation / revenue multiple (latest year)
    latest_valuation = _latest_observation(observations, ("valuation",
                                                          "pre_money_valuation",
                                                          "post_money_valuation"))
    latest_revenue = _latest_year_observation(observations, "revenue")
    if latest_valuation and latest_revenue:
        vi, v_obs = latest_valuation
        ri, r_obs = latest_revenue
        if r_obs.normalized_value and r_obs.normalized_value > 0:
            mult = v_obs.normalized_value / r_obs.normalized_value
            computed.append(ComputedMetric(
                metric_name="valuation_to_revenue_multiple",
                value=f"{mult:.1f}x",
                formula=f"{v_obs.metric_name} / revenue",
                input_metric_names=[v_obs.metric_name, "revenue"],
                input_observation_indexes=[vi, ri],
                confidence=min(v_obs.confidence, r_obs.confidence),
                notes=f"Based on {v_obs.metric_name} ({v_obs.value}) and "
                      f"revenue {r_obs.period or 'latest'} ({r_obs.value}).",
            ))

    # valuation / ARR multiple
    latest_arr = _latest_year_observation(observations, "arr")
    if latest_valuation and latest_arr:
        vi, v_obs = latest_valuation
        ai, a_obs = latest_arr
        if a_obs.normalized_value and a_obs.normalized_value > 0:
            mult = v_obs.normalized_value / a_obs.normalized_value
            computed.append(ComputedMetric(
                metric_name="valuation_to_arr_multiple",
                value=f"{mult:.1f}x",
                formula=f"{v_obs.metric_name} / arr",
                input_metric_names=[v_obs.metric_name, "arr"],
                input_observation_indexes=[vi, ai],
                confidence=min(v_obs.confidence, a_obs.confidence),
                notes=f"Based on {v_obs.metric_name} ({v_obs.value}) and "
                      f"ARR ({a_obs.value}).",
            ))

    # gross margin trend
    gm_obs = [(i, o) for i, o in enumerate(observations)
              if o.metric_name == "gross_margin" and o.normalized_value is not None]
    if len(gm_obs) >= 2:
        # take first two for prototype
        (i1, o1), (i2, o2) = gm_obs[0], gm_obs[1]
        delta = o2.normalized_value - o1.normalized_value
        computed.append(ComputedMetric(
            metric_name="gross_margin_trend",
            value=f"{delta * 100:+.1f} ppt",
            formula="gross_margin_b - gross_margin_a",
            input_metric_names=["gross_margin"],
            input_observation_indexes=[i1, i2],
            confidence=min(o1.confidence, o2.confidence),
            notes="Direction of change between two reported gross margin values.",
        ))

    return computed, tasks


def _latest_observation(observations: list[MetricObservation],
                        metric_names: tuple[str, ...]
                        ) -> Optional[tuple[int, MetricObservation]]:
    best: Optional[tuple[int, MetricObservation]] = None
    for i, obs in enumerate(observations):
        if obs.metric_name not in metric_names:
            continue
        if obs.normalized_value is None:
            continue
        if best is None or obs.confidence > best[1].confidence:
            best = (i, obs)
    return best


def _latest_year_observation(observations: list[MetricObservation], metric: str
                             ) -> Optional[tuple[int, MetricObservation]]:
    best: Optional[tuple[int, MetricObservation]] = None
    best_year = -1
    for i, obs in enumerate(observations):
        if obs.metric_name != metric or obs.normalized_value is None:
            continue
        y = _extract_year(obs.period) or 0
        if y >= best_year:
            best_year = y
            best = (i, obs)
    return best
