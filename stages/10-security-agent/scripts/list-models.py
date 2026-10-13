"""Read-only gateway model inventory. Never print credentials or raw responses."""
import argparse
import asyncio
import json
from pathlib import Path
import httpx


async def main(args):
    key=Path(args.key_file).read_text(encoding='utf-8-sig').strip()
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=10) as client:
        response=await client.get('https://hgapi.dieqiyun.top/v1/models',headers={'Authorization':'Bearer '+key})
    models=[]
    if response.status_code==200:
        models=sorted({item['id'] for item in response.json().get('data',[]) if isinstance(item,dict) and isinstance(item.get('id'),str)
                       and item['id'].startswith('deepseek') and len(item['id'])<=100})
    result={'status_code':response.status_code,'deepseek_models':models}
    if args.output:Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--key-file',required=True);parser.add_argument('--output')
    asyncio.run(main(parser.parse_args()))
