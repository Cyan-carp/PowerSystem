"""运维智能体场站/范围工具契约：schema、非法参数、参数透传、路由与分页说明。

只验证纯 schema 与路由函数，不启动服务、不连接后端、数据库或 Redis。
"""

import pytest

from ops_agent.executor import MANIFEST, SCHEMAS
from ops_agent.platform import DESCRIPTIONS, MODELS, Overview, Stations, business_route

SCOPED = ("list_devices", "list_alarms", "get_dashboard_overview")
STATION_CODE = r"^[A-Za-z0-9_-]{1,64}$"


@pytest.mark.parametrize("name", SCOPED + ("list_stations",))
def test_scope_is_required_enum_with_all_default(name):
    fields = MODELS[name].model_fields
    assert "scope" in fields
    assert fields["scope"].default == "all"
    schema = MODELS[name].model_json_schema()["properties"]["scope"]
    assert schema["enum"] == ["all", "business", "baseline"]
    assert schema["default"] == "all"


@pytest.mark.parametrize("name", SCOPED)
def test_station_code_is_optional_bounded_string(name):
    fields = MODELS[name].model_fields
    assert fields["station_code"].default is None
    schema = MODELS[name].model_json_schema()["properties"]["station_code"]
    assert schema["anyOf"][0] == {"pattern": STATION_CODE, "type": "string"}
    assert schema["anyOf"][1] == {"type": "null"}


def test_list_stations_only_takes_scope():
    assert set(Stations.model_fields) == {"scope"}
    assert set(Overview.model_fields) == {"scope", "station_code"}


@pytest.mark.parametrize("name", SCOPED + ("list_stations",))
@pytest.mark.parametrize("scope", ["fleet", "ALL", "pv", ""])
def test_invalid_scope_is_rejected(name, scope):
    with pytest.raises(ValueError):
        MODELS[name].model_validate({"scope": scope})


@pytest.mark.parametrize("name", SCOPED)
@pytest.mark.parametrize("station", ["PV 01", "pv/01", "示范园区", "", "A" * 65, "PV-01\n"])
def test_invalid_station_code_is_rejected(name, station):
    with pytest.raises(ValueError):
        MODELS[name].model_validate({"station_code": station})


@pytest.mark.parametrize("name", SCOPED)
@pytest.mark.parametrize("station", ["PV-01", "ST-01", "pv_01", "A" * 64])
def test_valid_station_code_is_accepted(name, station):
    args = MODELS[name].model_validate({"station_code": station}).model_dump(exclude_none=True)
    assert args["station_code"] == station and args["scope"] == "all"


def test_scope_and_station_are_passed_through_unchanged():
    args = MODELS["list_devices"].model_validate(
        {"scope": "business", "station_code": "PV-01", "page": 2, "page_size": 50, "keyword": "INV"}
    ).model_dump(exclude_none=True)
    assert business_route("list_devices", args) == (
        "/api/v1/devices",
        {"scope": "business", "station_code": "PV-01", "page": 2, "page_size": 50, "keyword": "INV"},
    )
    alarms = MODELS["list_alarms"].model_validate(
        {"scope": "baseline", "station_code": "ST-01", "status": "unhandled", "level": "major"}
    ).model_dump(exclude_none=True)
    assert business_route("list_alarms", alarms) == (
        "/api/v1/alarms",
        {"scope": "baseline", "station_code": "ST-01", "page": 1, "page_size": 100,
         "status": "unhandled", "level": "major"},
    )


def test_default_scope_is_forwarded_and_old_calls_still_validate():
    for name, path in (("list_devices", "/api/v1/devices"), ("list_alarms", "/api/v1/alarms")):
        args = MODELS[name].model_validate({}).model_dump(exclude_none=True)
        assert args["scope"] == "all"
        assert "station_code" not in args
        assert business_route(name, args) == (path, args)
    legacy = MODELS["list_devices"].model_validate({"keyword": "INV-1001"}).model_dump(exclude_none=True)
    assert business_route("list_devices", legacy) == (
        "/api/v1/devices",
        {"scope": "all", "page": 1, "page_size": 100, "keyword": "INV-1001"},
    )


def test_new_routes_match_backend_paths():
    assert business_route("list_stations", {"scope": "all"}) == ("/api/v1/stations", {"scope": "all"})
    assert business_route("list_stations", {"scope": "business"}) == ("/api/v1/stations", {"scope": "business"})
    assert business_route("get_dashboard_overview", {"scope": "all"}) == (
        "/api/v1/dashboard/overview",
        {"scope": "all"},
    )
    assert business_route("get_dashboard_overview", {"scope": "baseline", "station_code": "ST-01"}) == (
        "/api/v1/dashboard/overview",
        {"scope": "baseline", "station_code": "ST-01"},
    )


def test_business_route_does_not_mutate_caller_arguments():
    args = {"scope": "business", "station_code": "PV-01", "page": 1, "page_size": 100}
    snapshot = dict(args)
    business_route("list_devices", args)
    assert args == snapshot


def test_new_tools_are_registered_in_ops_executor_manifest():
    schemas = {schema["function"]["name"]: schema for schema in SCHEMAS}
    for name in ("list_stations", "get_dashboard_overview"):
        assert "platform__" + name in schemas
        entry = MANIFEST["platform." + name]
        assert entry["risk_class"] == "R0" and entry["enabled"] is True
        assert entry["args_schema"] == MODELS[name].model_json_schema()
        assert schemas["platform__" + name]["function"]["parameters"] == MODELS[name].model_json_schema()


def test_legacy_dashboard_summary_is_kept_without_parameters():
    assert "get_dashboard_summary" in MODELS
    assert MODELS["get_dashboard_summary"].model_json_schema()["properties"] == {}
    assert business_route("get_dashboard_summary", {"scope": "business"}) == ("/api/v1/dashboard/summary", {})
    assert "platform.get_dashboard_summary" in MANIFEST


@pytest.mark.parametrize("name", ("list_devices", "list_alarms"))
def test_pagination_description_does_not_claim_full_coverage(name):
    text = DESCRIPTIONS[name]
    assert "total" in text and "翻页" in text and "一页" in text
    assert "不能声称" in text
    assert "全量结论" in text


def test_overview_description_explains_partial_and_null():
    text = DESCRIPTIONS["get_dashboard_overview"]
    for token in ("partial", "expected", "registered", "observed", "null", "不等于0"):
        assert token in text
