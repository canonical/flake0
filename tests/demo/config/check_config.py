import unittest

from charm import render_config


class TestConfig(unittest.TestCase):
    def test_port_option_reaches_the_workload(self):
        self.assertEqual(render_config({"port": 9000}), "listen 9000\n")


if __name__ == "__main__":
    unittest.main()
