import unittest
from hermes_nerve.progress_integrity import tool_kind

class LauncherPrefixTests(unittest.TestCase):
    def kind(self, cmd): return tool_kind("terminal", {"command": cmd})

    def test_uv_run_with_pytest_is_a_test(self):
        self.assertEqual(self.kind("uv run -q --with pytest python -m pytest app/test_calc.py -q"), "test")

    def test_uv_run_pytest_and_uvx(self):
        self.assertEqual(self.kind("uv run pytest"), "test")
        self.assertEqual(self.kind("uvx pytest -q"), "test")

    def test_poetry_pipenv_pdm_hatch_conda(self):
        for cmd in ("poetry run pytest", "pipenv run python -m pytest", "pdm run pytest -x", "hatch run pytest", "conda run -n ci pytest"):
            with self.subTest(cmd=cmd): self.assertEqual(self.kind(cmd), "test")

    def test_launcher_without_test_is_not_a_test(self):
        self.assertEqual(self.kind("uv run python app.py"), "")
        self.assertEqual(self.kind("poetry run flask run"), "")

    def test_env_assignment_before_launcher(self):
        self.assertEqual(self.kind("CI=1 uv run pytest -q"), "test")

if __name__ == "__main__": unittest.main()
