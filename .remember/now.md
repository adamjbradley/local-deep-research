# Session State: Structured Research Engine

## Branch & Commit
- Branch: `feature/git-repo-search-engine`
- Commit: `7a41462d6` + pending fixes commit
- PR: https://github.com/adamjbradley/local-deep-research/pull/1
- Container: running at http://localhost:5000 (healthy)

## What Was Built

### Core: Structured Research Engine
- `structured_strategy.py` — multi-level dimensions, enum normalization, confidence, discovery, drill-down, chaining
- `_structured_runner.py` — execution handler, delta merge, history compaction
- `_structured_discovery.py` — LLM-based dimension value discovery
- `_research_context.py` — prior research context for chaining
- `csv_exporter.py` + `json_exporter.py` — structured data exporters
- `search_engine_structured.py` — queries structured data across all research
- `search_engine_git_repo.py` — git repo search engine (multi-repo, auth, SSH)
- `document_version.py` — document versioning model

### UI Components
- `structured_research.js` — dimensions/fields builder, templates, discovery, chaining dropdown
- `structured_results.js` — results table, nav, collapsible sections, summaries, export buttons
- `structured_refinement.js` — edit and re-run from results page
- Modified: research.html, results.html, research.js, history.js, formatting.js, styles.css

### API Endpoints (on research_bp)
- `POST /api/research/structured` — create session with discovery
- `PATCH /api/research/{id}/dimensions` — update dimensions
- `POST /api/research/{id}/execute` — start cell research
- `GET /api/research/{id}/history` — dimension history
- `PATCH /api/research/{id}/resolve-conflict` — resolve data conflicts
- `POST /api/research/{id}/generate-summaries` — on-demand summaries
- `POST /api/research/{id}/refresh` — re-run with same schema
- `GET /api/templates/structured` — list templates
- Library: version history, re-research, point-in-time snapshot

### Docker Optimizations
- `.dockerignore` (87MB → 10MB context)
- BuildKit cache mounts (npm/pdm)
- Vite layer split + pdm dep/source split
- `build:` directive in docker-compose.yml

## Code Review Issues Fixed
- SSH injection via ssh_key_path → shlex.quote + path validation
- 3x missing flag_modified on JSON mutations → added
- git grep argument injection → -e flag
- Detached ORM object in refresh → captured before session close
- Missing class names in whitelist → added GitRepoSearchEngine, StructuredDataSearchEngine

## Known Issues
- CSRF token rotation between API calls requires careful session management in curl tests (browser handles automatically)
- SearXNG cold-start returns empty on first query → retry with 3s delay added
- Settings 500 on `/settings/api` — pre-existing issue, not caused by our changes

## Design Spec
- `docs/superpowers/specs/2026-04-15-structured-research-design.md`
- 8 rounds of feedback review (codex.feedback, gemini.feedback)
