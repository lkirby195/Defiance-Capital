"""services/passwords.py: hashing, verifying, and the cost upgrade path.  # SPEC §11"""

from __future__ import annotations

import pytest

from services.passwords import (
    ALGORITHM,
    ITERATIONS,
    MIN_LENGTH,
    WeakPassword,
    check_strength,
    hash_password,
    needs_rehash,
    verify_password,
)

PASSWORD = "correct-horse-battery-staple"
# Everything below hashes at a cost the tests can afford; the real cost is exercised once,
# in test_the_default_cost_is_the_configured_one.
CHEAP = 1_000


def test_a_hash_carries_its_algorithm_cost_and_salt() -> None:
    encoded = hash_password(PASSWORD, iterations=CHEAP)
    algorithm, cost, salt, key = encoded.split("$")
    assert algorithm == ALGORITHM
    assert int(cost) == CHEAP
    assert salt and key
    assert PASSWORD not in encoded


def test_the_same_password_hashes_differently_every_time() -> None:
    """A fresh salt per hash, so two people with one password do not share a row."""
    first = hash_password(PASSWORD, iterations=CHEAP)
    second = hash_password(PASSWORD, iterations=CHEAP)
    assert first != second
    assert verify_password(PASSWORD, first)
    assert verify_password(PASSWORD, second)


def test_the_wrong_password_does_not_verify() -> None:
    encoded = hash_password(PASSWORD, iterations=CHEAP)
    assert not verify_password(PASSWORD + "!", encoded)
    assert not verify_password("", encoded)
    assert not verify_password(PASSWORD.upper(), encoded)


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-a-hash",
        "pbkdf2_sha256$600000$only-three-parts",
        "argon2$3$c2FsdA==$a2V5",  # an algorithm this module does not know
        "pbkdf2_sha256$not-a-number$c2FsdA==$a2V5",
        "pbkdf2_sha256$600000$not base64!$a2V5",
        "pbkdf2_sha256$0$c2FsdA==$a2V5",  # no work at all
    ],
)
def test_a_hash_it_cannot_read_is_a_failed_sign_in_not_an_exception(encoded: str) -> None:
    """A corrupt row must not be distinguishable from a wrong password by its failure."""
    assert verify_password(PASSWORD, encoded) is False


def test_a_short_password_is_refused_before_it_is_hashed() -> None:
    with pytest.raises(WeakPassword) as caught:
        hash_password("a" * (MIN_LENGTH - 1))
    assert str(MIN_LENGTH) in str(caught.value)
    check_strength("a" * MIN_LENGTH)  # exactly the floor is fine


def test_a_hash_below_the_current_cost_is_marked_for_rehash() -> None:
    """Raising the cost is a value change, not a migration: old hashes still verify."""
    old = hash_password(PASSWORD, iterations=CHEAP)
    assert verify_password(PASSWORD, old)
    assert needs_rehash(old)
    assert not needs_rehash(old, iterations=CHEAP)
    assert needs_rehash("argon2$3$c2FsdA==$a2V5")
    assert needs_rehash("nonsense")


def test_the_default_cost_is_the_configured_one() -> None:
    assert hash_password(PASSWORD).split("$")[1] == str(ITERATIONS)
    assert ITERATIONS >= 600_000
