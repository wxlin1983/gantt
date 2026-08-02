"""Working-time calendars over HTTP (implement.md §3.2).

Nothing could edit these before, so `taiwan_office` shipped with an empty
holiday list and every business-mode task scheduled through every public
holiday.
"""

from __future__ import annotations

OFFICE_HOURS = {
    "mon": [["09:00", "18:00"]],
    "tue": [["09:00", "18:00"]],
    "wed": [["09:00", "18:00"]],
    "thu": [["09:00", "18:00"]],
    "fri": [["09:00", "18:00"]],
    "sat": [],
    "sun": [],
}


async def office(client):
    rows = (await client.get("/calendars")).json()
    return next(row for row in rows if row["name"] == "taiwan_office")


class TestReading:
    async def test_anyone_signed_in_may_list(self, as_qa):
        response = await as_qa.get("/calendars")
        assert response.status_code == 200
        assert {row["name"] for row in response.json()} >= {
            "continuous",
            "taiwan_office",
        }

    async def test_reports_what_one_day_converts_to(self, as_admin):
        """`1D` resolves against this, and that has been a real error."""
        assert (await office(as_admin))["day_seconds"] == 9 * 3600
        rows = (await as_admin.get("/calendars")).json()
        continuous = next(r for r in rows if r["name"] == "continuous")
        assert continuous["day_seconds"] == 86400

    async def test_continuous_is_marked_not_editable(self, as_admin):
        rows = (await as_admin.get("/calendars")).json()
        continuous = next(r for r in rows if r["name"] == "continuous")
        assert continuous["is_editable"] is False


class TestEditing:
    async def test_holidays_can_be_added(self, as_admin):
        row = await office(as_admin)
        response = await as_admin.patch(
            f"/calendars/{row['id']}",
            json={"holidays": ["2026-10-10", "2026-01-01"]},
        )
        assert response.status_code == 200
        # Sorted and de-duplicated on save, so a later diff is about the
        # dates rather than the order they were pasted in
        assert response.json()["holidays"] == ["2026-01-01", "2026-10-10"]

    async def test_duplicates_collapse(self, as_admin):
        row = await office(as_admin)
        response = await as_admin.patch(
            f"/calendars/{row['id']}",
            json={"holidays": ["2026-01-01", "2026-01-01", " 2026-01-01 "]},
        )
        assert response.json()["holidays"] == ["2026-01-01"]

    async def test_a_holiday_that_is_not_a_date_is_refused(self, as_admin):
        row = await office(as_admin)
        response = await as_admin.patch(
            f"/calendars/{row['id']}", json={"holidays": ["next tuesday"]}
        )
        assert response.json()["error"]["code"] == "E_BAD_HOLIDAY"

    async def test_working_hours_change_the_day_length(self, as_admin):
        row = await office(as_admin)
        shorter = {**OFFICE_HOURS, "mon": [["09:00", "13:00"]]}
        response = await as_admin.patch(
            f"/calendars/{row['id']}", json={"working_hours": shorter}
        )
        # The modal day, not the mean: four other nine-hour days still make
        # a normal day nine hours
        assert response.json()["day_seconds"] == 9 * 3600

    async def test_overlapping_windows_are_refused(self, as_admin):
        row = await office(as_admin)
        response = await as_admin.patch(
            f"/calendars/{row['id']}",
            json={
                "working_hours": {
                    "mon": [["09:00", "13:00"], ["12:00", "18:00"]]
                }
            },
        )
        assert response.json()["error"]["code"] == "E_BAD_WORKING_HOURS"

    async def test_a_window_ending_before_it_starts_is_refused(
        self, as_admin
    ):
        row = await office(as_admin)
        response = await as_admin.patch(
            f"/calendars/{row['id']}",
            json={"working_hours": {"mon": [["18:00", "09:00"]]}},
        )
        assert response.json()["error"]["code"] == "E_BAD_WORKING_HOURS"

    async def test_an_unknown_timezone_is_refused(self, as_admin):
        row = await office(as_admin)
        response = await as_admin.patch(
            f"/calendars/{row['id']}", json={"timezone": "Mars/Olympus"}
        )
        assert response.json()["error"]["code"] == "E_BAD_TIMEZONE"

    async def test_nothing_is_written_when_validation_fails(self, as_admin):
        row = await office(as_admin)
        await as_admin.patch(
            f"/calendars/{row['id']}",
            json={"timezone": "Mars/Olympus", "holidays": ["2026-01-01"]},
        )
        after = await office(as_admin)
        assert after["timezone"] == row["timezone"]
        assert after["holidays"] == row["holidays"]

    async def test_continuous_cannot_be_edited(self, as_admin):
        rows = (await as_admin.get("/calendars")).json()
        continuous = next(r for r in rows if r["name"] == "continuous")
        response = await as_admin.patch(
            f"/calendars/{continuous['id']}", json={"timezone": "UTC"}
        )
        # The engine ignores this row entirely, so an editable form here would
        # be a control that does nothing
        assert response.json()["error"]["code"] == "E_READ_ONLY_CALENDAR"

    async def test_a_non_admin_cannot_edit(self, as_pm):
        row = await office(as_pm)
        response = await as_pm.patch(
            f"/calendars/{row['id']}", json={"holidays": []}
        )
        assert response.status_code == 403


class TestCreatingAndDeleting:
    async def test_create_and_delete(self, as_admin):
        created = await as_admin.post(
            "/calendars",
            json={
                "name": "berlin_office",
                "timezone": "Europe/Berlin",
                "working_hours": OFFICE_HOURS,
                "holidays": ["2026-12-25"],
            },
        )
        assert created.status_code == 201
        assert created.json()["day_seconds"] == 9 * 3600
        assert (
            await as_admin.delete(f"/calendars/{created.json()['id']}")
        ).status_code == 204

    async def test_a_duplicate_name_is_refused(self, as_admin):
        response = await as_admin.post(
            "/calendars", json={"name": "taiwan_office"}
        )
        assert response.json()["error"]["code"] == "E_DUPLICATE_CALENDAR"

    async def test_continuous_cannot_be_redefined(self, as_admin):
        response = await as_admin.post(
            "/calendars", json={"name": "continuous"}
        )
        assert response.json()["error"]["code"] == "E_RESERVED_NAME"

    async def test_a_builtin_cannot_be_deleted(self, as_admin):
        """Editable -- adding holidays is the point -- but not removable.

        `taiwan_office` is what `schedule_mode: business` defaults to.
        """
        row = await office(as_admin)
        response = await as_admin.delete(f"/calendars/{row['id']}")
        assert response.json()["error"]["code"] == "E_BUILTIN_CALENDAR"

    async def test_a_calendar_a_template_names_is_kept(self, as_admin):
        """Deleting is silent otherwise: the task falls back to 24x7."""
        await as_admin.post("/calendars", json={"name": "night_shift"})
        await as_admin.post(
            "/templates/import",
            json={
                "document": (
                    "gantt:\n"
                    "  template_name: nights\n"
                    "  flow:\n"
                    "    - id: a\n"
                    "      duration: 2H\n"
                    "      calendar: night_shift\n"
                )
            },
        )
        rows = (await as_admin.get("/calendars")).json()
        night = next(r for r in rows if r["name"] == "night_shift")
        response = await as_admin.delete(f"/calendars/{night['id']}")
        body = response.json()["error"]
        assert body["code"] == "E_CALENDAR_IN_USE"
        assert "nights" in body["message"]

    async def test_a_non_admin_cannot_create(self, as_pm):
        response = await as_pm.post("/calendars", json={"name": "sneaky"})
        assert response.status_code == 403


class TestWhatAHolidayActuallyMoves:
    """The boundary that makes this feature safe to use.

    A holiday must change what is planned next and must not touch what has
    already been agreed. Cases freeze the calendar definitions into their
    snapshot and reschedule against that copy (§4.8), so an administrator
    adding a holiday will see existing dates stay put -- which looks like the
    feature not working unless you know it is the point.
    """

    DOCUMENT = (
        "gantt:\n"
        "  template_name: office_only\n"
        "  flow:\n"
        "    - id: work\n"
        "      duration: 8H\n"
        "      schedule_mode: business\n"
        "      calendar: taiwan_office\n"
    )
    # Wednesday 18:00 in Taipei, which is 10:00 UTC.
    TARGET = "2026-09-30T10:00:00Z"

    async def make_case(self, client, name):
        created = await client.post(
            "/cases",
            json={
                "name": name,
                "template_name": "office_only",
                "target_date": self.TARGET,
            },
        )
        assert created.status_code == 201, created.json()
        return next(
            task for task in created.json()["tasks"] if task["name"] == "work"
        )

    async def test_a_holiday_moves_the_next_case_and_not_the_last_one(
        self, as_admin
    ):
        await as_admin.post(
            "/templates/import", json={"document": self.DOCUMENT}
        )
        await as_admin.post("/templates/office_only/publish", json={})

        before = await self.make_case(as_admin, "planned before")
        # 8 working hours back from Wednesday 18:00 leaves it on Wednesday
        assert before["baseline_start"].startswith("2026-09-30")

        row = await office(as_admin)
        assert (
            await as_admin.patch(
                f"/calendars/{row['id']}", json={"holidays": ["2026-09-30"]}
            )
        ).status_code == 200

        after = await self.make_case(as_admin, "planned after")
        # The Wednesday is gone, so the work lands on the Tuesday
        assert after["baseline_start"].startswith("2026-09-29")

        listed = (await as_admin.get("/cases?q=planned before")).json()
        still = next(c for c in listed if c["name"] == "planned before")
        detail = (await as_admin.get(f"/cases/{still['id']}")).json()
        frozen = next(t for t in detail["tasks"] if t["name"] == "work")
        assert frozen["baseline_start"] == before["baseline_start"]
        assert frozen["forecast_start"] == before["forecast_start"]
