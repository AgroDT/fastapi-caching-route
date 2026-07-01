# FastAPI Caching Route

FastAPI route class for response caching before entering the endpoint handler.

`fastapi-caching-route` plugs into `fastapi.APIRouter`, stores complete response
payloads in an `aiocache` backend, and serves cache hits directly from the route
handler. It is useful for expensive read endpoints where the cache key can be
derived from the request path, query parameters, or a custom key builder.

**⚠️ This project is a proof of concept and is not yet recommended for production use!**

## Features

- Cache regular FastAPI responses and `StreamingResponse` bodies.
- Return `X-Cache: MISS` for stored responses and `X-Cache: HIT` for cache hits.
- Generate `ETag` headers for cached responses.
- Return `304 Not Modified` for matching `If-None-Match` requests.
- Build default cache keys from the route path and declared query parameters.
- Provide custom key builders for path parameters or application-specific keys.
- Include selected request headers in default cache keys for negotiated responses.
- Run explicitly configured dependencies before cache lookup, for example API key
  checks.
- Pass `namespace` and `ttl` through to the underlying `aiocache` backend.
- Manually invalidate cached values through `FastAPICache.invalidate_cached()`.

## Installation

```sh
uv add fastapi-caching-route
```

```sh
pip install fastapi-caching-route
```

Install FastAPI and a cache backend that matches your application. For local
development or tests, `aiocache.SimpleMemoryCache` is enough:

```sh
uv add fastapi aiocache
```

```sh
pip install fastapi aiocache
```

## Basic Usage

Use `CachingRoute` as the router route class and decorate endpoints with
`FastAPICache`.

```py
from aiocache import SimpleMemoryCache
from fastapi import APIRouter, FastAPI
from fastapi_caching_route import CachingRoute, FastAPICache


router = APIRouter(route_class=CachingRoute)
cache = FastAPICache(SimpleMemoryCache())


@cache()
@router.get('/')
def cached() -> str:
    return 'Hello, World!'


app = FastAPI()
app.include_router(router)
```

Start an app with this route and call it twice. The first response is produced by
the endpoint and stored in the cache:

```text
X-Cache: MISS
```

The second response is returned from the cache before the endpoint handler is
called:

```text
X-Cache: HIT
```

Routes decorated with `@cache()` must be registered on a router that uses
`CachingRoute`. A plain `APIRoute` ignores the cache configuration.

## Cache Keys

By default, the cache key is built from the request path and declared query
parameters. Query parameter order is normalized, so these two requests hit the
same cache entry when the endpoint declares `a` and `b`:

```text
/query?a=a&b=b
/query?b=b&a=a
```

Use a custom key builder when the cache key should be based on path parameters,
headers, user context, or another application-specific value.

```py
from fastapi import Request


def user_key_builder(request: Request) -> str:
    user_id = request.scope['path_params']['user_id']
    return f'user:{user_id}'


@cache(key_builder=user_key_builder, ttl=60, namespace='users')
@router.get('/users/{user_id}')
def get_user(user_id: int) -> dict[str, int]:
    return {'id': user_id}
```

`ttl` and `namespace` are passed to `aiocache`. When the underlying cache instance
also has a namespace, `FastAPICache` concatenates the root and endpoint namespace
by default:

```py
cache = FastAPICache(RedisCache(namespace='api'))


@cache(namespace='users')
@router.get('/users/{user_id}')
def get_user(user_id: int): ...


# Resulting namespace: "api:users"
```

Pass `namespace_policy="replace"` to `FastAPICache` if endpoint namespaces should
replace the root namespace instead.

If the response representation depends on request headers, include those headers
in the default cache key with `vary_headers`. The route also returns a matching
`Vary` header:

```py
@cache(vary_headers=['Accept-Language'])
@router.get('/localized')
def localized(request: Request) -> str:
    return request.headers.get('accept-language', 'en')
```

Responses with `Vary: *` are not cached.

## Dependencies Before Cache Lookup

FastAPI dependencies on the route still run on cache misses. If a dependency
must also be resolved before cache lookup, pass it to `@cache(dependencies=...)`.
This is mainly useful for security dependencies that should reject unauthorized
requests before a cached response can be served.

```py
from fastapi import Depends
from fastapi.security import APIKeyHeader


api_key = Depends(APIKeyHeader(name='X-Key'))


@cache(dependencies=[api_key])
@router.get('/private', dependencies=[api_key])
def private_data() -> str:
    return 'secret'
```

Keep the dependency on the route as well if it must be enforced for cache misses.

## Conditional Requests

Cached responses without an existing `ETag` get one based on the response body.
If the endpoint already sets `ETag`, that value is preserved. On a cache hit,
requests with a matching `If-None-Match` header return `304 Not Modified` with an
empty body. `If-None-Match` supports weak tags, tag lists, and `*`.

```sh
curl http://127.0.0.1:8000/cached -H 'If-None-Match: "..."'
```

## Invalidation

Use the same key builder logic when invalidating cache entries after writes.

```py
from fastapi import Request


def user_cache_key(user_id: int) -> str:
    return f'user:{user_id}'


def user_key_builder(request: Request) -> str:
    return user_cache_key(request.scope['path_params']['user_id'])


@cache(key_builder=user_key_builder)
@router.get('/users/{user_id}')
def get_user(user_id: int): ...


@router.patch('/users/{user_id}')
async def update_user(user_id: int) -> dict[str, int]:
    await cache.invalidate_cached(user_cache_key(user_id))
    return {'id': user_id}
```

`invalidate_cached()` returns the number of deleted keys reported by the
underlying cache backend.

## Examples

The repository contains runnable examples:

- `examples/simple.py`: minimal cached route.
- `examples/complex.py`: cache hits and misses, auth dependency, ETag handling,
  streaming response caching, query parameter keys, and non-cacheable responses.
- `examples/invalidate.py`: custom key builder and manual invalidation after an
  update.

Follow the detailed walkthrough in the examples README.
