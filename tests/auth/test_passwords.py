"""Password hashing (implement.md §7.1)."""

from __future__ import annotations

import pytest

from app.auth.passwords import hash_password, needs_rehash, verify_password


class TestHashing:
    def test_round_trip(self):
        stored = hash_password("correct horse")
        assert verify_password("correct horse", stored)

    def test_wrong_password_fails(self):
        stored = hash_password("correct horse")
        assert not verify_password("battery staple", stored)

    def test_hash_is_salted(self):
        # Two hashes of the same password must differ, otherwise identical
        # passwords would be visible to anyone reading the table.
        assert hash_password("same") != hash_password("same")

    def test_hash_does_not_contain_the_password(self):
        assert "hunter2" not in hash_password("hunter2")

    def test_empty_password_is_refused(self):
        with pytest.raises(ValueError):
            hash_password("")


class TestFederatedAccounts:
    @pytest.mark.parametrize("stored", [None, ""])
    def test_account_without_a_local_hash_cannot_log_in(self, stored):
        assert not verify_password("anything", stored)

    def test_garbage_hash_is_rejected_not_raised(self):
        assert not verify_password("anything", "not-a-hash")


class TestRehash:
    def test_current_hash_does_not_need_rehashing(self):
        assert not needs_rehash(hash_password("x"))

    def test_unparseable_hash_needs_rehashing(self):
        assert needs_rehash("not-a-hash")
