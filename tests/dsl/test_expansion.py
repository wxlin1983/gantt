"""Build-time expansion pipeline behaviour (implement.md §4.15)."""

from __future__ import annotations

import pytest

from app.dsl.errors import DslError
from app.dsl.expansion import expand
from app.dsl.loader import parse_gantt_template

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


class TestForEach:
    @pytest.fixture
    def flow(self):
        return [
            {"id": "a", "duration": "1H"},
            {
                "id": "batch",
                "duration": "1H",
                "for_each": "{{ range(para.n) }}",
                "label": "batch {{ index + 1 }}",
                "requirement": "a",
            },
            {"id": "z", "duration": "1H", "requirement": "batch"},
        ]

    @pytest.fixture
    def template_para(self):
        return [{"para_name": "n", "para_type": "int", "para_default": 1}]

    def test_expands_into_parallel_instances(
        self, flow, template_para, task_templates
    ):
        result = run(
            flow, task_templates, {"n": 3}, template_para=template_para
        )
        assert ids_of(result) == ["a", "batch_0", "batch_1", "batch_2", "z"]
        assert [t.label for t in result.tasks if t.for_each_group] == [
            "batch 1",
            "batch 2",
            "batch 3",
        ]
        # Fan out from a, fan in to z: depending on the group means all of it
        assert edges_of(result) == {
            ("a", "batch_0", 0),
            ("a", "batch_1", 0),
            ("a", "batch_2", 0),
            ("batch_0", "z", 0),
            ("batch_1", "z", 0),
            ("batch_2", "z", 0),
        }

    def test_single_instance(self, flow, template_para, task_templates):
        result = run(
            flow, task_templates, {"n": 1}, template_para=template_para
        )
        assert ids_of(result) == ["a", "batch_0", "z"]

    def test_zero_instances_behaves_like_a_skipped_node(
        self, flow, template_para, task_templates
    ):
        result = run(
            flow, task_templates, {"n": 0}, template_para=template_para
        )
        assert ids_of(result) == ["a", "z"]
        assert edges_of(result) == {("a", "z", 0)}
        assert [s.id for s in result.skipped] == ["batch"]

    def test_sequential_chains_instances(self, template_para, task_templates):
        flow = [
            {
                "id": "batch",
                "duration": "1H",
                "for_each": "{{ range(para.n) }}",
                "sequential": True,
            }
        ]
        result = run(
            flow, task_templates, {"n": 3}, template_para=template_para
        )
        assert edges_of(result) == {
            ("batch_0", "batch_1", 0),
            ("batch_1", "batch_2", 0),
        }

    def test_custom_id_suffix(self, task_templates):
        flow = [
            {
                "id": "check",
                "duration": "1H",
                "for_each": "{{ ['x', 'y'] }}",
                "id_suffix": "{{ item }}",
            }
        ]
        result = run(flow, task_templates)
        assert ids_of(result) == ["check_x", "check_y"]

    def test_colliding_suffix_is_rejected(self, task_templates):
        flow = [
            {
                "id": "check",
                "duration": "1H",
                "for_each": "{{ range(2) }}",
                "id_suffix": "same",
            }
        ]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates)
        assert "E_DUP_EXPANDED_ID" in codes(exc.value)

    def test_non_sequence_is_rejected(self, task_templates):
        flow = [{"id": "a", "duration": "1H", "for_each": "{{ para.n }}"}]
        template_para = [
            {"para_name": "n", "para_type": "int", "para_default": 3}
        ]
        with pytest.raises(DslError) as exc:
            run(flow, task_templates, template_para=template_para)
        assert "E_BAD_FOR_EACH" in codes(exc.value)

    def test_when_is_evaluated_before_for_each(
        self, template_para, task_templates
    ):
        flow = [
            {
                "id": "batch",
                "duration": "1H",
                "when": "{{ False }}",
                "for_each": "{{ range(para.n) }}",
            },
            {"id": "z", "duration": "1H"},
        ]
        result = run(
            flow, task_templates, {"n": 5}, template_para=template_para
        )
        assert ids_of(result) == ["z"]


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
            {
                "id": "batch",
                "duration": "1H",
                "for_each": "{{ range(4) }}",
                "requirement": "a",
            },
            {"id": "z", "duration": "1H", "requirement": "batch"},
        ]
        first = run(flow, task_templates)
        second = run(flow, task_templates)
        assert ids_of(first) == ids_of(second)
        assert [
            (e.predecessor, e.successor, e.lag_seconds) for e in first.edges
        ] == [
            (e.predecessor, e.successor, e.lag_seconds) for e in second.edges
        ]
