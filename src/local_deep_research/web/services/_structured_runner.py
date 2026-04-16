"""Structured research mode runner.

Handles the structured research execution path, bypassing
AdvancedSearchSystem in favor of StructuredResearchStrategy directly.
Called from ``run_research_process()`` when ``mode == "structured"``.

Supports delta research: when previous results exist, only new cells
are researched and results are merged.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from ...database.models import ResearchHistory
from ...database.session_context import get_user_db_session

_MAX_HISTORY_ENTRIES = 50


def run_structured_mode(
    research_id,
    query,
    llm,
    search,
    kwargs,
    settings_snapshot,
    progress_callback,
    username,
):
    """Execute structured research and store results.

    Supports both initial runs and delta re-runs.  On re-run, loads
    previous cells, computes which cell_ids are new, and only researches
    the delta.
    """
    from ...advanced_search_system.strategies.structured_strategy import (
        StructuredResearchStrategy,
    )

    schema_definition = kwargs.get("schema_definition", {})
    previous_data = None

    if not schema_definition:
        # Load from research_meta (re-run / refinement case)
        try:
            with get_user_db_session(username) as db_session:
                research = (
                    db_session.query(ResearchHistory)
                    .filter_by(id=research_id)
                    .first()
                )
                if research and research.research_meta:
                    meta = research.research_meta
                    schema_definition = (
                        meta.get("submission", {})
                        .get("schema_definition", {})
                    )
                    previous_data = meta.get("structured_data")
        except Exception:
            logger.warning("Could not load schema from research_meta")

    if not schema_definition:
        progress_callback(
            "Error: No schema definition found",
            100,
            {"phase": "error", "error": "No schema definition", "status": "failed"},
        )
        _update_research_status(
            research_id, username, "failed", error="No schema definition found"
        )
        return

    # Create the strategy directly (bypasses search_system_factory)
    strategy = StructuredResearchStrategy(
        model=llm,
        search=search,
        schema_definition=schema_definition,
        settings_snapshot=settings_snapshot,
    )
    strategy.set_progress_callback(progress_callback)

    try:
        progress_callback(
            "Starting structured research",
            5,
            {"phase": "structured_research"},
        )

        # Compute delta if previous results exist
        previous_cell_ids = set()
        if previous_data:
            prev_schema = previous_data.get("schema", {})
            curr_fields = schema_definition.get("fields", [])
            prev_fields = prev_schema.get("fields", [])

            # If field definitions changed, invalidate everything
            fields_changed = (
                [f.get("name") for f in curr_fields] !=
                [f.get("name") for f in prev_fields]
            )
            if fields_changed:
                logger.info(
                    "Field definitions changed — full re-research required"
                )
                previous_cell_ids = set()  # No reuse
            else:
                previous_cell_ids = {
                    c.get("cell_id", "")
                    for c in previous_data.get("cells", [])
                    if c.get("status") != "removed"
                }
                cross_context = schema_definition.get("options", {}).get(
                    "cross_cell_context", True
                )
                if cross_context and previous_cell_ids:
                    # With cross-cell context, cells depend on predecessors.
                    # For simplicity, research all cells when context is on.
                    logger.info(
                        "Cross-cell context enabled — researching all cells "
                        "for consistency"
                    )
                    previous_cell_ids = set()

        results = strategy.analyze_topic(query)
        new_structured_data = results.get("structured_data", {})
        formatted_findings = results.get("formatted_findings", "")

        # Merge with previous data if this is a delta run
        if previous_data and previous_cell_ids:
            new_structured_data = _merge_delta(
                previous_data, new_structured_data, previous_cell_ids
            )

        # Store results
        _store_structured_results(
            research_id=research_id,
            username=username,
            structured_data=new_structured_data,
            formatted_findings=formatted_findings,
            previous_data=previous_data,
        )

        progress_callback(
            "Structured research complete",
            100,
            {"phase": "complete", "status": "completed"},
        )

    except Exception as e:
        logger.exception(f"Structured research failed: {e}")
        progress_callback(
            f"Error: {e!s}",
            100,
            {"phase": "error", "error": str(e), "status": "failed"},
        )
        _update_research_status(research_id, username, "failed", error=str(e))


# ---------------------------------------------------------------------------
# Delta merge
# ---------------------------------------------------------------------------


def _merge_delta(
    previous: Dict[str, Any],
    new: Dict[str, Any],
    reused_cell_ids: set,
) -> Dict[str, Any]:
    """Merge new results with previous, keeping reused cells intact."""
    prev_cells = {
        c["cell_id"]: c
        for c in previous.get("cells", [])
        if c.get("cell_id")
    }
    new_cells = {
        c["cell_id"]: c
        for c in new.get("cells", [])
        if c.get("cell_id")
    }

    # Merged cell list: reused from previous + new from this run
    merged_cells = []
    all_cell_ids = set(prev_cells.keys()) | set(new_cells.keys())

    for cid in sorted(all_cell_ids):
        if cid in new_cells:
            merged_cells.append(new_cells[cid])
        elif cid in reused_cell_ids and cid in prev_cells:
            merged_cells.append(prev_cells[cid])
        # else: removed cell — not included in active data

    # Merge sources (deduplicate by id)
    prev_sources = {s["id"]: s for s in previous.get("sources", [])}
    for s in new.get("sources", []):
        prev_sources[s["id"]] = s
    merged_sources = list(prev_sources.values())

    # Merge warnings
    merged_warnings = list(
        set(previous.get("warnings", []) + new.get("warnings", []))
    )

    return {
        "schema": new.get("schema", previous.get("schema", {})),
        "cells": merged_cells,
        "sources": merged_sources,
        "warnings": merged_warnings,
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _store_structured_results(
    research_id: str,
    username: str,
    structured_data: Dict[str, Any],
    formatted_findings: str,
    previous_data: Optional[Dict[str, Any]] = None,
):
    """Store structured research results in the database."""
    try:
        with get_user_db_session(username) as db_session:
            research = (
                db_session.query(ResearchHistory)
                .filter_by(id=research_id)
                .first()
            )
            if not research:
                logger.error(f"Research {research_id} not found for storing results")
                return

            meta = research.research_meta or {}
            cells = structured_data.get("cells", [])

            # Store structured data
            meta["structured_data"] = structured_data

            # Build history entry
            history = meta.get("dimension_history", [])
            new_cell_ids = {c.get("cell_id", "") for c in cells}

            if previous_data:
                prev_cells_map = {
                    c.get("cell_id", ""): c
                    for c in previous_data.get("cells", [])
                }
                prev_cell_ids = set(prev_cells_map.keys())
                added = sorted(new_cell_ids - prev_cell_ids)
                removed = sorted(prev_cell_ids - new_cell_ids)

                # Detect changed cells (same cell_id but different item count)
                changed = []
                for cell in cells:
                    cid = cell.get("cell_id", "")
                    if cid in prev_cells_map:
                        prev_item_count = len(prev_cells_map[cid].get("items", []))
                        curr_item_count = len(cell.get("items", []))
                        if prev_item_count != curr_item_count:
                            changed.append(cid)
                action = "refinement"
            else:
                added = sorted(new_cell_ids)
                removed = []
                changed = []
                action = "initial"

            diff_entry: Dict[str, Any] = {
                "added": added if len(added) <= 100 else [f"bulk_add:{len(added)}"],
                "removed": removed if len(removed) <= 100 else [f"bulk_remove:{len(removed)}"],
                "changed": changed if len(changed) <= 100 else [f"bulk_changed:{len(changed)}"],
            }

            history.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "cells_researched": len(cells),
                "diff": diff_entry,
            })

            # Compact history if needed
            history = _compact_history(history)
            meta["dimension_history"] = history

            # Update phase
            meta["structured_phase"] = "refinable"

            research.research_meta = meta
            research.report_content = formatted_findings
            research.status = "completed"
            research.completed_at = datetime.now(timezone.utc).isoformat()
            research.progress = 100

            # Force SQLAlchemy to detect the JSON mutation
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(research, "research_meta")

            db_session.commit()

            # Verify the commit worked
            db_session.refresh(research)
            stored_meta = research.research_meta or {}
            has_sd = "structured_data" in stored_meta
            logger.info(
                f"Post-commit verification: structured_data present={has_sd}, "
                f"meta keys={list(stored_meta.keys())}"
            )
            logger.info(
                f"Stored structured results for {research_id}: "
                f"{len(cells)} cells, "
                f"{sum(len(c.get('items', [])) for c in cells)} items, "
                f"{len(history)} history entries"
            )

    except Exception:
        logger.exception(f"Failed to store structured results for {research_id}")


# ---------------------------------------------------------------------------
# History compaction
# ---------------------------------------------------------------------------


def _compact_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact history to stay within _MAX_HISTORY_ENTRIES.

    Strategy: keep first entry, last entry, and one entry per day.
    Merge same-day entries into daily summaries.
    """
    if len(history) <= _MAX_HISTORY_ENTRIES:
        return history

    if not history:
        return history

    first = history[0]
    last = history[-1]
    middle = history[1:-1]

    # Group by date
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for entry in middle:
        ts = entry.get("timestamp", "")
        date_key = ts[:10] if len(ts) >= 10 else "unknown"
        by_date.setdefault(date_key, []).append(entry)

    # Keep one entry per day (the last one for that day)
    compacted_middle = []
    for date_key in sorted(by_date.keys()):
        entries = by_date[date_key]
        if len(entries) == 1:
            compacted_middle.append(entries[0])
        else:
            # Merge into a daily summary
            total_added = 0
            total_removed = 0
            total_cells = 0
            for e in entries:
                diff = e.get("diff", {})
                added = diff.get("added", [])
                removed = diff.get("removed", [])
                total_added += (
                    len(added)
                    if added and not str(added[0]).startswith("bulk_")
                    else 0
                )
                total_removed += (
                    len(removed)
                    if removed and not str(removed[0]).startswith("bulk_")
                    else 0
                )
                total_cells += e.get("cells_researched", 0)

            compacted_middle.append({
                "timestamp": entries[-1].get("timestamp", ""),
                "action": "daily_summary",
                "detail": {
                    "entries_merged": len(entries),
                    "date": date_key,
                },
                "cells_researched": total_cells,
                "diff": {
                    "added": [f"bulk_add:{total_added}"] if total_added else [],
                    "removed": [f"bulk_remove:{total_removed}"] if total_removed else [],
                    "changed": [],
                },
            })

    result = [first] + compacted_middle + [last]

    # If still over limit, keep only first, last, and evenly spaced entries
    if len(result) > _MAX_HISTORY_ENTRIES:
        step = max(1, len(result) // (_MAX_HISTORY_ENTRIES - 2))
        sampled = [result[i] for i in range(1, len(result) - 1, step)]
        result = [first] + sampled[:_MAX_HISTORY_ENTRIES - 2] + [last]

    return result


# ---------------------------------------------------------------------------
# Status helper
# ---------------------------------------------------------------------------


def _update_research_status(research_id, username, status, error=None):
    """Update research status in the database."""
    try:
        with get_user_db_session(username) as db_session:
            research = (
                db_session.query(ResearchHistory)
                .filter_by(id=research_id)
                .first()
            )
            if research:
                research.status = status
                research.completed_at = datetime.now(timezone.utc).isoformat()
                if error:
                    meta = research.research_meta or {}
                    meta["error"] = error
                    meta["structured_phase"] = "error"
                    research.research_meta = meta
                db_session.commit()
    except Exception:
        logger.exception(f"Failed to update status for {research_id}")
