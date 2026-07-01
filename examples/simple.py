from aiocache import SimpleMemoryCache
from fastapi import APIRouter, FastAPI
from fastapi_caching_route import CachingRoute, FastAPICache


router = APIRouter(route_class=CachingRoute)
cache = FastAPICache(SimpleMemoryCache())


@cache()
@router.get('/')
def cached() -> str:
    """Return cached response."""
    return 'Hello, World!'


app = FastAPI()
app.include_router(router)
