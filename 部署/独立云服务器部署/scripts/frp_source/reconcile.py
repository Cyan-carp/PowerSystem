"""Write-ahead, forward recovery across cloud/iptables/file changes."""
import json
import time
from pathlib import Path
from uuid import uuid4

from .common import atomic_json, public_ipv4, require


class Reconciler:
    def __init__(self, directory, host, cloud):
        self.directory, self.host, self.cloud = Path(directory), host, cloud

    def read(self, name):
        return json.loads((self.directory / name).read_text(encoding="utf-8"))

    def save(self, name, value):
        atomic_json(self.directory / name, value)

    def snapshots(self):
        host, cloud = self.host.snapshot(), self.cloud.snapshot()
        return host, cloud

    def initialize(self):
        require(not (self.directory / "baseline.json").exists(), "baseline_already_exists")
        require(not (self.directory / "pending.json").exists(), "transaction_pending")
        host, cloud = self.snapshots()
        require(host["source"] == host["live_source"] == cloud["source"], "initial_sources_differ")
        self.save("accepted.json", {"source": host["source"]})
        self.save("baseline.json", {"host_policy": host["policy"], "cloud_policy": cloud["policy"]})
        return {"initialized": True, "changed": False}

    def guard(self, host, cloud, baseline):
        require(host["policy"] == baseline["host_policy"], "non_target_host_change")
        require(cloud["policy"] == baseline["cloud_policy"], "non_target_cloud_change")

    def check(self):
        baseline = self.read("baseline.json")
        accepted = self.read("accepted.json")["source"]
        host, cloud = self.snapshots()
        self.guard(host, cloud, baseline)
        pending = (self.directory / "pending.json").exists()
        require(pending or host["source"] == host["live_source"] == cloud["source"] == accepted, "untracked_source_change")
        return {"ready": True, "pending": pending, "sources_match": host["source"] == host["live_source"] == cloud["source"] == accepted}

    def finish(self, transaction, baseline):
        old, target = transaction["old"], transaction["target"]
        host, cloud = self.snapshots()
        self.guard(host, cloud, baseline)
        require(self.read("accepted.json")["source"] in (old, target), "accepted_state_conflict")
        require(all(value in (old, target) for value in (host["source"], host["live_source"], cloud["source"])), "transaction_source_conflict")
        # An ambiguous timeout deliberately keeps pending.json. Read actual state next time.
        if cloud["source"] != target:
            self.cloud.replace(old, target)
            cloud = self.cloud.snapshot()
            self.guard(host, cloud, baseline)
            require(cloud["source"] == target, "cloud_readback_pending")
        if host["source"] != target or host["live_source"] != target:
            self.host.replace(target, baseline["host_policy"])
        host, cloud = self.snapshots()
        self.guard(host, cloud, baseline)
        require(host["source"] == host["live_source"] == cloud["source"] == target, "reconcile_incomplete")
        self.save("accepted.json", {"source": target})
        self.save("history-" + transaction["id"] + ".json", {**transaction, "committed": True, "completed_at": time.time()})
        (self.directory / "pending.json").unlink()

    def refresh(self, source):
        source = public_ipv4(source)
        baseline = self.read("baseline.json")
        recovered = False
        if (self.directory / "pending.json").exists():
            self.finish(self.read("pending.json"), baseline)
            recovered = True
        host, cloud = self.snapshots()
        self.guard(host, cloud, baseline)
        accepted = self.read("accepted.json")["source"]
        require(host["source"] == host["live_source"] == cloud["source"] == accepted, "untracked_source_change")
        if source == accepted:
            return {"allowlist_updated": True, "changed": recovered, "recovered": recovered}
        transaction = {"id": uuid4().hex, "old": accepted, "target": source, "started_at": time.time()}
        self.save("pending.json", transaction)
        self.finish(transaction, baseline)
        return {"allowlist_updated": True, "changed": True, "recovered": recovered}

    def rollback(self):
        """Only an interrupted transaction can be rolled back, never an entire ruleset."""
        transaction = self.read("pending.json")
        baseline = self.read("baseline.json")
        host, cloud = self.snapshots()
        self.guard(host, cloud, baseline)
        old, target = transaction["old"], transaction["target"]
        require(all(value in (old, target) for value in (host["source"], host["live_source"], cloud["source"])), "transaction_source_conflict")
        if cloud["source"] != old:
            self.cloud.replace(target, old)
        self.host.replace(old, baseline["host_policy"])
        host, cloud = self.snapshots()
        self.guard(host, cloud, baseline)
        require(host["source"] == host["live_source"] == cloud["source"] == old, "rollback_incomplete")
        self.save("accepted.json", {"source": old})
        self.save("history-" + transaction["id"] + ".json", {**transaction, "rolled_back": True})
        (self.directory / "pending.json").unlink()
        return {"rolled_back": True}
