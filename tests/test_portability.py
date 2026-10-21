import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortableInstall(unittest.TestCase):
    def test_all_skills_keep_shared_resources(self):
        installer = load("install_skills")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent-skills"
            result = installer.install(target)
            self.assertEqual(len(result["skills"]), 25)
            bundle = Path(result["bundle"])
            self.assertTrue((bundle / "scripts/workflow.py").is_file())
            self.assertTrue((target / "offerist-quick/assets/reference-proposal.docx").is_file())
            router = (target / "offerist/SKILL.md").read_text(encoding="utf-8")
            self.assertIn((bundle / "skills/offerist-assistant/SKILL.md").as_posix(), router)
            self.assertIn((bundle / "docs/workflow.md").as_posix(), router)

    def test_existing_skill_is_preserved_before_writes(self):
        installer = load("install_skills")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            existing = target / "offerist-assistant"
            existing.mkdir()
            marker = existing / "SKILL.md"
            marker.write_text("user changes", encoding="utf-8")
            with self.assertRaises(ValueError):
                installer.install(target, ["offerist-assistant", "offerist-brief"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "user changes")
            self.assertFalse((target / "offerist-brief").exists())
            self.assertFalse((target / ".offerist-bundles").exists())

    def test_dry_run_and_unknown_selection_write_nothing(self):
        installer = load("install_skills")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "new"
            installer.install(target, ["offerist-quick"], dry_run=True)
            self.assertFalse(target.exists())
            with self.assertRaises(ValueError):
                installer.install(target, ["../private"])
            self.assertFalse(target.exists())


class ExampleArithmetic(unittest.TestCase):
    def test_traffic_groups_and_query_denominators(self):
        with (ROOT / "examples/demo/pages.csv").open(encoding="utf-8") as f:
            pages = list(csv.DictReader(f))
        self.assertEqual(len({p["url"] for p in pages}), 6)
        self.assertEqual(sum(int(p["yandex_visits"]) for p in pages), 340)
        self.assertEqual(sum(int(p["google_visits"]) for p in pages), 235)
        groups = {g: sum(int(p["yandex_visits"]) + int(p["google_visits"]) for p in pages if p["group"] == g)
                  for g in ("strong", "quick", "weak", "not_applicable")}
        self.assertEqual(groups, {"strong": 360, "quick": 205, "weak": 0, "not_applicable": 10})
        with (ROOT / "examples/demo/queries.csv").open(encoding="utf-8") as f:
            queries = list(csv.DictReader(f))
        monitored = [q for q in queries if q["monitored"] == "yes"]
        self.assertEqual(len(queries), 8)
        self.assertEqual(len(monitored), 6)
        self.assertEqual(sum(bool(q["url"]) for q in queries), 7)
        self.assertEqual(sum(int(q["yandex_rank"]) <= 10 for q in monitored), 3)
        self.assertEqual(sum(int(q["google_rank"]) <= 10 for q in monitored), 1)

    def test_forecast_distinguishes_monthly_and_cumulative(self):
        generator = load("build_demo")
        rows = generator.forecast_rows()
        self.assertEqual(len(rows), 18)
        work = [r for r in rows if r["scenario"] == "рабочий"]
        self.assertEqual([r["additional_visits"] for r in work], [0, 24.5, 49, 98, 98, 98])
        self.assertEqual(work[-1]["cumulative_additional_visits"], 367.5)
        self.assertEqual(work[-1]["total_visits"], 673)
        with (ROOT / "examples/demo/forecast.csv").open(encoding="utf-8") as f:
            saved = list(csv.DictReader(f))
        self.assertEqual([float(r["cumulative_additional_visits"]) for r in saved], [r["cumulative_additional_visits"] for r in rows])


if __name__ == "__main__":
    unittest.main()
