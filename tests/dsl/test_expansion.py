"""Build-time expansion pipeline behaviour (implement.md §4.15)."""

from __future__ import annotations

import pytest

from app.dsl.errors import DslError
from app.dsl.expansion import expand
from app.dsl.loader import parse_gantt_template, parse_task_template

from .conftest import ORIGINAL_TEMPLATE, edges_of, ids_of, run


def codes(exc: DslError) -> set[str]:
    return {issue.code for issue in exc.issues}


class TestOriginalExample:
    def test_expands_to_a_linear_chain(self, task_templates):
        template = parse_gantt_template(ORIGINAL_TEMPLATE)
        result = expand(template, task_templates)

        assert ids_of(result) == ["my_task1", "my_task2", "my_task3"]
        assert edges_of(result) == {
            ("my_task1", "my_task2", 0),
            ("my_task2", "my_task3", 0),
        }
        assert all(t.duration_seconds == 12 * 3600 for t in result.tasks)
        assert [t.owner for t in result.tasks] == [
            "my_user_name",
            "my_user_name2",
            "my_user_name3",
        ]


class TestConditionalTasks:
    def test_false_condition_removes_the_node(self, task_templates):
        flow = [
            {"id": "a", "duration": "1H"},
            {
                "id": "b",
                "duration": "1H",
                "when": "{{ para.on }}",
                "requirement": "a",
            },
            {"id": "c", "duration": "1H", "requirement": "b"},
        ]
        params = {"on": False}
        template_para = [
            {"para_name": "on", "para_type": "bool", "para_default": True}
        ]
        result = run(flow, task_templates, params, template_para=template_para)

        assert ids_of(result) == ["a", "c"]
        # a -> b -> c collapses to a -> c rather than leaving c unblocked
        assert edges_of(result) == {("a", "c", 0)}
        assert [s.id for s in result.skipped] == ["b"]

    def test_bypass_sums_lag_on_both_sides(self, task_templates):
        flow = [
            {"id": "a", "duration": "1H"},
            {
                "id": "b",
                "duration": "1H",
                "when": "{{ False }}",
                "requirement": [{"task": "a", "lag": "2H"}],
            },
            {
                "id": "c",
                "duration": "1H",
                "requirement": [{"task": "b", "lag": "3H"}],
            },
        ]
        result = run(flow, task_templates)
        # The wait either side of a skipped step must not be swallowed
        assert edges_of(result) == {("a", "c", 5 * 3600)}

    def test_consecutive_skips_are_bypassed_transitively(self, task_templates):
        flow = [
            {"id": "a", "duration": "1H"},
            {
                "id": "b",
                "duration": "1H",
                "when": "{{ False }}",
                "requirement": "a",
            },
            {
                "id": "c",
                "duration": "1H",
                "when": "{{ False }}",
                "requirement": "b",
            },
            {"id": "d", "duration": "1H", "requirement": "c"},
        ]
        result = run(flow, task_templates)
        assert ids_of(result) == ["a", "d"]
        assert edges_of(result) == {("a", "d", 0)}

    def test_non_boolean_condition_is_rejected(self, task_templates):
        flow = [{"id": "a", "duration": "1H", "when": "{{ 1 + 1 }}"}]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert "E_BAD_WHEN" in codes(exc.value)

    def test_skipping_everything_is_an_error(self, task_templates):
        flow = [{"id": "a", "duration": "1H", "when": "{{ False }}"}]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert "E_ALL_TASKS_SKIPPED" in codes(exc.value)


class TestParallelBranches:
    """Fan-out and fan-in, which is what a real flow uses instead of
    expansion: two independent branches that join at a later task."""

    @pytest.fixture
    def flow(self):
        return [
            {"id": "a", "duration": "1H"},
            {"id": "left", "duration": "2H", "requirement": "a"},
            {"id": "right", "duration": "4H", "requirement": "a"},
            {"id": "z", "duration": "1H", "requirement": ["left", "right"]},
        ]

    def test_diamond(self, flow, task_templates):
        result = run(flow, task_templates)
        assert ids_of(result) == ["a", "left", "right", "z"]
        assert edges_of(result) == {
            ("a", "left", 0),
            ("a", "right", 0),
            ("left", "z", 0),
            ("right", "z", 0),
        }

    def test_skipping_one_branch_rewires_it_away(self, flow, task_templates):
        flow[1] = {**flow[1], "when": "{{ False }}"}
        result = run(flow, task_templates)
        assert ids_of(result) == ["a", "right", "z"]
        # z keeps its dependency on the surviving branch, and picks up a
        # direct edge from a in place of the removed one
        assert edges_of(result) == {
            ("a", "right", 0),
            ("right", "z", 0),
            ("a", "z", 0),
        }


class TestOwnerResolution:
    def test_role_binding(self, task_templates):
        flow = [{"id": "a", "duration": "1H", "owner": {"role": "pm"}}]
        result = run(
            flow,
            task_templates,
            assignments={"pm": "alice"},
            roles=[{"name": "pm"}],
        )
        assert result.tasks[0].owner == "alice"
        assert result.tasks[0].owner_source == "role:pm"

    def test_same_as_copies_the_target(self, task_templates):
        flow = [
            {"id": "a", "duration": "1H", "owner": "alice"},
            {"id": "b", "duration": "1H", "owner": {"same_as": "a"}},
        ]
        result = run(flow, task_templates)
        assert result.task("b").owner == "alice"
        assert result.task("b").owner_source == "same_as:a"

    def test_same_as_chains(self, task_templates):
        flow = [
            {"id": "a", "duration": "1H", "owner": "alice"},
            {"id": "b", "duration": "1H", "owner": {"same_as": "a"}},
            {"id": "c", "duration": "1H", "owner": {"same_as": "b"}},
        ]
        result = run(flow, task_templates)
        assert result.task("c").owner == "alice"

    def test_same_as_cycle_is_rejected(self, task_templates):
        flow = [
            {"id": "a", "duration": "1H", "owner": {"same_as": "b"}},
            {"id": "b", "duration": "1H", "owner": {"same_as": "a"}},
        ]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert "E_SAME_AS_CYCLE" in codes(exc.value)

    def test_group_lead_is_left_for_the_service_layer(self, task_templates):
        flow = [{"id": "a", "duration": "1H", "owner": {"group_lead": "qa"}}]
        result = run(flow, task_templates)
        assert result.tasks[0].owner is None
        assert result.tasks[0].owner_source == "group_lead:qa"

    def test_expression_owner(self, task_templates):
        flow = [{"id": "a", "duration": "1H", "owner": "{{ para.who }}"}]
        template_para = [{"para_name": "who", "para_default": "bob"}]
        result = run(flow, task_templates, template_para=template_para)
        assert result.tasks[0].owner == "bob"

    def test_fallback_order_node_then_phase_then_template(
        self, task_templates
    ):
        parsed = parse_gantt_template(
            {
                "template_name": "t",
                "default_owner": "template_default",
                "flow": [
                    {
                        "phase": "p",
                        "default_owner": "phase_default",
                        "tasks": [
                            {"id": "a", "duration": "1H", "owner": "own"},
                            {"id": "b", "duration": "1H"},
                        ],
                    },
                    {
                        "phase": "q",
                        "tasks": [{"id": "c", "duration": "1H"}],
                    },
                ],
            }
        )
        result = expand(parsed, task_templates)
        assert result.task("a").owner == "own"
        assert result.task("b").owner == "phase_default"
        assert result.task("c").owner == "template_default"

    def test_missing_owner_produces_a_warning(self, task_templates):
        flow = [{"id": "a", "duration": "1H"}]
        result = run(flow, task_templates)
        assert result.tasks[0].owner is None
        assert "W_UNASSIGNED_OWNER" in {w.code for w in result.warnings}


class TestReferences:
    def test_unknown_requirement(self, task_templates):
        flow = [{"id": "a", "duration": "1H", "requirement": "nope"}]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert "E_UNKNOWN_REQUIREMENT" in codes(exc.value)

    def test_unknown_task_template(self, task_templates):
        flow = [{"id": "a", "duration": "1H", "uses": "nope"}]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert "E_UNKNOWN_TASK_TEMPLATE" in codes(exc.value)

    def test_duplicate_task_id(self, task_templates):
        flow = [{"id": "a", "duration": "1H"}, {"id": "a", "duration": "1H"}]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert "E_DUP_TASK_NAME" in codes(exc.value)

    def test_unknown_role(self, task_templates):
        flow = [{"id": "a", "duration": "1H", "owner": {"role": "nope"}}]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert "E_UNKNOWN_ROLE" in codes(exc.value)

    def test_dependency_cycle(self, task_templates):
        flow = [
            {"id": "a", "duration": "1H", "requirement": "c"},
            {"id": "b", "duration": "1H", "requirement": "a"},
            {"id": "c", "duration": "1H", "requirement": "b"},
        ]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert "E_CYCLE" in codes(exc.value)

    def test_negative_lag(self, task_templates):
        flow = [
            {"id": "a", "duration": "1H"},
            {
                "id": "b",
                "duration": "1H",
                "requirement": [{"task": "a", "lag": -60}],
            },
        ]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert codes(exc.value) & {"E_NEGATIVE_LAG", "E_BAD_DURATION"}


class TestTaskTemplateMerge:
    def test_node_overrides_template_defaults(self, task_templates):
        flow = [{"id": "a", "uses": "tt1"}]
        result = run(flow, task_templates)
        # duration falls back to the task template's 10H
        assert result.tasks[0].duration_seconds == 10 * 3600

        flow = [{"id": "a", "uses": "tt1", "duration": "2H"}]
        result = run(flow, task_templates)
        assert result.tasks[0].duration_seconds == 2 * 3600

    def test_duration_expression_uses_parameters(self, task_templates):
        flow = [{"id": "a", "duration": "{{ para.n * 12 }}H"}]
        template_para = [
            {"para_name": "n", "para_type": "int", "para_default": 1}
        ]
        result = run(
            flow, task_templates, {"n": 3}, template_para=template_para
        )
        assert result.tasks[0].duration_seconds == 36 * 3600


class TestDeterminism:
    def test_same_input_yields_the_same_graph(self, task_templates):
        flow = [
            {"id": "a", "duration": "1H"},
            {"id": "b", "duration": "1H", "requirement": "a"},
            {"id": "c", "duration": "1H", "requirement": "a"},
            {"id": "z", "duration": "1H", "requirement": ["b", "c"]},
        ]
        first = run(flow, task_templates)
        second = run(flow, task_templates)
        assert ids_of(first) == ids_of(second)
        assert [
            (e.predecessor, e.successor, e.lag_seconds) for e in first.edges
        ] == [
            (e.predecessor, e.successor, e.lag_seconds) for e in second.edges
        ]


class TestCalendarResolution:
    """`schedule_mode` and `calendar` express one intent (implement.md §4.5).

    They were two independent fields with contradictory defaults, so a task
    asking for business hours and not naming a calendar got `continuous` --
    scheduled around the clock, silently.
    """

    def test_business_without_a_calendar_gets_the_office_one(
        self, task_templates
    ):
        result = run(
            [{"id": "a", "duration": "1H", "schedule_mode": "business"}],
            task_templates,
        )
        assert result.tasks[0].calendar == "taiwan_office"

    def test_continuous_stays_continuous(self, task_templates):
        result = run([{"id": "a", "duration": "1H"}], task_templates)
        assert result.tasks[0].calendar == "continuous"

    def test_a_named_calendar_always_wins(self, task_templates):
        result = run(
            [
                {
                    "id": "a",
                    "duration": "1H",
                    "schedule_mode": "business",
                    "calendar": "berlin_office",
                }
            ],
            task_templates,
        )
        assert result.tasks[0].calendar == "berlin_office"

    def test_the_task_template_can_carry_the_mode(self):
        # The seeded `tt_review` does exactly this, which is how the demo
        # case ended up with four tasks planned around the clock
        templates = {
            "tt": parse_task_template(
                {
                    "id": "tt",
                    "default_duration": "1H",
                    "schedule_mode": "business",
                }
            )
        }
        result = run([{"id": "a", "uses": "tt"}], templates)
        assert result.tasks[0].calendar == "taiwan_office"

    def test_a_node_may_override_back_to_continuous(self, task_templates):
        templates = dict(task_templates)
        templates["tt1"] = parse_task_template(
            {
                "id": "tt1",
                "default_duration": "1H",
                "schedule_mode": "business",
            }
        )
        result = run(
            [{"id": "a", "uses": "tt1", "schedule_mode": "continuous"}],
            templates,
        )
        assert result.tasks[0].calendar == "continuous"
