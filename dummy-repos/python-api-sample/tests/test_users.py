"""Behavioural spec for the user service.

These tests define the validation contract the harness must implement. Against
the un-refactored `app/users.py` the two rejection tests FAIL (no validation);
after the Executor adds `validate_payload`, all three pass.
"""
import unittest

from app import users
from app.users import create_user, get_user


class TestUsers(unittest.TestCase):
    def setUp(self):
        users._USERS.clear()

    def test_create_valid_user(self):
        u = create_user({"email": "ada@example.com", "name": "Ada"})
        self.assertEqual(u["id"], 1)
        self.assertEqual(get_user(1)["email"], "ada@example.com")

    def test_missing_email_rejected(self):
        with self.assertRaises(ValueError):
            create_user({"name": "Ada"})

    def test_invalid_email_rejected(self):
        with self.assertRaises(ValueError):
            create_user({"email": "not-an-email", "name": "Ada"})


if __name__ == "__main__":
    unittest.main()
