"""Synthetic release regressions: source reports cannot become specialist memos."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "offerist" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import evidence_contract as evidence
import proposal_contract as contract
import proposal_docs as native


class SourceHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.report = Path(self.tmp.name) / "full-report.csv"
        self.report.write_text("term,weight\nfixture,1\n", encoding="utf-8")
        self.memo = Path(self.tmp.name) / "internal-note.txt"
        self.memo.write_text("INTERNAL-ONLY: interpretation for author", encoding="utf-8")
        self.sources = [self.source("report", "original", self.report, "https://files.test/reports/full.csv", True),
                        self.source("note", "internal_memo", self.memo, None, False)]
        self.review = {"section_id": "relevance", "source_ids": ["report"], "memo_ids": ["note"],
                       "status": "limited", "reason": "One comparable supplier; cannot infer the whole market.",
                       "author_checks": [self.check("report")]}
        self.proposal = {"sections": [{"id": "relevance", "analysis_links": [{"label": "Full export", "url": self.sources[0]["url"]}], "tables": []}],
                         "research_handoff": {"version": evidence.VERSION, "sources": self.sources, "sections": [self.review]}}

    def source(self, sid, kind, path, url, safe):
        return {"id": sid, "kind": kind, "local_path": str(path), "url": url, "client_safe": safe,
                "scope": "Synthetic supplier corpus, one page pair, 2026-01-01; no market extrapolation.",
                "completeness": "full", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def check(self, sid):
        return {"source_id": sid, "locator": "table 1, rows 1–2", "observation": "Verified both rows and denominator.",
                "decision": "Use the observed page comparison only; do not recommend term stuffing."}

    def errors(self, required=("relevance",)):
        return evidence.validate_handoff(self.proposal, required, client_ready=True)

    def test_full_original_with_separate_memo_and_author_inspection_releases(self):
        self.assertEqual(self.errors(), [])

    def test_five_individual_original_reports_need_five_direct_links(self):
        for index in range(1, 5):
            sid = f"report{index}"
            self.sources.append(self.source(sid, "original", self.report, f"https://files.test/reports/{index}.json", True))
            self.review["source_ids"].append(sid)
            self.review["author_checks"].append(self.check(sid))
            self.proposal["sections"][0]["analysis_links"].append({"label": sid, "url": self.sources[-1]["url"]})
        self.assertEqual(self.errors(), [])
        self.proposal["sections"][0]["analysis_links"].pop()
        self.assertTrue(any("Link each complete" in e["message"] for e in self.errors()))

    def test_memo_masquerading_as_full_results_is_rejected(self):
        self.sources[1]["url"] = "https://files.test/internal-note"
        self.proposal["sections"][0]["analysis_links"] = [{"label": "Full results", "url": self.sources[1]["url"]}]
        self.assertTrue(any(e["field"] == "client_source" for e in self.errors()))

    def test_memo_cannot_be_declared_client_safe(self):
        self.sources[1]["client_safe"] = True
        self.assertTrue(any("never client" in e["message"] for e in self.errors()))

    def test_no_author_check_is_not_source_review(self):
        self.review["author_checks"] = []
        self.assertTrue(any("cover every" in e["message"] for e in self.errors()))

    def test_missing_internal_memo_fails_handoff(self):
        self.review["memo_ids"] = []
        self.assertTrue(any("internal specialist memo" in e["message"] for e in self.errors()))

    def test_reviewed_file_change_invalidates_handoff(self):
        self.report.write_text("replacement report", encoding="utf-8")
        self.assertTrue(any("Source changed" in e["message"] for e in self.errors()))

    def test_summary_without_original_lineage_is_rejected(self):
        self.sources[0]["kind"] = "derived"
        self.assertTrue(any("traceable original" in e["message"] for e in self.errors()))

    def test_derived_summary_still_requires_original_checks_and_links(self):
        self.sources.append(self.source("summary", "derived", self.report, "https://files.test/summary", True))
        self.sources[-1]["parent_ids"] = ["report"]
        self.review["source_ids"] = ["summary"]
        self.review["author_checks"] = [self.check("summary")]
        self.assertTrue(any("cover every" in e["message"] for e in self.errors()))
        self.review["author_checks"].append(self.check("report"))
        self.assertEqual(self.errors(), [])

    def test_broken_or_cyclic_derived_lineage_is_not_hidden_by_one_good_parent(self):
        self.sources.append(self.source("summary", "derived", self.report, "https://files.test/summary", True))
        self.sources[-1]["parent_ids"] = ["report", "missing"]
        self.assertTrue(any("missing parents" in e["message"] for e in self.errors()))
        self.sources[-1]["parent_ids"] = ["report", "summary"]
        self.assertTrue(any("cycle" in e["message"] for e in self.errors()))

    def test_unregistered_analysis_link_is_rejected(self):
        self.proposal["sections"][0]["analysis_links"].append({"label": "Undeclared summary", "url": "https://files.test/unknown"})
        self.assertTrue(any("unclassified" in e["message"] for e in self.errors()))

    def test_honestly_bounded_export_is_allowed_with_explicit_limits(self):
        self.sources[0]["completeness"] = "bounded"
        self.sources[0]["scope"] = "Search export: 100 of 200 matching rows, page 1 only, regional desktop sample; not full demand."
        self.assertEqual(self.errors(), [])
        self.review["reason"] = ""
        self.assertTrue(self.errors())

    def test_unavailable_forecast_needs_reason_and_unknowns_not_invented_numbers(self):
        self.proposal["sections"][0] = {"id": "forecast", "analysis_links": [], "tables": [{"rows": [["month 1", {"value": None, "reason": "No verified baseline"}]]}]}
        self.review.update(section_id="forecast", status="unavailable", source_ids=[], author_checks=[], reason="No target analytics access and no validated baseline; quantitative forecast not prepared.")
        self.assertEqual(self.errors(("forecast",)), [])
        self.proposal["sections"][0]["tables"][0]["rows"][0][1] = 200
        self.assertTrue(any("arbitrary scenario" in e["message"] for e in self.errors(("forecast",))))
        self.proposal["sections"][0]["visual"] = {"type": "line", "labels": ["baseline", "target"], "values": [100, 200], "unit": "index"}
        self.assertTrue(any("quantitative forecast chart" in e["message"] for e in self.errors(("forecast",))))

    def test_private_primary_can_use_complete_explicitly_sanitized_client_copy(self):
        self.sources[0].update(client_safe=False, url=None, client_copy_id="sanitized")
        self.sources.append(self.source("sanitized", "derived", self.report, "https://files.test/sanitized.csv", True))
        self.sources[-1].update(parent_ids=["report"], redaction_note="Removed account email only; all analytical rows preserved.")
        self.review["author_checks"].append(self.check("sanitized"))
        self.proposal["sections"][0]["analysis_links"][0]["url"] = self.sources[-1]["url"]
        self.assertEqual(self.errors(), [])

    def baseline_forecast(self):
        self.proposal["sections"][0] = {"id": "forecast", "analysis_links": self.proposal["sections"][0]["analysis_links"],
            "tables": [{"id": "forecast", "rows": [["Observed baseline", 100], ["Future", {"value": None, "reason": "Uncalibrated effect"}]]}],
            "visual": {"type": "image", "path": "/synthetic/baseline.png", "alt": "Measured baseline"}}
        self.review.update(section_id="forecast", status="unavailable", baseline_rows={"forecast": [0]}, visual_role="measured_baseline",
                           reason="Baseline measured; future effect not calibrated.")

    def test_explicit_measured_baseline_is_allowed_beside_unavailable_forecast(self):
        self.baseline_forecast()
        self.assertEqual(self.errors(("forecast",)), [])

    def test_unmarked_quantitative_forecast_is_still_rejected(self):
        self.baseline_forecast()
        self.review.pop("baseline_rows")
        self.review.pop("visual_role")
        self.assertTrue(any(e["field"] == "tables" for e in self.errors(("forecast",))))
        self.assertTrue(any(e["field"] == "visual" for e in self.errors(("forecast",))))

    def test_future_numbers_outside_declared_baseline_rows_are_rejected(self):
        self.baseline_forecast()
        self.proposal["sections"][0]["tables"][0]["rows"][1][1] = 200
        self.assertTrue(any("arbitrary scenario" in e["message"] for e in self.errors(("forecast",))))

    def test_baseline_label_requires_original_and_author_inspection(self):
        self.baseline_forecast()
        self.review["source_ids"] = []
        self.review["author_checks"] = []
        self.assertTrue(any("label alone is insufficient" in e["message"] for e in self.errors(("forecast",))))
        self.review["source_ids"] = ["report"]
        self.assertTrue(any("label alone is insufficient" in e["message"] for e in self.errors(("forecast",))))

    def test_unsafe_link_in_table_not_only_analysis_links_is_rejected(self):
        self.sources[1]["url"] = "https://files.test/private-note"
        self.proposal["sections"][0]["tables"] = [{"rows": [["https://files.test/private-note"]]}]
        self.assertTrue(any(e["field"] == "client_source" for e in self.errors()))

    def test_drive_alias_cannot_bypass_internal_memo_classification(self):
        self.sources[1]["url"] = "https://drive.google.com/file/d/privateNote123/view?usp=drivesdk"
        self.proposal["sections"][0]["tables"] = [{"rows": [["https://drive.google.com/open?id=privateNote123"]]}]
        self.assertTrue(any(e["field"] == "client_source" for e in self.errors()))

    def test_prose_punctuation_does_not_hide_internal_memo_url(self):
        self.sources[1]["url"] = "https://files.test/internal-note"
        self.proposal["sections"][0]["details"] = [{"type": "paragraph", "text": "See https://files.test/internal-note."}]
        self.assertTrue(any(e["field"] == "client_source" for e in self.errors()))

    def test_drive_alias_of_safe_original_is_accepted_without_rewriting_url(self):
        self.sources[0]["url"] = "https://drive.google.com/file/d/originalReport123/view?usp=drivesdk"
        alias = "https://drive.google.com/open?id=originalReport123&authuser=0"
        self.proposal["sections"][0]["analysis_links"][0]["url"] = alias
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.proposal["sections"][0]["analysis_links"][0]["url"], alias)

    def test_workspace_account_scoped_alias_preserves_file_identity(self):
        self.sources[1]["url"] = "https://docs.google.com/document/d/privateNote123/edit"
        self.proposal["sections"][0]["tables"] = [{"rows": [["https://docs.google.com/document/u/0/d/privateNote123/edit"]]}]
        self.assertTrue(any(e["field"] == "client_source" for e in self.errors()))

    def test_unavailable_section_still_rejects_unclassified_client_analysis_link(self):
        self.review.update(status="unavailable", source_ids=[], author_checks=[], reason="Source unavailable.")
        self.proposal["sections"][0]["analysis_links"] = [{"label": "Full results", "url": "https://files.test/unclassified-note"}]
        self.assertTrue(any("unclassified" in e["message"] for e in self.errors()))

    def test_two_drive_aliases_cannot_declare_same_memo_as_original(self):
        self.sources[0]["url"] = "https://docs.google.com/document/d/sharedFile123/edit"
        self.sources[1]["url"] = "https://drive.google.com/open?id=sharedFile123"
        self.assertTrue(any("two evidence identities" in e["message"] for e in self.errors()))

    def test_legacy_draft_allowed_but_client_release_requires_manifest(self):
        self.proposal.pop("research_handoff")
        self.assertEqual(evidence.validate_handoff(self.proposal, ["relevance"]), [])
        self.assertTrue(self.errors())

    def test_init_includes_new_contract_and_main_validator_enforces_it(self):
        proposal = contract.init_proposal()
        self.assertEqual(proposal["research_handoff"]["version"], evidence.VERSION)
        errors = contract.validate(proposal, client_ready=True)
        self.assertTrue(any(e["section"] == "forecast" and e["field"] == "research_handoff" for e in errors))

    def test_native_compiler_keeps_author_metadata_out_of_client_content(self):
        template = copy.deepcopy(contract.load_template())
        definition = next(s for s in template["sections"] if s["id"] == "relevance")
        definition.update(tables=[], required_fields=["intro", "takeaway"], visual_required=False)
        template["sections"] = [definition]
        record = self.proposal["sections"][0]
        record.update(intro="Мы проверили исходный отчёт.", takeaway="Подтверждена ограниченная выборка.",
                      state={"value": None, "reason": "Insufficient sample."}, opportunity={"value": None, "reason": "Insufficient sample."},
                      action="Проверим дополнительные сопоставимые страницы.", source="Original report and direct author inspection.",
                      source_url=None, visual=None, visual_explanation="", details=[
                          {"type": "paragraph", "text": "Observed page comparison; market extrapolation is unavailable."},
                          {"type": "example", "title": "Observed comparison", "observation": "One page pair.",
                           "implication": "Limited inference.", "action": "Extend relevant peers.", "links": record["analysis_links"]}])
        self.proposal.update(schema_version=contract.SCHEMA_VERSION, mode="client",
                             client={"name": "Synthetic fixture", "domain": "https://supplier.test", "date": "2026-01-01"})
        specification = native.specification(self.proposal, template)
        rendered = json.dumps(specification, ensure_ascii=False)
        self.assertIn(self.sources[0]["url"], rendered)
        self.assertNotIn(str(self.memo), rendered)
        self.assertNotIn("INTERNAL-ONLY", rendered)
        self.assertNotIn(self.review["author_checks"][0]["decision"], rendered)
        self.sources[1]["url"] = "https://files.test/private-note"
        record["analysis_links"].append({"label": "Full analysis", "url": self.sources[1]["url"]})
        with self.assertRaises(ValueError):
            native.specification(self.proposal, template)


if __name__ == "__main__":
    unittest.main()
