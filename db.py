"""SQLite persistence for deal room runs.

Uses sqlite3 directly to keep the prototype dependency-light. Nested source
objects and list fields are stored as JSON text on the row.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from schemas import DealRoomUpdate


def _db_path_from_url(database_url: str) -> str:
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///") :]
    parsed = urlparse(database_url)
    if parsed.scheme == "sqlite":
        return parsed.path.lstrip("/") or "deal_room.db"
    return database_url


def get_connection(database_url: Optional[str] = None) -> sqlite3.Connection:
    url = database_url or os.environ.get("DATABASE_URL", "sqlite:///deal_room.db")
    path = _db_path_from_url(url)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    company_name TEXT,
    website TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS company_profiles (
    run_id TEXT,
    company_name TEXT,
    website TEXT,
    sector TEXT,
    description TEXT,
    headquarters TEXT,
    business_model TEXT,
    market_position TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    document_name TEXT,
    document_type TEXT,
    path TEXT,
    processing_status TEXT
);
CREATE TABLE IF NOT EXISTS metric_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    metric_name TEXT,
    value TEXT,
    normalized_value REAL,
    unit TEXT,
    period TEXT,
    as_of_date TEXT,
    source_json TEXT,
    confidence REAL,
    observation_type TEXT,
    review_status TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS computed_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    metric_name TEXT,
    value TEXT,
    formula TEXT,
    input_metric_names_json TEXT,
    confidence REAL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS review_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    task_type TEXT,
    title TEXT,
    description TEXT,
    related_metrics_json TEXT,
    severity TEXT,
    status TEXT
);
CREATE TABLE IF NOT EXISTS agent_trace_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    step_name TEXT,
    tool_used TEXT,
    input_summary TEXT,
    output_summary TEXT,
    decision_reason TEXT
);
"""


def init_db(database_url: Optional[str] = None) -> None:
    conn = get_connection(database_url)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def create_run(run_id: str, company_name: str, website: Optional[str],
               database_url: Optional[str] = None) -> None:
    conn = get_connection(database_url)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO runs (id, company_name, website, created_at) VALUES (?, ?, ?, ?)",
            (run_id, company_name, website, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def persist_deal_room_update(update: DealRoomUpdate, doc_paths: list[str],
                             database_url: Optional[str] = None) -> None:
    """Write the full DealRoomUpdate into SQLite tables."""
    conn = get_connection(database_url)
    try:
        run_id = update.run_id

        # company profile
        cp = update.company_profile
        conn.execute(
            """INSERT INTO company_profiles
               (run_id, company_name, website, sector, description, headquarters,
                business_model, market_position)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, cp.company_name, cp.website, cp.sector, cp.description,
             cp.headquarters, cp.business_model, cp.market_position),
        )

        # documents
        path_by_name = {Path(p).name: p for p in doc_paths}
        for dc in update.document_classifications:
            conn.execute(
                """INSERT INTO documents (run_id, document_name, document_type, path, processing_status)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, dc.document_name, dc.document_type,
                 path_by_name.get(dc.document_name, dc.document_name), "classified"),
            )

        # metric observations
        for obs in update.metric_observations:
            conn.execute(
                """INSERT INTO metric_observations
                   (run_id, metric_name, value, normalized_value, unit, period,
                    as_of_date, source_json, confidence, observation_type,
                    review_status, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, obs.metric_name, obs.value, obs.normalized_value,
                 obs.unit, obs.period, obs.as_of_date,
                 json.dumps(obs.source.model_dump()),
                 obs.confidence, obs.observation_type, obs.review_status,
                 obs.notes),
            )

        # computed metrics
        for cm in update.computed_metrics:
            conn.execute(
                """INSERT INTO computed_metrics
                   (run_id, metric_name, value, formula, input_metric_names_json,
                    confidence, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, cm.metric_name, cm.value, cm.formula,
                 json.dumps(cm.input_metric_names), cm.confidence, cm.notes),
            )

        # review tasks
        for rt in update.review_tasks:
            conn.execute(
                """INSERT INTO review_tasks
                   (run_id, task_type, title, description, related_metrics_json,
                    severity, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, rt.task_type, rt.title, rt.description,
                 json.dumps(rt.related_metrics), rt.severity, "open"),
            )

        # trace
        for step in update.agent_trace:
            conn.execute(
                """INSERT INTO agent_trace_steps
                   (run_id, step_name, tool_used, input_summary, output_summary, decision_reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, step.step_name, step.tool_used, step.input_summary,
                 step.output_summary, step.decision_reason),
            )

        conn.commit()
    finally:
        conn.close()
