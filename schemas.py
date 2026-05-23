"""Pydantic models for the AI Deal Room Agent.

These are the canonical data shapes the agent produces. Every AI-generated
claim is wrapped in a MetricObservation (with a SourceReference + confidence
+ review_status) so nothing is silently accepted as fact.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class CompanyProfile(BaseModel):
    company_name: str
    website: Optional[str] = None
    sector: Optional[str] = None
    description: Optional[str] = None
    headquarters: Optional[str] = None
    business_model: Optional[str] = None
    market_position: Optional[str] = None


class DocumentClassification(BaseModel):
    document_name: str
    document_type: str  # pitch_deck, term_sheet, financials, diligence_memo, contract, transcript, public_filing, unknown
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class SourceReference(BaseModel):
    source_type: str  # document, parallel_search, parallel_task, parallel_extract, user_input
    source_name: Optional[str] = None
    document_name: Optional[str] = None
    section: Optional[str] = None
    quote: Optional[str] = None
    url: Optional[str] = None
    retrieved_at: Optional[str] = None


class MetricObservation(BaseModel):
    metric_name: str
    value: str
    normalized_value: Optional[float] = None
    unit: Optional[str] = None
    period: Optional[str] = None
    as_of_date: Optional[str] = None
    source: SourceReference
    confidence: float = Field(ge=0.0, le=1.0)
    observation_type: str  # extracted, researched, user_provided
    review_status: str  # proposed, needs_review, rejected, accepted_candidate
    notes: Optional[str] = None


class ComputedMetric(BaseModel):
    metric_name: str
    value: str
    formula: str
    input_metric_names: List[str]
    input_observation_indexes: Optional[List[int]] = None
    confidence: float = Field(ge=0.0, le=1.0)
    notes: Optional[str] = None


class ReviewTask(BaseModel):
    task_type: str  # verify_metric, resolve_conflict, clarify_definition, missing_data, external_research_failed
    title: str
    description: str
    related_metrics: List[str] = Field(default_factory=list)
    severity: str  # low, medium, high


class AgentTraceStep(BaseModel):
    step_name: str
    tool_used: str
    input_summary: str
    output_summary: str
    decision_reason: str


class DealRoomUpdate(BaseModel):
    run_id: str
    company_profile: CompanyProfile
    document_classifications: List[DocumentClassification] = Field(default_factory=list)
    metric_observations: List[MetricObservation] = Field(default_factory=list)
    computed_metrics: List[ComputedMetric] = Field(default_factory=list)
    review_tasks: List[ReviewTask] = Field(default_factory=list)
    agent_trace: List[AgentTraceStep] = Field(default_factory=list)
    summary: str = ""
