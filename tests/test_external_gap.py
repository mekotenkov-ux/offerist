"""Synthetic regressions for own-only and sitemap-only GAP claims; no network."""
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/offerist/scripts'
sys.path.insert(0, str(SCRIPTS))
import landing_cohorts
import site_gap


def fixture():
    return {
        'client_domain': 'client.test',
        'inventory': {
            'complete': True,
            'scope': dict(definition='crawl', source='saved HTML', period='2026-01-01',
                          engine='none', region='Moscow', device='desktop', segment='drivers'),
            'pages': [{'url': 'https://client.test/drivers/',
                       'canonical_url': 'https://client.test/drivers/', 'purpose': 'commercial',
                       'page_type': 'category', 'action': 'keep', 'basis': 'Matching product offering'}]},
        'competitors': [{'domain': 'peer.test', 'relevant': True}],
        'demand_review': {
            'complete': True, 'scope': 'Saved external driver queries, Moscow',
            'sources': ['own.csv', 'peer.csv'],
            'comparison': {'mode': 'search', 'peers': [
                {'domain': 'peer.test', 'source_refs': ['peer.csv'], 'complete': True}]},
            'clusters': [{'id': 'drivers', 'page_type': 'category', 'status': 'covered',
                          'basis': 'Same driver task and applicable assortment',
                          'evidence': ['own.csv:2', 'peer.csv:2'], 'source_refs': ['peer.csv'],
                          'peer_urls': ['https://peer.test/drivers/']}]},
        'landing_jobs': []}


class ExternalGap(unittest.TestCase):
    def review(self, data):
        return site_gap.summarize(data)['assessment']['demand_review']

    def test_complete_own_inventory_and_queries_do_not_complete_gap(self):
        data = fixture()
        data['demand_review'].pop('comparison')
        data['demand_review']['sources'] = ['own.csv']
        data['demand_review']['clusters'][0].pop('source_refs')
        data['demand_review']['clusters'][0].pop('peer_urls')
        result = site_gap.summarize(data)
        self.assertEqual(result['inventory']['canonical_pages_observed'], 1)
        self.assertFalse(result['assessment']['demand_review']['complete'])
        self.assertFalse(result['assessment']['whole_site_gap_complete'])

    def test_saved_external_query_comparison_can_complete_declared_scope(self):
        self.assertTrue(self.review(fixture())['complete'])

    def test_sitemap_comparison_is_structural_not_search_coverage(self):
        data = fixture()
        data['demand_review']['comparison']['mode'] = 'structure'
        data['demand_review']['sources'] = ['own.csv', 'peer.xml']
        data['demand_review']['comparison']['peers'][0]['source_refs'] = ['peer.xml']
        data['demand_review']['clusters'][0]['source_refs'] = ['peer.xml']
        review = self.review(data)
        self.assertTrue(review['structural_complete'])
        self.assertFalse(review['complete'])
        self.assertFalse(site_gap.summarize(data)['assessment']['whole_site_gap_complete'])

    def test_peer_label_without_source_bound_page_is_not_external_gap(self):
        data = fixture()
        data['demand_review']['clusters'][0]['peer_urls'] = ['https://client.test/drivers/']
        self.assertFalse(self.review(data)['complete'])

    def test_unfinished_competitor_pagination_keeps_gap_partial(self):
        data = fixture()
        data['demand_review']['comparison']['peers'][0]['complete'] = False
        self.assertFalse(self.review(data)['complete'])

    def test_open_business_question_keeps_demand_review_partial(self):
        data = fixture()
        data['demand_review']['clusters'][0]['status'] = 'needs_business_answer'
        self.assertFalse(self.review(data)['complete'])

    def test_client_cannot_be_its_own_external_competitor(self):
        data = fixture()
        data['competitors'].append({'domain': 'client.test', 'relevant': True})
        data['demand_review']['comparison']['peers'][0]['domain'] = 'client.test'
        with self.assertRaises(ValueError):
            self.review(data)

    def test_unregistered_source_does_not_validate_comparison(self):
        data = fixture()
        data['demand_review']['comparison']['peers'][0]['source_refs'] = ['not-saved.csv']
        with self.assertRaises(ValueError):
            self.review(data)

    def test_internal_cohorts_keep_their_explicit_boundary(self):
        _, summary = landing_cohorts.analyze(
            [{'url': 'https://client.test/drivers/', 'page_type': 'category'}],
            [{'url': 'https://client.test/drivers/', 'engine': 'yandex', 'top5': '8',
              'top10': '10', 'top50': '12', 'source': 'fixture', 'region': 'Moscow', 'period': '2026-01-01'}])
        self.assertEqual(summary['counts'][0]['cohort'], 'strong')
        self.assertFalse(summary['external_gap_assessed'])


if __name__ == '__main__':
    unittest.main()
