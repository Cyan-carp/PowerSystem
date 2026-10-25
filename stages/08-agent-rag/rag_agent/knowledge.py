import asyncio
import threading
import re
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
        aliases = await self.client.get(self.config.qdrant_url + '/aliases', timeout=self.config.tool_timeout)
        aliases.raise_for_status()
        collection = next((a['collection_name'] for a in aliases.json()['result']['aliases'] if a['alias_name']==self.config.collection), self.config.collection)
        # Pin the physical collection before querying so an alias switch cannot
        # relabel already-returned citations as the next version.
        response = await self.client.post(self.config.qdrant_url + f"/collections/{collection}/points/query",
            json={"query": vector, "limit": limit, "with_payload": True}, timeout=self.config.tool_timeout)
        response.raise_for_status()
        # Citations keep source/version/hash and the collection they came from.
        return [{**row["payload"], "score": row["score"], "collection": collection}
                for row in response.json()["result"]["points"]]

    async def source(self, chunk_id, collection=None, version=None):
        prefix=getattr(self.config,'source_collection_prefix',None) or self.config.collection.removesuffix('_active')+'_'
        if collection is not None and (not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',collection) or not collection.startswith(prefix) or collection==self.config.collection):
            raise ValueError('invalid_source_collection')
        if version is not None and not re.fullmatch(r'[a-f0-9]{64}',version):
            raise ValueError('invalid_source_version')
        async def lookup(collection):
            response = await self.client.get(self.config.qdrant_url + f"/collections/{collection}/points/{point_id(chunk_id)}", timeout=10)
            if response.status_code == 404: return None
            response.raise_for_status()
            payload=response.json()["result"]["payload"]
            if version is not None and payload.get('version')!=version:return None
            return {**payload, "collection": collection}
        if collection is not None:
            # A pinned historical reference must never silently resolve to a
            # different import version with the same content-derived chunk ID.
            return await lookup(collection)
        row = await lookup(self.config.collection)
        if row: return row
        # Retained collection versions also serve citations in historical chats.
        response = await self.client.get(self.config.qdrant_url + "/collections", timeout=10)
        response.raise_for_status()
        for collection in reversed(response.json()["result"]["collections"]):
            name = collection["name"]
            if name.startswith(prefix):
                row = await lookup(name)
                if row: return row
        return None
