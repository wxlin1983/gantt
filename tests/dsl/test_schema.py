"""Parsing, legacy aliases and structural validation."""

from __future__ import annotations

import pytest

from app.dsl.errors import DslError
from app.dsl.loader import parse_gantt_template, parse_task_template
from app.dsl.schema import ApiMode, OwnerKind

from .conftest import ORIGINAL_TASK_TEMPLATE, ORIGINAL_TEMPLATE


def codes(exc: DslError) -> set[str]:
    return {issue.code for issue in exc.issues}


class TestOriginalExample:
    """The DSL must never stop accepting the shape it started with."""

    def test_gantt_template_parses(self):
        template = parse_gantt_template(ORIGINAL_TEMPLATE)
        assert template.template_name == "my_template_name"
        assert [n.id for n in template.flow] == [
            "my_task1",
            "my_task2",
            "my_task3",
        ]
        assert template.flow[0].requirement == []
        assert template.flow[1].requirement[0].task == "my_task1"
        assert template.flow[0].duration == "12H"
        assert str(template.flow[0].owner) == "my_user_name"

    def test_task_template_parses(self):
        task = parse_task_template(ORIGINAL_TASK_TEMPLATE)
        assert task.id == "bt1"
        assert task.default_duration == "10H"
        assert [p.name for p in task.task_para] == ["my_para1", "my_para2"]
        assert task.task_api == "my_function"
        # api_mode is unset in the source; the documented default applies
        assert task.api_mode is ApiMode.TRIGGER_POLL


class TestAliases:
    def test_canonical_and_legacy_produce_identical_nodes(self):
        legacy = parse_gantt_template(
            {
                "template_name": "t",
                "flow": [
                    {
                        "task_name": "a",
                        "task_template": "tt1",
                        "display_name": "A",
                        "target_duration": "3H",
                        "task_owner": "u",
                        "task_group": "g",
                    }
                ],
            }
        )
        canonical = parse_gantt_template(
            {
                "template_name": "t",
                "flow": [
                    {
                        "id": "a",
                        "uses": "tt1",
                        "label": "A",
                        "duration": "3H",
                        "owner": "u",
                        "group": "g",
                    }
                ],
            }
        )
        assert legacy.flow[0] == canonical.flow[0]

    def test_setting_both_names_is_rejected(self):
        with pytest.raises(DslError) as exc:
            parse_gantt_template(
                {
                    "template_name": "t",
                    "flow": [{"id": "a", "task_name": "a"}],
                }
            )
        assert "E_ALIAS_CONFLICT" in codes(exc.value)


class TestStructuralValidation:
    def test_unknown_field_is_rejected(self):
        # A typo must be loud: silently ignoring it would produce a schedule
        # the author never intended.
        with pytest.raises(DslError) as exc:
            parse_gantt_template(
                {
                    "template_name": "t",
                    "flow": [{"id": "a", "requirment": "b"}],
                }
            )
        assert "E_UNKNOWN_FIELD" in codes(exc.value)

    def test_missing_template_name(self):
        with pytest.raises(DslError) as exc:
            parse_gantt_template({"flow": []})
        assert "E_MISSING_FIELD" in codes(exc.value)

    def test_mixed_flow_forms_rejected(self):
        with pytest.raises(DslError) as exc:
            parse_gantt_template(
                {
                    "template_name": "t",
                    "flow": [
                        {"phase": "p", "tasks": [{"id": "a"}]},
                        {"id": "b"},
                    ],
                }
            )
        assert "E_MIXED_FLOW_FORM" in codes(exc.value)

    def test_invalid_yaml(self):
        with pytest.raises(DslError) as exc:
            parse_gantt_template("gantt: [unclosed")
        assert "E_BAD_YAML" in codes(exc.value)

    def test_enum_param_requires_choices(self):
        with pytest.raises(DslError) as exc:
            parse_gantt_template(
                {
                    "template_name": "t",
                    "template_para": [{"para_name": "x", "para_type": "enum"}],
                    "flow": [{"id": "a"}],
                }
            )
        assert "E_MISSING_FIELD" in codes(exc.value)

    def test_sequential_requires_for_each(self):
        with pytest.raises(DslError) as exc:
            parse_gantt_template(
                {
                    "template_name": "t",
                    "flow": [{"id": "a", "sequential": True}],
                }
            )
        assert "E_MISSING_FIELD" in codes(exc.value)


class TestRequirementShapes:
    @pytest.mark.parametrize(
        "written,expected",
        [
            (None, []),
            ("none", []),
            ([], []),
            ("a", [("a", 0)]),
            (["a", "b"], [("a", 0), ("b", 0)]),
            ({"task": "a", "lag": "4H"}, [("a", "4H")]),
            (
                ["a", {"task": "b", "lag": "30M"}],
                [("a", 0), ("b", "30M")],
            ),
        ],
    )
    def test_every_documented_shape(self, written, expected):
        template = parse_gantt_template(
            {
                "template_name": "t",
                "flow": [
                    {"id": "a"},
                    {"id": "b"},
                    {"id": "c", "requirement": written},
                ],
            }
        )
        node = template.node("c")
        assert [(r.task, r.lag) for r in node.requirement] == expected


class TestOwnerShapes:
    @pytest.mark.parametrize(
        "written,kind,value",
        [
            ("alice", OwnerKind.LITERAL, "alice"),
            ({"role": "pm"}, OwnerKind.ROLE, "pm"),
            ({"group_lead": "qa"}, OwnerKind.GROUP_LEAD, "qa"),
            ({"same_as": "a"}, OwnerKind.SAME_AS, "a"),
        ],
    )
    def test_every_documented_shape(self, written, kind, value):
        template = parse_gantt_template(
            {
                "template_name": "t",
                "roles": [{"name": "pm"}],
                "flow": [{"id": "a"}, {"id": "b", "owner": written}],
            }
        )
        owner = template.node("b").owner
        assert owner.kind is kind
        assert owner.value == value

    def test_multi_key_owner_rejected(self):
        with pytest.raises(DslError) as exc:
            parse_gantt_template(
                {
                    "template_name": "t",
                    "flow": [
                        {"id": "a", "owner": {"role": "pm", "same_as": "b"}}
                    ],
                }
            )
        assert "E_MISSING_FIELD" in codes(exc.value)


class TestPhases:
    def test_phases_flatten_and_keep_labels(self):
        template = parse_gantt_template(
            {
                "template_name": "t",
                "flow": [
                    {
                        "phase": "prep",
                        "default_owner": {"role": "pm"},
                        "tasks": [{"id": "a"}],
                    },
                    {"phase": "test", "tasks": [{"id": "b"}, {"id": "c"}]},
                ],
                "roles": [{"name": "pm"}],
            }
        )
        assert [n.id for n in template.flow] == ["a", "b", "c"]
        assert [n.phase for n in template.flow] == ["prep", "test", "test"]
        assert [n.source_index for n in template.flow] == [0, 1, 2]
        assert template.phase_defaults["prep"].value == "pm"
