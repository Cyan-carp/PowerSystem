import sys
import runpy
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
from source_review import review_items, review_passes

mixed_answer_checks = runpy.run_path(str(Path(__file__).resolve().parents[1] /
    'deploy' / 'acceptance-v2m3.py'))['mixed_answer_checks']


class SourceReviewTest(unittest.TestCase):
    def test_mixed_acceptance_rejects_degraded_and_missing_source(self):
        result = {'status':'degraded', 'conclusion':None, 'suggestions':[], 'evidence':[]}
        self.assertEqual(mixed_answer_checks(result), (False, False, False))
        result = {'status':'answered', 'conclusion':{'text':'风险', 'evidence_ids':['E1']},
                  'suggestions':[], 'evidence':[
                      {'id':'E1','kind':'business','tool':'get_prediction','status':'ok'},
                      {'id':'E2','kind':'document','tool':'search_knowledge','status':'ok'}]}
        self.assertEqual(mixed_answer_checks(result), (True, True, False))
        result['suggestions'] = [{'text':'人工查看说明', 'evidence_ids':['E2']}]
        self.assertEqual(mixed_answer_checks(result), (True, True, True))

    def test_manual_gate_is_bound_to_exact_claim_and_source(self):
        response = {
            'conclusion': {'text': '资料提到覆冰检测', 'evidence_ids': ['E1']},
            'suggestions': [{'text': '人工核查厂家手册', 'evidence_ids': ['E1']}],
            'evidence': [{'id': 'E1', 'status': 'ok', 'source': 'https://docs.example.org/ice',
                          'collected_at': '2026-10-03T00:00:00Z',
                          'data': {'title': '技术资料', 'summary': '讨论覆冰检测'}}],
        }
        expected = review_items(0, response)
        self.assertFalse(review_passes(expected, expected))
        reviewed = [dict(item, supported=True, review_note='原文直接支持该说法', reviewer='operator')
                    for item in expected]
        self.assertTrue(review_passes(expected, reviewed))
        edited = {**response, 'suggestions': [{'text': '执行断电复位', 'evidence_ids': ['E1']}]}
        self.assertFalse(review_passes(review_items(0, edited), reviewed))
        changed_source = {**response, 'evidence': [{**response['evidence'][0],
                          'data': {'title': '技术资料', 'summary': '与覆冰无关'}}]}
        self.assertFalse(review_passes(review_items(0, changed_source), reviewed))
        self.assertFalse(review_passes(expected, reviewed[:1]))


if __name__ == '__main__':
    unittest.main()
