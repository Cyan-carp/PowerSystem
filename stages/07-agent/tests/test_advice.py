import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from agent.advice import equipment_operation, suggestion
from agent.schemas import Claim, ModelAnswer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy'))
from source_review import protocol_passes, review_items


class AdviceTest(unittest.TestCase):
    def test_physical_and_unknown_actions_warn_but_reading_documents_does_not(self):
        for text in ('断电后复位', '测量绝缘', '检查设备接线', '核对手册后更换部件', '排查异常',
                     '查看日志并检查设备冷却情况', '核对手册后测试逆变器', '查看日志并打开配电柜',
                     '查阅资料之后进入运行模式'):
            self.assertTrue(equipment_operation(text), text)
        for text in ('查阅厂家手册', '查看告警记录', '联系专业工程师', '确认告警并标记已读'):
            self.assertFalse(equipment_operation(text), text)
        result = suggestion(Claim(text='建议停机检查', evidence_ids=['E1']))
        self.assertTrue(result.equipment_operation)
        self.assertEqual(result.evidence_ids, ['E1'])
        # Metadata is not a model-controlled channel.
        with self.assertRaises(ValueError):
            ModelAnswer.model_validate({'conclusion': {'text': '资料', 'evidence_ids': ['E1']},
                'suggestions': [{'text': '停机', 'evidence_ids': ['E1'], 'equipment_operation': False}], 'limitations': []})

    def test_development_review_records_deferral_without_fabricating_human_review(self):
        finalize = runpy.run_path(str(ROOT / 'deploy/finalize-bocha-review.py'))['main']
        response = {'status': 'answered', 'web_status': 'hit', 'knowledge_status': 'miss',
            'miss_record_status': 'recorded', 'notices': ['本地未命中'],
            'conclusion': {'text': '公开资料提供排查参考', 'evidence_ids': ['E1']},
            'suggestions': [{'text': '专业人员核查接线', 'evidence_ids': ['E1'], 'equipment_operation': True}],
            'evidence': [{'id': 'E1', 'status': 'ok', 'kind': 'web', 'source': 'https://docs.example.org/guide',
                'collected_at': '2026-10-04T00:00:00Z', 'data': {'summary': '核查接线'}}]}
        self.assertTrue(protocol_passes(response))
        self.assertFalse(protocol_passes({**response, 'miss_record_status': 'write_failed'}))
        self.assertFalse(protocol_passes({**response, 'status': 'degraded'}))
        changed = {**response, 'suggestions': [{**response['suggestions'][0], 'equipment_operation': False}]}
        self.assertNotEqual(review_items(0, response)[1]['sha256'], review_items(0, changed)[1]['sha256'])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); (folder / 'protocol-success').mkdir()
            (folder / 'summary.json').write_text(json.dumps([{'protocol_passed': True, 'passed': False}] * 5), encoding='utf-8')
            for index in range(5):
                (folder / 'protocol-success' / f'answer-{index}.json').write_text(json.dumps({'protocol_passed': True, 'response': response}), encoding='utf-8')
            self.assertFalse(finalize(folder, mode='development-assumed'))
            self.assertTrue(finalize(folder, mode='development-assumed', authorization='项目所有者明确授权开发阶段暂缓专业审核'))
            result = json.loads((folder / 'source-review-deferred.json').read_text(encoding='utf-8'))
            self.assertIsNone(result['supported'])
            self.assertEqual(result['professional_review'], 'pending')
            self.assertTrue(all(row['passed'] for row in json.loads((folder / 'summary.json').read_text(encoding='utf-8'))))
            record = json.loads((folder / 'protocol-success/answer-0.json').read_text(encoding='utf-8'))
            record['response']['miss_record_status'] = 'write_failed'
            (folder / 'protocol-success/answer-0.json').write_text(json.dumps(record), encoding='utf-8')
            self.assertFalse(finalize(folder, mode='development-assumed', authorization='owner'))
            self.assertFalse(json.loads((folder / 'source-review-result.json').read_text(encoding='utf-8'))['accepted'])

    def test_gap_summary_preserves_sources_and_pending_knowledge_followup(self):
        summarize = runpy.run_path(str(ROOT / 'scripts/knowledge-misses.py'))['summarize']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {'normalized_question': '风机过热怎么排查', 'at': '2026-10-04T00:00:00Z', 'reason': 'miss',
                'request_id': 'request', 'web_status': 'hit', 'sources': ['https://docs.example.org/heat'],
                'source_details': [{'title': '公开资料', 'url': 'https://docs.example.org/heat', 'summary': '过热排查参考'}]}
            (root / 'misses-20261004.jsonl').write_text(json.dumps(row, ensure_ascii=False) + '\n', encoding='utf-8')
            result = summarize(root)['questions'][0]
            self.assertEqual(result['knowledge_followup'], 'pending')
            self.assertEqual(result['sources'], row['sources'])
            self.assertEqual(result['source_details'], row['source_details'])
            self.assertEqual(result['request_ids'], ['request'])
