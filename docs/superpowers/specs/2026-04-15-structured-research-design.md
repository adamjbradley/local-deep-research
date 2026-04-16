# Structured Research: Design Spec

**Date:** 2026-04-15  
**Status:** Implemented  
**Author:** Adam Bradley + Claude  
**Branch:** feature/git-repo-search-engine  
**PR:** https://github.com/adamjbradley/local-deep-research/pull/1  
**Tests:** 35 unit tests passing  

### Implementation Status

| Feature | Status | Notes |
|---------|--------|-------|
| Schema definition + nested dimensions | Done | Unlimited depth, cross-product |
| Enum normalization | Done | 4-step: exact → case → LLM → unknown |
| Canonical flat cells list | Done | cell_id = sorted pipe-delimited |
| Two-phase session (discovery → execute) | Done | Service layer owns discovery |
| Delta research + history | Done | Field change detection, compaction |
| Item drill-down (source: items) | Done | Second pass after parent level |
| Multi-source research | Partial | ParallelSearchEngine hook, factory TODO |
| Knowledge flow (context, enrichment) | Done | Cross-cell context, prior research chaining |
| CSV + JSON exporters | Done | ZIP bundle, roundtrip JSON |
| Confidence scoring | Done | high/medium/low/unverified |
| Temporal provenance | Done | content_date + discovered_at |
| Dimension summaries | Done | On-demand + opt-in auto |
| Dashboard UI | Done | Mode toggle, builders, templates, refinement |
| Results page | Done | Table, nav, export, summaries, refinement panel |
| Knowledge base integration | Done | RAG indexing, structured search engine |
| Document versioning | Done | Model + table creation, API endpoints |
| Refresh/re-research | Done | Same-schema re-run with chaining |
| Point-in-time snapshots | Done | API endpoint |
| Docker optimizations | Done | .dockerignore, BuildKit, layer splits |
| Per-level engine overrides | Hook only | Detection + logging, factory wiring TODO |

## Problem

Local Deep Research produces narrative markdown reports. Users who need structured, aggregatable data — e.g., "which Digital Identity products does each APAC country use" — have no way to get machine-readable output. They can't merge multiple runs, query with XPath, or import into Excel/databases without manual extraction.

## Solution

A **template-driven structured research** mode that:
1. Takes a query + dimension definitions (axes with values, fields to extract)
2. Researches each cell in the matrix with configurable depth
3. Extracts typed fields via LLM
4. Produces structured output in CSV (for Excel/DB) and JSON (for programmatic use)
5. Available via both dashboard UI and programmatic API

---

## 0. Architectural Identity

Structured research is a **mode** — a value of `ResearchHistory.mode`, alongside `"quick"` and `"detailed"`. The frontend entry point is a mode toggle in the dashboard.

Internally it uses a dedicated `StructuredResearchStrategy`, but the strategy is plumbing — not user-visible. The user selects "Structured" mode, not a strategy name.

The session-based API endpoints (`POST /api/research/structured`, `PATCH .../dimensions`, `POST .../execute`) are the mode's interface. They use the existing queued/in-progress/completed lifecycle from `ResearchHistory`, extended with structured-specific states (`discovery`, `ready`, `refinable`).

### Status storage

- `ResearchMode` enum: add `"structured"` alongside `"quick"` and `"detailed"`.
- `ResearchStatus` enum: **unchanged**. The existing `queued → in_progress → completed → failed` lifecycle continues to drive queue/runtime behavior.
- Structured-specific phases (`discovery`, `ready`, `researching`, `refinable`) are stored in `research_meta["structured_phase"]` — a metadata field, not a new status enum value. This avoids touching the status enum that other code depends on.
- The frontend reads `mode == "structured"` to decide whether to render the structured UI, and reads `structured_phase` to determine which panel to show (discovery review, executing, results, refinement).

### Layer responsibilities

| Layer | Owns | Does NOT own |
|-------|------|-------------|
| **Route/Service layer** | Session lifecycle, discovery orchestration, dimension mutations, delta computation, history recording | Cell research, LLM extraction |
| **StructuredResearchStrategy** | Cell research, sub-query generation, LLM extraction, multi-source merging, knowledge accumulation | Discovery, session state, dimension mutations |
| **Exporters (CSV/JSON)** | Format conversion from canonical data | Research logic, storage |

Discovery is owned by the **service layer**, not the strategy. The strategy's `analyze_topic()` receives an already-expanded dimension tree with all discovery resolved. This eliminates the dual-ownership contradiction between Sections 1c and 2.

---

## 1. Schema Definition

The input format used by both UI and API.

```json
{
  "query": "Digital Identity products used in APAC",
  "dimensions": [
    {
      "name": "country",
      "values": ["Australia", "Singapore", "India"],
      "children": {
        "name": "sector",
        "discover": true,
        "prompt": "What sectors in {country} use Digital Identity products?",
        "max_values": 5,
        "children": {
          "name": "program_type",
          "values": ["national-id", "verification", "authentication"]
        }
      }
    }
  ],
  "fields": [
    {"name": "product_name", "type": "string", "description": "Name of the product or platform"},
    {"name": "vendor", "type": "string", "description": "Organization that operates or sells it"},
    {"name": "category", "type": "enum", "options": ["national-id", "verification", "authentication", "biometric"]},
    {"name": "status", "type": "enum", "options": ["active", "pilot", "planned", "deprecated"]},
    {"name": "year_launched", "type": "string"}
  ],
  "options": {
    "iterations_per_cell": 1,
    "max_items_per_cell": 10,
    "max_cells": 200
  }
}
```

### Dimensions

- `dimensions` defines the research axes. Each value becomes a cell to research.
- **Nesting:** `children` is recursive — any dimension can have `children`, to any depth. This produces a tree: region -> country -> sector -> program_type.
- **Static values:** `"values": ["Australia", "Singapore"]` — user specifies all values upfront.
- **Discovery:** `"discover": true` with a `prompt` and `max_values` — the system searches and asks the LLM to enumerate child values from results. Discovery and static can be mixed at any level.
- **Cross-product:** Multiple top-level entries in the `dimensions` array create independent axes. For example, `[{name: "country", values: [...]}, {name: "sector", values: [...]}]` at the top level would research every country x sector combination. This differs from nesting via `children`, where the child is scoped to its parent (sectors within a specific country).

### Fields

- Defines what to extract per item found in each cell.
- Types: `string`, `enum` (with `options`), `number`, `boolean`.
- `description` is optional — passed to the LLM to guide extraction.

### Enum Normalization

LLMs often return values that don't exactly match enum options (e.g., `"Active"` instead of `"active"`, `"currently live"` instead of `"active"`, `"active since 2019"` instead of `"active"`). For aggregation to work, enum values must resolve to exactly one of the defined options.

Post-extraction normalization for enum fields:
1. **Exact match** — return as-is.
2. **Case-insensitive match** — normalize to the defined option's casing.
3. **Fuzzy/synonym match** — ask the LLM to re-classify: *"The value '{raw_value}' was returned for field '{field_name}'. Classify it as exactly one of: {options}. Return ONLY the option."*
4. **Unresolvable** — store the raw value in a `_{field}_raw` attribute and set the normalized field to `"unknown"`.

In the canonical data:
```json
{"status": "active", ...}                                  // clean match
{"status": "unknown", "_status_raw": "operational", ...}   // unresolvable
```

This ensures pivot tables and database queries always work against the defined enum set, while preserving the raw LLM output for manual review.

**Schema contract:** `"unknown"` is an implicit valid value for ALL enum fields — it does not need to be listed in `options`. The `_{field}_raw` key is standard across all export formats:
- **CSV:** Extra column `status_raw` alongside `status`
- **JSON:** Extra key `"_status_raw"` on the item object

### Options

- `iterations_per_cell`: 1 = quick single search, higher = iterative research with follow-up questions per cell. Default: 1.
- `max_items_per_cell`: Cap on items extracted per cell. Default: 10.
- `max_cells`: Global budget. If discovery produces more cells than this, research stops with a warning. Default: 200.
- `cross_cell_context`: Whether sibling cell findings are passed as context to later cells. Default: true. Set false for independent, deterministic cells.
- `max_context_items`: Cap on accumulated knowledge summary length. Default: 50.
- `enrichment_mode`: How child findings affect parent data. One of `"correct"` (flag conflicts), `"append"` (add missed items), `"ignore"` (levels isolated). Default: `"correct"`.
- `search_engines`: List of engines to use. Default: web engines only (local/private engines require explicit opt-in due to privacy — content is sent to LLM for extraction). Overridable per dimension level.
- `auto_execute`: Skip discovery review, go straight to research. Default: false.

### Operational safeguards

- `max_requests_per_engine`: Cap on total search requests per engine per session. Default: 500. Prevents rate limit exhaustion when using multiple engines across large matrices.
- `throttle_ms`: Minimum delay between search requests to the same engine. Default: 0 (no throttle). Set higher for rate-limited APIs.
- **Pre-execution cost estimate:** Before Phase 2 (execute), the API returns an estimate: `{"estimated_cells": 18, "estimated_searches": 90, "engines": 3, "estimated_llm_calls": 18}`. The UI displays this with a confirmation prompt. The API requires explicit `POST /execute` — it never auto-starts expensive work without the estimate being available.
- **Enum normalization cache:** Fuzzy enum reclassifications are cached within a session. If "operational" is mapped to "active" for field `status`, the same mapping is reused for all subsequent cells without additional LLM calls.

---

## 1a-i. Item-Level Drill-Down (Progressive Depth)

### The problem

The base dimension model supports adding countries, sectors, and child levels — but not drilling into specific discovered items. When the broad scan finds myGovID in Australia, the user needs to "zoom in" on myGovID specifically with richer fields (technical stack, compliance, user count) without re-researching all 20 countries.

### Solution: items-as-dimensions

Discovered items can be promoted to a new child dimension level. A special `"source": "items"` dimension auto-populates its values from the parent level's extracted items:

```json
{
  "name": "country",
  "values": ["Australia", "Singapore", "India"],
  "children": {
    "name": "product",
    "source": "items",
    "source_field": "product_name",
    "fields": [
      {"name": "technical_stack", "type": "string", "description": "Core technology and architecture"},
      {"name": "user_count", "type": "string", "description": "Number of active users"},
      {"name": "compliance_certs", "type": "string", "description": "Regulatory certifications held"},
      {"name": "integration_partners", "type": "string", "description": "Key integration partners"}
    ]
  }
}
```

### How it works

1. **Level 1 runs first** with the parent field set (`product_name`, `vendor`, `status`).
2. User reviews results. Sees myGovID, Singpass, Aadhaar, etc.
3. User adds a child dimension with `"source": "items"`. Optionally selects which items to drill into (all, or a subset).
4. **Level 2 runs** with the enriched field set. Sub-queries become: *"Technical details of myGovID in Australia: find technical_stack, user_count, compliance_certs, integration_partners"*.
5. Level 2 cells link back to their parent items via `parent_item_id`.

### Key properties

- **Parent data is untouched** — adding a drill-down level does NOT invalidate the parent cells.
- **Each level has its own field set** — Level 1 fields (broad) and Level 2 fields (deep) are independent.
- **Selective drill-down** — user can choose to drill into 3 of 15 discovered items, not all.
- **Unlimited depth** — Level 2 items can themselves become dimensions for Level 3, and so on.
### In the canonical flat cells list

Drill-down cells are **regular cells in the flat list** — not a special nested structure. They have the same shape as any other cell, plus linking fields. Each level has its own `fields` list (declared at the dimension level, not globally).

Drill-down cells have `parent_cell_id`, `parent_item_id`, and `level`:

```json
{
  "cell_id": "country=Australia|product=myGovID",
  "dimension_values": {"country": "Australia", "product": "myGovID"},
  "parent_cell_id": "country=Australia",
  "parent_item_id": "f8a2b1",
  "level": 2,
  "items": [
    {
      "technical_stack": "TDIF framework",
      "user_count": "7.5 million",
      "compliance_certs": "TDIF, ISO 27001",
      "integration_partners": "Services Australia, ATO",
      "source_ids": ["s15", "s16"],
      "confidence": "medium"
    }
  ]
}
```

---

## 1a-ii. Multi-Source Research

### The problem

Each cell currently uses one search engine. But for a topic like "Digital Identity products in Australia," the best data is spread across web results (SearXNG), government policy repos (git_repo), local research files (elasticsearch), and academic papers (arXiv/PubMed). Single-source research creates systematic blind spots.

### Solution: multi-source per cell

The structured strategy queries **multiple search engines per cell** and merges results before LLM extraction.

### Source configuration (three levels of control)

1. **Default: web engines only** — Matches the existing `ParallelSearchEngine` default of `allow_local_engines=false`. Local/private engines (git_repo, elasticsearch, paperless) require explicit opt-in via the `search_engines` option, because their content will be sent to the configured LLM for extraction — which may be an external API (OpenAI, Anthropic). The pre-execution estimate flags local engine inclusion: `"includes_local_sources": true, "privacy_warning": "Local source content will be sent to the configured LLM for extraction."`

2. **Per-session override** — When creating a research session, specify which engines to use:
   ```json
   {
     "options": {
       "search_engines": ["searxng", "git_repo", "elasticsearch"]
     }
   }
   ```

3. **Per-dimension-level override** — Different levels can use different sources:
   ```json
   {
     "name": "country",
     "values": ["Australia", "Singapore"],
     "search_engines": ["searxng"],
     "children": {
       "name": "product",
       "source": "items",
       "search_engines": ["searxng", "git_repo", "elasticsearch"]
     }
   }
   ```
   Level 1 (broad scan) uses web only. Level 2 (drill-down) adds git repos and local files.

### How multi-source merging works

For each cell:
1. Run the sub-query against each configured engine in parallel.
2. Collect all results into a unified list.
3. Deduplicate by URL (same URL from different engines = one result).
4. Each result is tagged with `source_type` (web, git, file, academic) and `engine` (searxng, git_repo, etc.).
5. Pass the merged, deduplicated results to the LLM for extraction.
6. Source tracking preserves which engine found each source:

```json
{
  "id": "s15",
  "url": "https://...",
  "title": "TDIF Framework Overview",
  "source_type": "web",
  "engine": "searxng",
  "content_date": "2024-03-15",
  "discovered_at": "2026-04-15T14:40:00Z"
}
```

### Querying by source type

With JSON export and `jq`:
- `jq '.sources[] | select(.source_type == "git")'` — sources from code repos
- `jq '.cells[].items[] | select(.source_ids[] as $s | .sources[] | select(.id == $s and .engine == "git_repo"))'` — items backed by repo sources

### Reuse

The existing `ParallelSearchEngine` (`engines/parallel_search_engine.py`) already implements multi-engine parallel search. The structured strategy can instantiate it internally with the configured engine list, rather than requiring the user to manually select "parallel" as their engine.

---

## 1a-iii. Knowledge Flow Between Levels and Cells

### The problem

Research levels and cells are currently isolated. Level 2 (drill-down into myGovID) doesn't know what Level 1 already learned about myGovID. Cell 15 (Indonesia) doesn't know what cells 1-14 found in other countries. And when deeper research contradicts earlier findings, there's no way to propagate corrections back.

### Three knowledge flow mechanisms

#### 1. Context propagation (parent → child)

When researching a child cell, the sub-query includes the parent item's extracted fields as context:

```
Sub-query for Level 2 cell (country=Australia, product=myGovID):

"Technical details of myGovID, a national-id product in Australia.
Known from prior research:
  - Vendor: Australian Government
  - Category: national-id
  - Status: active
  - Year launched: 2019
  - Sources: myGovID Overview (2023), Digital.gov.au (2024)

Find additional detail: technical_stack, user_count, compliance_certs, integration_partners"
```

This makes Level 2 searches dramatically more targeted — the LLM doesn't waste tokens re-establishing basic facts.

**Implementation:** When building the sub-query for a child cell with `parent_item_id`, look up the parent item's fields and include them as context in the prompt.

#### 2. Accumulated knowledge across sibling cells

As cells within the same level complete, their findings accumulate into a running knowledge summary. Later cells receive this summary to:
- Avoid redundant extraction (don't re-extract Singpass when researching Indonesia, even though APAC search results may mention it)
- Enable comparative context ("other APAC countries use X, Y, Z — what does Indonesia have?")

```
Sub-query for cell 15 of 20 (country=Indonesia):

"Digital Identity products used in Indonesia.
Already found in other APAC countries:
  - Australia: myGovID, Digital iD, ConnectID
  - Singapore: Singpass, SingVerify
  - India: Aadhaar, DigiLocker
  ...
Find products SPECIFIC TO Indonesia. Do not repeat products already listed above."
```

**Implementation:** Maintain a `knowledge_accumulator` during the research loop. After each cell completes, append a summary of its items (names only, to keep the prompt manageable). Pass the accumulator as context to subsequent cells.

**Configurable via options:**
```json
{
  "options": {
    "cross_cell_context": true,
    "max_context_items": 50
  }
}
```

`cross_cell_context: false` disables this for cases where cell independence is preferred (e.g., repeated runs for consistency testing). `max_context_items` caps the summary length to prevent prompt overflow.

#### 3. Cross-level enrichment (child → parent)

When deeper research produces findings that contradict or supplement parent-level data, three enrichment modes control the behavior:

| Mode | Behavior | When to use |
|------|----------|-------------|
| `append` | New items discovered at deeper levels are added to the parent cell's items list. | When drilling down reveals products the broad scan missed. |
| `correct` | If a child finding contradicts a parent field (e.g., year_launched = "2018" vs parent's "2019"), both values are kept and the conflict is flagged for user review. | When accuracy matters and contradictions should be resolved manually. |
| `ignore` | Levels are isolated. Child findings don't affect parent data. | When levels serve different purposes and shouldn't interfere. |

Default: `correct`. Configurable per session:
```json
{
  "options": {
    "enrichment_mode": "correct"
  }
}
```

**Conflict representation in the canonical data:**

```json
{
  "cell_id": "country=Australia",
  "items": [
    {
      "product_name": "myGovID",
      "year_launched": "2019",
      "conflicts": [
        {
          "field": "year_launched",
          "child_value": "2018",
          "child_cell_id": "country=Australia|product=myGovID",
          "child_source_ids": ["s15", "s16"],
          "status": "unresolved"
        }
      ]
    }
  ]
}
```

**In the UI:** Conflicts appear as highlighted fields with both values shown. User can resolve by picking one, merging, or keeping both.

**Conflict resolution persistence:**

Resolutions are stored on the conflict object:

```json
{
  "field": "year_launched",
  "child_value": "2018",
  "child_cell_id": "country=Australia|product=myGovID",
  "child_source_ids": ["s15", "s16"],
  "status": "resolved",
  "resolved_value": "2018",
  "resolved_by": "user",
  "resolved_at": "2026-04-16T10:00:00Z"
}
```

**API:** `PATCH /api/research/{id}/resolve-conflict` with body `{"cell_id": "...", "item_id": "...", "field": "...", "resolved_value": "..."}`.

**Rerun semantics:** If future child research introduces new evidence for an already-resolved conflict (e.g., a third source says "2017"), the conflict status is set to `"reopened"` and the new evidence is added. The previous resolution is preserved as `previous_resolution`. The user can re-resolve. Conflicts with status `"resolved"` are never silently overwritten by automated research.

**Append mode:** When drill-down discovers a product the broad scan missed (e.g., a new government pilot program mentioned only in a git repo source), it's added to the parent cell with a flag:

```json
{
  "product_name": "GovVerify Pilot",
  "appended_from": "country=Australia|product=GovVerify Pilot",
  "confidence": "low"
}
```

---

## 1b. Canonical Data Model

### Internal representation: flat cells list

Three representations exist in this system. To prevent implementation drift, one is declared canonical:

- **Input:** Recursive dimension tree (schema definition) — input-only, used to define the research.
- **Canonical (internal):** Flat `cells[]` list — used for storage, history, delta computation, and all internal logic.
- **Output:** CSV (flat tabular) and JSON (structured) — generated by exporters from the flat cells list.

The canonical cell shape:

```json
{
  "cell_id": "country=Australia|sector=government",
  "dimension_values": {"country": "Australia", "sector": "government"},
  "status": "completed",
  "items": [ ... ],
  "source_ids": ["s1", "s3"],
  "error": null,
  "researched_at": "2026-04-15T14:35:00Z",
  "coverage": {
    "search_results_found": 12,
    "sources_consulted": 5,
    "items_extracted": 3,
    "search_language": "en",
    "engines_used": ["searxng", "git_repo"],
    "model": "gpt-4o"
  }
}
```

### Cell identity

`cell_id` = pipe-delimited sorted `key=value` pairs of all dimension values. This is stable across re-runs and independent of tree structure or discovery order. Examples:
- `"country=Australia"` (single dimension)
- `"country=Australia|sector=government"` (nested)
- `"country=Australia|sector=government|program_type=national-id"` (deep)

Cell identity determines delta behavior: if a cell_id exists in the previous run, it's already researched. New cell_ids trigger new research.

### Deterministic cell ordering

Because knowledge flow (Section 1a-iii) makes later cells depend on earlier cells' output, execution order is part of the correctness contract. The canonical ordering is:

1. **Depth-first traversal** of the dimension tree.
2. **Within each level**, values are processed in **declaration order** (the order they appear in the `values` array). For discovered values, **alphabetical order after normalization** (case-insensitive). This ensures reruns produce identical ordering even if the LLM returns values in a different order.
3. **For top-level cross-products** (multiple dimensions at the same level), left-to-right dimension order in the `dimensions` array, then value order within each. The cross-product iterates as: first dimension varies slowest, last dimension varies fastest.

Each cell is assigned a `cell_index` (0-based integer) reflecting this ordering. The index is stored alongside `cell_id` in the canonical data and is used to determine predecessors for delta invalidation.

Example for `[country: [AU, SG], sector: [gov, bank]]` cross-product:
```
0: country=AU|sector=gov
1: country=AU|sector=bank
2: country=SG|sector=gov
3: country=SG|sector=bank
```

This ensures repeated runs and partial reruns produce identical ordering and predecessor relationships.

---

## 1c. Session Lifecycle and Execution Model

### Two-phase session

Structured research runs as a **stateful session** with explicit phases, not a single fire-and-forget request.

**Phase 1: Setup + Discovery**

```
POST /api/research/structured
{
  "query": "...", "dimensions": [...], "fields": [...], "options": {...}
}

Response: {"research_id": "abc-123", "status": "in_progress", "phase": "discovery", "discovered": {...}}
```

The API creates a research session in `discovery` state. If any dimensions have `"discover": true`, discovery runs immediately and returns discovered values. If all dimensions are static, the session moves directly to `ready` state.

**Review step (UI or API):**

```
PATCH /api/research/{id}/dimensions
{
  "approve": ["Australia", "Singapore"],
  "remove": ["Timor-Leste"],
  "add": ["Thailand"],
  "discover_next_level": true
}

Response: {"status": "in_progress", "phase": "discovery", "discovered": {...}}  // if more levels
Response: {"status": "in_progress", "phase": "ready", "total_cells": 18}        // if done
```

Users review discovered values, curate the list, and optionally trigger deeper discovery. This repeats until all levels are resolved and the session is `ready`.

**Phase 2: Execute**

```
POST /api/research/{id}/execute

Response: {"status": "in_progress", "phase": "researching"}
// Progress via WebSocket
// Final: {"status": "completed", "phase": "refinable", "cells_researched": 18}
```

Cell research begins. Progress emitted per cell. Results stored on completion.

**Auto-execute shortcut:**

For API users who don't need the review step:

```json
{
  "query": "...", "dimensions": [...], "fields": [...],
  "options": {"auto_execute": true}
}
```

Skips the review pause — discovery flows straight into research.

### Session states

```
ResearchStatus:   queued → in_progress → completed
                                         (or failed)

Structured phase (in research_meta["structured_phase"]):
  discovery → ready → researching → refinable
            ↗                         ↓
      (re-refine)  ←  ←  ←  ←  ←  (edit dimensions)
```

`ResearchStatus` drives queue/runtime behavior. `structured_phase` drives the UI and API `phase` field. After research completes, `ResearchStatus = completed` and `structured_phase = refinable`. Editing dimensions sets `ResearchStatus` back to `in_progress` and `structured_phase` back to `researching` for delta cells only.

**API payload contract:** All responses include both fields: `{"status": "<ResearchStatus>", "phase": "<structured_phase>"}`. This applies to both new structured endpoints AND existing research endpoints that return structured sessions:
- `GET /api/research/{id}` — include `phase` in response when `mode == "structured"`
- `GET /api/research/{id}/status` — include `phase` alongside `status`
- History/details API responses — include `phase` for structured entries
- WebSocket progress messages — include `phase` alongside existing `progress` and `message` fields

For non-structured research (`mode == "quick"` or `"detailed"`), `phase` is omitted or null — no change to existing behavior.

**Queue semantics:** Discovery runs synchronously for single-level, single-parent cases (one search + one LLM call). For **nested discovery** that fans out across multiple parent values (e.g., discovering sectors for each of 10 countries = 10 searches + 10 LLM calls), discovery is queued as a background task and progress is reported via WebSocket. The threshold: if discovery requires more than 5 search+LLM calls, it goes through the queue. If queued and waiting for a worker slot: `{"status": "queued", "phase": "discovery"}`. Once a worker picks it up: `{"status": "in_progress", "phase": "discovery"}`. The client distinguishes "waiting" from "actively discovering" via the standard `status` field. Execute and rerun always go through the existing research queue. If no worker slot is available, the response is `{"status": "queued", "phase": "researching"}`. Queue position is surfaced via the existing queue API. When a refinable session triggers delta work, it transitions to `status: "queued", phase: "researching"` and re-enters the queue like any other research job.

**Mode-to-strategy mapping:** When `mode == "structured"`, the service layer instantiates `StructuredResearchStrategy` directly. It does NOT go through the strategy dropdown or `search.search_strategy` setting. The strategy is internal plumbing — the user selects "Structured" mode, not a strategy name.

---

## 1d. Dimension Refinement and Delta Rules

### Refinement operations

After initial research completes, users can:
- **Add values:** Add "Thailand" to country list → new cells researched, existing untouched.
- **Remove values:** Remove "India" → cells excluded from active data, retained in history.
- **Add child levels:** Add "sector" under country → new deeper cells researched for all existing countries.
- **Remove child levels:** Remove "sector" → flattens back to country-level cells, deeper data retained in history.

### Delta computation

On re-run after refinement:
1. Compute current cell_ids from the updated dimension tree.
2. Compare against previously researched cell_ids.
3. **New cell_ids** → research these cells.
4. **Removed cell_ids** → mark as `removed` in current data, keep in history.
5. **Unchanged cell_ids** → reuse previous results IF `cross_cell_context: false` OR if all predecessor siblings are also unchanged. If any predecessor changed, the cell must be re-researched (because its sibling context would differ).

**Why:** Knowledge flow (Section 1a-iii) makes cells order-dependent within a single run. Cell 15's results depend on what cells 1-14 found. If cell 3 is re-researched and produces different results, cells 4-20 may need re-research too. Setting `cross_cell_context: false` makes all cells independent and fully delta-reusable.

### Invalidation rules

Not all changes are additive. Some require re-research of existing cells:

| Change | Invalidation |
|--------|-------------|
| Add/remove dimension values | Delta only (new/removed cells) |
| Rename a dimension value | Treated as remove old + add new |
| Change discovery prompt | Re-discover that level. New discovered values → delta research. |
| Change field definitions | Invalidate ALL cells (extraction schema changed, all must re-extract) |
| Change `iterations_per_cell` | No invalidation (only applies to future research) |

### Dedup and normalization

Discovered values are normalized before use:
- Trimmed whitespace
- Case-preserved but case-insensitive dedup (if LLM returns "Banking" and "banking", keep the first)
- cell_id uses the normalized form

---

## 1e. Dimension History and Timelines

### History entries

Every mutation to the research is stored as a history entry with a timestamp:

```json
{
  "research_id": "abc-123",
  "dimension_history": [
    {
      "timestamp": "2026-04-15T14:30:00Z",
      "action": "initial",
      "cells_researched": 18,
      "diff": {
        "added": ["country=Australia", "country=Singapore", ...],
        "removed": [],
        "changed": []
      }
    },
    {
      "timestamp": "2026-04-22T09:15:00Z",
      "action": "add_value",
      "detail": {"dimension": "country", "value": "Thailand"},
      "cells_researched": 3,
      "diff": {
        "added": ["country=Thailand", "country=Thailand|sector=government", ...],
        "removed": [],
        "changed": []
      }
    }
  ]
}
```

### Diff-based snapshots (not full copies)

History entries store **diffs**, not full snapshots. Each diff records:
- `added`: list of cell_ids added with their items
- `removed`: list of cell_ids removed
- `changed`: list of cell_ids whose items changed (with before/after)

The current state is always the live `structured_data`. Historical states are reconstructed by replaying diffs backward from the current state.

### Retention policy

- **Max 50 history entries** per research session.
- Beyond 50, compact: merge consecutive same-day entries into daily summaries, keeping the first entry, last entry, and one entry per day.
- Each diff entry is bounded: if a single mutation touches >100 cells, the diff stores a summary (`"bulk_add": 150`) instead of listing every cell_id.

**Replay fidelity guarantee:** Exact item-level replay is guaranteed only for the most recent 50 uncompacted entries. After compaction, only cell-level summaries (added/removed counts) are available — not exact item data. If exact historical reconstruction is needed, users should export snapshots (JSON) before compaction occurs.

### Timeline queries

History enables:
- "When did Thailand first appear?" → scan diffs for first `added` containing `country=Thailand`.
- "How has Australia changed?" → filter diffs where any `added`/`removed`/`changed` cell_id starts with `country=Australia`.
- "What changed between April and May?" → reconstruct state at both timestamps, diff.

### Storage

Dimension history stored as a JSON array in `ResearchHistory.research_meta["dimension_history"]`. The live `structured_data` (current cells + sources) is stored separately in `research_meta["structured_data"]`. This keeps the history append-only and the current state directly accessible without replay.

---

## 2. Research Strategy

### Class: `StructuredResearchStrategy(BaseSearchStrategy)`

New strategy in `src/local_deep_research/advanced_search_system/strategies/structured_strategy.py`.

### Flow

The strategy receives an **already-expanded dimension tree** (all discovery resolved by the service layer). It only does cell research and extraction. The strategy returns **raw extraction results** per cell (dimension_values, items, source_ids). The **service layer** hydrates these into canonical cells by adding `cell_id`, `cell_index`, `status`, `coverage`, `researched_at`, and other metadata before storage, history, and export.

1. **Parse** the expanded schema (dimensions with concrete values, per-level fields, options).
2. **Enumerate leaf cells** and compute cell_ids.
3. **Generate sub-queries** — one per leaf cell. For the APAC example: *"Digital Identity products used by the Federal Government in Australia: find product_name, vendor, category, status, year_launched"*.
4. **Research each cell** sequentially:
   - Run `search_engine.run(sub_query)` using the existing search infrastructure.
   - If `iterations_per_cell > 1`, generate follow-up questions (reusing `StandardQuestionGenerator`) and search again.
   - Pass accumulated search results + field definitions to the LLM with an extraction prompt. The LLM returns JSON.
   - Parse the LLM's JSON response into typed records.
   - Track source URLs/titles for each extracted item, assigning source IDs.
5. **Emit progress** per cell via existing WebSocket mechanism: *"Researching Australia x government... (cell 7/18)"*.
6. **Return** structured dict with all cells' extracted data + sources.

### Return Structure from `analyze_topic()`

```python
{
    # Required by BaseSearchStrategy
    "findings": [...],
    "iterations": 18,           # total cells researched
    "questions": {},
    "formatted_findings": "## Summary\n6 countries, 3 sectors each, 23 products found...",
    "current_knowledge": "",
    "all_links_of_system": [...],

    # New: structured output
    "structured_data": {
        "schema": { ... },      # echo back the input schema
        "cells": [
            {
                "dimension_values": {"country": "Australia", "sector": "government"},
                "items": [
                    {
                        "product_name": "myGovID",
                        "vendor": "Australian Government",
                        "category": "national-id",
                        "status": "active",
                        "year_launched": "2019",
                        "source_ids": ["s1", "s3"],
                        "source_count": 2,
                        "confidence": "medium",
                        "item_id": "a1b2c3d4"
                    }
                ]
            }
        ],
        "sources": [
            {
                "id": "s1",
                "url": "https://...",
                "title": "...",
                "snippet": "...",
                "content_date": "2023-06-15",
                "discovered_at": "2026-04-15T14:35:00Z"
            }
        ],
        "warnings": []
    }
}
```

### Instantiation

The service layer instantiates `StructuredResearchStrategy` **directly** when `mode == "structured"`. It does NOT go through `search_system_factory.py` or the strategy dropdown. The factory is for strategy selection within quick/detailed modes — structured mode bypasses it entirely.

```python
# In research_service.py, structured mode handler:
from ..advanced_search_system.strategies.structured_strategy import StructuredResearchStrategy

strategy = StructuredResearchStrategy(
    model=model, search=search,
    schema_definition=schema_definition,
    settings_snapshot=settings_snapshot,
)
```

`search_system_factory.py` is **not modified**.

---

## 3. Exporters (CSV and JSON)

### Shared structured exporter input contract

Both structured exporters (CSV, JSON) consume the same input: `ExportOptions.custom_options["structured_data"]`. The export route loads this from `research_meta["structured_data"]` and passes it identically to whichever exporter the format parameter selects. Both require structured data — they return an error for non-structured research.

### Temporal Provenance

Every source carries two dates:

- **`content_date`** — When the source content was published or last updated. Extracted from:
  1. HTML meta tags (`article:published_time`, `datePublished` from JSON-LD/OpenGraph) — already captured by the existing `metadata_extractor.py`
  2. Search engine result metadata (SearXNG, Zenodo, PubMed return publication dates)
  3. LLM extraction as a fallback — the extraction prompt asks the LLM to identify the publication date from the content if available
  
  If no date can be determined, `content_date` is empty string (not fabricated).

- **`discovered_at`** — When our system fetched/found this source. Always populated (system timestamp).

This enables downstream queries like:
- "Show me only findings based on sources published after 2024"
- "Which data points come from sources older than 2 years?"
- "How fresh is the data for each country?"

Items also carry a `researched_at` timestamp (when the cell was researched), which is distinct from the source dates — a cell researched today may cite a source from 2019.

---

## 4. UI and API Integration

### API

Detailed in Section 1c. Summary of endpoints:

| Endpoint | Purpose |
|----------|---------|
| `POST /api/research/structured` | Create session, run discovery. Returns `research_id` + discovered values. |
| `PATCH /api/research/{id}/dimensions` | Review/curate discovered values, trigger deeper discovery. |
| `POST /api/research/{id}/execute` | Begin cell research. Progress via WebSocket. |
| `POST /api/v1/research/{id}/export/{format}` | Export structured data. Format: `csv`, `json`. Matches existing route shape. |
| `GET /api/research/{id}/history` | Get dimension history entries. |
| `PATCH /api/research/{id}/resolve-conflict` | Resolve a cross-level data conflict. Body: `{cell_id, item_id, field, resolved_value}`. |

**Export wiring:**

The existing export route `POST /api/v1/research/{id}/export/{format}` currently calls `export_report_to_memory(markdown_content, format, title)` — a markdown-oriented service helper. For structured research, the route adds a branch **before** calling that helper:

1. Load research record from `ResearchHistory`.
2. Check if `research_meta["structured_data"]` exists.
3. **If yes (structured research):** Load the structured_data dict. Look up the exporter from `ExporterRegistry.get_exporter(format)`. Call `exporter.export(markdown_content="", options=ExportOptions(custom_options={"structured_data": data}))`. Return the `ExportResult` directly. The existing `export_report_to_memory` helper is **bypassed** entirely.
4. **If no (regular research):** Existing flow — call `export_report_to_memory(markdown_content, format, title)` as before.

Both structured exporters (CSV, JSON) consume `custom_options["structured_data"]` as specified in the shared exporter input contract (Section 3). The exporter registry and `ExportResult` return type remain unified across both paths.

### Dashboard UI

A new "Structured Research" mode alongside "Quick" and "Detailed":

1. **Query input** — same text field as today.
2. **Dimensions builder** — dynamic form:
   - Name field (e.g., "country")
   - Toggle: "Specify values" vs "Discover during research"
   - If specify: tag input for values
   - If discover: prompt template field + max_values slider
   - "+ Add child level" button for nesting
   - "x Remove" button on each dimension/value
3. **Fields builder** — dynamic form:
   - Name, type dropdown (string/enum/number/boolean), description
   - If enum: options input
4. **Options** — iterations per cell slider, max items per cell, max cells cap.
5. **Templates dropdown** — pre-fills the form from built-in or user templates.
6. **Discovery review step** — when discovery runs, results appear as a checklist. User checks/unchecks values, adds manual values, then clicks "Continue" to proceed to the next level or begin research.
7. **Refinement panel** — after initial research completes, the dimensions builder remains editable. User can add/remove values, add child levels, and re-run. Only delta cells are researched. A "History" tab shows timestamped snapshots with diff view.

### Progress

Reuses existing WebSocket progress system. Strategy emits per-cell updates:
- `"Discovering sectors for Australia... (cell 3/18)"`
- `"Researching Australia x government... (cell 7/18)"`
- Progress percentage = cells completed / total cells estimated.

---

## 5. Built-in Templates

Stored as JSON in `src/local_deep_research/defaults/templates/structured/`. Users can add custom templates in the same directory.

### Country-Product Matrix

```json
{
  "name": "Country-Product Matrix",
  "description": "Map products/services by country or region",
  "query_hint": "e.g., 'Digital Identity products used in APAC'",
  "dimensions": [
    {
      "name": "country",
      "values": [],
      "placeholder": "Enter countries or set to discover"
    }
  ],
  "fields": [
    {"name": "product_name", "type": "string"},
    {"name": "vendor", "type": "string"},
    {"name": "category", "type": "string"},
    {"name": "status", "type": "enum", "options": ["active", "pilot", "planned", "deprecated"]},
    {"name": "year_launched", "type": "string"}
  ]
}
```

### Competitor Analysis

```json
{
  "name": "Competitor Analysis",
  "description": "Compare competitors across key dimensions",
  "query_hint": "e.g., 'Identity verification vendors in North America'",
  "dimensions": [
    {
      "name": "competitor",
      "discover": true,
      "prompt": "What are the major competitors for {query}?",
      "max_values": 10
    }
  ],
  "fields": [
    {"name": "product", "type": "string"},
    {"name": "pricing_model", "type": "string"},
    {"name": "target_market", "type": "string"},
    {"name": "strengths", "type": "string"},
    {"name": "weaknesses", "type": "string"},
    {"name": "market_share", "type": "string"}
  ]
}
```

### Technology Comparison

```json
{
  "name": "Technology Comparison",
  "description": "Compare technologies, standards, or approaches",
  "query_hint": "e.g., 'Biometric authentication methods'",
  "dimensions": [
    {
      "name": "technology",
      "values": [],
      "placeholder": "Enter technologies or set to discover"
    }
  ],
  "fields": [
    {"name": "description", "type": "string"},
    {"name": "maturity", "type": "enum", "options": ["emerging", "growing", "mature", "declining"]},
    {"name": "primary_use_cases", "type": "string"},
    {"name": "key_vendors", "type": "string"},
    {"name": "limitations", "type": "string"},
    {"name": "cost_range", "type": "string"}
  ]
}
```

---

## 5b. Data Quality: Confidence, Deduplication, and Attribution

### Confidence scoring

Each extracted item carries a confidence level based on source corroboration:

| Level | Criteria | Meaning |
|-------|----------|---------|
| `high` | 3+ independent sources reference this item | Multiple corroborating sources |
| `medium` | 2 sources | Some corroboration |
| `low` | 1 source | Single source, unverified |
| `unverified` | LLM inferred without direct source match | LLM generated this from context, no specific source names it |

The extraction prompt asks the LLM to cite which source(s) each item came from. The system then counts distinct source_ids per item to compute confidence.

In the canonical data:
```json
{"product_name": "myGovID", "confidence": "high", "source_count": 4, "source_ids": ["s1", "s3", "s7", "s12"]}
{"product_name": "TrustID", "confidence": "low", "source_count": 1, "source_ids": ["s8"]}
```

Downstream filtering with `jq`: `.cells[].items[] | select(.confidence == "high")` returns only well-corroborated items.

### Item-level source attribution

`source_ids` on an item means **these sources directly reference this specific item** — not merely "these sources were consulted for this cell." This is a stronger claim than cell-level attribution.

- Item-level: `source_ids` on the item — "s1 and s3 specifically mention myGovID"
- Cell-level: `coverage.sources_consulted` in cell metadata — "5 sources were searched for the Australia/government cell"

The extraction prompt instructs the LLM: *"For each item you extract, indicate which source number(s) it came from."*

### Cross-cell item deduplication

Items are **not deduplicated** across cells. The same product appearing in multiple cells is meaningful — it shows that product serves multiple sectors/regions.

However, each item carries a **global-per-entity** `item_id` — a hash of `product_name` + `vendor` only (NOT including dimension context). The same product in different cells gets the **same** item_id, enabling downstream deduplication:

In CSV, the same product appears as two rows with different dimension values but the same `item_id`:
```csv
Singapore,government,x7y8z9,Singpass,...
Singapore,banking,x7y8z9,Singpass,...
```

JSON dedup with `jq`: `[.cells[].items[]] | unique_by(.item_id)` returns unique items only.

### Coverage signals

Each cell carries metadata about research thoroughness, enabling users to distinguish "no products exist" from "we couldn't find any":

```json
{"cell_id": "country=Indonesia", "items": [], "coverage": {"search_results_found": 0, "sources_consulted": 0, "items_extracted": 0, ...}}
{"cell_id": "country=Australia", "items": [...], "coverage": {"search_results_found": 15, "sources_consulted": 8, "items_extracted": 4, ...}}
```

Coverage queries with `jq`:
- `.cells[] | select(.coverage.search_results_found == 0)` — cells with no search results
- `.cells[] | select(.coverage.items_extracted == 0 and .coverage.search_results_found > 5)` — cells with content but no extractable items (topic likely doesn't apply, not a search failure)

---

## 5c. Additional Export Formats

The canonical flat cells list supports multiple output formats. All are generated from the same internal data.

### CSV
Flat tabular export — one row per item, dimension values repeated:

```csv
country,sector,product_name,vendor,category,status,year_launched,confidence,source_count,sources,content_date
Australia,government,myGovID,Australian Government,national-id,active,2019,high,4,"s1;s3;s7;s12",2023-06-15
Australia,government,Digital iD,Australia Post,verification,active,2020,medium,2,"s2;s9",2022-03-20
Singapore,government,Singpass,GovTech Singapore,national-id,active,2003,high,3,"s4;s5;s11",2024-01-10
```

Ideal for Excel pivot tables, Power BI, and database import.

**Flattening rules for multi-source items:** When an item cites multiple sources with different `content_date` values, the CSV uses the **most recent** `content_date` among cited sources. For deep nesting (5+ levels), CSV column headers include the dimension level prefix (e.g., `L1_country`, `L2_sector`) to avoid ambiguity.

**Delivery format:** CSV export returns a **ZIP bundle** (`research_export.zip`) containing:
- `data.csv` — one row per item with dimension values, fields, confidence, source IDs
- `sources.csv` — one row per source with id, url, title, content_date, discovered_at, source_type, engine

The `ExportResult` for CSV: `content = ZIP bytes`, `mimetype = "application/zip"`, `filename = "{title}_export.zip"`. This fits the existing single-file `ExportResult` contract — the ZIP is one file. JSON export remains single-file.

### JSON
Direct serialization of the canonical `structured_data` dict. Ideal for programmatic consumption and API integrations.

### Export endpoint
```
POST /api/v1/research/{id}/export/csv
POST /api/v1/research/{id}/export/json
```

Both exporters registered via `@ExporterRegistry.register`.

---

## 6. Error Handling

### Cell-level failures

If a single cell fails (search timeout, LLM extraction error), it's recorded with an `error` status — not fatal. Other cells continue.

```json
{"cell_id": "country=Indonesia", "status": "error", "error": "Search returned no results for this cell", "items": []}
```

### Discovery edge cases

- **Zero values discovered:** Cell marked empty, system moves on. No crash.
- **Too many values:** `max_values` is a hard cap. LLM is prompted to rank by relevance; only top N are used.
- **Max cells budget exceeded:** Research stops discovery, logs a warning, researches only cells within budget. Warning included in export output.

### LLM extraction failures

- Bad JSON from LLM: Retry once with a stricter prompt. If still invalid, record zero items for that cell with an error status.

### Empty results

A cell with zero items is valid — appears as an empty cell with `"items": []` in the canonical data, an empty row section in CSV, and an empty items array in JSON. This is meaningful data ("nothing found" is a finding).

### User cancellation

Uses existing `check_termination()` from `BaseSearchStrategy`, checked between cells. Partial results up to the cancellation point are preserved and exportable.

---

## 7. Files to Create/Modify

| Action | File | Purpose |
|--------|------|---------|
| Create | `src/local_deep_research/advanced_search_system/strategies/structured_strategy.py` | Strategy implementation |
| Create | `src/local_deep_research/exporters/csv_exporter.py` | CSV exporter (flat tabular) |
| Create | `src/local_deep_research/exporters/json_exporter.py` | JSON exporter |
| Create | `src/local_deep_research/defaults/templates/structured/country_product_matrix.json` | Built-in template |
| Create | `src/local_deep_research/defaults/templates/structured/competitor_analysis.json` | Built-in template |
| Create | `src/local_deep_research/defaults/templates/structured/technology_comparison.json` | Built-in template |
| — | `src/local_deep_research/search_system_factory.py` | NOT modified — structured mode bypasses the factory |
| Modify | `src/local_deep_research/exporters/__init__.py` | Import csv_exporter and json_exporter for registration |
| Modify | `src/local_deep_research/web/routes/research_routes.py` | Add structured research endpoints (create, dimensions, execute, history) |
| Modify | `src/local_deep_research/web/services/research_service.py` | Add structured session management and two-phase execution |
| Modify | `src/local_deep_research/web/routes/` (export route) | Branch export path for structured vs markdown research |
| Modify | `src/local_deep_research/database/` | Add structured_data and dimension_history fields to ResearchHistory.research_meta |

---

## 8. Verification

1. **Unit test:** Create a `StructuredResearchStrategy` with a mock search engine and mock LLM. Verify it generates correct sub-queries, parses JSON extraction results, and handles discovery.
2. **Exporter tests:** Feed known structured_data → correct CSV (columns, rows, ZIP bundle with data.csv + sources.csv), roundtrip JSON.
3. **Template test:** Load each built-in template, verify it passes schema validation.
4. **Integration test:** Run a structured research via API with a small 2-country, 2-field schema. Verify the returned CSV and JSON have correct structure.
5. **Error test:** Simulate cell failures, discovery overflow, and cancellation. Verify partial results and warnings.
6. **End-to-end:** Use the dashboard UI to run the Country-Product Matrix template against a real query, export CSV, open in Excel.
