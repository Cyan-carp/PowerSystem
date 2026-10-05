"""Finalize a saved real-search batch after claim-by-claim human source review."""
import argparse
import json
from pathlib import Path
from source_review import review_items, review_passes, protocol_passes


def main(run_dir, review_file=None, mode='human', authorization=''):
    summary = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
    def reject(reason):
        (run_dir / 'source-review-result.json').write_text(json.dumps({
            'accepted': False, 'run': run_dir.name, 'basis': reason}, ensure_ascii=False, indent=2), encoding='utf-8')
        for item in summary:
            item.update(passed=False, source_review='not_accepted')
        (run_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        return False
    if len(summary) != 5 or not all(item.get('protocol_passed') is True for item in summary):
        return reject('five protocol successes required')
    expected = []; records = []
    for index in range(5):
        record = json.loads((run_dir / 'protocol-success' / f'answer-{index}.json').read_text(encoding='utf-8'))
        if record.get('protocol_passed') is not True:
            return reject('saved answer protocol failed')
        if mode == 'development-assumed' and not protocol_passes(record['response']):
            return reject('saved answer no longer meets development acceptance')
        records.append(record)
        expected.extend(review_items(index, record['response']))
    if mode == 'development-assumed':
        if not authorization.strip(): return reject('explicit owner authorization required')
        accepted = bool(expected)
        state = 'assumed_pass_development'
        basis = 'Owner-authorized development acceptance; professional source and equipment applicability review deferred.'
        (run_dir / 'source-review-deferred.json').write_text(json.dumps({
            'authorization': authorization, 'professional_review': 'pending', 'claims': expected,
            'supported': None, 'knowledge_followup': 'pending'}, ensure_ascii=False, indent=2), encoding='utf-8')
    elif mode == 'human' and review_file is not None:
        reviewed = json.loads(review_file.read_text(encoding='utf-8'))
        accepted = bool(expected) and isinstance(reviewed, list) and review_passes(expected, reviewed)
        state = 'human_passed' if accepted else 'human_failed'
        basis = 'exact claim and source digest; human source-support review'
    else:
        return reject('human review file or supported review mode required')
    (run_dir / 'source-review-result.json').write_text(json.dumps({
        'accepted': accepted, 'reviewed_claims': len(expected), 'run': run_dir.name,
        'basis': basis, 'source_review': state, 'authorization': authorization,
        'professional_review': 'pending' if mode == 'development-assumed' else state,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    for index, record in enumerate(records):
        record.update(passed=accepted, source_review=state)
        (run_dir / 'protocol-success' / f'answer-{index}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        summary[index].update(passed=accepted, source_review=state)
    (run_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return accepted


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--review', type=Path)
    parser.add_argument('--mode', choices=('human', 'development-assumed'), default='human')
    parser.add_argument('--authorization', default='')
    args = parser.parse_args()
    raise SystemExit(0 if main(args.run, args.review, args.mode, args.authorization) else 1)
