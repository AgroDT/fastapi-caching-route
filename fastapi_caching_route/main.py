"""Implementation for FastAPI Caching Route."""

from __future__ import annotations

import base64
from contextlib import AsyncExitStack
from dataclasses import dataclass
from hashlib import sha256
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    ParamSpec,
    TypedDict,
    TypeVar,
    cast,
)

from fastapi import Request, Response
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from fastapi.routing import APIRoute
from starlette.responses import StreamingResponse
from starlette.status import HTTP_200_OK, HTTP_304_NOT_MODIFIED


if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        Awaitable,
        Callable,
        Coroutine,
        Iterable,
        Sequence,
        Set as AbstractSet,
    )

    from aiocache import BaseCache
    from fastapi.params import Depends
    from starlette.datastructures import MutableHeaders
    from typing_extensions import Buffer, Doc, NotRequired

    KeyBuilder = Callable[[Request], str]
    RouteHandler = Callable[[Request], Coroutine[Any, Any, Response]]

    class CacheParamsBase(TypedDict):
        """Cache parameters to be passed to aiocache."""

        namespace: NotRequired[str | None]
        ttl: NotRequired[float]

    class CacheParams(CacheParamsBase):
        """Cache parameters for a specific endpoint."""

        key_builder: NotRequired[KeyBuilder]
        dependencies: NotRequired[Sequence[Depends]]

    class CachedResponse(TypedDict):
        """Response data to be stored in cache."""

        content: bytes
        headers: dict[str, str]
        media_type: str | None

    _T = TypeVar('_T')
    _P = ParamSpec('_P')


_CACHE_CONFIG = '__fastapi_caching_route_87233d36__'

DEFAULT_ACCEPTED_STATUS_CODES = frozenset({HTTP_200_OK})


@dataclass(frozen=True, slots=True)
class _CacheConfig:
    cache: FastAPICache
    key_builder: KeyBuilder | None
    early_dependencies: Sequence[Depends]
    params: CacheParamsBase


class FastAPICache:
    """Manages cached routes.

    ## Example

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
    """

    __slots__ = (
        '_cache_header',
        '_cache_header_hit',
        '_cache_header_miss',
        '_concat_namespace',
        '_inner',
        'accepted_status_codes',
    )

    def __init__(
        self,
        cache: Annotated[BaseCache, Doc('aiocache instance to perform caching.')],
        *,
        namespace_policy: Annotated[
            Literal['concat', 'replace'],
            Doc(
                """How to process namespaces passed to the decorator.

                ## concat (default)

                Add to the root (passed to the aiocache instance) namespace.

                ```py
                cache = FastAPICache(RedisCache(namespace='cache'))

                # resulting namespace is 'cache:user'
                @cache(namespace='user')
                @router.get('/{user_id}')
                async def get_user(user_id: str):
                    ...
                ```

                ## replace

                Replace the root namespace.

                ```py
                cache = FastAPICache(RedisCache(namespace='cache'), namespace_policy='replace')

                # resulting namespace is 'user'
                @cache(namespace='user')
                @router.get('/{user_id}')
                async def get_user(user_id: str):
                    ...
                ```
                """,
            ),
        ] = 'concat',
        cache_header: str = 'X-Cache',
        cache_header_hit: str = 'HIT',
        cache_header_miss: str = 'MISS',
        accepted_status_codes: Annotated[
            AbstractSet[int] | Iterable[int],
            Doc('Only cache responses with these HTTP status codes.'),
        ] = DEFAULT_ACCEPTED_STATUS_CODES,
    ) -> None:
        self._inner = cache
        self._concat_namespace = namespace_policy == 'concat'
        self._cache_header = cache_header
        self._cache_header_hit = cache_header_hit
        self._cache_header_miss = cache_header_miss
        self.accepted_status_codes = frozenset(accepted_status_codes)

    def __call__(
        self,
        *,
        key_builder: KeyBuilder | None = None,
        dependencies: Sequence[Depends] = (),
        namespace: str | None = None,
        ttl: float | None = None,
    ) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
        """Decorate caching route.

        Marks the endpoint for caching by :class:`CachingRoute`.

        ```py hl_lines="3"
            cache = FastAPICache(SimpleMemoryCache())

            @cache()
            @router.get('/')
            def cached() -> str:
                ...
        ```
        """
        params: CacheParamsBase = {}
        if namespace is not None:
            params['namespace'] = namespace
        if ttl is not None:
            params['ttl'] = ttl

        def decorator(endpoint: Callable[_P, _T]) -> Callable[_P, _T]:
            config = _CacheConfig(
                cache=self,
                key_builder=key_builder,
                early_dependencies=tuple(dependencies),
                params=params,
            )
            setattr(endpoint, _CACHE_CONFIG, config)
            return endpoint

        return decorator

    def get_cached(
        self,
        cache_key: str,
        namespace: str | None = None,
    ) -> Awaitable[CachedResponse | None]:
        """Get cached response.

        Returns:
            Cached response.
        """
        return self._inner.get(cache_key, namespace=namespace)

    def set_cached(
        self,
        cache_key: str,
        value: CachedResponse,
        caching_params: CacheParamsBase,
    ) -> Awaitable[bool]:
        """Set cached response.

        Returns:
            `True` if the value was set.
        """
        return self._inner.set(cache_key, value, **caching_params)

    def invalidate_cached(
        self,
        cache_key: str,
        namespace: str | None = None,
    ) -> Annotated[Awaitable[int], Doc('Number of deleted keys.')]:
        """Delete cached response.

        Returns:
            Number of deleted keys.
        """
        return self._inner.delete(cache_key, namespace)

    def prepare_cache_params(self, cache_params: CacheParamsBase) -> CacheParamsBase:
        """Prepare cache backend parameters for a request."""
        caching_params = cache_params.copy()
        namespace = caching_params.get('namespace', None)
        root = self._inner.namespace
        if self._concat_namespace and root and namespace:
            caching_params['namespace'] = f'{root}:{namespace}'
        return cast('CacheParamsBase', caching_params)

    def set_cache_header(self, headers: MutableHeaders | dict[str, str], *, hit: bool) -> None:
        """Set a cache status header."""
        headers[self._cache_header] = self._cache_header_hit if hit else self._cache_header_miss


class CachingRoute(APIRoute):
    """FastAPI route to perform caching.

    Intended for use with fastapi.APIRouter.

    ```py hl_lines="4"
    from fastapi import APIRouter
    from fastapi_caching_route import CachingRoute

    router = APIRouter(route_class=CachingRoute)
    ```
    """

    def get_route_handler(self) -> RouteHandler:  # noqa: D102
        original_handler = super().get_route_handler()
        early_dependant: Dependant | None = None
        early_dependant_initialized = False
        default_key_builder: KeyBuilder | None = None

        async def app(request: Request) -> Response:
            nonlocal default_key_builder, early_dependant, early_dependant_initialized

            config = _get_cache_config(self.endpoint)
            if config is None:
                return await original_handler(request)

            if not early_dependant_initialized:
                early_dependant = _build_early_dependant(self.path, config)
                early_dependant_initialized = True

            if not await _early_dependencies_solved(request, early_dependant):
                return await original_handler(request)

            cache = config.cache
            caching_params = cache.prepare_cache_params(config.params)
            namespace = caching_params.get('namespace', None)
            key_builder = config.key_builder
            if key_builder is None:
                if default_key_builder is None:
                    default_key_builder = _key_builder_factory(self.dependant.query_params)
                key_builder = default_key_builder

            cache_key = key_builder(request)
            if cached := await cache.get_cached(cache_key, namespace):
                cache.set_cache_header(cached['headers'], hit=True)
                return _build_cached_response(request, cached)

            response = await original_handler(request)
            if response.status_code not in cache.accepted_status_codes:
                return response

            if isinstance(response, StreamingResponse):
                cached, response = await _cache_streaming_response(response)
            else:
                _set_etag(response.headers, response.body)
                cached = _cached_response(
                    content=response.body,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )

            await cache.set_cached(cache_key, cached, caching_params)
            cache.set_cache_header(response.headers, hit=False)
            return response

        return app


def _get_cache_config(endpoint: Any) -> _CacheConfig | None:
    config = getattr(endpoint, _CACHE_CONFIG, None)
    if isinstance(config, _CacheConfig):
        return config
    return None


def _build_early_dependant(path: str, config: _CacheConfig) -> Dependant | None:
    dependencies = [
        get_dependant(
            path=path,
            call=dependency.dependency,
            use_cache=dependency.use_cache,
        )
        for dependency in config.early_dependencies
        if dependency.dependency
    ]
    if not dependencies:
        return None
    return Dependant(dependencies=dependencies)


def _key_builder_factory(params: Sequence[Any]) -> KeyBuilder:
    params_ = []
    for param in sorted(params, key=lambda p: p.alias or p.name):
        default = '' if param.field_info.is_required() else param.default
        params_.append((param.alias or param.name, default))

    def _impl(request: Request) -> str:
        key = request.scope['path'] + '?'
        key += '&'.join(f'{k}={request.query_params.get(k, d)}' for k, d in params_)
        digest = sha256(key.encode('utf-8'), usedforsecurity=False).digest()
        return base64.b64encode(digest).decode()

    return _impl


def _build_cached_response(request: Request, cached: CachedResponse) -> Response:
    headers = cached['headers'].copy()

    if etag := request.headers.get('if-none-match', None):
        etag = etag.removeprefix('W/')
        if headers['etag'] == etag:
            headers['content-length'] = '0'
            return Response(
                content=b'',
                status_code=HTTP_304_NOT_MODIFIED,
                headers=headers,
                media_type=cached['media_type'],
            )

    return Response(
        content=cached['content'],
        headers=headers,
        media_type=cached['media_type'],
    )


async def _cache_streaming_response(
    response: StreamingResponse,
) -> tuple[CachedResponse, StreamingResponse]:
    status_code = response.status_code
    headers = response.headers
    media_type = response.media_type

    content = b''
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            content += chunk.encode(response.charset)
        else:
            content += chunk

    _set_etag(headers, content)

    cached = _cached_response(
        content=content,
        headers=dict(headers),
        media_type=media_type,
    )

    response = StreamingResponse(
        _content_stream(content),
        status_code=status_code,
        headers=headers,
        media_type=media_type,
    )

    return cached, response


async def _early_dependencies_solved(request: Request, dependant: Dependant | None) -> bool:
    if dependant is None:
        return True
    async with AsyncExitStack() as async_exit_stack:
        solved_dependency = await solve_dependencies(
            request=request,
            dependant=dependant,
            async_exit_stack=async_exit_stack,
            embed_body_fields=False,
        )
    return not solved_dependency.errors


def _cached_response(**kwargs: Any) -> CachedResponse:
    return cast('CachedResponse', kwargs)


async def _content_stream(content: bytes) -> AsyncGenerator[bytes, None]:
    b = 0
    for e in range(b, len(content), 10240):
        yield content[b:e]
        b = e
    yield content[b:]


def _set_etag(headers: MutableHeaders | dict[str, str], content: Buffer) -> None:
    etag = sha256(content, usedforsecurity=False).hexdigest()
    headers['etag'] = f'"{etag}"'
