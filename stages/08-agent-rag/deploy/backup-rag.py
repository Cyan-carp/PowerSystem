"""Stream consistent Qdrant collection snapshots and alias metadata to stdout."""
import io,json,os,sys,tarfile
import httpx

with httpx.Client(base_url=os.environ['RAG_QDRANT_URL'],timeout=120,trust_env=False) as client:
    def result(method,path):
        response=client.request(method,path);response.raise_for_status();return response.json()['result']
    collections=[v['name'] for v in result('GET','/collections')['collections'] if v['name'].startswith('powersystem_rag_')]
    metadata={'aliases':result('GET','/aliases')['aliases'],'collections':[]}
    with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
        def add(name,raw):
            info=tarfile.TarInfo(name);info.size=len(raw);info.mode=0o600;archive.addfile(info,io.BytesIO(raw))
        for name in collections:
            snap=result('POST',f'/collections/{name}/snapshots')['name']
            response=client.get(f'/collections/{name}/snapshots/{snap}');response.raise_for_status()
            add(name+'.snapshot',response.content)
            metadata['collections'].append({'name':name,'snapshot':name+'.snapshot'})
            result('DELETE',f'/collections/{name}/snapshots/{snap}')
        add('metadata.json',json.dumps(metadata).encode())
