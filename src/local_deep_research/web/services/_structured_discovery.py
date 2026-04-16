"""Structured research discovery service.

Runs LLM-based discovery of dimension values. Called by the session
layer before execution to resolve dimensions marked with ``discover: true``.
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from ...utilities.json_utils import extract_json, get_llm_response_text


def run_discovery(
    dimensions: List[Dict[str, Any]],
    query: str,
    llm,
    search,
    parent_values: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Resolve all discovery dimensions in the tree.

    Modifies dimensions in place — replaces ``discover: true`` with
    concrete ``values`` lists. Returns the list of discovered value sets
    for review.

    Args:
        dimensions: The dimension tree (mutated in place).
        query: The research query for context.
        llm: LangChain LLM instance.
        search: Search engine instance.
        parent_values: Parent dimension values for context in nested discovery.

    Returns:
        List of dicts describing what was discovered, for UI review.
    """
    discoveries = []

    for dim in dimensions:
        if dim.get("discover") and not dim.get("values"):
            discovered = _discover_values(
                dim_name=dim["name"],
                prompt_template=dim.get("prompt", ""),
                query=query,
                llm=llm,
                search=search,
                max_values=dim.get("max_values", 10),
                parent_values=parent_values,
            )

            dim["values"] = discovered
            dim["_discovered"] = True

            discoveries.append({
                "dimension": dim["name"],
                "values": discovered,
                "parent_values": parent_values,
            })

            logger.info(
                f"Discovered {len(discovered)} values for dimension "
                f"'{dim['name']}': {discovered}"
            )

        # Recurse into children
        children = dim.get("children")
        if children:
            child_list = [children] if isinstance(children, dict) else children

            if dim.get("values"):
                # For each parent value, discover child values
                for parent_val in dim["values"]:
                    child_parent = {**(parent_values or {}), dim["name"]: parent_val}
                    child_discoveries = run_discovery(
                        child_list, query, llm, search, child_parent
                    )
                    discoveries.extend(child_discoveries)

    return discoveries


def _discover_values(
    dim_name: str,
    prompt_template: str,
    query: str,
    llm,
    search,
    max_values: int = 10,
    parent_values: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Discover dimension values using search + LLM extraction.

    1. Build a search query from the prompt template and context.
    2. Run a search to get relevant content.
    3. Ask the LLM to extract dimension values from the results.
    """
    # Build search query
    if prompt_template:
        search_query = prompt_template
        # Replace placeholders
        search_query = search_query.replace("{query}", query)
        if parent_values:
            for k, v in parent_values.items():
                search_query = search_query.replace(f"{{{k}}}", v)
    else:
        search_query = f"List of {dim_name} for {query}"
        if parent_values:
            context = " ".join(str(v) for v in parent_values.values())
            search_query = f"List of {dim_name} for {query} {context}"

    # Search
    search_context = ""
    try:
        results = search.run(search_query)
        if results:
            snippets = []
            for r in results[:10]:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                if title or snippet:
                    snippets.append(f"- {title}: {snippet[:150]}")
            search_context = "\n".join(snippets)
    except Exception as e:
        logger.warning(f"Discovery search failed for {dim_name}: {e}")

    # LLM extraction
    prompt = (
        f"Based on the following search results, list the {dim_name} values "
        f"relevant to: {query}\n\n"
    )
    if parent_values:
        prompt += f"Context: {parent_values}\n\n"
    if search_context:
        prompt += f"Search results:\n{search_context}\n\n"
    else:
        prompt += "(No search results found — use your knowledge.)\n\n"

    prompt += (
        f"Return a JSON array of up to {max_values} {dim_name} values "
        f"as strings. Return ONLY the JSON array, nothing else.\n"
        f'Example: ["value1", "value2", "value3"]'
    )

    try:
        response = llm.invoke(prompt)
        text = get_llm_response_text(response)
        parsed = extract_json(text, expected_type=list)

        if parsed and isinstance(parsed, list):
            # Normalize: strings only, deduplicate, cap at max
            values = []
            seen = set()
            for v in parsed:
                s = str(v).strip()
                if s and s.lower() not in seen:
                    values.append(s)
                    seen.add(s.lower())
                if len(values) >= max_values:
                    break
            return values

    except Exception as e:
        logger.warning(f"Discovery LLM extraction failed for {dim_name}: {e}")

    return []
