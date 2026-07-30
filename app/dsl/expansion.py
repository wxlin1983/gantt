"""Build-time expansion pipeline (implement.md §4.15).

The ordering of the stages is part of the specification, not an implementation
detail: `when` decides whether a node exists at all, and only afterwards can
the remaining expressions and the dependency edges be resolved.

One flow node yields at most one task. That invariant is what keeps edge
handling trivial -- a task's id is its node id, so the edge list that survives
`bypass_nodes` is already the final one.

The whole module is a pure function of (template, task templates, parameters).
The same inputs always produce the same graph, which is what lets case
creation and the dry-run preview share one code path.
"""

from __future__ import annotations

from typing import Any

from .duration import parse_duration, parse_duration_parts
from .errors import DslError, Issue, error, warning
from .expressions import EvalContext, render
from .graph import CycleError, Edge, bypass_nodes, topological_sort
from .params import resolve_params, resolve_roles
from .schema import (
    ExpandedEdge,
    ExpandedTask,
    ExpansionResult,
    FailurePolicy,
    FlowNode,
    GanttTemplate,
    OwnerKind,
    OwnerSpec,
    ScheduleMode,
    SkippedTask,
    TaskTemplate,
    resolve_calendar,
)


def expand(
    template: GanttTemplate,
    task_templates: dict[str, TaskTemplate],
    params: dict[str, Any] | None = None,
    roles: dict[str, str] | None = None,
    case: dict[str, Any] | None = None,
) -> ExpansionResult:
    """Run stages 1-9 of the pipeline and return the concrete task graph.

    Raises :class:`DslError` carrying every issue found, so the caller can show
    a complete problem list rather than one error at a time.
    """
    resolved_params, param_issues = resolve_params(template, params or {})
    resolved_roles, role_issues = resolve_roles(template, roles or {})
    issues = [*param_issues, *role_issues]
    if issues:
        raise DslError(issues)

    ctx = EvalContext(
        para=resolved_params, case=dict(case or {}), role=resolved_roles
    )
    builder = _Expansion(template, task_templates, ctx)
    return builder.run()


class _Expansion:
    def __init__(
        self,
        template: GanttTemplate,
        task_templates: dict[str, TaskTemplate],
        ctx: EvalContext,
    ):
        self.template = template
        self.task_templates = task_templates
        self.ctx = ctx
        self.issues: list[Issue] = []
        self.warnings: list[Issue] = []
        self.skipped: list[SkippedTask] = []
        self.tasks: list[ExpandedTask] = []

    # -- stage driver ------------------------------------------------------

    def run(self) -> ExpansionResult:
        self._check_unknown_references()
        skipped = self._evaluate_when()
        self._flush()

        edges = bypass_nodes(self._node_edges(), skipped)

        self._build_tasks(skipped)
        self._flush()

        self._resolve_owners()
        self._check_graph(edges)
        self._flush()

        return ExpansionResult(
            tasks=self.tasks,
            edges=[
                ExpandedEdge(predecessor=p, successor=s, lag_seconds=lag)
                for p, s, lag in edges
            ],
            skipped=self.skipped,
            warnings=self.warnings,
            buffer_seconds=parse_duration(
                self.template.buffer, "gantt.buffer"
            ),
        )

    def _flush(self) -> None:
        if self.issues:
            raise DslError(self.issues)

    def _fail(self, code: str, message: str, path: str = "") -> None:
        self.issues.append(error(code, message, path))

    # -- stage 4: when -----------------------------------------------------

    def _evaluate_when(self) -> set[str]:
        skipped: set[str] = set()
        for node in self.template.flow:
            if node.when is None:
                continue
            path = f"{node.path}.when"
            try:
                verdict = render(node.when, self.ctx, path)
            except DslError as exc:
                self.issues.extend(exc.issues)
                continue
            if not isinstance(verdict, bool):
                self._fail(
                    "E_BAD_WHEN",
                    f"`when` must evaluate to a boolean, got "
                    f"{type(verdict).__name__} ({verdict!r})",
                    path,
                )
                continue
            if not verdict:
                skipped.add(node.id)
                self.skipped.append(
                    SkippedTask(
                        id=node.id,
                        label=node.label or node.id,
                        reason=f"when evaluated false: {node.when}",
                    )
                )
        return skipped

    # -- stage 5+6: tasks and edges ---------------------------------------

    def _node_edges(self) -> list[Edge]:
        """Dependency edges, including those touching skipped nodes.

        Skipped nodes are left in deliberately: ``bypass_nodes`` needs them to
        work out which neighbours to stitch together, and how much lag to
        carry across the gap.
        """
        edges: list[Edge] = []
        for node in self.template.flow:
            for position, ref in enumerate(node.requirement):
                path = f"{node.path}.requirement[{position}]"
                try:
                    lag_value = render(ref.lag, self.ctx, path)
                    lag_seconds = parse_duration(lag_value, path)
                except DslError as exc:
                    self.issues.extend(exc.issues)
                    continue
                if lag_seconds < 0:
                    self._fail(
                        "E_NEGATIVE_LAG", "lag cannot be negative", path
                    )
                    continue
                edges.append((ref.task, node.id, lag_seconds))
        return edges

    def _build_tasks(self, skipped: set[str]) -> None:
        for node in self.template.flow:
            if node.id in skipped:
                continue
            self.tasks.append(self._build_task(node))

    def _build_task(self, node: FlowNode) -> ExpandedTask:
        source = self.task_templates.get(node.uses) if node.uses else None
        ctx = self.ctx

        def evaluate(value: Any, field_name: str) -> Any:
            try:
                return render(value, ctx, f"{node.path}.{field_name}")
            except DslError as exc:
                self.issues.extend(exc.issues)
                return None

        raw_duration = node.duration
        if raw_duration is None and source is not None:
            raw_duration = source.default_duration
        duration_value = evaluate(raw_duration or 0, "duration")
        try:
            duration_seconds, duration_days = parse_duration_parts(
                duration_value if duration_value is not None else 0,
                f"{node.path}.duration",
            )
        except DslError as exc:
            self.issues.extend(exc.issues)
            duration_seconds, duration_days = 0, None

        label = evaluate(node.label, "label") or (
            source.display_label if source else node.id
        )

        params = {
            p.name: p.default for p in (source.task_para if source else [])
        }
        for key, value in node.task_para.items():
            params[key] = evaluate(value, f"task_para.{key}")

        warn_before = source.warn_before if source else "2H"
        try:
            warn_seconds = parse_duration(
                warn_before, f"{node.path}.warn_before"
            )
        except DslError as exc:
            self.issues.extend(exc.issues)
            warn_seconds = 7200

        owner_spec = self._owner_spec(node, source)
        owner_value, owner_source = self._resolve_static_owner(
            owner_spec, ctx, node
        )

        mode = node.schedule_mode or (
            source.schedule_mode if source else ScheduleMode.CONTINUOUS
        )

        return ExpandedTask(
            id=node.id,
            label=str(label),
            uses=node.uses,
            owner=owner_value,
            owner_source=owner_source,
            group=str(evaluate(node.group, "group") or ""),
            duration_seconds=duration_seconds,
            duration_days=duration_days,
            schedule_mode=mode,
            calendar=resolve_calendar(
                mode, node.calendar or (source.calendar if source else None)
            ),
            params=params,
            task_api=source.task_api if source else "",
            api_mode=source.api_mode if source else None,
            on_failure=node.on_failure
            or (source.on_failure if source else FailurePolicy.BLOCK),
            optional=node.optional,
            phase=node.phase,
            warn_before_seconds=warn_seconds,
            allow_manual_override=(
                source.allow_manual_override if source else True
            ),
            source_index=node.source_index,
        )

    def _owner_spec(
        self, node: FlowNode, source: TaskTemplate | None
    ) -> OwnerSpec | None:
        """Fall back through node -> phase -> task template -> template."""
        if node.owner is not None:
            return node.owner
        phase_default = self.template.phase_defaults.get(node.phase)
        if phase_default is not None:
            return phase_default
        if source is not None and source.default_owner is not None:
            return source.default_owner
        return self.template.default_owner

    def _resolve_static_owner(
        self, spec: OwnerSpec | None, ctx: EvalContext, node: FlowNode
    ) -> tuple[str | None, str]:
        """Resolve everything except ``same_as``, which needs the graph.

        ``group_lead`` needs a database lookup, so it is left unresolved here
        and carried in ``owner_source`` for the service layer to finish.
        """
        if spec is None:
            self.warnings.append(
                warning(
                    "W_UNASSIGNED_OWNER",
                    f"`{node.id}` has no owner and no fallback",
                    node.path,
                )
            )
            return None, "literal"

        match spec.kind:
            case OwnerKind.LITERAL:
                try:
                    value = render(spec.value, ctx, f"{node.path}.owner")
                except DslError as exc:
                    self.issues.extend(exc.issues)
                    return None, "literal"
                return (str(value) if value else None), "literal"
            case OwnerKind.ROLE:
                return ctx.role.get(spec.value), f"role:{spec.value}"
            case OwnerKind.GROUP_LEAD:
                return None, f"group_lead:{spec.value}"
            case OwnerKind.SAME_AS:
                return None, f"same_as:{spec.value}"
        return None, "literal"

    # -- stage 7: owners ---------------------------------------------------

    def _resolve_owners(self) -> None:
        """Resolve ``same_as`` chains, following them to their source."""
        by_id = {task.id: task for task in self.tasks}
        pending = {
            task.id: task.owner_source.removeprefix("same_as:")
            for task in self.tasks
            if task.owner_source.startswith("same_as:")
        }
        if not pending:
            return

        resolved: dict[str, str | None] = {}

        def resolve(task_id: str, seen: tuple[str, ...]) -> str | None:
            if task_id in resolved:
                return resolved[task_id]
            if task_id in seen:
                self._fail(
                    "E_SAME_AS_CYCLE",
                    "owner.same_as forms a cycle: "
                    + " -> ".join([*seen[seen.index(task_id) :], task_id]),
                    task_id,
                )
                return None
            target_node = pending.get(task_id)
            if target_node is None:
                value = by_id[task_id].owner
                resolved[task_id] = value
                return value

            if target_node not in by_id:
                # The target exists in the template but `when` removed it.
                self._fail(
                    "E_UNKNOWN_SAME_AS",
                    f"owner.same_as points at `{target_node}`, which is not "
                    "part of this case",
                    by_id[task_id].id,
                )
                resolved[task_id] = None
                return None
            value = resolve(target_node, (*seen, task_id))
            resolved[task_id] = value
            return value

        for task_id in list(pending):
            by_id[task_id].owner = resolve(task_id, ())

    # -- stage 8: graph validation ----------------------------------------

    def _check_unknown_references(self) -> None:
        known = {node.id for node in self.template.flow}
        duplicates = set()
        seen: set[str] = set()
        for node in self.template.flow:
            if node.id in seen:
                duplicates.add(node.id)
            seen.add(node.id)
            if node.uses and node.uses not in self.task_templates:
                self._fail(
                    "E_UNKNOWN_TASK_TEMPLATE",
                    f"`uses: {node.uses}` refers to an unknown task template",
                    node.path,
                )
            for ref in node.requirement:
                if ref.task not in known:
                    self._fail(
                        "E_UNKNOWN_REQUIREMENT",
                        f"requirement `{ref.task}` is not a task in this "
                        "template",
                        node.path,
                    )
            if node.owner is not None and node.owner.kind is OwnerKind.SAME_AS:
                if node.owner.value not in known:
                    self._fail(
                        "E_UNKNOWN_SAME_AS",
                        f"owner.same_as `{node.owner.value}` is not a task in "
                        "this template",
                        node.path,
                    )
            if node.owner is not None and node.owner.kind is OwnerKind.ROLE:
                if self.template.role(node.owner.value) is None:
                    self._fail(
                        "E_UNKNOWN_ROLE",
                        f"owner.role `{node.owner.value}` is not declared in "
                        "roles",
                        node.path,
                    )
        for node_id in sorted(duplicates):
            self._fail(
                "E_DUP_TASK_NAME",
                f"task id `{node_id}` is defined more than once",
                "flow",
            )
        self._flush()

    def _check_graph(self, edges: list[Edge]) -> None:
        if not self.tasks:
            self._fail(
                "E_ALL_TASKS_SKIPPED",
                "every task was filtered out; the case would be empty",
                "flow",
            )
            return
        ids = [task.id for task in self.tasks]
        try:
            topological_sort(ids, [(p, s) for p, s, _ in edges])
        except CycleError as exc:
            self._fail(
                "E_CYCLE",
                "dependency cycle: " + " -> ".join(exc.cycle),
                "flow",
            )
