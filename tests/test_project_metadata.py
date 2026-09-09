import pathlib
import tomllib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProjectMetadataTests(unittest.TestCase):
    def test_pyproject_declares_runtime_and_test_dependencies(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual("jarvis-home", project["name"])
        self.assertEqual(">=3.11", project["requires-python"])
        self.assertEqual(
            {"fastapi", "httpx", "uvicorn", "websockets"},
            {item.split("[")[0].split("=")[0].split("<")[0].split(">")[0] for item in project["dependencies"]},
        )
        self.assertIn("test", project["optional-dependencies"])
        self.assertEqual("jarvis_launcher:main", project["scripts"]["jarvis-home"])
        self.assertEqual("mock_home:main", project["scripts"]["jarvis-home-demo"])
        self.assertEqual("https://github.com/zhbch1997/JarvisHome", project["urls"]["Homepage"])
        self.assertEqual("https://github.com/zhbch1997/JarvisHome", project["urls"]["Repository"])
        self.assertEqual("https://github.com/zhbch1997/JarvisHome/issues", project["urls"]["Issues"])

    def test_readmes_use_the_canonical_public_url(self):
        for name in ("README.md", "README_EN.md"):
            readme = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("https://github.com/zhbch1997/JarvisHome", readme)
            self.assertNotIn("JavisHome", readme)
            self.assertNotIn("<repository-url>", readme)

    def test_public_contributor_entry_points_exist(self):
        expected = (
            "ROADMAP.md",
            "CONTRIBUTING.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/PULL_REQUEST_TEMPLATE.md",
        )
        for name in expected:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_setuptools_includes_launcher_and_bridge_modules(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        modules = set(data["tool"]["setuptools"]["py-modules"])
        self.assertIn("api_server", modules)
        self.assertIn("jarvis_launcher", modules)
        self.assertEqual("jarvis-bridge", data["tool"]["setuptools"]["package-dir"][""])


if __name__ == "__main__":
    unittest.main()
