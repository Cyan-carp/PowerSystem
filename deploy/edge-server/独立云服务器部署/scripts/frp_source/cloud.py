"""SAS SWAS-OPEN 2020-06-01 only. No ECS/IMDS role assumptions."""
import hashlib
import hmac
import ipaddress
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

from .common import GuardError, bounded_json, digest, direct_opener, private_read, public_ipv4, require

CREDENTIAL_FILE = "/etc/powersystem-edge/source-refresh-cloud-credentials.json"


def signed_request(host, action, parameters, credential, *, nonce=None, date=None):
    query = "&".join(urllib.parse.quote(str(k), safe="~") + "=" + urllib.parse.quote(str(v), safe="~")
                     for k, v in sorted(parameters.items()))
    headers = {"host": host, "content-type": "application/x-www-form-urlencoded",
               "x-acs-action": action, "x-acs-version": "2020-06-01",
               "x-acs-date": date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "x-acs-signature-nonce": nonce or uuid4().hex,
               "x-acs-content-sha256": hashlib.sha256(b"").hexdigest()}
    if credential.get("SecurityToken"):
        headers["x-acs-security-token"] = credential["SecurityToken"]
    names = ";".join(sorted(headers))
    canonical_headers = "".join(k + ":" + headers[k] + "\n" for k in sorted(headers))
    canonical = "POST\n/\n" + query + "\n" + canonical_headers + "\n" + names + "\n" + headers["x-acs-content-sha256"]
    string_to_sign = "ACS3-HMAC-SHA256\n" + hashlib.sha256(canonical.encode()).hexdigest()
    signed = hmac.new(credential["AccessKeySecret"].encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = "ACS3-HMAC-SHA256 Credential=" + credential["AccessKeyId"] + ",SignedHeaders=" + names + ",Signature=" + signed
    headers["Accept"] = "application/json"
    return urllib.request.Request("https://" + host + "/?" + query, data=b"", headers=headers, method="POST")


class Swas:
    ACTIONS = {"ListFirewallRules", "ModifyFirewallRule"}

    def __init__(self, config):
        self.config = config
        self.opener = direct_opener()
        self.deadline = float("inf")
        self.target = None

    def credentials(self):
        try:
            credential = json.loads(private_read(CREDENTIAL_FILE))
            require(set(credential) == {"AccessKeyId", "AccessKeySecret"}, "invalid_cloud_credentials")
            require(bool(re.fullmatch(r"[A-Za-z0-9]{8,128}", credential["AccessKeyId"]))
                    and isinstance(credential["AccessKeySecret"], str) and 16 <= len(credential["AccessKeySecret"]) <= 256
                    and not any(c.isspace() for c in credential["AccessKeySecret"]), "invalid_cloud_credentials")
            return credential
        except Exception:
            raise GuardError("sas_credentials_unavailable") from None

    def call(self, action, **parameters):
        require(action in self.ACTIONS, "cloud_action_refused")
        require(parameters.get("InstanceId") == self.config["instance_id"], "cloud_instance_refused")
        if action == "ModifyFirewallRule":
            require(parameters.get("RuleId") == self.config["target_rule_id"] and parameters.get("Port") == "7000"
                    and parameters.get("RuleProtocol") == "TCP", "cloud_target_refused")
            source = ipaddress.IPv4Network(parameters["SourceCidrIp"], strict=True)
            require(source.prefixlen == 32, "cloud_source_refused")
            public_ipv4(str(source.network_address))
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "cloud_deadline")
        request = signed_request("swas." + self.config["region_id"] + ".aliyuncs.com", action,
                                 {"RegionId": self.config["region_id"], **parameters}, self.credentials())
        try:
            with self.opener.open(request, timeout=min(8, remaining)) as response:
                value = bounded_json(response)
            require("Code" not in value, "cloud_api_failed")
            return value
        except GuardError:
            raise
        except Exception:
            raise GuardError("cloud_request_failed") from None

    def rules(self):
        rules, total = [], None
        for page in range(1, 101):
            response = self.call("ListFirewallRules", InstanceId=self.config["instance_id"], PageSize=100, PageNumber=page)
            batch = response.get("FirewallRules")
            require(isinstance(batch, list) and isinstance(response.get("TotalCount"), int), "cloud_response_schema")
            if total is None:
                total = response["TotalCount"]
            require(total == response["TotalCount"], "cloud_rules_changed_during_read")
            rules.extend(batch)
            if len(rules) >= total:
                require(len(rules) == total, "cloud_response_schema")
                require(len({r["RuleId"] for r in rules}) == len(rules), "cloud_duplicate_rule")
                return rules
            require(bool(batch), "cloud_pagination_incomplete")
        raise GuardError("cloud_pagination_limit")

    def snapshot(self):
        policy, targets = [], []
        for rule in self.rules():
            require(rule.get("InstanceId") == self.config["instance_id"], "cloud_instance_mismatch")
            normalized = dict(rule)
            normalized.setdefault("Remark", "")
            if rule.get("RuleId") == self.config["target_rule_id"]:
                require(rule.get("RuleProtocol") == "TCP" and rule.get("Port") in ("7000", "7000/7000")
                        and rule.get("Policy", "").lower() == "accept", "target_rule_changed")
                source = ipaddress.IPv4Network(rule["SourceCidrIp"], strict=True)
                require(source.prefixlen == 32, "cloud_source_not_single_host")
                targets.append(public_ipv4(str(source.network_address)))
                normalized["SourceCidrIp"] = "[MANAGED_SOURCE]/32"
                normalized["Port"] = "7000"
                self.target = dict(rule)
            elif rule.get("RuleProtocol") in ("TCP", "TCP+UDP"):
                ports = rule.get("Port", "").split("/")
                require(len(ports) in (1, 2) and all(p.isdigit() for p in ports), "cloud_port_schema")
                if int(ports[0]) <= 7000 <= int(ports[-1]):
                    # A disabled SAS rule is a drop policy, not an absent rule.
                    # Refuse either overlap; never rewrite somebody else's rule.
                    code = "other_rule_allows_7000" if rule.get("Policy", "").lower() == "accept" else "other_rule_blocks_7000"
                    require(False, code)
            policy.append(normalized)
        require(len(targets) == 1, "target_rule_missing")
        return {"source": targets[0], "policy": digest(sorted(policy, key=lambda r: r["RuleId"]))}

    def replace(self, previous, target):
        previous, target = public_ipv4(previous), public_ipv4(target)
        require(self.target is not None and self.target["RuleId"] == self.config["target_rule_id"], "target_snapshot_required")
        current = ipaddress.IPv4Network(self.target["SourceCidrIp"], strict=True)
        require(str(current.network_address) == previous and current.prefixlen == 32, "target_source_changed")
        self.call("ModifyFirewallRule", InstanceId=self.config["instance_id"], RuleId=self.config["target_rule_id"],
                  Port="7000", RuleProtocol="TCP", SourceCidrIp=target + "/32",
                  Remark=self.target.get("Remark", ""), ClientToken=uuid4().hex)
