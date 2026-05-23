Build a Python CLI prototype for an "AI Deal Room Agent" for an interview case study.

The goal is to demonstrate how an agentic system can reduce manual data entry and research work for an investment team by gathering company information, processing deal documents, extracting structured metrics, detecting ambiguity/conflicts, computing derived metrics, and creating human-review tasks.

This should be a command-line prototype, not a web app.

Use:
- Python 3.11+
- Pydantic
- SQLite
- Real LLM API via Anthropic Claude SDK
- Real Parallel Web Systems APIs
- JSON + Markdown outputs
- Typer or argparse for CLI
- Rich for readable terminal output
- python-dotenv for env vars
- SQLAlchemy or sqlite3 for SQLite

Important product principle:
AI-generated information is proposed, not blindly accepted. The system should preserve every claim as a sourced observation with confidence and review status. Low-confidence, ambiguous, or conflicting information should create human-review tasks instead of silently overwriting the deal profile.

The CLI should support both a sample/demo company and arbitrary companies.

Example commands:

python main.py demo

python main.py run \
  --company "Tesla" \
  --website "https://www.tesla.com" \
  --docs sample_docs/tesla_deal_notes.txt

python main.py run \
  --company "Acme Robotics" \
  --website "https://acmerobotics.example" \
  --docs sample_docs/acme_pitch_deck.txt sample_docs/acme_term_sheet.txt sample_docs/acme_financials.txt

The agent should output:
- outputs/{run_id}/deal_profile_update.json
- outputs/{run_id}/deal_summary.md
- outputs/{run_id}/agent_trace.json

It should also persist structured run data to SQLite.

Create this project structure:

ai-deal-room-agent/
  README.md
  main.py
  agent.py
  schemas.py
  db.py
  prompts.py
  tools/
    __init__.py
    document_tools.py
    llm_tools.py
    parallel_tools.py
    metric_tools.py
  sample_docs/
    acme_pitch_deck.txt
    acme_term_sheet.txt
    acme_financials.txt
    tesla_deal_notes.txt
  outputs/
  .env.example
  requirements.txt

Create .env.example with:

ANTHROPIC_API_KEY=
PARALLEL_API_KEY=
LLM_PROVIDER=anthropic
ANTHROPIC_MODEL=claude-sonnet-4-5
DATABASE_URL=sqlite:///deal_room.db

Do not commit real credentials.

Install/use dependencies:
- pydantic
- typer
- rich
- python-dotenv
- anthropic
- parallel-web
- sqlalchemy

Before implementing the Parallel integration, inspect the installed parallel-web package and/or official Parallel docs to verify exact SDK signatures. Do not hallucinate method names. Implement a thin wrapper in tools/parallel_tools.py so the rest of the app depends only on stable internal functions:
- parallel_task_research(company_name: str, website: str | None) -> dict
- parallel_search(query: str, objective: str) -> list[dict]
- parallel_extract(url: str, objective: str | None = None) -> dict

Use real Parallel APIs, not mocks. Prefer the Task API for structured company research. Use Search when the agent needs source URLs. Use Extract to convert high-value URLs into usable markdown/text if needed.

Implement an LLM wrapper in tools/llm_tools.py using the Anthropic SDK. Expose a stable internal function like:
- extract_json_with_llm(prompt: str, schema_name: str) -> dict

Make the LLM wrapper robust:
- request strict JSON outputs
- strip markdown code fences
- retry once on JSON parse failure
- validate with Pydantic
- on failure, return a needs_review item rather than crashing the whole run

Create Pydantic models in schemas.py.

CompanyProfile:
- company_name: str
- website: str | None = None
- sector: str | None = None
- description: str | None = None
- headquarters: str | None = None
- business_model: str | None = None
- market_position: str | None = None

DocumentClassification:
- document_name: str
- document_type: str  # pitch_deck, term_sheet, financials, diligence_memo, contract, transcript, public_filing, unknown
- confidence: float
- reasoning: str

SourceReference:
- source_type: str  # document, parallel_search, parallel_task, parallel_extract, user_input
- source_name: str | None = None
- document_name: str | None = None
- section: str | None = None
- quote: str | None = None
- url: str | None = None
- retrieved_at: str | None = None

MetricObservation:
- metric_name: str
- value: str
- normalized_value: float | None = None
- unit: str | None = None
- period: str | None = None
- as_of_date: str | None = None
- source: SourceReference
- confidence: float
- observation_type: str  # extracted, researched, user_provided
- review_status: str  # proposed, needs_review, rejected, accepted_candidate
- notes: str | None = None

ComputedMetric:
- metric_name: str
- value: str
- formula: str
- input_metric_names: list[str]
- input_observation_indexes: list[int] | None = None
- confidence: float
- notes: str | None = None

ReviewTask:
- task_type: str  # verify_metric, resolve_conflict, clarify_definition, missing_data, external_research_failed
- title: str
- description: str
- related_metrics: list[str]
- severity: str  # low, medium, high

AgentTraceStep:
- step_name: str
- tool_used: str
- input_summary: str
- output_summary: str
- decision_reason: str

DealRoomUpdate:
- run_id: str
- company_profile: CompanyProfile
- document_classifications: list[DocumentClassification]
- metric_observations: list[MetricObservation]
- computed_metrics: list[ComputedMetric]
- review_tasks: list[ReviewTask]
- agent_trace: list[AgentTraceStep]
- summary: str

Implement SQLite persistence in db.py.

Persist at least:
- runs
- company_profiles
- documents
- metric_observations
- computed_metrics
- review_tasks
- agent_trace_steps

Minimal SQLite schema is fine. Store nested source objects and lists as JSON if that keeps the prototype simple.

Suggested tables:

runs:
- id TEXT PRIMARY KEY
- company_name TEXT
- website TEXT
- created_at TEXT

company_profiles:
- run_id TEXT
- company_name TEXT
- website TEXT
- sector TEXT
- description TEXT
- headquarters TEXT
- business_model TEXT
- market_position TEXT

documents:
- id INTEGER PRIMARY KEY AUTOINCREMENT
- run_id TEXT
- document_name TEXT
- document_type TEXT
- path TEXT
- processing_status TEXT

metric_observations:
- id INTEGER PRIMARY KEY AUTOINCREMENT
- run_id TEXT
- metric_name TEXT
- value TEXT
- normalized_value REAL
- unit TEXT
- period TEXT
- as_of_date TEXT
- source_json TEXT
- confidence REAL
- observation_type TEXT
- review_status TEXT
- notes TEXT

computed_metrics:
- id INTEGER PRIMARY KEY AUTOINCREMENT
- run_id TEXT
- metric_name TEXT
- value TEXT
- formula TEXT
- input_metric_names_json TEXT
- confidence REAL
- notes TEXT

review_tasks:
- id INTEGER PRIMARY KEY AUTOINCREMENT
- run_id TEXT
- task_type TEXT
- title TEXT
- description TEXT
- related_metrics_json TEXT
- severity TEXT
- status TEXT

agent_trace_steps:
- id INTEGER PRIMARY KEY AUTOINCREMENT
- run_id TEXT
- step_name TEXT
- tool_used TEXT
- input_summary TEXT
- output_summary TEXT
- decision_reason TEXT

Implement tools/document_tools.py:
- read_documents(paths: list[str]) -> list[dict]
Read local .txt/.md files and return path, name, and text. Text files are sufficient for the prototype. PDF support is optional only if easy.

Implement document classification:
- classify_document(document_text, document_name) -> DocumentClassification
Use LLM first. If API call fails, fall back to keyword heuristics.
Document types:
- pitch_deck
- term_sheet
- financials
- diligence_memo
- contract
- transcript
- public_filing
- unknown

Heuristic examples:
- "valuation", "pre-money", "post-money", "terms" => term_sheet
- "revenue", "gross margin", "EBITDA", "income statement" => financials
- "market opportunity", "team", "product", "traction" => pitch_deck
- "10-K", "annual report", "SEC", "Form 10-K" => public_filing

Implement metric extraction:
- extract_metrics_from_document(document_text, document_name, classification) -> list[MetricObservation]
Use LLM structured extraction.
Extract metrics such as:
- revenue
- ARR
- gross_margin
- EBITDA
- valuation
- funding_amount
- headcount
- customer_count
- growth_rate
- market_share
- market_cap
Every metric must include:
- source quote
- document name
- period or as-of date if available
- confidence
- review status
- notes

Rules:
- Clear source-backed value => review_status = proposed
- Ambiguous definition => review_status = needs_review
- No quote/source => review_status = needs_review and lower confidence
- If "ARR" and "revenue" are used interchangeably, create a review task later

Implement tools/parallel_tools.py:
- research_company_with_parallel(company_name, website) -> dict
Use real Parallel APIs. The function should return:
{
  "company_profile": CompanyProfile-compatible dict,
  "researched_observations": list[MetricObservation-compatible dict],
  "sources": list[SourceReference-compatible dict],
  "raw_parallel_response": dict or str
}

Ask Parallel for source-backed facts:
- company description
- sector
- headquarters
- business model
- market position
- public revenue metrics if available
- latest market cap or valuation if public
- employee count/headcount if available
- major products
- recent notable developments
- source URLs

For public companies like Tesla, expect public metrics such as market cap, revenue, headcount, business description, and recent developments. For private companies, expect fewer facts and more review tasks.

Important: researched facts should be stored as MetricObservation objects or company profile fields with source_type equal to parallel_task, parallel_search, or parallel_extract.

Do not pretend web research is certain. If the fact is time-sensitive, source quality is unclear, or retrieved_at/as_of_date is important, lower confidence or mark needs_review.

Implement profile merge:
- merge_company_profile(user_input, document_extracted_profile, parallel_profile) -> tuple[CompanyProfile, list[ReviewTask]]
Merge company profile information.
Priority order:
1. user-provided company name/website
2. official website/reliable Parallel sources
3. document-extracted facts
4. weaker web sources
If fields disagree, do not silently overwrite. Add a review task.

Implement tools/metric_tools.py:
- detect_conflicts(metric_observations) -> list[ReviewTask]
Compare metric observations by:
- metric_name
- period
- unit
- semantic meaning

Detect:
- same metric + same period + materially different value
- ARR vs revenue ambiguity
- valuation from multiple dates/sources
- stale public data vs recent document data
- public company current market cap vs old valuation references

Examples:
- 2024 revenue = $22M from pitch deck and 2024 revenue = $18M from financials => resolve_conflict
- 2024 ARR = $22M and 2024 GAAP revenue = $18M => clarify_definition
- market cap from current Parallel research vs valuation in old document => mark as different as-of dates, not necessarily a direct conflict

Implement:
- compute_derived_metrics(metric_observations) -> tuple[list[ComputedMetric], list[ReviewTask]]
Compute when possible:
- YoY revenue growth
- valuation / revenue multiple
- valuation / ARR multiple
- gross margin trend
Rules:
- Only compute when required inputs exist
- If inputs are ambiguous, compute with lower confidence and create review task
- Reference input observations
- Do not compute a canonical metric from conflicting inputs unless selected input is clear
- If no derived metrics can be computed, explain why in the summary

Implement prompts.py with structured prompts.

Document classification prompt:
- Return JSON matching DocumentClassification
- Do not include markdown fences

Metric extraction prompt:
- Return JSON list matching MetricObservation
- Extract only metrics directly supported by the document
- Do not invent missing values
- Include exact source quotes
- Use needs_review when the metric definition is ambiguous
- Use lower confidence for unsupported or unclear values

Company profile extraction/research prompt:
- Extract or research description, sector, business model, headquarters, market position
- Include sources and confidence
- Do not invent unknown fields

Summary generation can be deterministic rather than LLM-based for reliability.

Implement agent.py orchestration.

Pseudo-flow:

def run_agent(company_name, website, doc_paths):
    create run_id
    initialize SQLite run
    trace Start from CLI input

    docs = read_documents(doc_paths)
    trace Read Documents

    classifications = []
    extracted_metrics = []
    optional_doc_profile_facts = []

    for doc in docs:
        classification = classify_document(doc)
        classifications.append(classification)
        trace Classify Document

        metrics = extract_metrics_from_document(doc, classification)
        extracted_metrics.extend(metrics)
        trace Extract Metrics

    Determine whether to research:
        should_research = company_name exists or website exists or company profile fields are missing
    If should_research:
        call research_company_with_parallel(company_name, website)
        trace Parallel Research

    Merge user/document/Parallel company profile
    all_observations = extracted_metrics + parallel researched observations

    review_tasks = []
    review_tasks.extend(profile merge review tasks)
    review_tasks.extend(detect_conflicts(all_observations))
    trace Conflict Detection

    computed_metrics, compute_review_tasks = compute_derived_metrics(all_observations)
    review_tasks.extend(compute_review_tasks)
    trace Computed Metrics

    Create DealRoomUpdate
    Persist to SQLite
    Write JSON output
    Write Markdown summary
    Write agent_trace.json
    Print Rich terminal summary

The agent trace should be explicit enough to show agentic decisions even if the orchestration is deterministic.

Example AgentTraceStep:
{
  "step_name": "Conflict Detection",
  "tool_used": "detect_metric_conflicts",
  "input_summary": "Compared extracted revenue and ARR observations across pitch deck and financials.",
  "output_summary": "Detected possible mismatch between 2024 ARR of $22M and 2024 GAAP revenue of $18M.",
  "decision_reason": "ARR and GAAP revenue may represent different metric definitions and should be reviewed before computing valuation multiples."
}

Generate Markdown summary in outputs/{run_id}/deal_summary.md with sections:

# AI Deal Room Update: {company}

## Company Profile

## Documents Processed

## Key Extracted Metrics

## Web Research via Parallel

## Computed Metrics

## Review Required

## Agent Trace

Keep it concise and useful to an investment associate.

Create sample docs.

sample_docs/acme_pitch_deck.txt:
Include:
Acme Robotics builds autonomous warehouse robotics systems for third-party logistics providers.
Founded in 2020 and headquartered in Boston, MA.
2023 Revenue: $10M
2024 ARR: $22M
Gross Margin: 64%
Current Customers: DHL, Walmart, Maersk
Team Size: 140 employees

sample_docs/acme_term_sheet.txt:
Include:
Proposed Series C financing.
Pre-money valuation: $180M
Investment amount: $30M
Target close date: Q3 2025.

sample_docs/acme_financials.txt:
Include:
2023 GAAP Revenue: $9M
2024 GAAP Revenue: $18M
Gross Margin: 61%

This should cause the agent to flag ARR vs GAAP revenue ambiguity and possibly compute revenue growth from GAAP revenue.

sample_docs/tesla_deal_notes.txt:
Include:
Tesla is being reviewed as a public market comparable for EV manufacturing, autonomy, batteries, and robotics exposure.
Internal notes:
- Review latest annual revenue from public filings or reliable web sources.
- Compare current market capitalization against revenue.
- Identify recent product/business developments relevant to the investment thesis.

For Tesla, the agent should use Parallel heavily because the local document intentionally lacks metrics.

CLI behavior:
Use Typer if possible.

Commands:
python main.py demo
Runs Acme demo with the three Acme documents.

python main.py run --company "Tesla" --website "https://www.tesla.com" --docs sample_docs/tesla_deal_notes.txt
Runs arbitrary company flow.

The CLI should print:
- run id
- documents processed
- metrics extracted
- conflicts/review tasks detected
- computed metrics generated
- output paths
- SQLite persistence confirmation

README.md requirements:
Create a polished README explaining:
- What this prototype does
- Why it exists
- How to install
- How to configure API keys
- How to run Acme demo
- How to run Tesla/arbitrary company mode
- What outputs are generated
- How SQLite persistence works
- How Parallel APIs are used
- How the LLM is used
- How conflicts and review tasks work
- Limitations
- GenAI disclosure

Include these exact ideas:
"This prototype demonstrates the agent workflow for the AI Deal Room, not the full production web application."

"In production, the same outputs would map to the Deal Room data model: company profile, documents, sources, metric observations, computed metrics, review tasks, and audit logs."

"The agent does not treat AI outputs as automatically canonical. It preserves source-backed observations and routes low-confidence or conflicting facts to human review."

Reliability requirements:
- Do not let one failed tool call crash the entire run.
- If Parallel fails: record failed trace step, continue with local documents, create review task external_research_failed.
- If LLM extraction fails: fall back to simple regex extraction where possible and create review task extraction requires manual review.
- If computed metric inputs conflict: do not compute a canonical metric, or compute only as tentative with low confidence and a review task.
- Validate outputs with Pydantic before writing JSON.
- Use clear errors for missing API keys.

Minimum success criteria:
Running python main.py demo should produce:
- At least 3 classified documents
- At least 5 extracted metric observations
- At least 1 computed metric
- At least 1 conflict or ambiguity review task
- Final markdown summary
- JSON output preserving provenance and confidence
- Agent trace showing tool usage and decisions
- SQLite run persisted

Running:
python main.py run --company "Tesla" --website "https://www.tesla.com" --docs sample_docs/tesla_deal_notes.txt
should:
- Use Parallel research
- Extract/research company profile
- Find public-company metrics if available
- Create review tasks for time-sensitive facts such as market cap/latest revenue if source confidence or as-of dates are uncertain
- Generate summary and JSON output

What to avoid:
- Do not build a web UI
- Do not build a huge database schema
- Do not make the agent a generic chatbot
- Do not let extracted facts overwrite each other
- Do not hide uncertainty
- Do not depend on Tesla only; keep arbitrary-company mode working

Deliverables:
- Working CLI
- SQLite persistence
- Real Parallel integration
- Real LLM integration
- Pydantic schemas
- Sample docs
- JSON output
- Markdown summary output
- README
- .env.example

After building, verify with:
pip install -r requirements.txt
cp .env.example .env
# add ANTHROPIC_API_KEY and PARALLEL_API_KEY
python main.py demo
python main.py run --company "Tesla" --website "https://www.tesla.com" --docs sample_docs/tesla_deal_notes.txt

One final design note:
Include both Acme and Tesla. Acme is best for demonstrating document extraction and conflict handling. Tesla is best for demonstrating arbitrary-company research using live Parallel APIs.
