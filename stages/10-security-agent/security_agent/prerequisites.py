"""A model opinion cannot approve missing or failed program prerequisites."""
from review_contract import M4_SERVER_POLICY, Opinion


def enforce_prerequisites(opinion, request):
    if request.policy_version != M4_SERVER_POLICY:
        return opinion
    # The signed runtime controller stores common prerequisites in P{step_no}. Never
    # infer identity from actor.role, asset.verified or another item's facts.
    expected_ref = 'P' + str(request.step_no)
    evidence = next((item for item in request.evidence if item.ref == expected_ref), None)
    facts = {item.key: item.value for item in evidence.facts} if evidence else {}
    required = ['identity_verified', 'ownership_verified', 'tool_registered',
                'args_validated', 'audit_ready', 'target_matches', 'evidence_sufficient']
    if request.asset is not None:
        required.append('asset_verified')
    if request.kind == 'data_egress':
        required += ['egress_permitted', 'payload_bound', 'registry_verified']
    denied = [key for key in required if key != 'evidence_sufficient' and facts.get(key) is False]
    unknown = [key for key in required if facts.get(key) is not True]
    if not unknown or (not denied and opinion.decision != 'allow'):
        return opinion
    return Opinion(decision='deny' if denied else 'needs_clarification',
        reason=('程序核验条件明确不通过，拒绝：' if denied else '程序核验条件缺失或不足，需要澄清：') + '、'.join(denied or unknown),
        rule_hits=['SEC-R01' if 'identity_verified' in (denied or unknown) or 'ownership_verified' in (denied or unknown) else 'SEC-R07'],
        evidence_refs=[expected_ref] if evidence else [])
