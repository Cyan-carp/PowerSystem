import ipaddress
import re
import shlex
import subprocess
from pathlib import Path

from .common import CLOUD_PIN, atomic_text, digest, private_read, public_ipv4, require

ALLOWLIST = Path("/etc/powersystem-edge/allowed-client-ipv4")


def command(arguments):
    try:
        return subprocess.run(arguments, capture_output=True, text=True, timeout=12, check=True).stdout
    except Exception:
        from .common import GuardError
        raise GuardError("host_command_failed") from None


def normalized(rules):
    return [re.sub(r"\[\d+:\d+\]", "[COUNTERS]", line) for line in rules.splitlines()
            if line.startswith(("-A ", ":", "*", "COMMIT"))]


def inspect_rules(ipv4, ipv6):
    lines = normalized(ipv4)
    require(next((x for x in lines if x.startswith("-A INPUT ")), "") == "-A INPUT -j POWERSYSTEM_EDGE", "edge_chain_not_first")
    chain = [x for x in lines if x.startswith("-A POWERSYSTEM_EDGE ")]
    target = []
    for index, line in enumerate(chain, 1):
        tokens = shlex.split(line)
        if "--dport" in tokens and tokens[tokens.index("--dport") + 1] == "7000" and tokens[-2:] == ["-j", "ACCEPT"]:
            require(tokens.count("-s") == 1, "target_source_missing")
            source = tokens[tokens.index("-s") + 1]
            network = ipaddress.IPv4Network(source, strict=True)
            require(network.prefixlen == 32, "target_not_single_host")
            address = public_ipv4(str(network.network_address))
            # Only the exact simple ACCEPT rule produced by our firewall template is supported.
            rest = tokens[2:]
            for pair in (("-s", source), ("-p", "tcp"), ("-m", "tcp"), ("--dport", "7000"), ("-j", "ACCEPT")):
                if pair[0] == "-m" and "-m" not in rest:
                    continue
                require(pair[0] in rest, "unexpected_target_rule")
                position = rest.index(pair[0])
                require(rest[position:position + 2] == list(pair), "unexpected_target_rule")
                del rest[position:position + 2]
            require(not rest, "unexpected_target_rule")
            target.append((index, line, tokens, address))
    require(len(target) == 1, "target_rule_not_unique")
    index, line, tokens, source = target[0]
    drop = [i for i, x in enumerate(chain, 1) if re.search(r"--dport 7000 -j DROP$", x)]
    require(len(drop) == 1 and drop[0] > index, "target_drop_missing")
    require(any(re.search(r"--dport 3667 -j DROP$", x) for x in chain), "web_port_guard_missing")
    masked = list(lines)
    masked[masked.index(line)] = line.replace("-s " + source + "/32", "-s [MANAGED_SOURCE]/32", 1)
    return {"live_source": source, "policy": digest({"ipv4": masked, "ipv6": normalized(ipv6)})}, index, tokens


class Host:
    def identity(self):
        fingerprint = command(["ssh-keygen", "-lf", "/etc/ssh/ssh_host_ed25519_key.pub"]).split()[1]
        require(fingerprint == CLOUD_PIN, "new_cloud_identity_mismatch")
        require(command(["systemctl", "is-active", "powersystem-edge-firewall"]).strip() == "active", "firewall_inactive")

    def snapshot(self):
        self.identity()
        ipv4, ipv6 = command(["iptables-save"]), command(["ip6tables-save"])
        state, _, _ = inspect_rules(ipv4, ipv6)
        state["source"] = public_ipv4(private_read(ALLOWLIST))
        return state

    def replace(self, target, policy):
        target = public_ipv4(target)
        before = self.snapshot()
        require(before["policy"] == policy, "non_target_host_change")
        ipv4, ipv6 = command(["iptables-save"]), command(["ip6tables-save"])
        state, index, tokens = inspect_rules(ipv4, ipv6)
        require(state["policy"] == policy, "non_target_host_change")
        if state["live_source"] != target:
            tokens[tokens.index("-s") + 1] = target + "/32"
            command(["iptables", "-w", "5", "-R", "POWERSYSTEM_EDGE", str(index)] + tokens[2:])
        after, _, _ = inspect_rules(command(["iptables-save"]), command(["ip6tables-save"]))
        require(after["policy"] == policy and after["live_source"] == target, "host_readback_failed")
        if before["source"] != target:
            atomic_text(ALLOWLIST, target + "\n")
