"""Research context builder for chaining.

Builds a context string from a previous research's structured data
and summaries. Used to provide prior knowledge to new research sessions.
"""

from typing import Any, Dict, Optional

from loguru import logger


def build_prior_context(
    research_meta: Dict[str, Any],
    max_chars: int = 4000,
) -> str:
    """Build a context string from previous structured research.

    Priority:
    1. Summaries (most synthesized, best as context)
    2. Structured data items (fallback if no summaries)

    Args:
        research_meta: The research_meta dict from a ResearchHistory record.
        max_chars: Maximum characters for the context string.

    Returns:
        A context string suitable for inclusion in search queries and
        LLM extraction prompts.
    """
    structured_data = research_meta.get("structured_data")
    if not structured_data:
        return ""

    parts = []

    # Prefer summaries if available
    summaries = structured_data.get("summaries", [])
    if summaries:
        parts.append("## Prior Research Findings\n")
        for s in summaries:
            dim_val = s.get("dimension_value", "")
            content = s.get("content", "")
            if dim_val and content:
                # Truncate each summary proportionally
                max_per_summary = max(200, max_chars // max(len(summaries), 1))
                if len(content) > max_per_summary:
                    content = content[:max_per_summary] + "..."
                parts.append(f"### {dim_val}\n{content}\n")
    else:
        # Fall back to structured data items
        cells = structured_data.get("cells", [])
        schema = structured_data.get("schema", {})
        field_names = [f["name"] for f in schema.get("fields", [])]

        if cells and field_names:
            parts.append("## Prior Research Data\n")
            for cell in cells:
                dim_vals = cell.get("dimension_values", {})
                items = cell.get("items", [])
                if not items:
                    continue

                label = " / ".join(f"{k}={v}" for k, v in dim_vals.items())
                parts.append(f"### {label}")
                for item in items[:5]:  # cap per cell
                    fields_str = ", ".join(
                        f"{f}: {item.get(f, 'N/A')}" for f in field_names[:4]
                    )
                    parts.append(f"  - {fields_str}")
                parts.append("")

    context = "\n".join(parts)

    # Final truncation
    if len(context) > max_chars:
        context = context[:max_chars] + "\n...(truncated)"

    return context


def get_prior_research_context(
    research_id: str,
    username: str,
    max_chars: int = 4000,
) -> Optional[str]:
    """Load a previous research and build context from it.

    Args:
        research_id: ID of the prior research to use as context.
        username: Username for database access.
        max_chars: Maximum context length.

    Returns:
        Context string, or None if research not found.
    """
    try:
        from ...database.models import ResearchHistory
        from ...database.session_context import get_user_db_session

        with get_user_db_session(username) as db_session:
            research = (
                db_session.query(ResearchHistory)
                .filter_by(id=research_id)
                .first()
            )
            if not research or not research.research_meta:
                return None

            return build_prior_context(research.research_meta, max_chars)

    except Exception as e:
        logger.warning(f"Failed to load prior research context: {e}")
        return None
