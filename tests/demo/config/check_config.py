import unittest

from charm import render_config


class TestConfig(unittest.TestCase):
    def test_port_option_reaches_the_workload(self):
        self.assertIn("listen 9000\n", render_config({"port": 9000}))

    def test_workers_default(self):
        self.assertIn("workers 2\n", render_config({}))


if __name__ == "__main__":
    unittest.main()
