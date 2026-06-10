"""Offline smoke test for the demo warehouse seeding.

Targets `setup_db` only (no Gemini call / no API key required) so it runs in CI and
locally without credentials. Verifies that the mock warehouse is created with the
expected tables and row counts.
"""

import os
import sqlite3
import sys
import unittest

# Make `setup_db` importable without installing the project as a package.
SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC_DIR)

import setup_db  # noqa: E402


class TestSetupDbSmoke(unittest.TestCase):
    def setUp(self):
        # Reset any DB left by a previous run so INSERT OR IGNORE row counts are exact.
        if os.path.exists(setup_db.DB_PATH):
            os.remove(setup_db.DB_PATH)

    def test_create_mock_data_builds_expected_schema(self):
        setup_db.create_mock_data()

        self.assertTrue(os.path.exists(setup_db.DB_PATH), "warehouse DB was not created")

        conn = sqlite3.connect(setup_db.DB_PATH)
        try:
            cur = conn.cursor()
            tables = {row[0] for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertIn("users", tables)
            self.assertIn("orders", tables)

            self.assertEqual(cur.execute("SELECT COUNT(*) FROM users").fetchone()[0], 3)
            self.assertEqual(cur.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 4)
        finally:
            conn.close()

    def test_create_sample_source_file_seeds_products_csv(self):
        setup_db.create_sample_source_file()
        products = os.path.join(setup_db.SOURCES_DIR, "products.csv")
        self.assertTrue(os.path.exists(products), "products.csv source was not created")


if __name__ == "__main__":
    unittest.main()
