"""Local password hashing (implement.md §7.1).

Argon2id, with the library's current defaults rather than pinned parameters:
the recommended cost moves over time, and :func:`needs_rehash` lets stored
hashes be upgraded transparently on the next successful login.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Check a password against a stored hash.

    A user with no local hash (federated account, or one that has never had a
    password set) can never authenticate this way. The comparison still runs
    against a dummy hash so that the response time does not reveal whether
    the account exists.
    """
    if not stored_hash:
        _hasher.hash("timing-equalisation")
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than current."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True
