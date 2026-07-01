import anyio
from aiocache import SimpleMemoryCache
from fastapi import APIRouter, Depends, FastAPI, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi_caching_route import CachingRoute, FastAPICache


router = APIRouter(route_class=CachingRoute)
cache = FastAPICache(SimpleMemoryCache())
# Any non-empty string is considered a valid key
APIKeyDep = Depends(APIKeyHeader(name='X-Key'))


async def get_data() -> str:
    """Simulate data loading."""
    await anyio.sleep(1)
    return 'Hello, World!'


@router.get(
    path='/not-cached',
    dependencies=[APIKeyDep],
    response_class=PlainTextResponse,
)
async def not_cached() -> str:
    """Return not cached response."""
    return await get_data()


@cache(dependencies=[APIKeyDep])
@router.get(
    path='/cached',
    dependencies=[APIKeyDep],
    response_class=PlainTextResponse,
)
async def cached() -> str:
    """Return a cached response."""
    return await get_data()


@cache(dependencies=[APIKeyDep])
@router.get(
    path='/stream-cached',
    dependencies=[APIKeyDep],
)
async def stream_cached() -> StreamingResponse:
    """Stream a cached response."""
    data = await get_data()

    async def content():
        for chunk in data:
            yield chunk

    return StreamingResponse(
        content=content(),
        media_type='text/plain',
        headers={'Content-Length': str(len(data))},
    )


@cache()
@router.get('/404')
def get_failed() -> Response:
    """Return status 404."""
    return Response(status_code=404)


@cache()
@router.get('/query')
def get_with_params(a: str, b: str = 'b') -> dict:
    """Return query params as object."""
    return {'a': a, 'b': b}


app = FastAPI()
app.include_router(router)
