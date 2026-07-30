"""The end-to-end flow the phase 3 acceptance criterion asks for.

Create a case from a template, look at it, edit a task, complete tasks, watch
the forecast move -- all over HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

TARGET = (datetime.now(tz=UTC) + timedelta(days=30)).replace(microsecond=0)

CREATE = {
    "name": "Launch A",
    "template_name": "launch",
    "target_date": TARGET.isoformat(),
    "params": {"test_hours": 16, "needs_review": True},
    "role_assignments": {"owner": "pm", "tester": "qa"},
}


def task_of(case: dict, name: str) -> dict:
    return next(task for task in case["tasks"] if task["name"] == name)


class TestAuth:
    async def test_login_returns_identity_and_groups(self, client, seeded):
        response = await client.post(
            "/auth/login", json={"username": "qa", "password": "qa-pw"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["user"]["username"] == "qa"
        assert body["groups"] == ["quality"]
        assert body["lead_of"] == ["quality"]

    async def test_wrong_password_is_rejected(self, client, seeded):
        response = await client.post(
            "/auth/login", json={"username": "qa", "password": "nope"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "E_BAD_CREDENTIALS"

    async def test_unknown_user_looks_the_same_as_a_bad_password(
        self, client, seeded
    ):
        response = await client.post(
            "/auth/login", json={"username": "ghost", "password": "nope"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "E_BAD_CREDENTIALS"

    async def test_anonymous_access_is_refused(self, client, seeded):
        response = await client.get("/cases")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "E_UNAUTHENTICATED"

    async def test_logout_clears_the_session(self, as_pm):
        assert (await as_pm.post("/auth/logout")).status_code == 204
        assert (await as_pm.get("/cases")).status_code == 401


class TestPreview:
    async def test_preview_creates_nothing(self, as_pm):
        response = await as_pm.post(
            "/cases/preview",
            json={
                "template_name": "launch",
                "target_date": TARGET.isoformat(),
                "params": {"test_hours": 16, "needs_review": True},
                "role_assignments": {"owner": "pm", "tester": "qa"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert {task["name"] for task in body["tasks"]} == {
            "plan",
            "test",
            "review",
            "report",
            "notify",
        }
        assert body["feasible"] is True
        assert body["buffer_seconds"] == 8 * 3600
        assert body["critical_path"]

        # Nothing was persisted
        assert (await as_pm.get("/cases")).json() == []

    async def test_preview_reports_skipped_steps(self, as_pm):
        response = await as_pm.post(
            "/cases/preview",
            json={
                "template_name": "launch",
                "target_date": TARGET.isoformat(),
                "params": {"test_hours": 8, "needs_review": False},
                "role_assignments": {"owner": "pm"},
            },
        )
        body = response.json()
        assert [entry["id"] for entry in body["skipped_tasks"]] == ["review"]

    async def test_infeasible_target_is_reported_not_refused(self, as_pm):
        # A flow that already had to start is normal; blocking it would just
        # make people enter fake dates (design.md §3).
        response = await as_pm.post(
            "/cases/preview",
            json={
                "template_name": "launch",
                "target_date": (
                    datetime.now(tz=UTC) + timedelta(hours=1)
                ).isoformat(),
                "params": {"test_hours": 16, "needs_review": True},
                "role_assignments": {"owner": "pm", "tester": "qa"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["feasible"] is False
        assert body["slack_seconds"] < 0


class TestFullFlow:
    async def test_create_view_edit_complete(self, as_pm, seeded):
        # 1. create
        created = await as_pm.post("/cases", json=CREATE)
        assert created.status_code == 201, created.text
        case = created.json()
        case_id = case["id"]

        assert case["health"] in {"on_track", "at_risk", "overdue"}
        assert task_of(case, "plan")["status"] == "ready"
        assert task_of(case, "test")["status"] == "pending"
        assert len(case["dependencies"]) == 5
        assert case["buffer_seconds"] == 8 * 3600

        # 2. it shows up in the list, with what it is waiting on
        listed = (await as_pm.get("/cases")).json()
        assert [row["id"] for row in listed] == [case_id]
        assert listed[0]["blocked_on"] == ["Planning"]

        # 3. edit a task's duration and see the forecast move
        detail = (await as_pm.get(f"/cases/{case_id}")).json()
        before = detail["forecast_end"]
        test_task = task_of(detail, "test")
        edited = await as_pm.patch(
            f"/cases/{case_id}/tasks/{test_task['id']}",
            json={"duration_seconds": 200 * 3600},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["forecast_end"] > before
        # the baseline is untouched by an edit
        assert (
            task_of(edited.json(), "test")["baseline_start"]
            == test_task["baseline_start"]
        )

        # 4. complete the root and watch its successors unblock
        plan = task_of(detail, "plan")
        completed = await as_pm.post(
            f"/cases/{case_id}/tasks/{plan['id']}/complete",
            json={"note": "signed off"},
        )
        assert completed.status_code == 200, completed.text
        after = completed.json()
        assert task_of(after, "plan")["status"] == "done"
        assert task_of(after, "plan")["completion_note"] == "signed off"
        assert task_of(after, "test")["status"] == "ready"
        assert task_of(after, "review")["status"] == "ready"
        assert task_of(after, "report")["status"] == "pending"
        assert after["progress_ratio"] > 0

    async def test_completing_everything_closes_the_case(self, as_admin):
        case_id = (await as_admin.post("/cases", json=CREATE)).json()["id"]
        for name in ("plan", "test", "review", "report"):
            detail = (await as_admin.get(f"/cases/{case_id}")).json()
            task = task_of(detail, name)
            response = await as_admin.post(
                f"/cases/{case_id}/tasks/{task['id']}/complete", json={}
            )
            assert response.status_code == 200, response.text

        final = (await as_admin.get(f"/cases/{case_id}")).json()
        assert final["status"] == "completed"
        # the unfinished optional task is closed out rather than left dangling
        assert task_of(final, "notify")["status"] == "cancelled"

    async def test_idempotency_key_returns_the_same_case(self, as_pm):
        body = {**CREATE, "idempotency_key": "wizard-1"}
        first = await as_pm.post("/cases", json=body)
        second = await as_pm.post("/cases", json=body)
        assert first.json()["id"] == second.json()["id"]
        assert len((await as_pm.get("/cases")).json()) == 1

    async def test_target_date_change_keeps_the_baseline(self, as_pm):
        created = (await as_pm.post("/cases", json=CREATE)).json()
        case_id = created["id"]
        original = task_of(created, "plan")["baseline_start"]

        moved = await as_pm.patch(
            f"/cases/{case_id}",
            json={
                "target_date": (TARGET + timedelta(days=10)).isoformat(),
                "note": "customer moved it",
            },
        )
        assert moved.status_code == 200, moved.text
        body = moved.json()
        assert task_of(body, "plan")["baseline_start"] == original
        assert len(body["target_date_history"]) == 1
        assert body["target_date_history"][0]["note"] == "customer moved it"

    async def test_cancel_closes_open_tasks(self, as_pm):
        case_id = (await as_pm.post("/cases", json=CREATE)).json()["id"]
        response = await as_pm.post(
            f"/cases/{case_id}/cancel", json={"note": "descoped"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "cancelled"
        assert all(
            task["status"] == "cancelled" for task in body["tasks"]
        )


class TestAuthorisation:
    async def test_group_member_may_complete_a_colleagues_task(
        self, as_pm, client, seeded
    ):
        case_id = (await as_pm.post("/cases", json=CREATE)).json()["id"]
        # review is owned by the quality lead, qa is in that group
        await client.post(
            "/auth/login", json={"username": "qa", "password": "qa-pw"}
        )
        detail = (await client.get(f"/cases/{case_id}")).json()
        review = task_of(detail, "review")
        assert review["permissions"]["can_complete"] is True

    async def test_outsider_cannot_complete(self, as_pm, client, seeded):
        case_id = (await as_pm.post("/cases", json=CREATE)).json()["id"]
        detail = (await as_pm.get(f"/cases/{case_id}")).json()
        plan = task_of(detail, "plan")

        await client.post(
            "/auth/login",
            json={"username": "outsider", "password": "out-pw"},
        )
        response = await client.post(
            f"/cases/{case_id}/tasks/{plan['id']}/complete", json={}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "E_FORBIDDEN"

    async def test_outsider_cannot_edit_the_case(
        self, as_pm, client, seeded
    ):
        case_id = (await as_pm.post("/cases", json=CREATE)).json()["id"]
        await client.post(
            "/auth/login",
            json={"username": "outsider", "password": "out-pw"},
        )
        response = await client.patch(
            f"/cases/{case_id}", json={"name": "hijacked"}
        )
        assert response.status_code == 403

    async def test_outsider_may_still_read(self, as_pm, client, seeded):
        # Reading is deliberately open; hiding flows from colleagues causes
        # more problems than it solves (§7.2).
        case_id = (await as_pm.post("/cases", json=CREATE)).json()["id"]
        await client.post(
            "/auth/login",
            json={"username": "outsider", "password": "out-pw"},
        )
        response = await client.get(f"/cases/{case_id}")
        assert response.status_code == 200
        assert response.json()["permissions"]["can_edit"] is False


class TestErrors:
    async def test_unknown_case_is_404(self, as_pm):
        response = await as_pm.get("/cases/999")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "E_CASE_NOT_FOUND"

    async def test_unknown_template_is_404(self, as_pm):
        response = await as_pm.post(
            "/cases", json={**CREATE, "template_name": "nope"}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "E_TEMPLATE_NOT_FOUND"

    async def test_missing_required_role_reports_the_domain_code(self, as_pm):
        response = await as_pm.post(
            "/cases", json={**CREATE, "role_assignments": {}}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "E_MISSING_ROLE"

    async def test_out_of_range_parameter(self, as_pm):
        response = await as_pm.post(
            "/cases", json={**CREATE, "params": {"test_hours": 5000}}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "E_BAD_PARAM_VALUE"

    async def test_double_completion_is_409(self, as_pm):
        created = (await as_pm.post("/cases", json=CREATE)).json()
        plan = task_of(created, "plan")
        path = f"/cases/{created['id']}/tasks/{plan['id']}/complete"
        assert (await as_pm.post(path, json={})).status_code == 200
        conflict = await as_pm.post(path, json={})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "E_ALREADY_DONE"

    async def test_stale_write_is_409(self, as_pm):
        created = (await as_pm.post("/cases", json=CREATE)).json()
        task = task_of(created, "test")
        response = await as_pm.patch(
            f"/cases/{created['id']}/tasks/{task['id']}",
            json={"duration_seconds": 3600, "expected_version": 999},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "E_STALE_WRITE"

    async def test_unknown_field_is_rejected(self, as_pm):
        response = await as_pm.post("/cases", json={**CREATE, "oops": 1})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "E_BAD_REQUEST"

    async def test_future_completion_is_refused(self, as_pm):
        created = (await as_pm.post("/cases", json=CREATE)).json()
        plan = task_of(created, "plan")
        response = await as_pm.post(
            f"/cases/{created['id']}/tasks/{plan['id']}/complete",
            json={
                "at": (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "E_FUTURE_COMPLETION"


class TestListing:
    async def test_search_matches_task_names(self, as_pm):
        await as_pm.post("/cases", json=CREATE)
        found = (await as_pm.get("/cases", params={"q": "Review"})).json()
        assert len(found) == 1
        missed = (await as_pm.get("/cases", params={"q": "zzz"})).json()
        assert missed == []

    async def test_summary_counts(self, as_pm):
        await as_pm.post("/cases", json=CREATE)
        counts = (await as_pm.get("/cases/summary")).json()
        assert sum(counts.values()) == 1

    async def test_filter_by_status(self, as_pm):
        case_id = (await as_pm.post("/cases", json=CREATE)).json()["id"]
        await as_pm.post(f"/cases/{case_id}/cancel", json={})
        assert (
            await as_pm.get("/cases", params={"status": "active"})
        ).json() == []
        assert len(
            (await as_pm.get("/cases", params={"status": "cancelled"})).json()
        ) == 1


class TestMyTasks:
    async def test_lists_only_actionable_work_for_the_user(self, as_pm):
        await as_pm.post("/cases", json=CREATE)
        mine = (await as_pm.get("/my/tasks")).json()
        names = {item["name"] for item in mine}
        # plan and report are owned by pm; test/review belong to qa
        assert "plan" in names
        assert "test" not in names
        assert mine[0]["name"] == "plan"
        assert mine[0]["status"] == "ready"

    async def test_group_view_includes_colleagues(self, as_pm, client, seeded):
        await as_pm.post("/cases", json=CREATE)
        await client.post(
            "/auth/login", json={"username": "qa", "password": "qa-pw"}
        )
        mine = (
            await client.get("/my/tasks", params={"include_group": "true"})
        ).json()
        assert {"test", "review"} <= {item["name"] for item in mine}


class TestHealthz:
    async def test_liveness_needs_no_database(self, client):
        response = await client.get("http://test/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
