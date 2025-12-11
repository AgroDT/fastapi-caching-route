from aiocache import SimpleMemoryCache
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi_caching_route import CachingRoute, FastAPICache
from pydantic import BaseModel


class UserInput(BaseModel):
    """User input model."""

    name: str


class User(UserInput):
    """User model."""

    id: int


app = FastAPI()
router = APIRouter(prefix='/users', route_class=CachingRoute)
cache = FastAPICache(SimpleMemoryCache())
# User DB
users: dict[int, User] = {}


def _user_cache_key(user_id: int) -> str:
    return f'cache:user:{user_id}'


def _user_key_builder(request: Request) -> str:
    user_id = request.scope['path_params']['user_id']
    return _user_cache_key(user_id)


@router.post(path='/')
def create_user(user_input: UserInput) -> User:
    """Create new user."""
    user_id = max(users.keys()) + 1 if users else 1
    user = User(id=user_id, name=user_input.name)
    users[user_id] = user
    return user


@cache(key_builder=_user_key_builder)
@router.get(path='/{user_id}')
def get_user(user_id: int) -> User:
    """Return cached user."""
    try:
        return users[user_id]
    except KeyError as exc:
        raise HTTPException(404) from exc


@router.patch(path='/{user_id}')
async def patch_user(user_id: int, user_input: UserInput) -> User:
    """Create new user."""
    try:
        user = users[user_id]
    except KeyError as exc:
        raise HTTPException(404) from exc
    else:
        user.name = user_input.name
        cache_key = _user_cache_key(user_id)
        await cache.invalidate_cached(cache_key)
        return user


app.include_router(router)
cache.configure_app(app)
