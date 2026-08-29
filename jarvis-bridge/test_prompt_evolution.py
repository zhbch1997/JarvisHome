import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from prompt_evolution import PromptEvolution


class PromptEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = PromptEvolution(self.root)

    def write(self, name, value):
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_passing_candidate_promotes_and_keeps_rollback(self):
        old = [{"request":"讲个笑话","route":"local_chat","intent":"chat","executor":"local_9b"}]
        candidate = [{"request":"每天九点提醒我","route":"task","intent":"create","executor":"openclaw"}]
        self.write("router-examples.json", old)
        self.write("router-candidate-examples.json", candidate)
        report = self.engine.promote_if_safe(lambda examples: {
            "valid_json_rate": 1.0, "baseline_accuracy": 1.0,
            "candidate_accuracy": 1.0, "p95_seconds": 0.9,
        })
        self.assertEqual(report["status"], "promoted")
        self.assertEqual(json.loads((self.root/"router-examples.json").read_text()), old + candidate)
        self.assertEqual(json.loads((self.root/"router-previous-examples.json").read_text()), old)
        self.assertEqual(json.loads((self.root/"router-candidate-examples.json").read_text()), [])
        self.assertEqual((self.root/"router-examples.json").stat().st_mode & 0o777, 0o600)

    def test_failed_gate_keeps_active_and_candidate(self):
        old = [{"request":"讲个笑话","route":"local_chat","intent":"chat","executor":"local_9b"}]
        candidate = [{"request":"每天九点提醒我","route":"task","intent":"create","executor":"openclaw"}]
        self.write("router-examples.json", old)
        self.write("router-candidate-examples.json", candidate)
        report = self.engine.promote_if_safe(lambda examples: {
            "valid_json_rate": 1.0, "baseline_accuracy": 0.9,
            "candidate_accuracy": 1.0, "p95_seconds": 0.9,
        })
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(json.loads((self.root/"router-examples.json").read_text()), old)
        self.assertEqual(json.loads((self.root/"router-candidate-examples.json").read_text()), candidate)

    def test_rollback_restores_previous(self):
        self.write("router-examples.json", [{"request":"new","route":"home","intent":"query","executor":"openclaw"}])
        self.write("router-previous-examples.json", [{"request":"old","route":"local_chat","intent":"chat","executor":"local_9b"}])
        report = self.engine.rollback()
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(json.loads((self.root/"router-examples.json").read_text())[0]["request"], "old")


if __name__ == "__main__":
    unittest.main()
