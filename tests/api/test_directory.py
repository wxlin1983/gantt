"""Users and groups over HTTP (implement.md §7.1).

The directory only existed in `gantt seed` before this, which is how a case
came to be created against a username nobody had registered.
"""

from __future__ import annotations


class TestReadingTheDirectory:
    async def test_anyone_signed_in_may_list_users(self, as_qa):
        response = await as_qa.get("/users")
        assert response.status_code == 200
        assert {row["username"] for row in response.json()} >= {"admin", "pm"}

    async def test_the_password_hash_never_leaves_the_server(self, as_admin):
        row = (await as_admin.get("/users")).json()[0]
        assert "password_hash" not in row
        # ...but whether one is set is worth knowing
        assert row["has_password"] is True

    async def test_listing_requires_a_session(self, client):
        assert (await client.get("/users")).status_code == 401


class TestCreatingUsers:
    async def test_admin_can_add_a_colleague(self, as_admin):
        response = await as_admin.post(
            "/users",
            json={
                "username": "dana",
                "display_name": "Dana Lee",
                "email": "dana@example.com",
                "password": "correct-horse",
            },
        )
        assert response.status_code == 201
        assert response.json()["display_name"] == "Dana Lee"
        assert response.json()["has_password"] is True

    async def test_display_name_defaults_to_the_username(self, as_admin):
        response = await as_admin.post("/users", json={"username": "eve"})
        assert response.json()["display_name"] == "eve"

    async def test_a_duplicate_username_is_named_not_just_refused(
        self, as_admin
    ):
        response = await as_admin.post("/users", json={"username": "pm"})
        assert response.status_code == 400
        body = response.json()["error"]
        assert body["code"] == "E_DUPLICATE_USER"
        assert "pm" in body["message"]

    async def test_a_duplicate_email_is_refused(self, as_admin):
        await as_admin.post(
            "/users", json={"username": "f1", "email": "shared@example.com"}
        )
        response = await as_admin.post(
            "/users", json={"username": "f2", "email": "shared@example.com"}
        )
        assert response.json()["error"]["code"] == "E_DUPLICATE_EMAIL"

    async def test_a_short_password_is_refused(self, as_admin):
        response = await as_admin.post(
            "/users", json={"username": "g1", "password": "short"}
        )
        assert response.status_code == 422

    async def test_a_non_admin_cannot_add_users(self, as_pm):
        response = await as_pm.post("/users", json={"username": "mallory"})
        assert response.status_code == 403


class TestEditingUsers:
    async def test_admin_can_deactivate_someone_else(self, as_admin):
        users = (await as_admin.get("/users")).json()
        pm = next(u for u in users if u["username"] == "pm")
        response = await as_admin.patch(
            f"/users/{pm['id']}", json={"is_active": False}
        )
        assert response.json()["is_active"] is False

    async def test_you_cannot_deactivate_yourself(self, as_admin):
        """One click, no way back that does not involve a shell."""
        me = (await as_admin.get("/auth/me")).json()["user"]
        response = await as_admin.patch(
            f"/users/{me['id']}", json={"is_active": False}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "E_SELF_LOCKOUT"

    async def test_you_cannot_drop_your_own_admin_rights(self, as_admin):
        me = (await as_admin.get("/auth/me")).json()["user"]
        response = await as_admin.patch(
            f"/users/{me['id']}", json={"is_template_admin": False}
        )
        assert response.json()["error"]["code"] == "E_SELF_LOCKOUT"

    async def test_setting_a_password_lets_that_person_sign_in(
        self, as_admin, client
    ):
        created = (
            await as_admin.post("/users", json={"username": "hank"})
        ).json()
        assert created["has_password"] is False
        assert (
            await as_admin.put(
                f"/users/{created['id']}/password",
                json={"password": "a-new-secret"},
            )
        ).status_code == 204
        signed_in = await client.post(
            "/auth/login",
            json={"username": "hank", "password": "a-new-secret"},
        )
        assert signed_in.status_code == 200

    async def test_unknown_user_is_a_clean_error(self, as_admin):
        response = await as_admin.patch("/users/9999", json={"email": "x@y.z"})
        assert response.json()["error"]["code"] == "E_USER_NOT_FOUND"


class TestGroups:
    async def test_create_and_list(self, as_admin):
        created = await as_admin.post(
            "/groups", json={"name": "safety", "display_name": "Safety"}
        )
        assert created.status_code == 201
        names = {g["name"] for g in (await as_admin.get("/groups")).json()}
        assert "safety" in names

    async def test_membership_is_replaced_wholesale(self, as_admin):
        users = (await as_admin.get("/users")).json()
        by_name = {u["username"]: u["id"] for u in users}
        group = (
            await as_admin.post("/groups", json={"name": "ops"})
        ).json()

        first = await as_admin.put(
            f"/groups/{group['id']}/members",
            json={
                "members": [
                    {"user_id": by_name["pm"], "is_lead": True},
                    {"user_id": by_name["qa"], "is_lead": False},
                ]
            },
        )
        members = first.json()["members"]
        assert {m["username"] for m in members} == {"pm", "qa"}
        assert [m["username"] for m in members if m["is_lead"]] == ["pm"]

        # Saving a shorter list removes the ones left out, rather than merging
        second = await as_admin.put(
            f"/groups/{group['id']}/members",
            json={"members": [{"user_id": by_name["qa"], "is_lead": True}]},
        )
        assert {m["username"] for m in second.json()["members"]} == {"qa"}

    async def test_membership_rejects_a_user_that_does_not_exist(
        self, as_admin
    ):
        group = (
            await as_admin.post("/groups", json={"name": "ghosts"})
        ).json()
        response = await as_admin.put(
            f"/groups/{group['id']}/members",
            json={"members": [{"user_id": 4242, "is_lead": False}]},
        )
        assert response.json()["error"]["code"] == "E_USER_NOT_FOUND"

    async def test_a_duplicate_group_is_refused(self, as_admin):
        await as_admin.post("/groups", json={"name": "dup"})
        response = await as_admin.post("/groups", json={"name": "dup"})
        assert response.json()["error"]["code"] == "E_DUPLICATE_GROUP"

    async def test_an_unused_group_can_be_deleted(self, as_admin):
        group = (
            await as_admin.post("/groups", json={"name": "temporary"})
        ).json()
        assert (
            await as_admin.delete(f"/groups/{group['id']}")
        ).status_code == 204

    async def test_a_group_still_assigned_to_tasks_is_kept(
        self, as_admin, seeded
    ):
        """Deleting would orphan the rows or erase who was responsible."""
        groups = (await as_admin.get("/groups")).json()
        quality = next(g for g in groups if g["name"] == "quality")
        await as_admin.post(
            "/cases",
            json={
                "name": "holds a group",
                "template_name": "launch",
                "target_date": "2026-09-30T18:00:00Z",
                "params": {"test_hours": 16, "needs_review": True},
                "role_assignments": {"owner": "pm", "tester": "qa"},
            },
        )
        response = await as_admin.delete(f"/groups/{quality['id']}")
        assert response.json()["error"]["code"] == "E_GROUP_IN_USE"

    async def test_a_non_admin_cannot_change_membership(self, as_pm):
        groups = (await as_pm.get("/groups")).json()
        assert (
            await as_pm.put(
                f"/groups/{groups[0]['id']}/members", json={"members": []}
            )
        ).status_code == 403
