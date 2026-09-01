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
        self.assertEqual("https://github.com/zhbch1997/JavisHome", project["urls"]["Homepage"])
        self.assertEqual("https://github.com/zhbch1997/JavisHome", project["urls"]["Repository"])
        self.assertEqual("https://github.com/zhbch1997/JavisHome/issues", project["urls"]["Issues"])

    def test_readme_uses_the_public_clone_url(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("git clone https://github.com/zhbch1997/JavisHome.git", readme)
        self.assertNotIn("<repository-url>", readme)

    def test_setuptools_includes_launcher_and_bridge_modules(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        modules = set(data["tool"]["setuptools"]["py-modules"])
        self.assertIn("api_server", modules)
        self.assertIn("jarvis_launcher", modules)
        self.assertEqual("jarvis-bridge", data["tool"]["setuptools"]["package-dir"][""])


if __name__ == "__main__":
    unittest.main()
