import pytest
from ops_agent.platform import MODELS, business_route
from ops_agent.executor import MANIFEST


def test_energy_is_fixed_read_only_route_with_intersection_filters():
    args=MODELS['get_energy_report'].model_validate({'device_id':28,'start_date':'2026-10-17','end_date':'2026-10-24','scope':'business','station_code':'PV-01'}).model_dump(exclude_none=True)
    assert business_route('get_energy_report',args)==('/api/v1/reports/energy',args)
    m=MANIFEST['platform.get_energy_report']
    assert m['risk_class']=='R0' and m['approval_policy']=='none'
    assert m['required_roles']==['operator','admin']


@pytest.mark.parametrize('patch',[{'start_date':'2026-02-30'}, {'end_date':'2026-10-17'}, {'end_date':'2026-11-18'}, {'device_id':True}, {'device_id':0}, {'command':'SELECT 1'}, {'scope':'ops'}])
def test_energy_rejects_bad_dates_targets_or_arbitrary_commands(patch):
    args={'device_id':28,'start_date':'2026-10-17','end_date':'2026-10-24',**patch}
    with pytest.raises(ValueError):MODELS['get_energy_report'].model_validate(args)
