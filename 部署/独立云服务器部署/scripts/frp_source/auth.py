import hmac
import contextlib
import re
import secrets
import sqlite3
import time

from .common import GuardError, public_ipv4, require, signature


class Challenges:
    """Root-owned persistent one-use challenges; the receiver never reads the key."""

    def __init__(self, database, client_id, key, clock=time.time):
        self.database, self.client_id, self.key, self.clock = str(database), client_id, key, clock
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS challenges (id TEXT PRIMARY KEY, nonce TEXT, source TEXT, created REAL, expires REAL)")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=3, isolation_level="IMMEDIATE")
        try:
            with db:
                yield db
        finally:
            db.close()

    def issue(self, source, body):
        source = public_ipv4(source)
        require(body == {"client_id": self.client_id}, "invalid_client")
        now = self.clock()
        with self.connect() as db:
            db.execute("DELETE FROM challenges WHERE expires <= ? OR created > ?", (now, now))
            require(db.execute("SELECT COUNT(*) FROM challenges").fetchone()[0] < 128, "challenge_capacity")
            challenge = {"client_id": self.client_id, "challenge_id": secrets.token_hex(16),
                         "nonce": secrets.token_hex(32), "source": source, "expires_in": 120}
            db.execute("INSERT INTO challenges VALUES (?,?,?,?,?)", (challenge["challenge_id"], challenge["nonce"], source, now, now + 120))
        return challenge

    def consume(self, source, body):
        source = public_ipv4(source)
        require(isinstance(body, dict) and set(body) == {"client_id", "challenge_id", "nonce", "signature"}, "invalid_request")
        require(body["client_id"] == self.client_id, "invalid_client")
        for field, length in [("challenge_id", 32), ("nonce", 64), ("signature", 64)]:
            require(isinstance(body[field], str) and bool(re.fullmatch(r"[0-9a-f]{%d}" % length, body[field])), "invalid_signature")
        # BEGIN IMMEDIATE makes consume atomic across processes as well as threads.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT nonce,source,created,expires FROM challenges WHERE id=?", (body["challenge_id"],)).fetchone()
            now = self.clock()
            require(row is not None and row[2] <= now < row[3], "expired_or_used_challenge")
            require(row[1] == source and hmac.compare_digest(row[0], body["nonce"]), "challenge_source_mismatch")
            signed = {**body, "source": source}
            require(hmac.compare_digest(signature(self.key, signed), body["signature"]), "invalid_signature")
            db.execute("DELETE FROM challenges WHERE id=?", (body["challenge_id"],))
        return source
