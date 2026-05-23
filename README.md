# AI Deal Room Agent — CLI Prototype

This prototype demonstrates the agent workflow for the AI Deal Room, not the
full production web application. It is a command-line program that takes a
target company plus a small bundle of deal documents and produces a structured,
review-ready deal-room update: classified documents, source-quoted metric
observations, web-researched company facts, derived metrics, and human-review
tasks for anything ambiguous or conflicting.

In production, the same outputs would map to the Deal Room data model:
company profile, documents, sources, metric observations, computed metrics,
review tasks, and audit logs.

## Why this exists

Investment teams spend a lot of time copying facts from PDFs, decks, and the
public web into a deal-tracking system, then arguing about which version of
each number to trust. The thesis behind the AI Deal Room is that an agentic
system can do the legwork — gathering, normalizing, and reconciling — while
preserving provenance so a human can quickly approve, reject, or correct each
proposed claim.

The agent does not treat AI outputs as automatically canonical. It preserves
source-backed observations and routes low-confidence or conflicting facts to
human review.

## What it does

For each run, the agent:

1. **Reads documents** from the paths you pass in.
2. **Classifies each document** (pitch deck, term sheet, financials, etc.)
   using the Anthropic Claude API, with a keyword heuristic fallback.
3. **Extracts metric observations** from each document with strict-JSON
   structured LLM prompts, including a verbatim source quote, period, and a
   review status. Falls back to regex extraction if the LLM call fails.
4. **Researches the company on the web** using the Parallel Task API, with a
   typed JSON output schema covering description, sector, headquarters,
   business model, revenue, market cap, headcount, products, and recent
   developments. Time-sensitive figures are marked `needs_review` by default.
5. **Merges** the company profile from user input, Parallel research, and any
   document hints. Disagreements between sources create a review task — they
   never silently overwrite.
6. **Detects conflicts** across observations: same metric + same period with
   materially different values, ARR vs GAAP-revenue ambiguity, and current
   market cap vs historical valuation references.
7. **Computes derived metrics** (YoY revenue growth, valuation / revenue
   multiple, valuation / ARR multiple, gross-margin trend) — only when the
   inputs are unambiguous. Otherwise it explains what's missing and creates a
   review task.
8. **Writes outputs**: a structured JSON update, a Markdown summary, and a
   trace of every agent step. Everything is also persisted to SQLite.

## Install

```bash
cd ai-deal-room-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure API keys

```bash
cp .env.example .env
# edit .env and set:
#   ANTHROPIC_API_KEY=...
#   PARALLEL_API_KEY=...
```

Both keys are required. The CLI prints a clear error and exits if they are
missing. Do not commit real credentials.

## Run the Acme demo

```bash
python main.py demo
```

This runs the bundled Acme Robotics deal: a pitch deck, a term sheet, and a
financial summary. The two revenue framings (pitch-deck ARR of $22M vs
financial-statement GAAP revenue of $18M) are intentional, and the agent will
flag the ARR-vs-revenue ambiguity rather than silently picking one. You should
see at least one computed metric, at least one review task, and full
provenance on every metric.

If you don't want to hit Parallel:

```bash
python main.py demo --no-research
```

## Run arbitrary companies

```bash
python main.py run \
  --company "Tesla" \
  --website "https://www.tesla.com" \
  --docs sample_docs/tesla_deal_notes.txt
```

The bundled Tesla document intentionally has no metrics — the goal is to show
that the agent leans heavily on Parallel for public-company research, and that
time-sensitive figures (market cap, latest revenue) come back as
`needs_review` rather than canonical.

You can pass multiple documents:

```bash
python main.py run \
  --company "Acme Robotics" \
  --website "https://acmerobotics.example" \
  --docs sample_docs/acme_pitch_deck.txt \
         sample_docs/acme_term_sheet.txt \
         sample_docs/acme_financials.txt
```

## Outputs

Every run writes to `outputs/{run_id}/`:

- `deal_profile_update.json` — the full `DealRoomUpdate` (company profile,
  classifications, metric observations with sources/confidence/status,
  computed metrics, review tasks, agent trace, summary). Validated by
  Pydantic before writing.
- `deal_summary.md` — Markdown summary aimed at an investment associate.
- `agent_trace.json` — list of trace steps (input, output, decision reason).

The CLI also prints a Rich-formatted summary of the run to the terminal.

## SQLite persistence

Everything is also written to SQLite (default: `deal_room.db` in the working
directory, configurable via `DATABASE_URL`). Tables:

- `runs` — one row per agent run
- `company_profiles` — merged profile per run
- `documents` — name, type, and processing status per source document
- `metric_observations` — one row per source-backed claim, with the full
  `SourceReference` stored as JSON
- `computed_metrics` — derived metrics with their input metric names
- `review_tasks` — open review items with severity
- `agent_trace_steps` — agent reasoning trail

This mirrors the production deal-room data model and makes the prototype's
outputs queryable across runs.

## How Parallel APIs are used

`tools/parallel_tools.py` is a thin wrapper around the official `parallel`
Python SDK (the `parallel-web` package). The agent depends only on three
internal functions so we can adapt to upstream changes without touching the
rest of the code:

- `parallel_task_research(company_name, website)` — Task API call with a
  typed JSON output schema covering profile fields plus public metrics
  (revenue, market cap, headcount), products, recent developments, and source
  URLs. Citations on the returned `basis` are used to attach URLs to specific
  metric observations.
- `parallel_search(query, objective)` — Search API for source URLs when the
  agent needs to back a specific question with web results.
- `parallel_extract(url, objective)` — Extract API to pull markdown/text from
  a single high-value URL when the Task output isn't enough.

The high-level helper `research_company_with_parallel` calls the Task API,
maps the structured response into `CompanyProfile` and `MetricObservation`
shapes, and lowers confidence + sets `review_status="needs_review"` for
time-sensitive figures (market cap, latest revenue, latest valuation,
headcount).

## How the LLM is used

`tools/llm_tools.py` is a thin wrapper around the Anthropic SDK with one
stable export, `extract_json_with_llm`. It:

- requests strict JSON (no fences, no prose),
- defensively strips fenced JSON if the model includes them,
- retries once with a stricter follow-up if the first response is invalid,
- and on persistent failure returns `{"_error": "..."}` so the caller can
  record a review task and keep going — a single bad call does not crash the
  run.

The LLM is used for document classification and metric extraction. The
summary text is generated deterministically for reliability.

## How conflicts and review tasks work

Every metric observation carries a `review_status`:

- `proposed` — clean, source-backed, unambiguous
- `needs_review` — ambiguous definition, missing period, time-sensitive
  external fact, low confidence, or regex-fallback extraction
- `rejected` / `accepted_candidate` — slots for downstream workflow

The agent then runs three kinds of cross-observation checks and creates
`ReviewTask` records for anything that needs a human:

- **`resolve_conflict`** — same metric + same period, materially different
  values across sources (e.g. pitch-deck revenue ≠ financials revenue).
- **`clarify_definition`** — ARR-vs-GAAP-revenue ambiguity, or current
  market cap vs historical valuation references.
- **`verify_metric`** — profile field disagreement between Parallel and the
  documents.
- **`missing_data`** — unreadable or empty document.
- **`external_research_failed`** — Parallel call or LLM extraction failed.

When derived metrics depend on conflicting inputs, the agent does not
silently pick one. It either skips the computation and creates a
`clarify_definition` task, or computes a tentative value with reduced
confidence and a review task.

## Limitations

- This is a CLI prototype, not the production web application. There is no
  multi-user state, no audit log beyond the per-run agent trace, no auth, no
  webhooks.
- Document parsing is text-only. PDFs are supported best-effort if `pypdf`
  is installed, but the bundled samples are `.txt`.
- The agent is deterministic in orchestration — the LLM is used as a tool,
  not as a planner. The trace is explicit enough to read as an agent log,
  but a production version would likely introduce an LLM-driven planner with
  tool-use.
- The Parallel Task API returns confident-looking output schemas; we mark
  time-sensitive fields `needs_review` to enforce human verification, but a
  production system would add source-quality scoring and as-of-date checks.

## GenAI disclosure

This prototype uses generative AI in two places: (1) the Anthropic Claude
API for document classification and metric extraction, and (2) the Parallel
Task / Search / Extract APIs for company research. Both can produce
incorrect or out-of-date outputs. Every AI-generated claim is preserved as a
source-backed observation with confidence and review status — the agent
proposes, it does not decide. Low-confidence, ambiguous, or conflicting
items are routed to human review rather than written into the deal profile
as fact.
