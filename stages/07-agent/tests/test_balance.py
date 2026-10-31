"""余额查询必须区分三件事：查不到额度、额度用完、Key 无效。

把「供应商不支持额度查询」显示成 0 元，比不显示更糟——值班员会去充值一个没问题的账号。
"""
import unittest

import httpx

from agent.balance import balance_endpoint, fetch_balance

BASE = "https://api.deepseek.com"


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class BalanceEndpointTest(unittest.TestCase):
    def test_only_official_host_has_balance_endpoint(self):
        self.assertEqual(balance_endpoint(BASE), "https://api.deepseek.com/user/balance")
        # 官方 base 允许带 /v1，但余额端点在站点根下，不能拼成 /v1/user/balance
        self.assertEqual(balance_endpoint(BASE + "/v1"), "https://api.deepseek.com/user/balance")
        self.assertEqual(balance_endpoint("https://hgapi.dieqiyun.top/v1"), "")
        self.assertEqual(balance_endpoint("http://127.0.0.1:18103/v1"), "")


class BalanceFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_parses_amounts_as_strings(self):
        def handler(request):
            self.assertEqual(request.headers.get("authorization"), "Bearer sk-test")
            self.assertEqual(str(request.url), "https://api.deepseek.com/user/balance")
            return httpx.Response(200, json={"is_available": True, "balance_infos": [
                {"currency": "CNY", "total_balance": "110.00",
                 "granted_balance": "10.00", "topped_up_balance": "100.00"},
            ]})

        async with client_for(handler) as client:
            result = await fetch_balance(client, channel="interpret", label="告警解读",
                                         base_url=BASE, model="deepseek-flash", api_key="sk-test")

        self.assertTrue(result["supported"])
        self.assertTrue(result["is_available"])
        self.assertEqual(result["error"], "")
        self.assertEqual(result["balances"][0]["total_balance"], "110.00")
        self.assertEqual(result["balances"][0]["currency"], "CNY")

    async def test_unsupported_provider_is_not_zero_balance(self):
        async with client_for(lambda request: httpx.Response(500)) as client:
            result = await fetch_balance(client, channel="assistant", label="主助手",
                                         base_url="https://hgapi.dieqiyun.top/v1",
                                         model="deepseek-v4.1-flash", api_key="sk-test")

        self.assertFalse(result["supported"])
        self.assertEqual(result["error"], "unsupported_provider")
        self.assertIsNone(result["is_available"])
        self.assertEqual(result["balances"], [])

    async def test_auth_failure_is_not_quota_exhaustion(self):
        async with client_for(lambda request: httpx.Response(401)) as client:
            result = await fetch_balance(client, channel="interpret", label="告警解读",
                                         base_url=BASE, model="deepseek-flash", api_key="sk-bad")
        self.assertEqual(result["error"], "auth_failed")
        self.assertIsNone(result["is_available"])

        async with client_for(lambda request: httpx.Response(
                200, json={"is_available": False, "balance_infos": []})) as client:
            result = await fetch_balance(client, channel="interpret", label="告警解读",
                                         base_url=BASE, model="deepseek-flash", api_key="sk-test")
        self.assertFalse(result["is_available"])
        self.assertEqual(result["error"], "")

    async def test_missing_key_does_not_call_vendor(self):
        def handler(request):  # pragma: no cover - 走到这里就是 bug
            raise AssertionError("不应在缺少 Key 时请求供应商")

        async with client_for(handler) as client:
            result = await fetch_balance(client, channel="interpret", label="告警解读",
                                         base_url=BASE, model="deepseek-flash", api_key="")
        self.assertEqual(result["error"], "missing_key")

    async def test_network_failure_is_reported_not_swallowed(self):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        async with client_for(handler) as client:
            result = await fetch_balance(client, channel="interpret", label="告警解读",
                                         base_url=BASE, model="deepseek-flash", api_key="sk-test")
        self.assertEqual(result["error"], "unreachable")


if __name__ == "__main__":
    unittest.main()
