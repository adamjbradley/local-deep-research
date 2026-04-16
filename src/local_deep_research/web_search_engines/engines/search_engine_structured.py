"""
Structured data search engine.

Queries structured_data JSON across all completed structured research
in the user's database. Enables precise filtering by dimension values,
field values, and confidence levels.
"""

from typing import Any, Dict, List, Optional

from langchain_core.language_models import BaseLLM
from loguru import logger

from ..search_engine_base import BaseSearchEngine


class StructuredDataSearchEngine(BaseSearchEngine):
    """Search across all structured research data in the knowledge base."""

    is_local = True
    is_lexical = True
    needs_llm_relevance_filter = False  # Results are already precise

    def __init__(
        self,
        username: Optional[str] = None,
        user_password: Optional[str] = None,
        max_results: int = 20,
        llm: Optional[BaseLLM] = None,
        max_filtered_results: Optional[int] = None,
        settings_snapshot: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        super().__init__(
            llm=llm,
            max_filtered_results=max_filtered_results,
            max_results=max_results,
            settings_snapshot=settings_snapshot,
            **kwargs,
        )
        self.username = username
        self.user_password = user_password

    def _get_previews(self, query: str) -> List[Dict[str, Any]]:
        """Search structured data across all completed structured research.

        Performs case-insensitive text matching across:
        - Dimension values
        - Item field values
        - Research queries
        """
        if not self.username:
            logger.warning("StructuredDataSearchEngine: no username configured")
            return []

        try:
            from ...database.models.research import ResearchHistory
            from ...database.session_context import get_user_db_session

            previews: List[Dict[str, Any]] = []
            query_lower = query.lower()

            with get_user_db_session(self.username, self.user_password) as session:
                # Load all completed structured research
                results = (
                    session.query(ResearchHistory)
                    .filter(ResearchHistory.mode == "structured")
                    .filter(ResearchHistory.status == "completed")
                    .all()
                )

                for research in results:
                    meta = research.research_meta or {}
                    sd = meta.get("structured_data", {})
                    cells = sd.get("cells", [])

                    for cell in cells:
                        dim_vals = cell.get("dimension_values", {})
                        items = cell.get("items", [])

                        for item in items:
                            # Check if query matches any field value or dimension
                            match = False
                            match_field = ""

                            # Check dimension values
                            for k, v in dim_vals.items():
                                if query_lower in str(v).lower():
                                    match = True
                                    match_field = f"{k}={v}"
                                    break

                            # Check item field values
                            if not match:
                                for k, v in item.items():
                                    if k.startswith("_") or k in (
                                        "source_ids", "source_count",
                                        "confidence", "item_id",
                                    ):
                                        continue
                                    if query_lower in str(v).lower():
                                        match = True
                                        match_field = f"{k}: {v}"
                                        break

                            # Check research query
                            if not match and query_lower in (research.query or "").lower():
                                match = True
                                match_field = f"query: {research.query[:50]}"

                            if match:
                                # Build preview
                                dim_label = " / ".join(
                                    f"{k}={v}" for k, v in dim_vals.items()
                                )
                                first_field = next(
                                    (v for k, v in item.items()
                                     if k not in ("source_ids", "source_count",
                                                  "confidence", "item_id")
                                     and not k.startswith("_") and v),
                                    "?"
                                )
                                snippet_parts = [
                                    f"Match: {match_field}",
                                    f"Confidence: {item.get('confidence', '?')}",
                                ]
                                # Add a few field values
                                for k, v in list(item.items())[:4]:
                                    if k not in ("source_ids", "source_count",
                                                 "confidence", "item_id") and not k.startswith("_"):
                                        snippet_parts.append(f"{k}: {v}")

                                previews.append({
                                    "id": f"sd:{research.id}:{cell.get('cell_id', '')}:{item.get('item_id', '')}",
                                    "title": f"{first_field} [{dim_label}]",
                                    "snippet": " | ".join(snippet_parts),
                                    "link": f"/results/{research.id}",
                                    "confidence": item.get("confidence", "unknown"),
                                    "research_id": research.id,
                                    "cell_id": cell.get("cell_id", ""),
                                    "dimension_values": dim_vals,
                                })

                                if len(previews) >= self.max_results:
                                    break
                        if len(previews) >= self.max_results:
                            break
                    if len(previews) >= self.max_results:
                        break

            logger.info(
                f"Structured data search for '{query}': {len(previews)} results "
                f"from {len(results)} research sessions"
            )
            return previews

        except Exception:
            logger.exception("Error searching structured data")
            return []
