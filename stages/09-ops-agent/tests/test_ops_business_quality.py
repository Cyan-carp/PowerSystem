import copy
import pytest
from ops_agent.business_quality import build_quality, finalize_diagnosis, quality_summary
from ops_agent.runtime_reviews import licensed_summary, validate_payload


def test_quality_persists_and_security_summary_binds_hash_without_raw_business_values():
    result={'tool_calls':[{'name':'get_device','status':'ok','arguments':{'device_id':2},'collected_at':'2026-10-19T05:00:00Z','result':{'id':2,'device_code':'INV-PRIVATE'}}]}
    finalize_diagnosis(result,'查询设备')
    q=result['business_quality']
    payload=licensed_summary({'status':'succeeded',**result})
    assert payload['business_quality']==quality_summary(q)
    assert 'INV-PRIVATE' not in str(payload) and 'summary' not in payload['business_quality']
    assert validate_payload(payload)==payload
    tampered=copy.deepcopy(q);tampered['facts'][0]['text']='invented'
    with pytest.raises(ValueError): licensed_summary({'status':'succeeded','business_quality':tampered})
    bad=copy.deepcopy(payload);bad['business_quality']['raw_prose']='must not leave'
    with pytest.raises(ValueError): validate_payload(bad)
