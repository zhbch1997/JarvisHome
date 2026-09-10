import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.release_guard import scan_release, scan_tree

ROOT = Path(__file__).resolve().parents[1]


class ReleaseGuardTests(unittest.TestCase):
    def test_clean_public_tree_passes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "README.md").write_text("Example path: ${JARVIS_HOME}\n", encoding="utf-8")
            result = scan_tree(root)
            self.assertEqual([], result)

    def test_runtime_and_camera_files_are_blocked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "snapshots").mkdir()
            (root / "snapshots" / "clip.mp4").write_bytes(b"video")
            token_name = "to" + "ken"
            (root / "config.json").write_text(
                '{"server":{"' + token_name + '":"real-value"}}', encoding="utf-8"
            )
            findings = scan_tree(root)
            kinds = {item.kind for item in findings}
            self.assertIn("forbidden_path", kinds)

    def test_secret_and_personal_absolute_path_are_blocked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            credential_name = "API" + "_KEY"
            personal_path = "/" + "Users" + "/alice/private"
            (root / "bad.py").write_text(
                f'{credential_name} = "abcdefghijklmno"\nHOME = "{personal_path}"\n',
                encoding="utf-8",
            )
            findings = scan_tree(root)
            kinds = {item.kind for item in findings}
            self.assertIn("credential_literal", kinds)
            self.assertIn("personal_absolute_path", kinds)

    def test_exact_public_demo_assets_with_valid_signatures_are_allowed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "docs" / "assets"
            assets.mkdir(parents=True)
            for name in ("jarvis-home-demo.gif", "jarvis-home-demo.mp4", "social-preview.png"):
                shutil.copyfile(ROOT / "docs" / "assets" / name, assets / name)
            self.assertEqual([], scan_tree(root))

    def test_public_asset_allowlist_rejects_wrong_path_signature_and_size(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "docs" / "assets").mkdir(parents=True)
            (root / "other.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (root / "docs" / "assets" / "social-preview.png").write_bytes(b"not a png")
            (root / "docs" / "assets" / "jarvis-home-demo.gif").write_bytes(
                b"GIF89a" + b"x" * (5 * 1024 * 1024)
            )
            findings = scan_tree(root)
            found = {item.path for item in findings if item.kind == "forbidden_path"}
            self.assertIn("other.png", found)
            self.assertIn("docs/assets/social-preview.png", found)
            self.assertIn("docs/assets/jarvis-home-demo.gif", found)

    def test_public_asset_with_valid_header_and_private_payload_is_blocked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            asset = root / "docs" / "assets" / "social-preview.png"
            asset.parent.mkdir(parents=True)
            original = (ROOT / "docs" / "assets" / "social-preview.png").read_bytes()
            personal_path = b"/" + b"Users" + b"/alice/private"
            credential = b"API" + b"_KEY=abcdefghijklmno"
            asset.write_bytes(original + personal_path + b" " + credential)
            findings = scan_tree(root)
            self.assertIn("forbidden_path", {item.kind for item in findings})

    def test_signature_only_public_assets_are_blocked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "docs" / "assets"
            assets.mkdir(parents=True)
            (assets / "social-preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (assets / "jarvis-home-demo.gif").write_bytes(b"GIF89a")
            (assets / "jarvis-home-demo.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
            found = {item.path for item in scan_tree(root) if item.kind == "forbidden_path"}
            self.assertEqual(
                {
                    "docs/assets/social-preview.png",
                    "docs/assets/jarvis-home-demo.gif",
                    "docs/assets/jarvis-home-demo.mp4",
                },
                found,
            )

    def test_all_runtime_state_and_binary_asset_shapes_are_blocked(self):
        blocked = [
            "state/live.json",
            "home_profile/member.json",
            "recording.wav",
            "weights.gguf",
            "prod.sqlite-wal",
            "camera.gif",
            "server.crt",
            "deploy/production.plist",
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for relative in blocked:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"private")
            findings = scan_tree(root)
            found = {item.path for item in findings if item.kind == "forbidden_path"}
            for relative in blocked:
                self.assertTrue(
                    any(item == relative or item == relative.split("/")[0] for item in found),
                    relative,
                )

    def test_unquoted_env_and_json_credentials_are_blocked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            credential_name = "API" + "_KEY"
            token_name = "to" + "ken"
            (root / ".env").write_text(f"{credential_name}=abcdefghijklmno\n", encoding="utf-8")
            (root / "settings.json").write_text(
                '{"' + token_name + '":"abcdefghijklmno"}', encoding="utf-8"
            )
            findings = scan_tree(root)
            credential_paths = {item.path for item in findings if item.kind == "credential_literal"}
            self.assertIn(".env", credential_paths)
            self.assertIn("settings.json", credential_paths)

    def test_household_specific_terms_are_blocked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            terms = ["芝" + "麻糊", "龟" + "龟", "电竞" + "房", "乌龟" + "灯", "觉觉" + "猪"]
            (root / "private.py").write_text(" ".join(terms), encoding="utf-8")
            findings = scan_tree(root)
            self.assertIn("household_specific_term", {item.kind for item in findings})

    def test_cache_directories_are_blocked(self):
        for cache_name in (".pytest_cache", "__pycache__"):
            with self.subTest(cache_name=cache_name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                path = root / cache_name
                path.mkdir()
                (path / "cache").write_bytes(b"local path data")
                findings = scan_tree(root)
                self.assertIn("forbidden_path", {item.kind for item in findings})

    def test_worktree_scan_skips_generated_virtual_environment(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            venv = root / ".venv" / "bin"
            venv.mkdir(parents=True)
            personal_path = "/" + "Users" + "/alice/private"
            (venv / "activate").write_text(f"VIRTUAL_ENV={personal_path}\n", encoding="utf-8")
            self.assertEqual([], scan_tree(root))

    def test_release_scan_blocks_tampered_public_asset_in_staged_index(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            asset = root / "docs" / "assets" / "social-preview.png"
            asset.parent.mkdir(parents=True)
            original = (ROOT / "docs" / "assets" / "social-preview.png").read_bytes()
            personal_path = b"/" + b"Users" + b"/alice/private"
            credential = b"API" + b"_KEY=abcdefghijklmno"
            asset.write_bytes(original + personal_path + b" " + credential)
            subprocess.run(["git", "add", "docs/assets/social-preview.png"], cwd=root, check=True)
            findings, source = scan_release(root)
            self.assertEqual("staged", source)
            self.assertIn("forbidden_path", {item.kind for item in findings})

    def test_release_scan_prefers_staged_snapshot_over_ignored_worktree_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            (root / "README.md").write_text("safe\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=root, check=True)
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "local.pyc").write_bytes(b"local")
            findings, source = scan_release(root)
            self.assertEqual([], findings)
            self.assertEqual("staged", source)

    def test_release_scan_blocks_private_content_in_staged_snapshot(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            private_name = "张" + "北辰"
            (root / "bad.py").write_text(private_name, encoding="utf-8")
            subprocess.run(["git", "add", "bad.py"], cwd=root, check=True)
            findings, source = scan_release(root)
            self.assertEqual("staged", source)
            self.assertIn("household_specific_term", {item.kind for item in findings})

    def test_release_scan_blocks_force_staged_virtual_environment(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            runtime = root / ".venv" / "runtime.txt"
            runtime.parent.mkdir()
            runtime.write_text("otherwise harmless\n", encoding="utf-8")
            subprocess.run(["git", "add", "-f", ".venv/runtime.txt"], cwd=root, check=True)
            findings, source = scan_release(root)
            self.assertEqual("staged", source)
            self.assertIn("forbidden_path", {item.kind for item in findings})

    def test_release_scan_checks_complete_index_not_only_staged_diff(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            private_name = "张" + "北辰"
            (root / "existing.py").write_text(private_name, encoding="utf-8")
            subprocess.run(["git", "add", "existing.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"],
                cwd=root,
                check=True,
            )
            (root / "unrelated.txt").write_text("safe\n", encoding="utf-8")
            subprocess.run(["git", "add", "unrelated.txt"], cwd=root, check=True)
            findings, source = scan_release(root)
            self.assertEqual("staged", source)
            self.assertIn("existing.py", {item.path for item in findings})
            self.assertIn("household_specific_term", {item.kind for item in findings})


if __name__ == "__main__":
    unittest.main()
