"""Shared fixtures for DSL tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.dsl.expansion import expand
from app.dsl.loader import parse_gantt_template, parse_task_template
from app.dsl.schema import ExpansionResult

#: The YAML from the original design conversation, byte for byte. Every change
#: to the DSL must keep this parsing, so it is used as a regression guard.
ORIGINAL_TEMPLATE = """
gantt:
  template_name: my_template_name
  template_para:
    - para_name: my_para1
      para_type: int
      para_default: 1
    - para_name: my_para2
      para_type: str
      para_default: test
  flow:
    - task_template: tt1
      task_name: my_task1
      task_owner: my_user_name
      task_group: my_group_name
      requirement: none
      target_duration: 12H
    - task_template: tt2
      task_name: my_task2
      task_owner: my_user_name2
      task_group: my_group_name2
      target_duration: 12H
      requirement: my_task1
    - task_template: tt3
      task_name: my_task3
      task_owner: my_user_name3
      task_group: my_group_name3
      target_duration: 12H
      requirement: my_task2
"""

ORIGINAL_TASK_TEMPLATE = """
task:
  task_name: bt1
  task_duration_default: 10H
  task_para:
    - para_name: my_para1
    - para_name: my_para2
  task_api: my_function
"""


@pytest.fixture
def task_templates():
    """Task templates covering every id the flow fixtures refer to."""
    ids = ("tt1", "tt2", "tt3", "tt4", "tt5", "tt6", "tt7")
    return {
        name: parse_task_template({"id": name, "default_duration": "10H"})
        for name in ids
    }


def build(flow: list[dict[str, Any]], **template: Any) -> dict[str, Any]:
    """Assemble a minimal gantt template around a flow."""
    return {"template_name": "t", "flow": flow, **template}


def run(
    flow: list[dict[str, Any]],
    task_templates: dict[str, Any],
    params: dict[str, Any] | None = None,
    assignments: dict[str, str] | None = None,
    **template: Any,
) -> ExpansionResult:
    """Parse and expand in one step; the common shape of most tests.

    ``assignments`` is the role -> username binding supplied at case creation.
    Template-level fields (including the ``roles`` declaration itself) go in
    ``**template``, which is why the binding does not reuse that name.
    """
    parsed = parse_gantt_template(build(flow, **template))
    return expand(parsed, task_templates, params=params, roles=assignments)


def edges_of(result: ExpansionResult) -> set[tuple[str, str, int]]:
    return {(e.predecessor, e.successor, e.lag_seconds) for e in result.edges}


def ids_of(result: ExpansionResult) -> list[str]:
    return [t.id for t in result.tasks]
