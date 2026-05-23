"""Agent orchestration for the AI Deal Room Agent.

The flow is deterministic (no LLM-driven planner) but the agent_trace makes
every decision explicit so the run reads like an agent log: which tool was
called, what came back, and why the next step was taken.

Key product principle: AI-generated information is *proposed*, not accepted.
Every claim is wrapped in a MetricObservation with a SourceReference,
confidence, and review_status. Low-confidence, ambiguous, or conflicting
items create ReviewTask records instead of overwriting the deal profile.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from db import create_run, init_db, persist_deal_room_update
from schemas import (
    AgentTraceStep,
    CompanyProfile,
    ComputedMetric,
    DealRoomUpdate,
    DocumentClassification,
    MetricObservation,
    ReviewTask,
    SourceReference,
)
from tools.document_tools import (
    classify_document,
    extract_metrics_from_document,
    read_documents,
)
from tools.metric_tools import (
    compute_derived_metrics,
    detect_conflicts,
    merge_company_profile,
)
from tools.parallel_tools import research_company_with_parallel


OUTPUTS_DIR = Path(__file__).parent / "outputs"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trace(step_name: str, tool_used: str, input_summary: str,
           output_summary: str, decision_reason: str) -> AgentTraceStep:
    return AgentTraceStep(
        step_name=step_name,
        tool_used=tool_used,
        input_summary=input_summary,
        output_summary=output_summary,
        decision_reason=decision_reason,
    )


def _safe_int(maybe) -> int:
    try:
        return int(maybe)
    except Exception:
        return 0


def _generate_markdown_summary(update: DealRoomUpdate,
                               parallel_extras: dict | None) -> str:
    cp = update.company_profile
    parts: list[str] = []
    parts.append(f"# AI Deal Room Update: {cp.company_name}\n")
    parts.append(f"*Run ID:* `{update.run_id}`  ")
    parts.append(f"*Generated:* {_now_iso()}\n")

    parts.append("## Company Profile\n")
    parts.append(f"- **Name:** {cp.company_name}")
    if cp.website:
        parts.append(f"- **Website:** {cp.website}")
    if cp.sector:
        parts.append(f"- **Sector:** {cp.sector}")
    if cp.headquarters:
        parts.append(f"- **Headquarters:** {cp.headquarters}")
    if cp.business_model:
        parts.append(f"- **Business model:** {cp.business_model}")
    if cp.market_position:
        parts.append(f"- **Market position:** {cp.market_position}")
    if cp.description:
        parts.append(f"\n{cp.description}\n")

    parts.append("\n## Documents Processed\n")
    if not update.document_classifications:
        parts.append("_No documents provided._")
    for dc in update.document_classifications:
        parts.append(f"- **{dc.document_name}** → `{dc.document_type}` "
                     f"(confidence {dc.confidence:.2f}) — {dc.reasoning}")

    parts.append("\n## Key Extracted Metrics\n")
    extracted = [o for o in update.metric_observations if o.observation_type == "extracted"]
    if not extracted:
        parts.append("_No document-extracted metrics._")
    else:
        parts.append("| Metric | Value | Period | Source | Confidence | Status |")
        parts.append("|---|---|---|---|---|---|")
        for o in extracted:
            src = o.source.document_name or o.source.source_name or o.source.source_type
            parts.append(
                f"| {o.metric_name} | {o.value} | {o.period or '—'} | {src} "
                f"| {o.confidence:.2f} | {o.review_status} |"
            )

    parts.append("\n## Web Research via Parallel\n")
    researched = [o for o in update.metric_observations if o.observation_type == "researched"]
    if not researched and not parallel_extras:
        parts.append("_No external research performed (or no facts returned)._")
    else:
        if researched:
            parts.append("| Metric | Value | Period | URL | Confidence | Status |")
            parts.append("|---|---|---|---|---|---|")
            for o in researched:
                url = o.source.url or "—"
                parts.append(
                    f"| {o.metric_name} | {o.value} | {o.period or '—'} | {url} "
                    f"| {o.confidence:.2f} | {o.review_status} |"
                )
        if parallel_extras:
            products = parallel_extras.get("major_products") or []
            developments = parallel_extras.get("recent_developments") or []
            if products:
                parts.append("\n**Major products:** " + ", ".join(products))
            if developments:
                parts.append("\n**Recent developments:**")
                for d in developments:
                    parts.append(f"- {d}")

    parts.append("\n## Computed Metrics\n")
    if not update.computed_metrics:
        parts.append("_No derived metrics could be computed (see Review Required)._")
    else:
        for cm in update.computed_metrics:
            parts.append(f"- **{cm.metric_name}**: {cm.value}  ")
            parts.append(f"  formula: `{cm.formula}` — "
                         f"confidence {cm.confidence:.2f}")
            if cm.notes:
                parts.append(f"  notes: {cm.notes}")

    parts.append("\n## Review Required\n")
    if not update.review_tasks:
        parts.append("_No review tasks created._")
    else:
        for rt in update.review_tasks:
            parts.append(f"- **[{rt.severity}] {rt.title}** "
                         f"(`{rt.task_type}`) — {rt.description}")

    parts.append("\n## Agent Trace\n")
    for i, step in enumerate(update.agent_trace, start=1):
        parts.append(f"{i}. **{step.step_name}** (`{step.tool_used}`)  ")
        parts.append(f"   - input: {step.input_summary}")
        parts.append(f"   - output: {step.output_summary}")
        parts.append(f"   - reason: {step.decision_reason}")

    return "\n".join(parts) + "\n"


def _deterministic_summary(update: DealRoomUpdate) -> str:
    cp = update.company_profile
    n_docs = len(update.document_classifications)
    n_obs = len(update.metric_observations)
    n_computed = len(update.computed_metrics)
    n_review = len(update.review_tasks)
    return (
        f"Processed {n_docs} document(s) for {cp.company_name}. "
        f"Recorded {n_obs} metric observation(s), computed {n_computed} "
        f"derived metric(s), and flagged {n_review} item(s) for human review."
    )


def run_agent(
    company_name: str,
    website: Optional[str],
    doc_paths: list[str],
    do_research: bool = True,
    database_url: Optional[str] = None,
) -> tuple[DealRoomUpdate, Path]:
    """Run the deal-room agent end-to-end. Returns (update, run_output_dir)."""

    run_id = "run_" + uuid.uuid4().hex[:12]
    run_dir = OUTPUTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    init_db(database_url)
    create_run(run_id, company_name, website, database_url)

    trace: list[AgentTraceStep] = []
    review_tasks: list[ReviewTask] = []

    trace.append(_trace(
        step_name="Start",
        tool_used="cli",
        input_summary=f"company={company_name!r}, website={website!r}, "
                      f"docs={len(doc_paths)}",
        output_summary=f"run_id={run_id}",
        decision_reason="Initialize a new deal-room run and SQLite record.",
    ))

    # ---- Documents -------------------------------------------------------
    docs = read_documents(doc_paths)
    trace.append(_trace(
        step_name="Read Documents",
        tool_used="read_documents",
        input_summary=f"{len(doc_paths)} path(s)",
        output_summary=f"loaded {sum(1 for d in docs if d.get('text'))} document(s)"
                       + (
                           f", {sum(1 for d in docs if d.get('error'))} error(s)"
                           if any(d.get("error") for d in docs) else ""
                       ),
        decision_reason="Need raw text before classification and metric extraction.",
    ))

    classifications: list[DocumentClassification] = []
    extracted_metrics: list[MetricObservation] = []

    for doc in docs:
        if doc.get("error") or not doc.get("text"):
            classifications.append(DocumentClassification(
                document_name=doc["name"], document_type="unknown",
                confidence=0.0,
                reasoning=f"Could not read document: {doc.get('error', 'empty file')}",
            ))
            review_tasks.append(ReviewTask(
                task_type="missing_data",
                title=f"Unable to read document {doc['name']}",
                description=str(doc.get("error", "Empty content.")),
                related_metrics=[],
                severity="medium",
            ))
            continue

        classification = classify_document(doc["text"], doc["name"])
        classifications.append(classification)
        trace.append(_trace(
            step_name="Classify Document",
            tool_used="llm:classify_document",
            input_summary=f"{doc['name']} ({len(doc['text'])} chars)",
            output_summary=f"type={classification.document_type}, "
                           f"confidence={classification.confidence:.2f}",
            decision_reason="Document type drives downstream metric extraction style "
                            "and review thresholds.",
        ))

        observations, error = extract_metrics_from_document(
            doc["text"], doc["name"], classification,
        )
        extracted_metrics.extend(observations)

        if error:
            review_tasks.append(ReviewTask(
                task_type="external_research_failed",
                title=f"LLM metric extraction failed for {doc['name']}",
                description=f"{error}. Regex fallback may have produced partial results.",
                related_metrics=[],
                severity="medium",
            ))

        trace.append(_trace(
            step_name="Extract Metrics",
            tool_used="llm:extract_metrics" if not error else "regex:extract_metrics_fallback",
            input_summary=f"{doc['name']} ({classification.document_type})",
            output_summary=f"{len(observations)} observation(s)"
                           + (f"; fallback after error: {error}" if error else ""),
            decision_reason="Capture structured, source-quoted metrics for review.",
        ))

    # ---- Parallel research ----------------------------------------------
    parallel_result: dict | None = None
    parallel_extras: dict | None = None
    should_research = do_research and bool(company_name)
    if should_research:
        try:
            parallel_result = research_company_with_parallel(company_name, website)
            researched_observations_raw = parallel_result.get("researched_observations", [])
            parallel_extras = parallel_result.get("raw_parallel_response", {})
            n_researched = len(researched_observations_raw)
            trace.append(_trace(
                step_name="Parallel Research",
                tool_used="parallel.task_run",
                input_summary=f"company={company_name!r}, website={website!r}",
                output_summary=f"profile fields populated; {n_researched} researched "
                               f"observation(s); "
                               f"{len(parallel_extras.get('recent_developments', []))} "
                               f"recent development(s)",
                decision_reason="Augment local documents with source-backed external facts; "
                                "treat time-sensitive figures as needs_review.",
            ))
        except Exception as e:
            review_tasks.append(ReviewTask(
                task_type="external_research_failed",
                title="Parallel web research failed",
                description=f"Parallel API call failed: {e}. Proceeding with local "
                            "documents only — verify company profile manually.",
                related_metrics=[],
                severity="medium",
            ))
            trace.append(_trace(
                step_name="Parallel Research",
                tool_used="parallel.task_run",
                input_summary=f"company={company_name!r}",
                output_summary=f"FAILED: {e}",
                decision_reason="External research unavailable — continue with local docs only.",
            ))

    # ---- Profile merge ---------------------------------------------------
    parallel_profile_dict = (parallel_result or {}).get("company_profile")
    merged_profile, merge_tasks = merge_company_profile(
        user_input={"company_name": company_name, "website": website},
        document_extracted_profile=None,  # we only extract metrics from docs, not profile fields
        parallel_profile=parallel_profile_dict,
    )
    review_tasks.extend(merge_tasks)
    trace.append(_trace(
        step_name="Merge Company Profile",
        tool_used="merge_company_profile",
        input_summary="user input + Parallel research (+ document hints)",
        output_summary=f"merged profile for {merged_profile.company_name}; "
                       f"{len(merge_tasks)} merge conflict task(s)",
        decision_reason="User input wins for identity fields; Parallel preferred for "
                        "fact fields; disagreements never silently overwrite.",
    ))

    # Convert researched observation dicts into proper MetricObservation objects.
    researched_observations: list[MetricObservation] = []
    if parallel_result:
        for raw in parallel_result.get("researched_observations", []):
            try:
                src = raw.get("source", {})
                obs = MetricObservation(
                    metric_name=raw["metric_name"],
                    value=raw["value"],
                    normalized_value=raw.get("normalized_value"),
                    unit=raw.get("unit"),
                    period=raw.get("period"),
                    as_of_date=raw.get("as_of_date"),
                    source=SourceReference(
                        source_type=src.get("source_type", "parallel_task"),
                        source_name=src.get("source_name"),
                        url=src.get("url"),
                        quote=src.get("quote"),
                        retrieved_at=src.get("retrieved_at") or _now_iso(),
                    ),
                    confidence=float(raw.get("confidence", 0.5)),
                    observation_type=raw.get("observation_type", "researched"),
                    review_status=raw.get("review_status", "needs_review"),
                    notes=raw.get("notes"),
                )
                researched_observations.append(obs)
            except (ValidationError, KeyError):
                continue

    all_observations = extracted_metrics + researched_observations

    # ---- Conflict detection ---------------------------------------------
    conflict_tasks = detect_conflicts(all_observations)
    review_tasks.extend(conflict_tasks)
    trace.append(_trace(
        step_name="Conflict Detection",
        tool_used="detect_metric_conflicts",
        input_summary=f"{len(all_observations)} observation(s) across "
                      f"{len({o.metric_name for o in all_observations})} metric(s)",
        output_summary=f"{len(conflict_tasks)} conflict/ambiguity task(s)",
        decision_reason="Different sources may report different definitions or "
                        "as-of dates; reviewers should resolve before publishing.",
    ))

    # ---- Computed metrics -----------------------------------------------
    computed_metrics, compute_tasks = compute_derived_metrics(all_observations)
    review_tasks.extend(compute_tasks)
    trace.append(_trace(
        step_name="Computed Metrics",
        tool_used="compute_derived_metrics",
        input_summary=f"{len(all_observations)} observation(s)",
        output_summary=f"{len(computed_metrics)} computed metric(s), "
                       f"{len(compute_tasks)} compute review task(s)",
        decision_reason="Only compute when inputs are unambiguous; flag the rest "
                        "for human review.",
    ))

    # ---- Assemble + persist + write outputs -----------------------------
    update = DealRoomUpdate(
        run_id=run_id,
        company_profile=merged_profile,
        document_classifications=classifications,
        metric_observations=all_observations,
        computed_metrics=computed_metrics,
        review_tasks=review_tasks,
        agent_trace=trace,
        summary="",
    )
    update.summary = _deterministic_summary(update)

    # validate via pydantic by re-parsing
    update = DealRoomUpdate(**update.model_dump())

    persist_deal_room_update(update, doc_paths, database_url)

    (run_dir / "deal_profile_update.json").write_text(
        json.dumps(update.model_dump(), indent=2, default=str), encoding="utf-8",
    )
    (run_dir / "deal_summary.md").write_text(
        _generate_markdown_summary(update, parallel_extras), encoding="utf-8",
    )
    (run_dir / "agent_trace.json").write_text(
        json.dumps([s.model_dump() for s in update.agent_trace], indent=2),
        encoding="utf-8",
    )

    return update, run_dir
