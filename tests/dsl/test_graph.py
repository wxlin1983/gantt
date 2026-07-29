"""DAG helpers: ordering, cycle detection and bypass rewiring."""

from __future__ import annotations

import pytest

from app.dsl.graph import (
    CycleError,
    bypass_nodes,
    descendants,
    find_cycle,
    topological_sort,
)


class TestTopologicalSort:
    def test_linear_chain(self):
        order = topological_sort(["c", "b", "a"], [("a", "b"), ("b", "c")])
        assert order == ["a", "b", "c"]

    def test_diamond_respects_both_branches(self):
        order = topological_sort(
            ["a", "b", "c", "d"],
            [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
        )
        assert order[0] == "a"
        assert order[-1] == "d"
        assert set(order[1:3]) == {"b", "c"}

    def test_independent_nodes_keep_declaration_order(self):
        # Stable ordering is what makes expansion deterministic
        assert topological_sort(["x", "y", "z"], []) == ["x", "y", "z"]

    def test_cycle_raises_with_the_loop(self):
        with pytest.raises(CycleError) as exc:
            topological_sort(
                ["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")]
            )
        cycle = exc.value.cycle
        assert cycle[0] == cycle[-1]
        assert set(cycle) == {"a", "b", "c"}

    def test_self_loop(self):
        with pytest.raises(CycleError):
            topological_sort(["a"], [("a", "a")])


class TestFindCycle:
    def test_acyclic_returns_none(self):
        assert find_cycle(["a", "b"], [("a", "b")]) is None

    def test_reports_only_the_cycle_not_its_tail(self):
        found = find_cycle(
            ["a", "b", "c", "d"],
            [("a", "b"), ("b", "c"), ("c", "b"), ("c", "d")],
        )
        assert set(found) == {"b", "c"}


class TestDescendants:
    def test_transitive_reach(self):
        edges = [("a", "b"), ("b", "c"), ("c", "d")]
        assert descendants("a", edges) == {"b", "c", "d"}
        assert descendants("c", edges) == {"d"}
        assert descendants("d", edges) == set()

    def test_terminates_on_cyclic_input(self):
        assert descendants("a", [("a", "b"), ("b", "a")]) == {"a", "b"}


class TestBypass:
    def test_removes_node_and_reconnects(self):
        edges = [("a", "b", 0), ("b", "c", 0)]
        assert bypass_nodes(edges, {"b"}) == [("a", "c", 0)]

    def test_sums_lag_across_the_removed_node(self):
        edges = [("a", "b", 100), ("b", "c", 200)]
        assert bypass_nodes(edges, {"b"}) == [("a", "c", 300)]

    def test_chained_removals(self):
        edges = [("a", "b", 1), ("b", "c", 2), ("c", "d", 4)]
        assert bypass_nodes(edges, {"b", "c"}) == [("a", "d", 7)]

    def test_fan_in_and_fan_out_cross_product(self):
        edges = [
            ("a1", "b", 0),
            ("a2", "b", 0),
            ("b", "c1", 0),
            ("b", "c2", 0),
        ]
        assert set(bypass_nodes(edges, {"b"})) == {
            ("a1", "c1", 0),
            ("a1", "c2", 0),
            ("a2", "c1", 0),
            ("a2", "c2", 0),
        }

    def test_removing_a_source_leaves_successors_unblocked(self):
        edges = [("a", "b", 0), ("b", "c", 0)]
        assert bypass_nodes(edges, {"a"}) == [("b", "c", 0)]

    def test_removing_a_sink(self):
        edges = [("a", "b", 0), ("b", "c", 0)]
        assert bypass_nodes(edges, {"c"}) == [("a", "b", 0)]

    def test_nothing_removed_is_a_passthrough(self):
        edges = [("a", "b", 5)]
        assert bypass_nodes(edges, set()) == edges

    def test_duplicate_edges_keep_the_longest_lag(self):
        # Two skipped branches can rewire onto the same pair; the longer wait
        # is the binding constraint.
        edges = [
            ("a", "x", 100),
            ("x", "c", 0),
            ("a", "y", 500),
            ("y", "c", 0),
        ]
        assert bypass_nodes(edges, {"x", "y"}) == [("a", "c", 500)]
