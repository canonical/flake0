import unittest

from charm import unit_names


class TestUnits(unittest.TestCase):
    def test_unit_names(self):
        self.assertEqual(unit_names("db", 2), ["db/1", "db/2"])

    def test_no_units(self):
        self.assertEqual(unit_names("db", 0), [])


if __name__ == "__main__":
    unittest.main()
