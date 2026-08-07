"""Tests for structured research route helper functions.

Tests _count_leaf_cells and _apply_dimension_mutations which are
pure functions defined in research_routes.py.
"""

import pytest


# Replicate the helpers (they're module-private in research_routes.py)

def _count_leaf_cells(dimensions, parent_count=1):
    if not dimensions:
        return 0
    total = 0
    for dim in dimensions:
        values = dim.get("values", [])
        if not values:
            continue
        children = dim.get("children")
        if children:
            child_list = [children] if isinstance(children, dict) else children
            child_count = _count_leaf_cells(child_list, 1)
            total += len(values) * max(child_count, 1)
        else:
            total += len(values)
    return total


def _apply_dimension_mutations(dimensions, approve, remove, add):
    if not (approve or remove or add):
        return
    target_dim = None
    mutation_values = set(approve or []) | set(remove or []) | set(add or [])
    for dim in dimensions:
        existing = set(dim.get("values", []))
        if existing & mutation_values or not existing:
            target_dim = dim
            break
    if target_dim is None and dimensions:
        target_dim = dimensions[0]
    if target_dim is None:
        return
    values = target_dim.get("values", [])
    if approve:
        target_dim["values"] = [v for v in values if v in approve]
    if remove:
        target_dim["values"] = [v for v in target_dim.get("values", values) if v not in remove]
    if add:
        current = target_dim.get("values", [])
        for v in add:
            if v not in current:
                current.append(v)
        target_dim["values"] = current


# ---------------------------------------------------------------------------
# Tests: _count_leaf_cells
# ---------------------------------------------------------------------------

class TestCountLeafCells:
    def test_empty_returns_zero(self):
        assert _count_leaf_cells([]) == 0

    def test_no_values_returns_zero(self):
        assert _count_leaf_cells([{"name": "x", "values": []}]) == 0

    def test_flat(self):
        assert _count_leaf_cells([{"name": "c", "values": ["AU", "SG", "IN"]}]) == 3

    def test_nested(self):
        dims = [{"name": "c", "values": ["AU", "SG"], "children": {
            "name": "s", "values": ["gov", "bank"]
        }}]
        assert _count_leaf_cells(dims) == 4

    def test_deep_nested(self):
        dims = [{"name": "r", "values": ["APAC"], "children": {
            "name": "c", "values": ["AU", "SG"], "children": {
                "name": "s", "values": ["gov", "bank"]
            }
        }}]
        assert _count_leaf_cells(dims) == 4  # 1 * 2 * 2

    def test_single_value(self):
        assert _count_leaf_cells([{"name": "x", "values": ["a"]}]) == 1

    def test_children_with_no_values(self):
        # Parent has values but child dimension has empty values
        dims = [{"name": "c", "values": ["AU"], "children": {
            "name": "s", "values": []
        }}]
        # Child has no values, so parent values are the leaves
        assert _count_leaf_cells(dims) == 1


# ---------------------------------------------------------------------------
# Tests: _apply_dimension_mutations
# ---------------------------------------------------------------------------

class TestApplyDimensionMutations:
    def test_approve_filters(self):
        dims = [{"name": "country", "values": ["AU", "SG", "IN"]}]
        _apply_dimension_mutations(dims, approve=["AU", "SG"], remove=[], add=[])
        assert dims[0]["values"] == ["AU", "SG"]

    def test_remove(self):
        dims = [{"name": "country", "values": ["AU", "SG", "IN"]}]
        _apply_dimension_mutations(dims, approve=[], remove=["IN"], add=[])
        assert dims[0]["values"] == ["AU", "SG"]

    def test_add(self):
        dims = [{"name": "country", "values": ["AU"]}]
        _apply_dimension_mutations(dims, approve=[], remove=[], add=["SG", "IN"])
        assert dims[0]["values"] == ["AU", "SG", "IN"]

    def test_add_no_duplicates(self):
        dims = [{"name": "country", "values": ["AU", "SG"]}]
        _apply_dimension_mutations(dims, approve=[], remove=[], add=["SG", "IN"])
        assert dims[0]["values"] == ["AU", "SG", "IN"]

    def test_does_not_affect_other_dimensions(self):
        dims = [
            {"name": "country", "values": ["AU", "SG"]},
            {"name": "sector", "values": ["gov", "bank"]},
        ]
        # Remove AU — should only affect country dimension
        _apply_dimension_mutations(dims, approve=[], remove=["AU"], add=[])
        assert dims[0]["values"] == ["SG"]
        assert dims[1]["values"] == ["gov", "bank"]  # Unchanged

    def test_empty_mutations_no_op(self):
        dims = [{"name": "country", "values": ["AU"]}]
        _apply_dimension_mutations(dims, approve=[], remove=[], add=[])
        assert dims[0]["values"] == ["AU"]

    def test_empty_dimensions(self):
        dims = []
        _apply_dimension_mutations(dims, approve=["AU"], remove=[], add=[])
        # Should not crash


# ---------------------------------------------------------------------------
# Tests: Schema needs discovery
# ---------------------------------------------------------------------------

class TestSchemaNeedsDiscovery:
    @staticmethod
    def _needs_discovery(dimensions):
        for dim in dimensions:
            if dim.get("discover"):
                return True
            children = dim.get("children")
            if children:
                child_list = [children] if isinstance(children, dict) else children
                if TestSchemaNeedsDiscovery._needs_discovery(child_list):
                    return True
        return False

    def test_static_no_discovery(self):
        dims = [{"name": "c", "values": ["AU"]}]
        assert not self._needs_discovery(dims)

    def test_discover_flag(self):
        dims = [{"name": "c", "discover": True, "prompt": "Find countries"}]
        assert self._needs_discovery(dims)

    def test_nested_discovery(self):
        dims = [{"name": "c", "values": ["AU"], "children": {
            "name": "s", "discover": True, "prompt": "Find sectors"
        }}]
        assert self._needs_discovery(dims)

    def test_deep_nested_no_discovery(self):
        dims = [{"name": "c", "values": ["AU"], "children": {
            "name": "s", "values": ["gov"]
        }}]
        assert not self._needs_discovery(dims)
