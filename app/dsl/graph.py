"""DAG helpers shared by the expansion pipeline and the validator.

Kept free of DSL types so the scheduling engine can reuse them: everything
operates on opaque node ids plus ``(predecessor, successor, lag)`` edges.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence


class CycleError(Exception):
    """A dependency cycle was found.

    ``cycle`` is the node sequence with the entry node repeated at the end,
    e.g. ``["a", "b", "c", "a"]``, so the editor can highlight the loop.
    """

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(" -> ".join(cycle))


Edge = tuple[str, str, int]


def find_cycle(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> list[str] | None:
    """Return one cycle as a node list, or None when the graph is acyclic."""
    successors: dict[str, list[str]] = defaultdict(list)
    for predecessor, successor in edges:
        successors[predecessor].append(successor)

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = dict.fromkeys(nodes, WHITE)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = GREY
        stack.append(node)
        for nxt in successors.get(node, ()):
            if colour.get(nxt, WHITE) == GREY:
                # Found a back edge: slice the cycle out of the current stack
                start = stack.index(nxt)
                return [*stack[start:], nxt]
            if colour.get(nxt, WHITE) == WHITE:
                found = visit(nxt)
                if found is not None:
                    return found
        stack.pop()
        colour[node] = BLACK
        return None

    for node in list(colour):
        if colour[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None


def topological_sort(
    nodes: Sequence[str], edges: Iterable[tuple[str, str]]
) -> list[str]:
    """Kahn's algorithm, preserving the caller's node order among ties.

    Stable ordering matters: it is what makes expansion deterministic, so the
    same template plus the same parameters always yields the same graph.
    """
    edge_list = list(edges)
    indegree: dict[str, int] = dict.fromkeys(nodes, 0)
    successors: dict[str, list[str]] = defaultdict(list)

    for predecessor, successor in edge_list:
        if predecessor not in indegree or successor not in indegree:
            continue
        successors[predecessor].append(successor)
        indegree[successor] += 1

    position = {node: i for i, node in enumerate(nodes)}
    ready = sorted(
        (n for n, deg in indegree.items() if deg == 0), key=position.get
    )
    order: list[str] = []

    while ready:
        node = ready.pop(0)
        order.append(node)
        newly_ready = []
        for successor in successors.get(node, ()):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                newly_ready.append(successor)
        if newly_ready:
            ready = sorted(ready + newly_ready, key=position.get)

    if len(order) != len(indegree):
        cycle = find_cycle(nodes, edge_list)
        raise CycleError(cycle or sorted(set(indegree) - set(order)))
    return order


def descendants(start: str, edges: Iterable[tuple[str, str]]) -> set[str]:
    """Every node reachable downstream of ``start``.

    Used to reject a new dependency before the user commits to it: adding
    ``u -> v`` is safe only when ``u`` is not already downstream of ``v``.
    """
    successors: dict[str, list[str]] = defaultdict(list)
    for predecessor, successor in edges:
        successors[predecessor].append(successor)

    seen: set[str] = set()
    stack = list(successors.get(start, ()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(successors.get(node, ()))
    return seen


def bypass_nodes(edges: Iterable[Edge], removed: Iterable[str]) -> list[Edge]:
    """Drop ``removed`` nodes and reconnect their neighbours (§4.11).

    For every ``(p -> removed -> s)`` an edge ``p -> s`` is created with the
    two lags added together, so a skipped step does not silently swallow the
    wait time either side of it. Chains of removed nodes are handled by
    repeating until nothing changes, which also terminates on cyclic input
    (the cycle is reported later by the validator).
    """
    dropped = set(removed)
    if not dropped:
        return list(edges)

    current = list(edges)
    for _ in range(len(dropped) + 1):
        target = next(
            (
                node
                for node in dropped
                if any(e[0] == node or e[1] == node for e in current)
            ),
            None,
        )
        if target is None:
            break

        incoming = [e for e in current if e[1] == target]
        outgoing = [e for e in current if e[0] == target]
        rest = [e for e in current if e[0] != target and e[1] != target]

        rewired: list[Edge] = []
        for predecessor, _, lag_in in incoming:
            for _, successor, lag_out in outgoing:
                if predecessor == successor:
                    continue  # a self-loop here means the input was cyclic
                rewired.append((predecessor, successor, lag_in + lag_out))
        current = rest + rewired

    return _dedupe(current)


def _dedupe(edges: Iterable[Edge]) -> list[Edge]:
    """Collapse duplicate edges, keeping the largest lag.

    Duplicates arise when several skipped nodes rewire onto the same pair.
    The longest wait is the binding one, so that is what survives.
    """
    best: dict[tuple[str, str], int] = {}
    order: list[tuple[str, str]] = []
    for predecessor, successor, lag in edges:
        key = (predecessor, successor)
        if key not in best:
            order.append(key)
            best[key] = lag
        else:
            best[key] = max(best[key], lag)
    return [(p, s, best[(p, s)]) for p, s in order]
