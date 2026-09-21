"""Hash and verify a review-queue password.  # SPEC §11

Pure: text in, text out, no I/O and no clock. PBKDF2-HMAC-SHA256 from the standard library,
so there is no dependency to keep current and no build step on Windows; the iteration count
is the OWASP Password Storage figure for that algorithm. The encoded form carries the
algorithm, the cost and the salt:

    pbkdf2_sha256$600000$<salt b64>$<derived key b64>

so every stored hash says how to verify itself. Raising ``ITERATIONS`` later leaves existing
hashes verifiable, and ``needs_rehash`` says which of them are behind - there is no migration
to write, only a rehash on the next successful sign-in.

Nothing here ever logs, returns or raises with the password in it. A verify against a
malformed or unknown encoding is False, not an exception: a corrupt row must not be
distinguishable from a wrong password by the shape of the failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
SALT_BYTES = 16
KEY_BYTES = 32
# Long enough to be worth the iteration count; short enough that a passphrase is welcome.
MIN_LENGTH = 12


class WeakPassword(ValueError):
    """The password is too short to be worth hashing."""

    def __init__(self, minimum: int = MIN_LENGTH) -> None:
        super().__init__(f"password must be at least {minimum} characters")
        self.minimum = minimum


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, KEY_BYTES)


def check_strength(password: str) -> None:
    """Raise ``WeakPassword`` unless the password clears the length floor.

    Length only. A composition rule ("one digit, one symbol") buys less than length does and
    pushes people towards the same handful of substitutions; there is no self-signup here,
    so the floor is a backstop against a one-word password, not a policy engine.
    """
    if len(password) < MIN_LENGTH:
        raise WeakPassword()


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    """Encode the password with a fresh random salt.  # SPEC §11"""
    check_strength(password)
    salt = secrets.token_bytes(SALT_BYTES)
    return f"{ALGORITHM}${iterations}${_b64(salt)}${_b64(_derive(password, salt, iterations))}"


def verify_password(password: str, encoded: str) -> bool:
    """True when ``password`` produces ``encoded``; False for anything else.

    Constant-time on the derived key, so a near-miss and a wide miss take the same time. A
    hash in an algorithm this module does not know, or one that does not parse at all,
    verifies False rather than raising: a corrupt row is a failed sign-in, not a 500.
    """
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM:
        return False
    _, iterations_text, salt_text, key_text = parts
    try:
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(key_text, validate=True)
    except ValueError:
        return False
    if iterations < 1 or not salt or not expected:
        return False
    return hmac.compare_digest(_derive(password, salt, iterations), expected)


def needs_rehash(encoded: str, iterations: int = ITERATIONS) -> bool:
    """True when the stored hash is in an older algorithm or below the current cost."""
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM:
        return True
    try:
        return int(parts[1]) < iterations
    except ValueError:
        return True
