"""Sign-in: salted password hashes and the two roles (no Streamlit here, so it can be tested).

  admin    can see everything (read-only)
  operator can see everything AND control components (gates); manual control has the
           highest priority in the backend

Passwords are never stored: only PBKDF2-SHA256 with a random salt per user.
"""
import hashlib
import hmac
import secrets

ROLES = ("admin", "operator")
ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8
_SCHEME = "pbkdf2_sha256"


def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_SCHEME}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    """True only for the right password. A damaged or unknown stored value is just False."""
    try:
        scheme, iterations, salt_hex, digest_hex = str(stored).split("$")
        if scheme != _SCHEME:
            return False
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY = hash_password("not-a-real-password")


def authenticate(row, password):
    """`row` is (username, password_hash, role) from the database, or None for an unknown user.

    Returns the role for a correct password, otherwise None. An unknown user costs the
    same time as a wrong password, so the answer time does not reveal which names exist.
    """
    if row is None:
        verify_password(password, _DUMMY)
        return None
    username, stored, role = row
    if verify_password(password, stored) and role in ROLES:
        return role
    return None


def can_control(role):
    """Only the operator may operate components. The admin looks, never touches."""
    return role == "operator"


def check_new_password(password):
    """A reason the password is too weak, or None if it is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    return None
