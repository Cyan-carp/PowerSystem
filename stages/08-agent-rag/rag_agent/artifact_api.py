"""Fixed owner-only metadata/download routes behind existing service auth."""
from uuid import UUID
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse


def routes(app):
    def service(request):
        if not request.headers.get('Authorization','').startswith('Bearer '): raise HTTPException(401,'missing_user_token')
        value=getattr(app.state,'artifacts',None)
        if value is None: raise HTTPException(503,'artifacts_unavailable')
        return value

    @app.get('/internal/agent/artifacts/{aid}')
    async def details(aid:UUID,user_id:int,request:Request):
        return await service(request).get(aid,user_id)

    @app.get('/internal/agent/artifacts/{aid}/download')
    async def download(aid:UUID,user_id:int,request:Request):
        owner=service(request)
        meta,content=await owner.get(aid,user_id,download=True)
        async def chunks():
            try:
                for start in range(0,len(content),65536):
                    yield content[start:start+65536]
            except BaseException:
                await owner.audit(aid,user_id,'stream_interrupted')
                raise
            else: await owner.audit(aid,user_id,'stream_served',len(content))
        return StreamingResponse(chunks(),media_type=meta['mime_type'],headers={
            'Content-Length':str(meta['size']),'Content-Disposition':'attachment; filename="'+meta['name']+'"',
            'X-Content-SHA256':meta['sha256'],'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
