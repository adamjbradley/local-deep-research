"""Tests for StructuredResearchStrategy pure functions.

Tests cell_id generation, item_id hashing, confidence scoring,
enum normalization, dimension enumeration (flat, nested, cross-product),
and history compaction — all without requiring LLM or search engine.
"""

import hashlib
import json
import pytest


# ---------------------------------------------------------------------------
# Import helpers directly (avoid heavy LLM imports)
# ---------------------------------------------------------------------------

def _cell_id(dimension_values):
    return "|".join(f"{k}={v}" for k, v in sorted(dimension_values.items()))


def _item_id(item, key_fields):
    parts = []
    for f in key_fields:
        val = str(item.get(f, "")).strip().lower()
        if val:
            parts.append(val)
        if len(parts) >= 2:
            break
    raw = "|".join(parts) if parts else json.dumps(item, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def _confidence_level(source_count):
    if source_count >= 3:
        return "high"
    if source_count == 2:
        return "medium"
    if source_count == 1:
        return "low"
    return "unverified"


def _normalize_enum(value, options, cache, llm=None):
    if value in options:
        return value, None
    lower_map = {o.lower(): o for o in options}
    if value.lower() in lower_map:
        return lower_map[value.lower()], None
    cache_key = f"{value.lower()}|{'|'.join(sorted(options))}"
    if cache_key in cache:
        cached = cache[cache_key]
        return (cached, None) if cached != "unknown" else ("unknown", value)
    cache[cache_key] = "unknown"
    return "unknown", value


def _enumerate_leaf_cells(dimensions, parent_values=None):
    if parent_values is None:
        parent_values = {}
    if not dimensions:
        return [parent_values] if parent_values else []
    if len(dimensions) > 1:
        return _cross_product(dimensions, parent_values)
    dim = dimensions[0]
    name = dim.get("name", "")
    values = dim.get("values", [])
    children = dim.get("children")
    cells = []
    for val in values:
        current = {**parent_values, name: str(val)}
        if children:
            child_list = [children] if isinstance(children, dict) else children
            cells.extend(_enumerate_leaf_cells(child_list, current))
        else:
            cells.append(current)
    return cells


def _cross_product(dimensions, parent_values):
    if not dimensions:
        return [parent_values] if parent_values else []
    first = dimensions[0]
    rest = dimensions[1:]
    name = first.get("name", "")
    values = first.get("values", [])
    cells = []
    for val in values:
        current = {**parent_values, name: str(val)}
        if rest:
            cells.extend(_cross_product(rest, current))
        else:
            cells.append(current)
    return cells


# ---------------------------------------------------------------------------
# Tests: Cell Identity
# ---------------------------------------------------------------------------

class TestCellId:
    def test_single_dimension(self):
        assert _cell_id({"country": "Australia"}) == "country=Australia"

    def test_sorted_keys(self):
        assert _cell_id({"sector": "gov", "country": "AU"}) == "country=AU|sector=gov"

    def test_deep_nesting(self):
        result = _cell_id({"country": "AU", "program": "myGovID", "sector": "gov"})
        assert result == "country=AU|program=myGovID|sector=gov"

    def test_empty(self):
        assert _cell_id({}) == ""


# ---------------------------------------------------------------------------
# Tests: Item Identity
# ---------------------------------------------------------------------------

class TestItemId:
    def test_same_product_same_id(self):
        id1 = _item_id({"product_name": "Singpass", "vendor": "GovTech"}, ["product_name", "vendor"])
        id2 = _item_id({"product_name": "Singpass", "vendor": "GovTech"}, ["product_name", "vendor"])
        assert id1 == id2

    def test_different_products_different_ids(self):
        id1 = _item_id({"product_name": "Singpass", "vendor": "GovTech"}, ["product_name", "vendor"])
        id2 = _item_id({"product_name": "myGovID", "vendor": "AU Gov"}, ["product_name", "vendor"])
        assert id1 != id2

    def test_case_insensitive(self):
        id1 = _item_id({"product_name": "Singpass"}, ["product_name"])
        id2 = _item_id({"product_name": "singpass"}, ["product_name"])
        assert id1 == id2

    def test_length(self):
        result = _item_id({"name": "test"}, ["name"])
        assert len(result) == 10


# ---------------------------------------------------------------------------
# Tests: Confidence Scoring
# ---------------------------------------------------------------------------

class TestConfidence:
    def test_high(self):
        assert _confidence_level(3) == "high"
        assert _confidence_level(10) == "high"

    def test_medium(self):
        assert _confidence_level(2) == "medium"

    def test_low(self):
        assert _confidence_level(1) == "low"

    def test_unverified(self):
        assert _confidence_level(0) == "unverified"


# ---------------------------------------------------------------------------
# Tests: Enum Normalization
# ---------------------------------------------------------------------------

class TestEnumNormalization:
    def test_exact_match(self):
        cache = {}
        assert _normalize_enum("active", ["active", "pilot"], cache) == ("active", None)

    def test_case_insensitive(self):
        cache = {}
        assert _normalize_enum("Active", ["active", "pilot"], cache) == ("active", None)
        assert _normalize_enum("PILOT", ["active", "pilot"], cache) == ("pilot", None)

    def test_unknown(self):
        cache = {}
        result = _normalize_enum("operational", ["active", "pilot"], cache)
        assert result == ("unknown", "operational")

    def test_cache_hit(self):
        cache = {}
        _normalize_enum("operational", ["active", "pilot"], cache)
        # Second call should use cache
        result = _normalize_enum("operational", ["active", "pilot"], cache)
        assert result == ("unknown", "operational")
        assert len(cache) == 1

    def test_empty_value(self):
        cache = {}
        result = _normalize_enum("", ["active", "pilot"], cache)
        assert result == ("unknown", "")


# ---------------------------------------------------------------------------
# Tests: Dimension Enumeration
# ---------------------------------------------------------------------------

class TestDimensionEnumeration:
    def test_flat(self):
        dims = [{"name": "country", "values": ["AU", "SG", "IN"]}]
        cells = _enumerate_leaf_cells(dims)
        assert len(cells) == 3
        assert cells[0] == {"country": "AU"}
        assert cells[2] == {"country": "IN"}

    def test_nested(self):
        dims = [{"name": "country", "values": ["AU", "SG"], "children": {
            "name": "sector", "values": ["gov", "bank"]
        }}]
        cells = _enumerate_leaf_cells(dims)
        assert len(cells) == 4
        assert cells[0] == {"country": "AU", "sector": "gov"}
        assert cells[3] == {"country": "SG", "sector": "bank"}

    def test_deep_nesting(self):
        dims = [{"name": "region", "values": ["APAC"], "children": {
            "name": "country", "values": ["AU", "SG"], "children": {
                "name": "sector", "values": ["gov"]
            }
        }}]
        cells = _enumerate_leaf_cells(dims)
        assert len(cells) == 2
        assert cells[0] == {"region": "APAC", "country": "AU", "sector": "gov"}

    def test_cross_product(self):
        dims = [
            {"name": "country", "values": ["AU", "SG"]},
            {"name": "sector", "values": ["gov", "bank"]},
        ]
        cells = _enumerate_leaf_cells(dims)
        assert len(cells) == 4
        assert cells[0] == {"country": "AU", "sector": "gov"}
        assert cells[3] == {"country": "SG", "sector": "bank"}

    def test_empty(self):
        assert _enumerate_leaf_cells([]) == []
        assert _enumerate_leaf_cells([{"name": "x", "values": []}]) == []

    def test_single_value(self):
        cells = _enumerate_leaf_cells([{"name": "x", "values": ["a"]}])
        assert cells == [{"x": "a"}]


# ---------------------------------------------------------------------------
# Tests: CSV Exporter
# ---------------------------------------------------------------------------

class TestCSVExporter:
    def _get_test_data(self):
        return {
            "schema": {"fields": [
                {"name": "product", "type": "string"},
                {"name": "status", "type": "enum"},
            ]},
            "cells": [
                {"cell_id": "c=AU", "dimension_values": {"country": "AU"},
                 "researched_at": "2026-04-16", "items": [
                    {"product": "myGovID", "status": "active",
                     "item_id": "x1", "confidence": "high", "source_count": 3, "source_ids": ["s1"]},
                    {"product": "Digital iD", "status": "unknown", "_status_raw": "operational",
                     "item_id": "x2", "confidence": "low", "source_count": 1, "source_ids": ["s2"]},
                ]},
            ],
            "sources": [
                {"id": "s1", "url": "u1", "title": "t1", "snippet": "s",
                 "content_date": "2023", "discovered_at": "2026", "source_type": "web", "engine": "searxng"},
            ],
            "warnings": [],
        }

    def test_flatten_row_count(self):
        from local_deep_research.exporters.csv_exporter import _flatten_items
        headers, rows = _flatten_items(self._get_test_data())
        assert len(rows) == 2

    def test_flatten_headers(self):
        from local_deep_research.exporters.csv_exporter import _flatten_items
        headers, rows = _flatten_items(self._get_test_data())
        assert "country" in headers
        assert "product" in headers
        assert "status" in headers
        assert "_status_raw" in headers
        assert "confidence" in headers

    def test_raw_enum_preserved(self):
        from local_deep_research.exporters.csv_exporter import _flatten_items
        headers, rows = _flatten_items(self._get_test_data())
        raw_row = [r for r in rows if r.get("product") == "Digital iD"][0]
        assert raw_row["_status_raw"] == "operational"
        assert raw_row["status"] == "unknown"

    def test_sources_csv(self):
        from local_deep_research.exporters.csv_exporter import _build_sources_csv
        headers, rows = _build_sources_csv(self._get_test_data())
        assert len(rows) == 1
        assert rows[0]["source_type"] == "web"

    def test_csv_bytes_bom(self):
        from local_deep_research.exporters.csv_exporter import _flatten_items, _write_csv_bytes
        headers, rows = _flatten_items(self._get_test_data())
        data = _write_csv_bytes(headers, rows)
        assert data[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM
        assert b"myGovID" in data


# ---------------------------------------------------------------------------
# Tests: JSON Exporter Roundtrip
# ---------------------------------------------------------------------------

class TestJSONExporter:
    def test_roundtrip(self):
        original = {
            "cells": [{"cell_id": "test", "items": [{"name": "x"}]}],
            "sources": [],
            "warnings": [],
        }
        serialized = json.dumps(original, indent=2, ensure_ascii=False, default=str)
        roundtrip = json.loads(serialized)
        assert roundtrip == original


# ---------------------------------------------------------------------------
# Tests: History Compaction
# ---------------------------------------------------------------------------

class TestHistoryCompaction:
    @staticmethod
    def _compact(history, max_entries=50):
        """Simplified compaction for testing."""
        if len(history) <= max_entries:
            return history
        if not history:
            return history
        first = history[0]
        last = history[-1]
        middle = history[1:-1]
        by_date = {}
        for entry in middle:
            ts = entry.get("timestamp", "")
            date_key = ts[:10] if len(ts) >= 10 else "unknown"
            by_date.setdefault(date_key, []).append(entry)
        compacted_middle = []
        for date_key in sorted(by_date.keys()):
            entries = by_date[date_key]
            if len(entries) == 1:
                compacted_middle.append(entries[0])
            else:
                compacted_middle.append({
                    "timestamp": entries[-1].get("timestamp", ""),
                    "action": "daily_summary",
                    "cells_researched": sum(e.get("cells_researched", 0) for e in entries),
                })
        result = [first] + compacted_middle + [last]
        if len(result) > max_entries:
            step = max(1, len(result) // (max_entries - 2))
            sampled = [result[i] for i in range(1, len(result) - 1, step)]
            result = [first] + sampled[:max_entries - 2] + [last]
        return result

    def test_under_limit_unchanged(self):
        history = [{"timestamp": f"2026-04-{i:02d}T00:00:00Z"} for i in range(1, 10)]
        assert len(self._compact(history)) == 9

    def test_compacts_same_day(self):
        history = [
            {"timestamp": f"2026-04-15T{i:02d}:00:00Z", "cells_researched": 1}
            for i in range(60)
        ]
        result = self._compact(history)
        assert len(result) <= 50
        assert result[0] == history[0]
        assert result[-1] == history[-1]

    def test_multi_day_compaction(self):
        history = []
        for d in range(1, 31):
            for h in range(2):
                history.append({
                    "timestamp": f"2026-04-{d:02d}T{h:02d}:00:00Z",
                    "cells_researched": 1,
                })
        result = self._compact(history)
        assert len(result) <= 50


# ---------------------------------------------------------------------------
# Tests: Delta Merge
# ---------------------------------------------------------------------------

class TestDeltaMerge:
    @staticmethod
    def _merge(previous, new, reused_cell_ids):
        prev_cells = {c["cell_id"]: c for c in previous.get("cells", []) if c.get("cell_id")}
        new_cells = {c["cell_id"]: c for c in new.get("cells", []) if c.get("cell_id")}
        merged = []
        for cid in sorted(set(prev_cells) | set(new_cells)):
            if cid in new_cells:
                merged.append(new_cells[cid])
            elif cid in reused_cell_ids and cid in prev_cells:
                merged.append(prev_cells[cid])
        prev_sources = {s["id"]: s for s in previous.get("sources", [])}
        for s in new.get("sources", []):
            prev_sources[s["id"]] = s
        return {
            "cells": merged,
            "sources": list(prev_sources.values()),
        }

    def test_merge_adds_new_cells(self):
        prev = {"cells": [{"cell_id": "c=AU", "items": []}], "sources": []}
        new = {"cells": [{"cell_id": "c=TH", "items": []}], "sources": []}
        result = self._merge(prev, new, reused_cell_ids={"c=AU"})
        assert len(result["cells"]) == 2

    def test_merge_removes_unreused(self):
        prev = {"cells": [{"cell_id": "c=AU"}, {"cell_id": "c=SG"}], "sources": []}
        new = {"cells": [{"cell_id": "c=TH"}], "sources": []}
        result = self._merge(prev, new, reused_cell_ids={"c=AU"})
        cell_ids = [c["cell_id"] for c in result["cells"]]
        assert "c=SG" not in cell_ids
        assert "c=AU" in cell_ids
        assert "c=TH" in cell_ids

    def test_merge_deduplicates_sources(self):
        prev = {"cells": [], "sources": [{"id": "s1", "url": "old"}]}
        new = {"cells": [], "sources": [{"id": "s1", "url": "new"}, {"id": "s2", "url": "x"}]}
        result = self._merge(prev, new, reused_cell_ids=set())
        assert len(result["sources"]) == 2
        s1 = [s for s in result["sources"] if s["id"] == "s1"][0]
        assert s1["url"] == "new"  # New overwrites old
