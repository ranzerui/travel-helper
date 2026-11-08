import subprocess
import unittest
from unittest.mock import patch

from travel_planner.runtime import browser_available


class RuntimeTest(unittest.TestCase):
    @patch('travel_planner.runtime.subprocess.run')
    def test_configured_python_checks_dependency_not_just_file(self, run):
        run.return_value.returncode = 1
        with patch.dict('os.environ', {'BROWSER_USE_PY': '/custom/python'}):
            self.assertFalse(browser_available())
        self.assertEqual(run.call_args.args[0][0], '/custom/python')
        run.return_value.returncode = 0
        self.assertTrue(browser_available())

    @patch('travel_planner.runtime.subprocess.run', side_effect=FileNotFoundError)
    def test_missing_interpreter_is_unavailable(self, run):
        self.assertFalse(browser_available())

    @patch('travel_planner.runtime.subprocess.run', side_effect=subprocess.TimeoutExpired('python', 5))
    def test_hanging_interpreter_is_unavailable(self, run):
        self.assertFalse(browser_available())
