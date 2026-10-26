"""Drafts can await evidence; final audit sections need bounded state assessments."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/offerist/scripts'))
import proposal_contract as contract


class StateScores(unittest.TestCase):
    def setUp(self):
        self.template = copy.deepcopy(contract.load_template())
        definition = next(section for section in self.template['sections'] if section['id'] == 'semantic')
        definition.update(tables=[], visual_required=False,
                          required_fields=['intro', 'takeaway', 'state', 'opportunity'])
        self.template['sections'] = [definition]
        self.proposal = contract.init_proposal()
        self.record = next(section for section in self.proposal['sections'] if section['id'] == 'semantic')
        self.proposal.update(mode='client', sections=[self.record],
                             client={'name': 'Fixture', 'domain': 'https://supplier.test', 'date': '2026-01-01'})
        self.record.update(intro='Мы проверили доступные страницы.', takeaway='Наблюдения относятся к проверенной части.',
                           state={'value': None, 'reason': 'Awaiting the requested source data.'},
                           opportunity={'value': None, 'reason': 'Scope of further work remains open.'})

    def score_errors(self, ready, field='state'):
        errors = contract.validate(self.proposal, self.template, client_ready=ready)
        return [row for row in errors if row['section'] == 'semantic' and row['field'].startswith(field)]

    def test_draft_can_await_user_data_without_an_invented_score(self):
        self.assertEqual(self.score_errors(False), [])

    def test_missing_final_state_is_rejected_even_with_a_reason(self):
        self.assertTrue(self.score_errors(True))

    def test_expert_state_with_declared_observation_scope_is_allowed(self):
        self.record['state'] = {'value': 6, 'reason': 'Expert grade of inspected commercial pages: existing categories cover the observed tasks; filter descriptions need work. Uninspected URLs are outside this grade.'}
        self.assertEqual(self.score_errors(True), [])

    def test_source_unavailable_is_not_an_exemption_from_final_state(self):
        self.proposal['research_handoff'] = {'version': 'offerist-evidence-v1', 'sources': [],
            'sections': [{'section_id': 'semantic', 'status': 'unavailable', 'reason': 'API access denied.',
                          'source_ids': [], 'memo_ids': [], 'author_checks': []}]}
        self.assertTrue(self.score_errors(True))

    def test_unknown_opportunity_is_not_replaced_with_a_fabricated_number(self):
        self.record['state'] = {'value': 6, 'reason': 'Expert grade of the observed interface; statistical conversion is unavailable.'}
        self.assertEqual(self.score_errors(True, 'opportunity'), [])


if __name__ == '__main__':
    unittest.main()
