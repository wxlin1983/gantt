"""Template management and the health report (§8.6, §8.7, §9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml

from app.dsl.errors import DslError
from app.models import TaskStatus, TemplateStatus
from app.services import cases, schedules
from app.services import templates as template_service
from app.services.templates import TemplateError

TARGET = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)

MINIMAL = {
    "template_name": "simple",
    "flow": [
        {"id": "a", "uses": "tt_plan", "duration": "2H"},
        {"id": "b", "uses": "tt_plan", "duration": "3H", "requirement": "a"},
    ],
}


def codes(exc) -> set[str]:
    return {issue.code for issue in exc.value.issues}


class TestValidate:
    async def test_valid_template_passes(self, session, seeded):
        result = await template_service.validate(session, MINIMAL)
        assert result.ok

    async def test_unknown_task_template_is_an_error(self, session, seeded):
        broken = {
            **MINIMAL,
            "flow": [{"id": "a", "uses": "nope", "duration": "1H"}],
        }
        result = await template_service.validate(session, broken)
        assert not result.ok
        assert "E_UNKNOWN_TASK_TEMPLATE" in {i.code for i in result.errors}

    async def test_unknown_requirement_is_an_error(self, session, seeded):
        broken = {
            **MINIMAL,
            "flow": [{"id": "a", "uses": "tt_plan", "requirement": "ghost"}],
        }
        result = await template_service.validate(session, broken)
        assert "E_UNKNOWN_REQUIREMENT" in {i.code for i in result.errors}

    async def test_cycle_is_an_error(self, session, seeded):
        broken = {
            **MINIMAL,
            "flow": [
                {"id": "a", "uses": "tt_plan", "requirement": "b"},
                {"id": "b", "uses": "tt_plan", "requirement": "a"},
            ],
        }
        result = await template_service.validate(session, broken)
        assert "E_CYCLE" in {i.code for i in result.errors}

    async def test_undeclared_role_is_an_error(self, session, seeded):
        broken = {
            **MINIMAL,
            "flow": [
                {
                    "id": "a",
                    "uses": "tt_plan",
                    "owner": {"role": "nobody"},
                }
            ],
        }
        result = await template_service.validate(session, broken)
        assert "E_UNKNOWN_ROLE" in {i.code for i in result.errors}

    async def test_unused_parameter_is_a_warning(self, session, seeded):
        definition = {
            **MINIMAL,
            "template_para": [
                {"para_name": "spare", "para_type": "int", "para_default": 1}
            ],
        }
        result = await template_service.validate(session, definition)
        assert result.ok
        assert "W_UNUSED_PARAM" in {i.code for i in result.warnings}

    async def test_conditional_template_warns_to_run_a_trial(
        self, session, seeded
    ):
        # A template whose shape depends on parameters cannot be fully checked
        # statically, so the editor is told to push for a trial run (§4.7).
        definition = {
            **MINIMAL,
            "template_para": [
                {
                    "para_name": "opt",
                    "para_type": "bool",
                    "para_default": True,
                }
            ],
            "flow": [
                {"id": "a", "uses": "tt_plan", "duration": "1H"},
                {
                    "id": "b",
                    "uses": "tt_plan",
                    "duration": "1H",
                    "when": "{{ para.opt }}",
                    "requirement": "a",
                },
            ],
        }
        result = await template_service.validate(session, definition)
        assert "W_SHAPE_DEPENDS_ON_PARAMS" in {i.code for i in result.warnings}

    async def test_multiple_sinks_warns(self, session, seeded):
        definition = {
            **MINIMAL,
            "flow": [
                {"id": "a", "uses": "tt_plan", "duration": "1H"},
                {"id": "b", "uses": "tt_plan", "duration": "1H"},
            ],
        }
        result = await template_service.validate(session, definition)
        assert "W_MULTIPLE_SINKS" in {i.code for i in result.warnings}


class TestDrafts:
    async def test_save_creates_a_draft(self, session, seeded):
        draft = await template_service.save_draft(
            session, MINIMAL, seeded["admin"].id
        )
        assert draft.status is TemplateStatus.DRAFT
        assert draft.version == 1

    async def test_saving_again_overwrites_the_same_draft(
        self, session, seeded
    ):
        first = await template_service.save_draft(session, MINIMAL)
        second = await template_service.save_draft(
            session, {**MINIMAL, "description": "changed"}
        )
        assert first.id == second.id
        assert second.definition["description"] == "changed"

    async def test_invalid_draft_is_refused(self, session, seeded):
        # Storing an unparseable draft would break the editor's next load.
        with pytest.raises(DslError):
            await template_service.save_draft(
                session, {**MINIMAL, "flow": [{"id": "a", "uses": "nope"}]}
            )

    async def test_draft_version_follows_the_published_ones(
        self, session, seeded
    ):
        draft = await template_service.save_draft(
            session, {**MINIMAL, "template_name": "launch"}
        )
        # launch v1 is already published in the fixture
        assert draft.version == 2

    async def test_discard(self, session, seeded):
        await template_service.save_draft(session, MINIMAL)
        await template_service.discard_draft(session, "simple")
        assert await template_service.get_draft(session, "simple") is None

    async def test_discarding_nothing_is_an_error(self, session, seeded):
        with pytest.raises(TemplateError):
            await template_service.discard_draft(session, "simple")


class TestPublish:
    async def test_publish_makes_it_usable(self, session, seeded):
        await template_service.save_draft(session, MINIMAL)
        published = await template_service.publish(session, "simple")
        assert published.status is TemplateStatus.PUBLISHED
        assert published.published_at is not None
        # The version is stamped into the definition for the snapshot
        assert published.definition["version"] == published.version

        case = await cases.create(
            session,
            seeded["admin"],
            name="from simple",
            template_name="simple",
            target_date=TARGET,
        )
        assert {task.name for task in case.tasks} == {"a", "b"}

    async def test_publishing_without_a_draft_is_an_error(
        self, session, seeded
    ):
        with pytest.raises(TemplateError):
            await template_service.publish(session, "simple")

    async def test_publishing_leaves_running_cases_alone(
        self, session, seeded
    ):
        """A new version must not touch a case already in flight (§4.8)."""
        case = await cases.create(
            session,
            seeded["admin"],
            name="in flight",
            template_name="launch",
            target_date=TARGET,
            params={"test_hours": 16, "needs_review": True},
            role_assignments={"owner": "pm", "tester": "qa"},
        )
        before = {task.name: task.duration_seconds for task in case.tasks}

        changed = {
            **seeded["template"].definition,
            "flow": [
                {
                    "phase": "Prepare",
                    "tasks": [
                        {
                            "id": "plan",
                            "uses": "tt_plan",
                            "duration": "99H",
                            "requirement": "none",
                            "owner": {"role": "owner"},
                        }
                    ],
                }
            ],
        }
        await template_service.save_draft(session, changed)
        await template_service.publish(session, "launch")

        after = {task.name: task.duration_seconds for task in case.tasks}
        assert after == before
        assert case.template_version == 1


class TestDiff:
    def test_reports_added_removed_and_changed(self):
        left = {
            "flow": [
                {"id": "a", "duration": "2H"},
                {"id": "gone", "duration": "1H"},
            ]
        }
        right = {
            "flow": [
                {"id": "a", "duration": "6H"},
                {"id": "new", "duration": "1H"},
            ]
        }
        result = template_service.diff(left, right)
        assert result["added"] == ["new"]
        assert result["removed"] == ["gone"]
        assert result["changed"][0]["id"] == "a"
        assert result["changed"][0]["fields"]["duration"] == ("2H", "6H")

    def test_flattens_phase_sections(self):
        left = {
            "flow": [{"phase": "p", "tasks": [{"id": "a", "duration": "1H"}]}]
        }
        right = {"flow": [{"id": "a", "duration": "2H"}]}
        result = template_service.diff(left, right)
        assert result["changed"][0]["id"] == "a"

    def test_buffer_change_is_reported(self):
        result = template_service.diff({"buffer": "4H"}, {"buffer": "8H"})
        assert result["buffer"] == ("4H", "8H")


class TestExportImport:
    async def test_export_is_self_contained_yaml(self, session, seeded):
        document = await template_service.export(session, "launch")
        parsed = yaml.safe_load(document)
        assert parsed["gantt"]["template_name"] == "launch"
        assert {entry["id"] for entry in parsed["task_templates"]} == {
            "tt_plan",
            "tt_test",
            "tt_report",
        }

    async def test_export_never_contains_a_secret(self, session, seeded):
        from app.services import credentials

        await credentials.put(session, "ci_token", "super-secret-value")
        record = seeded["template"]
        record.definition = {**record.definition}
        await session.flush()

        document = await template_service.export(session, "launch")
        assert "super-secret-value" not in document

    async def test_import_lands_as_a_draft(self, session, seeded):
        document = yaml.safe_dump({"gantt": MINIMAL})
        report = await template_service.import_document(
            session, document, seeded["admin"].id
        )
        assert report.template_name == "simple"
        draft = await template_service.get_draft(session, "simple")
        # Never a direct overwrite of a published version
        assert draft is not None
        assert draft.status is TemplateStatus.DRAFT

    async def test_import_creates_missing_task_templates(
        self, session, seeded
    ):
        document = yaml.safe_dump(
            {
                "gantt": {
                    "template_name": "imported",
                    "flow": [{"id": "x", "uses": "tt_new", "duration": "1H"}],
                },
                "task_templates": [{"id": "tt_new", "default_duration": "4H"}],
            }
        )
        report = await template_service.import_document(session, document)
        assert report.task_templates_created == ["tt_new"]

    async def test_import_reports_differing_task_templates(
        self, session, seeded
    ):
        document = yaml.safe_dump(
            {
                "gantt": MINIMAL,
                "task_templates": [
                    {"id": "tt_plan", "default_duration": "99H"}
                ],
            }
        )
        report = await template_service.import_document(session, document)
        # Reported, not silently overwritten: other flows share this template
        assert report.task_templates_differing == ["tt_plan"]

    async def test_import_reports_missing_credentials(self, session, seeded):
        document = yaml.safe_dump(
            {
                "gantt": {
                    "template_name": "needs_cred",
                    "flow": [{"id": "x", "uses": "tt_api", "duration": "1H"}],
                },
                "task_templates": [
                    {
                        "id": "tt_api",
                        "default_duration": "1H",
                        "task_api": "http_request",
                        "api_config": {"auth_ref": "absent_token"},
                    }
                ],
            }
        )
        report = await template_service.import_document(session, document)
        # A warning, not a block: move the flow first, configure after
        assert report.missing_credentials == ["absent_token"]

    async def test_bad_yaml_is_refused(self, session, seeded):
        with pytest.raises(TemplateError):
            await template_service.import_document(session, "gantt: [oops")

    async def test_failed_import_leaves_nothing_behind(self, session, seeded):
        document = yaml.safe_dump(
            {
                "gantt": {
                    "template_name": "bad",
                    "flow": [{"id": "a", "uses": "ghost"}],
                }
            }
        )
        with pytest.raises(DslError):
            await template_service.import_document(session, document)
        assert await template_service.get_draft(session, "bad") is None


class TestHealthReport:
    async def test_compares_planned_against_actual(self, session, seeded):
        case = await cases.create(
            session,
            seeded["admin"],
            name="health case",
            template_name="launch",
            target_date=TARGET,
            params={"test_hours": 16, "needs_review": True},
            role_assignments={"owner": "pm", "tester": "qa"},
        )
        plan = next(t for t in case.tasks if t.name == "plan")
        await cases.complete_task(session, seeded["pm"], case, plan)
        # 18 real hours against a planned 12
        plan.actual_start = cases.now_utc() - timedelta(hours=18)
        plan.status = TaskStatus.DONE
        await session.flush()

        report = await template_service.health_report(session, "launch")
        entry = next(
            item for item in report["tasks"] if item["task_id"] == "plan"
        )
        assert entry["planned_duration_seconds"] == 12 * 3600
        assert entry["actual_median_seconds"] > 12 * 3600
        assert entry["overrun_ratio"] == 1.0
        assert entry["sample_size"] == 1

    async def test_unplanned_tasks_are_excluded(self, session, seeded):
        case = await cases.create(
            session,
            seeded["admin"],
            name="health case",
            template_name="launch",
            target_date=TARGET,
            params={"test_hours": 16, "needs_review": True},
            role_assignments={"owner": "pm", "tester": "qa"},
        )
        plan = next(t for t in case.tasks if t.name == "plan")
        await cases.complete_task(session, seeded["pm"], case, plan)
        # No baseline means there is no plan to be measured against
        plan.baseline_start = None
        await session.flush()

        report = await template_service.health_report(session, "launch")
        assert all(item["task_id"] != "plan" for item in report["tasks"])

    async def test_empty_history_is_not_an_error(self, session, seeded):
        report = await template_service.health_report(session, "launch")
        assert report["case_count"] == 0
        assert report["on_time_ratio"] is None
        assert report["tasks"] == []


class TestSchedules:
    @pytest.mark.parametrize(
        "expression,moment,expected",
        [
            ("0 9 5 * *", datetime(2026, 8, 5, 9, 0), True),
            ("0 9 5 * *", datetime(2026, 8, 5, 10, 0), False),
            ("0 9 5 * *", datetime(2026, 8, 6, 9, 0), False),
            ("*/15 * * * *", datetime(2026, 8, 5, 9, 30), True),
            ("*/15 * * * *", datetime(2026, 8, 5, 9, 31), False),
            ("0 9 * * 1", datetime(2026, 8, 3, 9, 0), True),
            ("0 9 * * 1", datetime(2026, 8, 4, 9, 0), False),
            ("0 9-17 * * *", datetime(2026, 8, 5, 12, 0), True),
            ("0 9,17 * * *", datetime(2026, 8, 5, 17, 0), True),
        ],
    )
    def test_cron_matching(self, expression, moment, expected):
        assert (
            schedules.matches(expression, moment.replace(tzinfo=UTC))
            is expected
        )

    @pytest.mark.parametrize(
        "expression",
        ["0 9 5 *", "bad", "99 * * * *", "0 9 5 * * *", "*/0 * * * *"],
    )
    def test_invalid_cron_is_refused(self, expression):
        with pytest.raises(schedules.CronError):
            schedules.parse_cron(expression)

    def test_next_run_is_strictly_after(self):
        base = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
        nxt = schedules.next_run_after("0 9 5 * *", base, "UTC")
        assert nxt > base
        assert nxt.month == 9

    async def test_upsert_validates_and_computes_next_run(
        self, session, seeded
    ):
        row = await schedules.upsert(
            session,
            "launch",
            cron="0 9 5 * *",
            timezone="UTC",
            target_date_offset_s=3 * 86400,
            created_by_id=seeded["admin"].id,
        )
        assert row.next_run_at > cases.now_utc()

    async def test_bad_cron_is_refused_at_configuration_time(
        self, session, seeded
    ):
        with pytest.raises(schedules.CronError):
            await schedules.upsert(session, "launch", cron="nonsense")

    async def test_due_schedule_creates_a_case(self, session, seeded):
        row = await schedules.upsert(
            session,
            "launch",
            cron="*/1 * * * *",
            timezone="UTC",
            params={"test_hours": 16, "needs_review": True},
            role_assignments={"owner": "pm", "tester": "qa"},
            created_by_id=seeded["admin"].id,
        )
        row.next_run_at = cases.now_utc() - timedelta(minutes=1)
        await session.flush()

        assert await schedules.run_due(session) == 1
        assert row.last_case_id is not None
        assert row.next_run_at > cases.now_utc()

    async def test_failure_disables_the_schedule(self, session, seeded):
        # Missing the required role makes creation fail; failing every minute
        # would be worse than stopping (§4.16).
        row = await schedules.upsert(
            session,
            "launch",
            cron="*/1 * * * *",
            timezone="UTC",
            role_assignments={},
            created_by_id=seeded["admin"].id,
        )
        row.next_run_at = cases.now_utc() - timedelta(minutes=1)
        await session.flush()

        assert await schedules.run_due(session) == 0
        assert row.enabled is False

    async def test_name_template_renders_dates(self, session, seeded):
        row = await schedules.upsert(
            session,
            "launch",
            cron="0 9 5 * *",
            timezone="UTC",
            name_template="{{ now.year }}-{{ now.month }} close",
        )
        rendered = schedules.render_name(
            row, datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
        )
        assert rendered == "2026-8 close"
