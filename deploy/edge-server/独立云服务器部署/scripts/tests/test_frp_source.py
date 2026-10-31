"""Isolated recovery tests. No production API calls, commands, or credentials."""
import copy
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import shutil
import unittest
from uuid import uuid4
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from frp_source.auth import Challenges
from frp_source.cloud import Swas, signed_request
from frp_source.common import GuardError, NoRedirect, signature, public_ipv4
from frp_source.host import inspect_rules
from frp_source.reconcile import Reconciler

OLD, NEW, NEXT = "1.1.1.1", "8.8.8.8", "9.9.9.9"


class TestDirectory:
    """Windows sandbox's default TEMP has an incompatible private-directory ACL."""

    def __init__(self):
        self.root = Path(os.environ.get("POWERSYSTEM_FRP_TEST_ROOT", str(Path(__file__).resolve().parents[4] / "artifacts/来源自动维护-测试目录")))
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / uuid4().hex
        self.path.mkdir(mode=0o755)
        self.name = str(self.path)

    def cleanup(self):
        if self.path.resolve().parent != self.root.resolve():
            raise ValueError("test_cleanup_outside_workspace")
        shutil.rmtree(self.path)


class Authentication(unittest.TestCase):
    def setUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000
        self.key = b"isolated-test-key-do-not-use-live"
        self.path = Path(self.directory.name) / "challenges.db"
        self.auth = Challenges(self.path, "test", self.key, clock=lambda: self.now)

    def body(self, source=OLD):
        challenge = self.auth.issue(source, {"client_id": "test"})
        return {k: challenge[k] for k in ("client_id", "challenge_id", "nonce")} | {"signature": signature(self.key, challenge)}

    def test_valid_signature_consumed_once_across_reopen(self):
        body = self.body()
        self.assertEqual(self.auth.consume(OLD, body), OLD)
        reopened = Challenges(self.path, "test", self.key, clock=lambda: self.now)
        with self.assertRaisesRegex(GuardError, "expired_or_used"):
            reopened.consume(OLD, body)

    def test_wrong_signature_cannot_write_or_consume(self):
        body = self.body()
        with self.assertRaisesRegex(GuardError, "invalid_signature"):
            self.auth.consume(OLD, body | {"signature": "0" * 64})
        self.assertEqual(self.auth.consume(OLD, body), OLD)

    def test_source_is_bound_to_authenticated_challenge(self):
        body = self.body()
        with self.assertRaisesRegex(GuardError, "source_mismatch"):
            self.auth.consume(NEW, body)
        self.assertEqual(self.auth.consume(OLD, body), OLD)

    def test_timestamp_expiry_and_clock_rollback_refused(self):
        for now in (1120, 999):
            self.now = 1000
            body = self.body()
            self.now = now
            with self.assertRaisesRegex(GuardError, "expired_or_used"):
                self.auth.consume(OLD, body)

    def test_payload_cannot_select_source_or_resource(self):
        for extra in ({"source": NEW}, {"port": 22}, {"prefix_list_id": "pl-other"}):
            with self.assertRaisesRegex(GuardError, "invalid_request"):
                self.auth.consume(OLD, self.body() | extra)

    def test_non_public_and_multicast_sources_refused(self):
        for source in ("127.0.0.1", "10.0.0.1", "224.0.0.1", "::1", "1.1.1.1, 8.8.8.8", None):
            with self.assertRaises(GuardError):
                public_ipv4(source)

    def test_capacity_and_expired_record_cleanup(self):
        for _ in range(128):
            self.body()
        with self.assertRaisesRegex(GuardError, "capacity"):
            self.body()
        self.now += 121
        self.body()
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM challenges").fetchone()[0], 1)

    def test_https_redirect_always_refused(self):
        with self.assertRaisesRegex(GuardError, "redirect_refused"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "http://other.invalid/")


class FakeHost:
    def __init__(self):
        self.state = {"source": OLD, "live_source": OLD, "policy": "host-baseline"}
        self.writes = 0
        self.fail_after_live = False

    def snapshot(self):
        return self.state.copy()

    def replace(self, target, policy):
        self.writes += 1
        self.state["live_source"] = target
        if self.fail_after_live:
            self.fail_after_live = False
            raise GuardError("injected_crash_before_persist")
        self.state["source"] = target


class FakeCloud:
    def __init__(self):
        self.state = {"source": OLD, "policy": "cloud-baseline"}
        self.writes = 0
        self.failure = None

    def snapshot(self):
        return self.state.copy()

    def replace(self, previous, target):
        self.writes += 1
        if self.failure == "before":
            self.failure = None
            raise GuardError("injected_cloud_outage")
        self.state["source"] = target
        if self.failure == "ambiguous":
            self.failure = None
            raise GuardError("injected_timeout_after_cloud_commit")


class Recovery(unittest.TestCase):
    def setUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.host, self.cloud = FakeHost(), FakeCloud()
        self.control = Reconciler(self.path, self.host, self.cloud)
        self.control.initialize()

    def test_same_source_is_noop(self):
        self.assertFalse(self.control.refresh(OLD)["changed"])
        self.assertEqual((self.host.writes, self.cloud.writes), (0, 0))

    def test_change_updates_both_layers_and_persistent_source(self):
        self.assertTrue(self.control.refresh(NEW)["changed"])
        self.assertEqual(self.host.state["source"], NEW)
        self.assertEqual(self.cloud.state["source"], NEW)
        self.assertEqual(self.control.read("accepted.json")["source"], NEW)
        self.assertFalse((self.path / "pending.json").exists())

    def test_cloud_failure_preserves_host_and_pending_transaction(self):
        self.cloud.failure = "before"
        with self.assertRaises(GuardError):
            self.control.refresh(NEW)
        self.assertEqual(self.host.writes, 0)
        self.assertTrue((self.path / "pending.json").exists())
        self.assertTrue(self.control.refresh(NEW)["recovered"])

    def test_ambiguous_timeout_reads_actual_state_without_duplicate_write(self):
        self.cloud.failure = "ambiguous"
        with self.assertRaises(GuardError):
            self.control.refresh(NEW)
        reopened = Reconciler(self.path, self.host, self.cloud)
        self.assertTrue(reopened.refresh(NEW)["recovered"])
        self.assertEqual(self.cloud.writes, 1)

    def test_crash_between_iptables_and_file_is_recovered(self):
        self.host.fail_after_live = True
        with self.assertRaises(GuardError):
            self.control.refresh(NEW)
        self.assertEqual((self.host.state["source"], self.host.state["live_source"]), (OLD, NEW))
        self.assertTrue(self.control.refresh(NEW)["recovered"])
        self.assertEqual(self.cloud.writes, 1)
        self.assertEqual(self.host.state["source"], NEW)

    def test_new_egress_during_pending_finishes_then_advances(self):
        self.cloud.failure = "ambiguous"
        with self.assertRaises(GuardError):
            self.control.refresh(NEW)
        self.control.refresh(NEXT)
        self.assertEqual(self.host.state["source"], NEXT)
        self.assertEqual(self.cloud.writes, 2)

    def test_crash_after_accepted_commit_is_idempotent(self):
        original = self.control.save
        def crashing_save(name, value):
            if name.startswith("history-"):
                raise GuardError("injected_crash")
            original(name, value)
        with patch.object(self.control, "save", side_effect=crashing_save):
            with self.assertRaises(GuardError):
                self.control.refresh(NEW)
        self.assertTrue(self.control.refresh(NEW)["recovered"])
        self.assertEqual((self.host.writes, self.cloud.writes), (1, 1))

    def test_non_target_host_or_cloud_change_stops_without_write(self):
        for which in (self.host, self.cloud):
            old = which.state["policy"]
            which.state["policy"] = "unrelated-change"
            with self.assertRaisesRegex(GuardError, "non_target"):
                self.control.refresh(NEW)
            which.state["policy"] = old
        self.assertEqual((self.host.writes, self.cloud.writes), (0, 0))

    def test_untracked_source_change_requires_manual_review(self):
        self.cloud.state["source"] = NEXT
        with self.assertRaisesRegex(GuardError, "untracked_source"):
            self.control.refresh(NEW)
        self.assertEqual(self.host.writes, 0)

    def test_pending_targeted_rollback(self):
        self.cloud.failure = "ambiguous"
        with self.assertRaises(GuardError):
            self.control.refresh(NEW)
        self.assertTrue(self.control.rollback()["rolled_back"])
        self.assertEqual(self.cloud.state["source"], OLD)
        self.assertEqual(self.host.state["source"], OLD)

    def test_initialization_does_not_overwrite_existing_baseline(self):
        with self.assertRaisesRegex(GuardError, "already_exists"):
            self.control.initialize()


V4 = """*filter
:INPUT ACCEPT [0:0]
-A INPUT -j POWERSYSTEM_EDGE
-A POWERSYSTEM_EDGE -i lo -j RETURN
-A POWERSYSTEM_EDGE -p tcp -m tcp --dport 3667 -j DROP
-A POWERSYSTEM_EDGE -s 1.1.1.1/32 -p tcp -m tcp --dport 7000 -j ACCEPT
-A POWERSYSTEM_EDGE -p tcp -m tcp --dport 7000 -j DROP
-A POWERSYSTEM_EDGE -s 9.9.9.9/32 -p tcp -m tcp --dport 7070 -j ACCEPT
-A POWERSYSTEM_EDGE -p tcp -m tcp --dport 7070 -j DROP
COMMIT
"""
V6 = "*filter\n:INPUT ACCEPT [0:0]\n-A INPUT -j POWERSYSTEM_EDGE6\nCOMMIT\n"


class Firewall(unittest.TestCase):
    def test_only_managed_source_and_counters_ignored(self):
        initial = inspect_rules(V4, V6)[0]
        updated = inspect_rules(V4.replace("1.1.1.1/32", "8.8.8.8/32").replace("[0:0]", "[200:9000]"), V6)[0]
        self.assertEqual(initial["policy"], updated["policy"])
        self.assertEqual(updated["live_source"], NEW)

    def test_admin_and_ipv6_changes_detected(self):
        initial = inspect_rules(V4, V6)[0]
        self.assertNotEqual(initial["policy"], inspect_rules(V4.replace("9.9.9.9/32", "8.8.4.4/32"), V6)[0]["policy"])
        self.assertNotEqual(initial["policy"], inspect_rules(V4, V6.replace("-j POWERSYSTEM_EDGE6", "-j DROP"))[0]["policy"])

    def test_duplicate_or_broad_source_refused(self):
        line = next(x for x in V4.splitlines() if "7000 -j ACCEPT" in x)
        for rules in (V4.replace(line, line + "\n" + line), V4.replace("1.1.1.1/32", "0.0.0.0/0")):
            with self.assertRaises(GuardError):
                inspect_rules(rules, V6)

    def test_accept_before_edge_chain_refused(self):
        with self.assertRaisesRegex(GuardError, "not_first"):
            inspect_rules(V4.replace("-A INPUT -j POWERSYSTEM_EDGE", "-A INPUT -j ACCEPT\n-A INPUT -j POWERSYSTEM_EDGE"), V6)


class CloudGuards(unittest.TestCase):
    def setUp(self):
        self.config = {"instance_id": "a" * 32, "target_rule_id": "b" * 32, "region_id": "cn-guangzhou"}
        self.cloud = Swas(self.config)
        self.rule = {"InstanceId": "a" * 32, "RuleId": "b" * 32, "RuleProtocol": "TCP", "Port": "7000",
                     "Policy": "accept", "SourceCidrIp": OLD + "/32", "Remark": "keep-me"}
        self.items = [self.rule]
        self.cloud.call = Mock(side_effect=lambda action, **kwargs: copy.deepcopy({"TotalCount":len(self.items), "FirewallRules":self.items}))

    def test_single_sas_rule_is_accepted(self):
        self.assertEqual(self.cloud.snapshot()["source"], OLD)

    def test_other_rule_covering_7000_is_refused(self):
        self.items.append(self.rule | {"RuleId":"c"*32,"Port":"1/65535","SourceCidrIp":"0.0.0.0/0"})
        with self.assertRaisesRegex(GuardError, "other_rule_allows"):
            self.cloud.snapshot()

    def test_disabled_overlapping_rule_is_refused_without_writes(self):
        self.items.append(self.rule | {"RuleId":"c"*32,"RuleProtocol":"TCP+UDP","Port":"1/65535",
                                       "SourceCidrIp":"0.0.0.0/0","Policy":"drop"})
        with self.assertRaisesRegex(GuardError, "other_rule_blocks_7000"):
            self.cloud.snapshot()
        self.assertTrue(all(call.args[0] == "ListFirewallRules" for call in self.cloud.call.call_args_list))

    def test_target_source_is_masked_but_other_rule_changes_are_detected(self):
        before = self.cloud.snapshot()["policy"]
        self.rule["SourceCidrIp"] = NEW + "/32"
        self.assertEqual(self.cloud.snapshot()["policy"], before)
        self.rule["Remark"] = "unexpected-change"
        self.assertNotEqual(self.cloud.snapshot()["policy"], before)

    def test_broad_target_and_wrong_instance_are_refused(self):
        self.rule["SourceCidrIp"] = "0.0.0.0/0"
        with self.assertRaisesRegex(GuardError, "not_single_host"):
            self.cloud.snapshot()
        self.rule["SourceCidrIp"] = OLD + "/32"
        self.rule["InstanceId"] = "c" * 32
        with self.assertRaisesRegex(GuardError, "instance_mismatch"):
            self.cloud.snapshot()

    def test_modify_preserves_rule_remark_and_fixed_port(self):
        self.cloud.snapshot()
        self.cloud.call = Mock(return_value={})
        self.cloud.replace(OLD, NEW)
        action = self.cloud.call.call_args.args[0]
        parameters = self.cloud.call.call_args.kwargs
        self.assertEqual(action, "ModifyFirewallRule")
        self.assertEqual(parameters, {"InstanceId":"a"*32,"RuleId":"b"*32,"Port":"7000","RuleProtocol":"TCP",
                                     "SourceCidrIp":NEW+"/32","Remark":"keep-me","ClientToken":parameters["ClientToken"]})
        self.assertEqual(len(parameters["ClientToken"]),32)

    def test_pagination_requires_complete_consistent_rules(self):
        self.cloud.call = Mock(return_value={"TotalCount":2,"FirewallRules":[self.rule]})
        with self.assertRaisesRegex(GuardError, "duplicate_rule"):
            self.cloud.snapshot()

    def test_signer_uses_sas_version_and_supports_accesskey_without_sts(self):
        credential = {"AccessKeyId":"fixture-id","AccessKeySecret":"fixture-secret"}
        request = signed_request("swas.cn-guangzhou.aliyuncs.com","ModifyFirewallRule",{"SourceCidrIp":NEW+"/32"},credential,
                                 nonce="fixture",date="2026-10-16T00:00:00Z")
        headers = {k.lower():v for k,v in request.header_items()}
        self.assertEqual(headers["x-acs-version"],"2020-06-01")
        self.assertNotIn("x-acs-security-token",headers)
        self.assertIn("SourceCidrIp=8.8.8.8%2F32",request.full_url)
        self.assertIn("ACS3-HMAC-SHA256",headers["authorization"])

    def test_unregistered_cloud_action_is_refused_before_credentials(self):
        cloud = Swas(self.config)
        with patch.object(cloud,"credentials") as credentials:
            with self.assertRaisesRegex(GuardError,"action_refused"):
                cloud.call("CreateFirewallRule",InstanceId="a"*32)
            credentials.assert_not_called()

    def test_ram_write_is_one_rule_only_and_list_is_readonly(self):
        policy = json.loads((Path(__file__).parents[2]/"config/source-refresh-ram-policy.example.json").read_text())
        write,read = policy["Statement"]
        self.assertEqual(write["Action"],["swas-open:ModifyFirewallRule"])
        self.assertNotIn("*",json.dumps(write))
        self.assertEqual(read["Action"],["swas-open:ListFirewallRules"])
        self.assertTrue(read["Resource"][0].endswith("firewallrule/*"))


if __name__ == "__main__":
    unittest.main()
