import asyncio
import threading
from uuid import uuid5, NAMESPACE_URL


def point_id(chunk_id):
    return str(uuid5(NAMESPACE_URL, chunk_id))


class Embedding:
    def __init__(self, path):
        self.path = path
        self.model = None
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            if self.model is None:
                from sentence_transformers import SentenceTransformer
                self.model = SentenceTransformer(self.path, device="cpu")
                self.model.max_seq_length = 512
        return self.model

    def encode(self, texts, query=False):
        model = self.load()
        with self.lock:
            return model.encode([("query: " if query else "passage: ") + text for text in texts],
                normalize_embeddings=True, show_progress_bar=False, batch_size=16).tolist()


class Knowledge:
    def __init__(self, config, client, embedding=None):
        self.config, self.client = config, client
        self.embedding = embedding or Embedding(config.embedding_path)

    async def search(self, query, limit=5):
        vector = (await asyncio.to_thread(self.embedding.encode, [query], True))[0]
        response = await self.client.post(self.config.qdrant_url + f"/collections/{self.config.collection}/points/query",
            json={"query": vector, "limit": limit, "with_payload": True}, timeout=self.config.tool_timeout)
        response.raise_for_status()
        return [{**row["payload"], "score": row["score"]} for row in response.json()["result"]["points"]]

    async def source(self, chunk_id):
        async def lookup(collection):
            response = await self.client.get(self.config.qdrant_url + f"/collections/{collection}/points/{point_id(chunk_id)}", timeout=10)
            if response.status_code == 404: return None
            response.raise_for_status()
            return response.json()["result"]["payload"]
        row = await lookup(self.config.collection)
        if row: return row
        # Retained collection versions also serve citations in historical chats.
        response = await self.client.get(self.config.qdrant_url + "/collections", timeout=10)
        response.raise_for_status()
        for collection in reversed(response.json()["result"]["collections"]):
            name = collection["name"]
            if name.startswith(self.config.collection.removesuffix("_active") + "_"):
                row = await lookup(name)
                if row: return row
        return None
