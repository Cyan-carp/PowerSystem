"""Offline human review gate for the exact web claims returned in one run."""
import hashlib
import json


def review_items(index, response):
    evidence = {item['id']: item for item in response['evidence'] if item['status'] == 'ok'}
    claims = ([response['conclusion']] if response['conclusion'] else []) + response['suggestions']
    items = []
    for number, claim in enumerate(claims):
        sources = [{key: evidence[ref].get(key) for key in ('id', 'source', 'collected_at', 'data')}
                   for ref in claim['evidence_ids'] if ref in evidence]
        payload = {'case': index, 'claim': number, 'text': claim['text'], 'sources': sources}
        if 'equipment_operation' in claim:
            payload['equipment_operation'] = claim['equipment_operation']
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        items.append({'case': index, 'claim': number, 'sha256': digest, 'text': claim['text'],
                      'sources': [{'id': item['id'], 'url': item['source']} for item in sources],
                      'supported': None, 'review_note': '', 'reviewer': ''})
    return items


def review_passes(expected, reviewed):
    by_key = {(row.get('case'), row.get('claim')): row for row in reviewed if isinstance(row, dict)}
    if len(by_key) != len(expected) or len(reviewed) != len(expected):
        return False
    for item in expected:
        row = by_key.get((item['case'], item['claim']))
        if (not row or row.get('sha256') != item['sha256'] or row.get('supported') is not True
                or not str(row.get('review_note', '')).strip()
                or not str(row.get('reviewer', '')).strip()):
            return False
    return True


def protocol_passes(response):
    """Current development acceptance still requires real, traceable answers."""
    if (response.get('status') != 'answered' or response.get('web_status') != 'hit'
            or response.get('knowledge_status') != 'miss' or response.get('miss_record_status') != 'recorded'
            or not response.get('conclusion') or not response.get('suggestions') or not response.get('notices')):
        return False
    available = {item['id'] for item in response.get('evidence', [])
                 if item.get('status') == 'ok' and item.get('kind') == 'web'}
    claims = [response['conclusion'], *response['suggestions']]
    return (bool(available) and all(claim.get('text') and claim.get('evidence_ids')
            and set(claim['evidence_ids']) <= available for claim in claims)
            and all(type(claim.get('equipment_operation')) is bool for claim in response['suggestions']))
