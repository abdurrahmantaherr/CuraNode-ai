"""D-VERIFY — invite-code hashing. Pure functions, no database."""

from __future__ import annotations

from app.verification.codes import generate_code, hash_code, verify_code


def test_generate_code_is_reasonably_long_and_uppercase_alnum():
    code = generate_code()
    assert len(code) >= 10
    assert code == code.upper()
    assert code.replace("-", "").isalnum()  # dash-grouped for readability, by design


def test_generate_code_is_not_constant():
    assert generate_code() != generate_code()


def test_hash_then_verify_the_same_code_succeeds():
    code = "CLINIC-TEST-0001"
    stored = hash_code(code)
    assert verify_code(code, stored) is True


def test_verify_rejects_a_wrong_code():
    stored = hash_code("CLINIC-TEST-0001")
    assert verify_code("CLINIC-TEST-0002", stored) is False


def test_verify_rejects_a_blank_code():
    stored = hash_code("CLINIC-TEST-0001")
    assert verify_code("", stored) is False


def test_hash_code_never_stores_the_plain_code():
    code = "CLINIC-TEST-0001"
    stored = hash_code(code)
    assert code not in stored


def test_two_hashes_of_the_same_code_differ():
    # Salted — two rotations of the same literal code must not look identical.
    code = "CLINIC-TEST-0001"
    assert hash_code(code) != hash_code(code)
