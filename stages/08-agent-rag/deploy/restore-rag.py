"""Validate RAG backups using an isolated PostgreSQL/Qdrant restoration only."""
import argparse,io,json,pathlib,tarfile,hashlib,asyncio,uuid,re
import asyncpg,httpx
from rag_agent.knowledge import Embedding

async def main(args):
    root=args.backup;checks={}
    alias=getattr(args,'collection','powersystem_rag_active')
    if not re.fullmatch(r'powersystem_rag_[A-Za-z0-9_-]{1,140}',alias):raise ValueError('registered_restore_collection_required')
    conn=await asyncpg.connect(args.database_url_file.read_text().strip())
    checks['history_rows_restored']=await conn.fetchval('SELECT COUNT(*) FROM rag_turns')>0
    checks['sessions_restored']=await conn.fetchval('SELECT COUNT(*) FROM rag_sessions')>0
    if args.history_file:
        before=json.loads(args.history_file.read_text())
        rows=await conn.fetch('SELECT question,response FROM rag_turns WHERE session_id=$1 ORDER BY id',uuid.UUID(before['session_id']))
        expected=before['history']['turns']
        checks['saved_history_content_restored']=len(rows)==len(expected) and all(row['question']==turn['question'] and json.loads(row['response'])==turn['response'] for row,turn in zip(rows,expected))
    await conn.close()
    with tarfile.open(root/'rag-vectors.tar') as archive,httpx.Client(base_url=args.qdrant_url,timeout=120,trust_env=False) as client:
        metadata=json.load(archive.extractfile('metadata.json'))
        for row in metadata['collections']:
            raw=archive.extractfile(row['snapshot']).read()
            response=client.post('/collections/'+row['name']+'/snapshots/upload',params={'priority':'snapshot','checksum':hashlib.sha256(raw).hexdigest()},files={'snapshot':(row['snapshot'],raw)})
            response.raise_for_status()
        aliases=[v for v in metadata['aliases'] if v['collection_name'] in {c['name'] for c in metadata['collections']}]
        response=client.post('/collections/aliases',json={'actions':[{'create_alias':v} for v in aliases]});response.raise_for_status()
        active=next(v['collection_name'] for v in aliases if v['alias_name']==alias)
        count=client.get('/collections/'+active).json()['result']['points_count'];checks['vectors_restored']=count>0
        embedding=Embedding(str(root/'rag-model'));vector=embedding.encode(['平台容器部署'],True)[0]
        response=client.post('/collections/'+alias+'/points/query',json={'query':vector,'limit':5,'with_payload':True});response.raise_for_status()
        checks['model_and_semantic_query_restored']=len(response.json()['result']['points'])==5
    result={'passed':all(checks.values()),'checks':checks,'points':count};args.report.write_text(json.dumps(result,indent=2));print(json.dumps(result));return result['passed']

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--backup',type=pathlib.Path,required=True);parser.add_argument('--database-url-file',type=pathlib.Path,required=True);parser.add_argument('--qdrant-url',required=True);parser.add_argument('--report',type=pathlib.Path,required=True);parser.add_argument('--history-file',type=pathlib.Path);parser.add_argument('--collection',default='powersystem_rag_active')
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
