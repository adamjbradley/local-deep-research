"""
Structured research strategy.

Decomposes a query along user-defined dimensions, researches each leaf cell,
and extracts typed fields via LLM.  Returns a canonical ``structured_data``
dict suitable for CSV/JSON export.

Discovery is NOT handled here — the service layer resolves all discovered
dimension values before calling ``analyze_topic()``.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.language_models import BaseLLM
from loguru import logger

from ...utilities.json_utils import extract_json, get_llm_response_text
from ..strategies.base_strategy import BaseSearchStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cell_id(dimension_values: Dict[str, str]) -> str:
    """Canonical cell identity: sorted pipe-delimited ``key=value`` pairs."""
    return "|".join(
        f"{k}={v}" for k, v in sorted(dimension_values.items())
    )


def _item_id(item: Dict[str, Any], key_fields: List[str]) -> str:
    """Global-per-entity hash from the first two populated key fields."""
    parts = []
    for f in key_fields:
        val = str(item.get(f, "")).strip().lower()
        if val:
            parts.append(val)
        if len(parts) >= 2:
            break
    raw = "|".join(parts) if parts else json.dumps(item, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def _normalize_enum(
    value: str,
    options: List[str],
    enum_cache: Dict[str, str],
    llm: Optional[BaseLLM] = None,
) -> tuple:
    """Normalize an enum value.  Returns ``(normalized, raw_or_none)``.

    Steps: exact → case-insensitive → LLM fuzzy → unknown.
    Uses *enum_cache* to avoid redundant LLM calls within a session.
    """
    if value in options:
        return value, None

    lower_map = {o.lower(): o for o in options}
    if value.lower() in lower_map:
        return lower_map[value.lower()], None

    cache_key = f"{value.lower()}|{'|'.join(sorted(options))}"
    if cache_key in enum_cache:
        cached = enum_cache[cache_key]
        return (cached, None) if cached != "unknown" else ("unknown", value)

    if llm:
        try:
            prompt = (
                f"The value '{value}' was returned for a field. "
                f"Classify it as exactly one of: {', '.join(options)}. "
                f"Return ONLY the option, nothing else."
            )
            resp = get_llm_response_text(llm.invoke(prompt)).strip()
            if resp in options:
                enum_cache[cache_key] = resp
                return resp, None
            if resp.lower() in lower_map:
                mapped = lower_map[resp.lower()]
                enum_cache[cache_key] = mapped
                return mapped, None
        except Exception:
            logger.debug(f"Enum reclassification failed for '{value}'")

    enum_cache[cache_key] = "unknown"
    return "unknown", value


def _confidence_level(source_count: int) -> str:
    if source_count >= 3:
        return "high"
    if source_count == 2:
        return "medium"
    if source_count == 1:
        return "low"
    return "unverified"


def _extract_content_date(result: Dict[str, Any]) -> str:
    """Try to pull a publication date from a search result."""
    for key in (
        "publication_date",
        "datePublished",
        "published_time",
        "date",
        "pub_date",
    ):
        val = result.get(key, "")
        if val:
            return str(val)
    return ""


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class StructuredResearchStrategy(BaseSearchStrategy):
    """Research each cell in a dimension matrix and extract structured fields."""

    def __init__(
        self,
        model: BaseLLM,
        search,
        schema_definition: Dict[str, Any],
        all_links_of_system=None,
        settings_snapshot=None,
        **kwargs,
    ):
        super().__init__(
            all_links_of_system=all_links_of_system,
            settings_snapshot=settings_snapshot,
        )
        self.model = model
        self.search = search
        self.schema = schema_definition or {}
        self.options = self.schema.get("options", {})
        self._enum_cache: Dict[str, str] = {}

    def _opt(self, key: str, settings_key: str, default):
        """Read option with priority: schema options > settings_snapshot > default."""
        val = self.options.get(key)
        if val is not None:
            return val
        val = self.get_setting(settings_key)
        if val is not None:
            return val
        return default

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def analyze_topic(self, query: str) -> Dict[str, Any]:
        """Research every leaf cell and return structured data."""
        if not self._validate_search_engine():
            return self._empty_result(query, "No search engine available")

        # Load prior research context if chaining is configured
        prior_context = ""
        prior_id = self.options.get("prior_research_id")
        if prior_id:
            try:
                from ...web.services._research_context import build_prior_context
                from ...database.models import ResearchHistory
                from ...database.session_context import get_user_db_session

                # Try to load — may fail outside Flask context
                username = self.get_setting("_username", "")
                if username:
                    ctx = None
                    with get_user_db_session(username) as db_session:
                        research = (
                            db_session.query(ResearchHistory)
                            .filter_by(id=prior_id)
                            .first()
                        )
                        if research and research.research_meta:
                            ctx = build_prior_context(research.research_meta)
                    if ctx:
                        prior_context = ctx
                        logger.info(
                            f"Loaded {len(prior_context)} chars of prior context "
                            f"from research {prior_id}"
                        )
            except Exception as e:
                logger.debug(f"Could not load prior research context: {e}")

        fields = self.schema.get("fields", [])
        field_names = [f["name"] for f in fields]
        key_fields = [
            f["name"]
            for f in fields
            if f.get("type") == "string"
        ][:2]

        # Build enum lookup
        enum_options: Dict[str, List[str]] = {}
        for f in fields:
            if f.get("type") == "enum" and f.get("options"):
                enum_options[f["name"]] = f["options"]

        # Enumerate leaf cells from the already-expanded dimension tree
        leaf_cells = self._enumerate_leaf_cells(self.schema.get("dimensions", []))
        max_cells = self._opt("max_cells", "search.structured.max_cells", 200)
        warnings: List[str] = []

        if len(leaf_cells) > max_cells:
            warnings.append(
                f"Cell budget exceeded: {len(leaf_cells)} cells, "
                f"max_cells={max_cells}. {len(leaf_cells) - max_cells} skipped."
            )
            leaf_cells = leaf_cells[:max_cells]

        iterations_per_cell = self._opt("iterations_per_cell", "search.structured.iterations_per_cell", 2)
        max_items = self._opt("max_items_per_cell", "search.structured.max_items_per_cell", 10)
        cross_context = self._opt("cross_cell_context", "search.structured.cross_cell_context", True)
        max_context_items = self._opt("max_context_items", "search.structured.max_context_items", 50)

        # Build per-dimension engine override lookup
        engine_overrides = self._collect_engine_overrides(
            self.schema.get("dimensions", [])
        )

        all_cells: List[Dict[str, Any]] = []
        all_sources: List[Dict[str, Any]] = []
        source_id_counter = 0
        knowledge_acc: List[str] = []

        total = len(leaf_cells)
        self._update_progress(
            f"Starting structured research: {total} cells",
            5,
            {"phase": "structured_research", "total_cells": total},
        )

        for idx, dim_values in enumerate(leaf_cells):
            self.check_termination()

            cid = _cell_id(dim_values)
            pct = 5 + int(90 * (idx / max(total, 1)))
            dim_label = ", ".join(f"{k}={v}" for k, v in dim_values.items())
            self._update_progress(
                f"Researching {dim_label}... (cell {idx + 1}/{total})",
                pct,
                {"phase": "cell_research", "cell_index": idx, "cell_id": cid},
            )

            cell_result = self._research_cell(
                query=query,
                dimension_values=dim_values,
                field_names=field_names,
                fields=fields,
                enum_options=enum_options,
                key_fields=key_fields,
                iterations=iterations_per_cell,
                max_items=max_items,
                knowledge_context=knowledge_acc if cross_context else [],
                source_id_start=source_id_counter,
                prior_context=prior_context,
            )

            cell_data = cell_result["cell"]
            cell_data["cell_id"] = cid
            cell_data["cell_index"] = idx
            cell_data["researched_at"] = datetime.now(timezone.utc).isoformat()

            all_cells.append(cell_data)
            all_sources.extend(cell_result["sources"])
            source_id_counter += len(cell_result["sources"])

            # Accumulate knowledge for sibling context
            if cross_context and cell_data.get("items"):
                names = [
                    it.get(field_names[0], "") for it in cell_data["items"]
                    if it.get(field_names[0])
                ][:5]
                if names:
                    knowledge_acc.append(f"{dim_label}: {', '.join(names)}")
                    if len(knowledge_acc) > max_context_items:
                        knowledge_acc = knowledge_acc[-max_context_items:]

        # Item-level drill-down: check for "source: items" dimensions
        drill_dims = self._find_item_source_dims(self.schema.get("dimensions", []))
        if drill_dims and all_cells:
            self._update_progress(
                "Running item drill-down...",
                92,
                {"phase": "drill_down"},
            )
            drill_cells, drill_sources = self._run_drill_down(
                query=query,
                parent_cells=all_cells,
                drill_dims=drill_dims,
                source_id_start=source_id_counter,
                prior_context=prior_context,
            )
            all_cells.extend(drill_cells)
            all_sources.extend(drill_sources)
            source_id_counter += len(drill_sources)

        # Build formatted summary
        total_items = sum(len(c.get("items", [])) for c in all_cells)
        summary = (
            f"## Structured Research Summary\n\n"
            f"- Query: {query}\n"
            f"- Cells researched: {len(all_cells)}\n"
            f"- Total items extracted: {total_items}\n"
            f"- Total sources: {len(all_sources)}\n"
        )
        if warnings:
            summary += "\n### Warnings\n" + "\n".join(f"- {w}" for w in warnings)

        # Generate per-dimension summaries if opted in
        generate_summaries = self._opt(
            "generate_summaries", "search.structured.generate_summaries", False
        )
        summaries: List[Dict[str, Any]] = []
        if generate_summaries and all_cells:
            self._update_progress(
                f"Generating dimension summaries...",
                95,
                {"phase": "summarizing"},
            )
            summaries = self._generate_dimension_summaries(
                query, all_cells, field_names
            )

        self._update_progress(
            f"Structured research complete: {total_items} items from {len(all_cells)} cells",
            100,
            {"phase": "complete"},
        )

        return {
            "findings": [],
            "iterations": len(all_cells),
            "questions": self.questions_by_iteration,
            "formatted_findings": summary,
            "current_knowledge": summary,
            "all_links_of_system": self.all_links_of_system,
            "structured_data": {
                "schema": self.schema,
                "cells": all_cells,
                "sources": all_sources,
                "warnings": warnings,
                "summaries": summaries,
            },
        }

    # ------------------------------------------------------------------
    # Dimension summaries
    # ------------------------------------------------------------------

    def _generate_dimension_summaries(
        self,
        query: str,
        cells: List[Dict[str, Any]],
        field_names: List[str],
    ) -> List[Dict[str, Any]]:
        """Generate a narrative summary for each unique top-level dimension value.

        Groups cells by the first dimension, then asks the LLM to synthesize
        a brief incorporating all child data.
        """
        if not cells:
            return []

        # Identify the top-level dimension (first key in first cell's dimension_values)
        first_cell_dims = cells[0].get("dimension_values", {})
        if not first_cell_dims:
            return []
        top_dim_name = list(first_cell_dims.keys())[0]

        # Group cells by top-level dimension value
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for cell in cells:
            val = cell.get("dimension_values", {}).get(top_dim_name, "")
            if val:
                groups.setdefault(val, []).append(cell)

        summaries: List[Dict[str, Any]] = []
        for dim_value, group_cells in sorted(groups.items()):
            self.check_termination()

            # Build structured context from all items in this group
            context_parts = []
            for cell in group_cells:
                child_dims = {
                    k: v for k, v in cell.get("dimension_values", {}).items()
                    if k != top_dim_name
                }
                child_label = ", ".join(f"{k}: {v}" for k, v in child_dims.items())
                items = cell.get("items", [])

                if items:
                    context_parts.append(f"\n### {child_label or 'General'}")
                    for item in items:
                        fields_str = " | ".join(
                            f"{f}: {item.get(f, 'N/A')}" for f in field_names
                        )
                        context_parts.append(f"  - {fields_str}")
                else:
                    context_parts.append(f"\n### {child_label or 'General'}: No data found")

            context = "\n".join(context_parts)

            prompt = (
                f"Write a comprehensive brief about {dim_value} regarding: {query}\n\n"
                f"Use the following structured research data to write a 500-800 word "
                f"narrative summary. Cover all key findings, highlight important "
                f"regulations and requirements, note gaps, and provide actionable "
                f"insight. Use a professional tone suitable for a stakeholder briefing.\n\n"
                f"Data for {dim_value}:\n{context}\n\n"
                f"Write the summary now. Use markdown formatting with headers."
            )

            try:
                response = self.model.invoke(prompt)
                content = get_llm_response_text(response)
                summaries.append({
                    "dimension_name": top_dim_name,
                    "dimension_value": dim_value,
                    "title": f"{dim_value}: {query}",
                    "content": content,
                    "items_referenced": sum(
                        len(c.get("items", [])) for c in group_cells
                    ),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                })
                logger.info(
                    f"Generated summary for {dim_value}: {len(content)} chars"
                )
            except Exception as e:
                logger.warning(f"Failed to generate summary for {dim_value}: {e}")
                summaries.append({
                    "dimension_name": top_dim_name,
                    "dimension_value": dim_value,
                    "title": f"{dim_value}: {query}",
                    "content": f"Summary generation failed: {e}",
                    "items_referenced": 0,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                })

        return summaries

    @staticmethod
    def generate_summaries_from_data(
        model,
        query: str,
        structured_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Generate summaries on-demand from existing structured data.

        This is a static method so it can be called without a full
        strategy instance — used by the on-demand API endpoint.
        """
        cells = structured_data.get("cells", [])
        schema = structured_data.get("schema", {})
        field_names = [f["name"] for f in schema.get("fields", [])]

        # Create a minimal instance just for summary generation
        strategy = StructuredResearchStrategy(
            model=model,
            search=None,
            schema_definition=schema,
        )
        return strategy._generate_dimension_summaries(query, cells, field_names)

    # ------------------------------------------------------------------
    # Cell research
    # ------------------------------------------------------------------

    def _research_cell(
        self,
        query: str,
        dimension_values: Dict[str, str],
        field_names: List[str],
        fields: List[Dict[str, Any]],
        enum_options: Dict[str, List[str]],
        key_fields: List[str],
        iterations: int,
        max_items: int,
        knowledge_context: List[str],
        source_id_start: int,
        prior_context: str = "",
    ) -> Dict[str, Any]:
        """Research a single cell and return extracted items + sources."""
        dim_label = " ".join(f"{v}" for v in dimension_values.values())

        # Search query is kept simple and natural for the search engine.
        # Field names and context go ONLY to the LLM extraction prompt.
        sub_query = f"{query} {dim_label}"

        # Search
        all_results: List[Dict[str, Any]] = []
        try:
            results = self.search.run(sub_query)
            if results:
                all_results.extend(results)
                for r in results:
                    link = r.get("link", "")
                    if link and link not in self.all_links_of_system:
                        self.all_links_of_system.append(link)
            elif not results:
                # Retry once on empty results — SearXNG backend engines
                # can return nothing on cold start or transient timeouts
                import time
                logger.info(f"No results for {dim_label}, retrying after 3s...")
                time.sleep(3)
                results = self.search.run(sub_query)
                if results:
                    all_results.extend(results)
                    for r in results:
                        link = r.get("link", "")
                        if link and link not in self.all_links_of_system:
                            self.all_links_of_system.append(link)
        except Exception as e:
            logger.warning(f"Search failed for cell {dim_label}: {e}")

        # Additional iterations
        if iterations > 1 and all_results:
            for _ in range(iterations - 1):
                try:
                    followup = self._generate_followup(
                        query, dim_label, field_names, all_results
                    )
                    if followup:
                        more = self.search.run(followup)
                        if more:
                            all_results.extend(more)
                except Exception as e:
                    logger.debug(f"Follow-up search failed: {e}")

        # Build source list
        sources: List[Dict[str, Any]] = []
        source_map: Dict[str, str] = {}  # url → source_id
        for r in all_results:
            url = r.get("link", "") or r.get("url", "")
            if not url or url in source_map:
                continue
            sid = f"s{source_id_start + len(sources) + 1}"
            source_map[url] = sid
            sources.append({
                "id": sid,
                "url": url,
                "title": r.get("title", ""),
                "snippet": (r.get("snippet", "") or "")[:300],
                "content_date": _extract_content_date(r),
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                "source_type": r.get("source_type", "web"),
                "engine": r.get("engine", ""),
            })

        # LLM extraction
        items: List[Dict[str, Any]] = []
        error = None
        if all_results:
            items, error = self._extract_items(
                query=query,
                dim_label=dim_label,
                field_names=field_names,
                fields=fields,
                enum_options=enum_options,
                key_fields=key_fields,
                search_results=all_results,
                source_map=source_map,
                max_items=max_items,
                prior_context=prior_context,
            )

        coverage = {
            "search_results_found": len(all_results),
            "sources_consulted": len(sources),
            "items_extracted": len(items),
            "search_language": "en",
            "engines_used": list(
                {r.get("engine", "unknown") for r in all_results if r.get("engine")}
            ) or ["unknown"],
            "model": str(getattr(self.model, "model_name", "unknown")),
        }

        cell = {
            "dimension_values": dimension_values,
            "status": "error" if error and not items else "completed",
            "items": items,
            "error": error,
            "coverage": coverage,
        }

        return {"cell": cell, "sources": sources}

    # ------------------------------------------------------------------
    # LLM extraction
    # ------------------------------------------------------------------

    def _extract_items(
        self,
        query: str,
        dim_label: str,
        field_names: List[str],
        fields: List[Dict[str, Any]],
        enum_options: Dict[str, List[str]],
        key_fields: List[str],
        search_results: List[Dict[str, Any]],
        source_map: Dict[str, str],
        max_items: int,
        prior_context: str = "",
    ) -> tuple:
        """Ask the LLM to extract structured items from search results.

        Returns ``(items_list, error_or_none)``.
        """
        # Build source index for the prompt
        source_lines = []
        for i, r in enumerate(search_results[:20]):
            title = r.get("title", "Untitled")
            snippet = (r.get("snippet", "") or "")[:200]
            source_lines.append(f"[{i}] {title}\n    {snippet}")
        sources_block = "\n\n".join(source_lines)

        # Build field description
        field_desc_parts = []
        for f in fields:
            desc = f.get("description", "")
            if f.get("type") == "enum" and f.get("options"):
                opts = ", ".join(f["options"])
                field_desc_parts.append(
                    f"- {f['name']} (one of: {opts}){': ' + desc if desc else ''}"
                )
            else:
                field_desc_parts.append(
                    f"- {f['name']} ({f.get('type', 'string')}){': ' + desc if desc else ''}"
                )
        field_desc = "\n".join(field_desc_parts)

        prior_block = ""
        if prior_context:
            prior_block = f"\n\nPrior research context (use to inform and supplement your extraction):\n{prior_context[:2000]}\n"

        prompt = f"""Extract structured data from these search results about {dim_label} related to: {query}
{prior_block}
Search results:
{sources_block}

Extract up to {max_items} items. For each item, provide:
{field_desc}
- source_numbers: which search result number(s) [0], [1], etc. this item came from
- publication_date: if the source mentions when it was published or last updated

Return a JSON array of objects. Example:
[{{"{ field_names[0]}": "value", "source_numbers": [0, 2], "publication_date": "2024-01-15"}}]

Return ONLY the JSON array, no other text."""

        for attempt in range(2):
            try:
                response = self.model.invoke(prompt)
                text = get_llm_response_text(response)
                parsed = extract_json(text, expected_type=list)

                if parsed is None:
                    if attempt == 0:
                        prompt += "\n\nYou MUST return a valid JSON array. Try again."
                        continue
                    return [], "Failed to parse LLM extraction response"

                # Post-process items
                items: List[Dict[str, Any]] = []
                for raw_item in parsed[:max_items]:
                    item: Dict[str, Any] = {}

                    for fname in field_names:
                        val = raw_item.get(fname, "")
                        if fname in enum_options:
                            normalized, raw_val = _normalize_enum(
                                str(val),
                                enum_options[fname],
                                self._enum_cache,
                                self.model,
                            )
                            item[fname] = normalized
                            if raw_val is not None:
                                item[f"_{fname}_raw"] = raw_val
                        else:
                            item[fname] = str(val) if val else ""

                    # Source attribution
                    src_nums = raw_item.get("source_numbers", [])
                    item_sources = []
                    for sn in src_nums:
                        if isinstance(sn, int) and sn < len(search_results):
                            url = search_results[sn].get("link", "") or search_results[sn].get("url", "")
                            if url in source_map:
                                item_sources.append(source_map[url])
                    item["source_ids"] = item_sources
                    item["source_count"] = len(item_sources)
                    item["confidence"] = _confidence_level(len(item_sources))
                    item["item_id"] = _item_id(item, key_fields)

                    items.append(item)

                return items, None

            except Exception as e:
                if attempt == 0:
                    logger.debug(f"Extraction attempt 1 failed: {e}")
                    continue
                logger.warning(f"Extraction failed after 2 attempts: {e}")
                return [], str(e)

        return [], "Extraction failed"

    # ------------------------------------------------------------------
    # Per-level search engine overrides
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_engine_overrides(
        dimensions: List[Dict[str, Any]],
    ) -> Dict[str, List[str]]:
        """Build a map of dimension_name → search_engines override.

        Walks the dimension tree and collects any ``search_engines`` lists
        specified on individual dimensions.
        """
        overrides: Dict[str, List[str]] = {}
        for dim in dimensions:
            engines = dim.get("search_engines")
            if engines and isinstance(engines, list):
                overrides[dim.get("name", "")] = engines
            children = dim.get("children")
            if children:
                child_list = [children] if isinstance(children, dict) else children
                overrides.update(
                    StructuredResearchStrategy._collect_engine_overrides(child_list)
                )
        return overrides

    def _get_search_for_cell(
        self,
        dim_values: Dict[str, str],
        engine_overrides: Dict[str, List[str]],
    ):
        """Get the search engine for a cell, respecting per-level overrides.

        Returns the override engine if configured, otherwise self.search.
        Currently returns self.search for all cells — override instantiation
        requires the search engine factory which needs settings_snapshot.
        This method provides the hook point for future implementation.
        """
        # Find the deepest dimension that has an override
        for dim_name in reversed(list(dim_values.keys())):
            if dim_name in engine_overrides:
                logger.debug(
                    f"Engine override for {dim_name}: {engine_overrides[dim_name]} "
                    f"(using default engine — factory integration pending)"
                )
                # TODO: instantiate override engines via search_engine_factory
                # For now, log the override but use the default engine
                break
        return self.search

    # ------------------------------------------------------------------
    # Item-level drill-down
    # ------------------------------------------------------------------

    def _find_item_source_dims(
        self, dimensions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Find dimensions with source: 'items' anywhere in the tree."""
        found = []
        for dim in dimensions:
            if dim.get("source") == "items":
                found.append(dim)
            children = dim.get("children")
            if children:
                child_list = [children] if isinstance(children, dict) else children
                found.extend(self._find_item_source_dims(child_list))
        return found

    def _run_drill_down(
        self,
        query: str,
        parent_cells: List[Dict[str, Any]],
        drill_dims: List[Dict[str, Any]],
        source_id_start: int,
        prior_context: str = "",
    ) -> tuple:
        """Run drill-down research for source: 'items' dimensions.

        For each drill-down dimension, populates values from parent items
        and researches each with the drill-down's field set.
        """
        drill_cells: List[Dict[str, Any]] = []
        drill_sources: List[Dict[str, Any]] = []
        sid_counter = source_id_start

        for drill_dim in drill_dims:
            source_field = drill_dim.get("source_field", "")
            drill_name = drill_dim.get("name", "item")
            drill_fields = drill_dim.get("fields", [])

            if not drill_fields:
                continue  # No enrichment fields defined

            drill_field_names = [f["name"] for f in drill_fields]
            drill_enum_options: Dict[str, List[str]] = {}
            for f in drill_fields:
                if f.get("type") == "enum" and f.get("options"):
                    drill_enum_options[f["name"]] = f["options"]
            drill_key_fields = [
                f["name"] for f in drill_fields if f.get("type") == "string"
            ][:2]

            # Collect unique item values from parent cells
            for parent_cell in parent_cells:
                if parent_cell.get("level", 1) > 1:
                    continue  # Don't drill into drill-down cells

                parent_dims = parent_cell.get("dimension_values", {})
                for item in parent_cell.get("items", []):
                    item_value = item.get(source_field, "")
                    if not item_value:
                        continue

                    self.check_termination()

                    # Build dimension values for this drill-down cell
                    drill_dim_values = {
                        **parent_dims,
                        drill_name: str(item_value),
                    }
                    cid = _cell_id(drill_dim_values)

                    # Build parent context from the item's fields
                    parent_item_context = ", ".join(
                        f"{k}: {item.get(k, 'N/A')}"
                        for k in item
                        if not k.startswith("_") and k not in (
                            "source_ids", "source_count", "confidence",
                            "item_id", "conflicts", "appended_from",
                        )
                    )

                    dim_label = " ".join(str(v) for v in drill_dim_values.values())
                    self._update_progress(
                        f"Drill-down: {item_value}...",
                        93,
                        {"phase": "drill_down", "cell_id": cid},
                    )

                    cell_result = self._research_cell(
                        query=f"{query} {item_value} details",
                        dimension_values=drill_dim_values,
                        field_names=drill_field_names,
                        fields=drill_fields,
                        enum_options=drill_enum_options,
                        key_fields=drill_key_fields,
                        iterations=1,
                        max_items=1,  # One detail record per item
                        knowledge_context=[parent_item_context],
                        source_id_start=sid_counter,
                        prior_context=prior_context,
                    )

                    cell_data = cell_result["cell"]
                    cell_data["cell_id"] = cid
                    cell_data["cell_index"] = len(parent_cells) + len(drill_cells)
                    cell_data["level"] = 2
                    cell_data["parent_cell_id"] = _cell_id(parent_dims)
                    cell_data["parent_item_id"] = item.get("item_id", "")
                    cell_data["researched_at"] = datetime.now(timezone.utc).isoformat()

                    drill_cells.append(cell_data)
                    drill_sources.extend(cell_result["sources"])
                    sid_counter += len(cell_result["sources"])

        logger.info(
            f"Drill-down complete: {len(drill_cells)} cells, "
            f"{sum(len(c.get('items', [])) for c in drill_cells)} items"
        )
        return drill_cells, drill_sources

    # ------------------------------------------------------------------
    # Dimension enumeration
    # ------------------------------------------------------------------

    def _enumerate_leaf_cells(
        self,
        dimensions: List[Dict[str, Any]],
        parent_values: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, str]]:
        """Recursively enumerate all leaf cells from the dimension tree.

        Each leaf cell is a dict of ``{dimension_name: value}`` pairs
        representing a unique combination to research.
        """
        if parent_values is None:
            parent_values = {}

        if not dimensions:
            return [parent_values] if parent_values else []

        # Handle multiple top-level dimensions (cross-product)
        if len(dimensions) > 1:
            return self._cross_product(dimensions, parent_values)

        dim = dimensions[0]
        name = dim.get("name", "")
        values = dim.get("values", [])
        children = dim.get("children")

        cells: List[Dict[str, str]] = []
        for val in values:
            current = {**parent_values, name: str(val)}
            if children:
                child_list = [children] if isinstance(children, dict) else children
                cells.extend(
                    self._enumerate_leaf_cells(child_list, current)
                )
            else:
                cells.append(current)

        return cells

    def _cross_product(
        self,
        dimensions: List[Dict[str, Any]],
        parent_values: Dict[str, str],
    ) -> List[Dict[str, str]]:
        """Compute cross-product of multiple same-level dimensions."""
        if not dimensions:
            return [parent_values] if parent_values else []

        first = dimensions[0]
        rest = dimensions[1:]
        name = first.get("name", "")
        values = first.get("values", [])

        cells: List[Dict[str, str]] = []
        for val in values:
            current = {**parent_values, name: str(val)}
            if rest:
                cells.extend(self._cross_product(rest, current))
            else:
                cells.append(current)

        return cells

    # ------------------------------------------------------------------
    # Follow-up generation
    # ------------------------------------------------------------------

    def _generate_followup(
        self,
        query: str,
        dim_label: str,
        field_names: List[str],
        results: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Generate a follow-up search query based on initial results."""
        snippets = "\n".join(
            r.get("snippet", "")[:100] for r in results[:5]
        )
        prompt = (
            f"Based on initial search results about {dim_label} for '{query}', "
            f"generate ONE follow-up search query to find more items with these fields: "
            f"{', '.join(field_names)}.\n\n"
            f"Initial results summary:\n{snippets}\n\n"
            f"Return ONLY the search query, nothing else."
        )
        try:
            resp = get_llm_response_text(self.model.invoke(prompt)).strip()
            return resp if resp and len(resp) < 500 else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _empty_result(self, query: str, error: str) -> Dict[str, Any]:
        return {
            "findings": [],
            "iterations": 0,
            "questions": {},
            "formatted_findings": f"Error: {error}",
            "current_knowledge": "",
            "all_links_of_system": [],
            "structured_data": {
                "schema": self.schema,
                "cells": [],
                "sources": [],
                "warnings": [error],
            },
        }
