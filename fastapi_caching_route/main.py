"""Implementation for FastAPI Caching Route."""

from __future__ import annotations

import base64
from contextlib import AsyncExitStack
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Any, Literal, ParamSpec, TypedDict, TypeVar

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
        vary_headers: NotRequired[Sequence[str]]

    class CachedResponse(TypedDict):
        """Response data to be stored in cache."""

        content: Buffer
        headers: dict[str, str]
        media_type: str | None
        status_code: int

    _T = TypeVar('_T')
    _P = ParamSpec('_P')


_CACHE_CONFIG = '__fastapi_caching_route_87233d36__'

DEFAULT_ACCEPTED_STATUS_CODES = frozenset({HTTP_200_OK})


@dataclass(frozen=True, slots=True)
class _CacheConfig:
    cache: FastAPICache
    key_builder: KeyBuilder | None
    early_dependencies: Sequence[Depends]
    vary_headers: tuple[str, ...]
    params: CacheParamsBase


@dataclass(frozen=True, slots=True)
class _CacheRequest:
    request: Request
    cache: FastAPICache
    cache_key: str
    caching_params: CacheParamsBase
    namespace: str | None
    vary_headers: tuple[str, ...]


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
        vary_headers: Sequence[str] = (),
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
                vary_headers=_normalize_header_names(vary_headers),
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
        return caching_params

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
        return _CachingRouteHandler(
            original_handler=super().get_route_handler(),
            path=self.path,
            endpoint=self.endpoint,
            query_params=self.dependant.query_params,
        )


@dataclass(slots=True, kw_only=True)
class _CachingRouteHandler:
    original_handler: RouteHandler
    path: str
    endpoint: Any
    query_params: Sequence[Any]
    early_dependant: Dependant | None | Literal[False] = False
    default_key_builder: KeyBuilder | None = None

    async def __call__(self, request: Request) -> Response:
        config = getattr(self.endpoint, _CACHE_CONFIG, None)
        if not isinstance(config, _CacheConfig):
            return await self.original_handler(request)

        if not await self._early_dependencies_solved(request, config):
            return await self.original_handler(request)

        cache_params = self._prepare_cache_request(request, config)
        if cached_response := await self._get_cached_response(cache_params):
            return cached_response

        response = await self.original_handler(request)
        return await self._cache_response(response, cache_params)

    async def _early_dependencies_solved(self, request: Request, config: _CacheConfig) -> bool:
        if self.early_dependant is False:
            self.early_dependant = _build_early_dependant(self.path, config)
        if self.early_dependant is None:
            return True
        async with AsyncExitStack() as async_exit_stack:
            solved_dependency = await solve_dependencies(
                request=request,
                dependant=self.early_dependant,
                async_exit_stack=async_exit_stack,
                embed_body_fields=False,
            )
        return not solved_dependency.errors

    def _prepare_cache_request(self, request: Request, config: _CacheConfig) -> _CacheRequest:
        cache = config.cache
        caching_params = cache.prepare_cache_params(config.params)
        key_builder = config.key_builder or self._get_default_key_builder(config)
        return _CacheRequest(
            request=request,
            cache=cache,
            cache_key=key_builder(request),
            caching_params=caching_params,
            namespace=caching_params.get('namespace', None),
            vary_headers=config.vary_headers,
        )

    def _get_default_key_builder(self, config: _CacheConfig) -> KeyBuilder:
        if self.default_key_builder is None:
            self.default_key_builder = _key_builder_factory(self.query_params, config.vary_headers)
        return self.default_key_builder

    async def _get_cached_response(self, cache_request: _CacheRequest) -> Response | None:
        cached = await cache_request.cache.get_cached(
            cache_request.cache_key,
            cache_request.namespace,
        )
        if cached is None:
            return None

        cache_request.cache.set_cache_header(cached['headers'], hit=True)
        return _build_cached_response(cache_request.request, cached)

    async def _cache_response(self, response: Response, cache_request: _CacheRequest) -> Response:
        cache = cache_request.cache
        if response.status_code not in cache.accepted_status_codes:
            return response

        _add_vary_header(response.headers, cache_request.vary_headers)
        if _get_vary_headers(response.headers) is None:
            return response

        if isinstance(response, StreamingResponse):
            cached, response = await _cache_streaming_response(response)
        else:
            _ensure_etag(response.headers, response.body)
            cached: CachedResponse = {
                'content': response.body,
                'headers': dict(response.headers),
                'media_type': response.media_type,
                'status_code': response.status_code,
            }

        await cache.set_cached(cache_request.cache_key, cached, cache_request.caching_params)
        cache.set_cache_header(response.headers, hit=False)
        return response


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


def _key_builder_factory(params: Sequence[Any], vary_headers: Sequence[str]) -> KeyBuilder:
    params_ = []
    for param in sorted(params, key=lambda p: p.alias or p.name):
        default = '' if param.field_info.is_required() else param.default
        params_.append((param.alias or param.name, default))
    vary_headers = _normalize_header_names(vary_headers)

    def _impl(request: Request) -> str:
        key = (
            request.method,
            request.scope['path'],
            tuple((k, request.query_params.get(k, d)) for k, d in params_),
            tuple((h, request.headers.get(h, '')) for h in vary_headers),
        )
        digest = sha256(repr(key).encode('utf-8'), usedforsecurity=False).digest()
        return base64.b64encode(digest).decode()

    return _impl


def _build_cached_response(request: Request, cached: CachedResponse) -> Response:
    headers = cached['headers'].copy()

    if_none_match = request.headers.get('if-none-match', None)
    if if_none_match and _if_none_match_matches(if_none_match, headers.get('etag', '')):
        headers['content-length'] = '0'
        return Response(
            content=b'',
            status_code=HTTP_304_NOT_MODIFIED,
            headers=headers,
            media_type=cached['media_type'],
        )

    return Response(
        content=cached['content'],
        status_code=cached['status_code'],
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

    _ensure_etag(headers, content)

    cached: CachedResponse = {
        'content': content,
        'headers': dict(headers),
        'media_type': media_type,
        'status_code': status_code,
    }

    response = StreamingResponse(
        _content_stream(content),
        status_code=status_code,
        headers=headers,
        media_type=media_type,
    )

    return cached, response


async def _content_stream(content: bytes) -> AsyncGenerator[bytes, None]:
    b = 0
    for e in range(b, len(content), 10240):
        yield content[b:e]
        b = e
    yield content[b:]


def _ensure_etag(headers: MutableHeaders | dict[str, str], content: Buffer) -> None:
    name = 'etag'
    if any(header.lower() == name for header in headers):
        return
    etag = sha256(content, usedforsecurity=False).hexdigest()
    headers[name] = f'"{etag}"'


def _if_none_match_matches(if_none_match: str, etag: str) -> bool:
    if not etag:
        return False

    etag_value = _normalize_etag(etag)
    if etag_value is None:
        return False

    for candidate in _split_etag_header(if_none_match):
        if candidate == '*':
            return True
        if _normalize_etag(candidate) == etag_value:
            return True
    return False


def _normalize_etag(etag: str) -> str | None:
    etag = etag.strip()
    weak_prefix_len = 2
    if etag[:weak_prefix_len].upper() == 'W/':
        etag = etag[weak_prefix_len:].lstrip()
    quoted_etag_len = 2
    if len(etag) >= quoted_etag_len and etag[0] == '"' and etag[-1] == '"':
        return etag
    return None


def _split_etag_header(header: str) -> list[str]:
    items: list[str] = []
    start = 0
    in_quotes = False
    escaped = False
    for idx, char in enumerate(header):
        if escaped:
            escaped = False
            continue
        if char == '\\' and in_quotes:
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char == ',' and not in_quotes:
            items.append(header[start:idx].strip())
            start = idx + 1
    items.append(header[start:].strip())
    return [item for item in items if item]


def _normalize_header_names(headers: Sequence[str]) -> tuple[str, ...]:
    stripped = (header.strip() for header in headers)
    return tuple(dict.fromkeys(header.lower() for header in stripped if header))


def _merge_header_names(*headers: Sequence[str]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for header_group in headers:
        for header in _normalize_header_names(header_group):
            if header not in seen:
                seen.add(header)
                merged.append(header)
    return tuple(merged)


def _get_vary_headers(headers: MutableHeaders | dict[str, str]) -> tuple[str, ...] | None:
    vary = headers.get('vary', '')
    if not vary:
        return ()

    vary_headers = _normalize_header_names(vary.split(','))
    if '*' in vary_headers:
        return None
    return vary_headers


def _add_vary_header(headers: MutableHeaders | dict[str, str], vary_headers: Sequence[str]) -> None:
    vary_headers = _normalize_header_names(vary_headers)
    if not vary_headers:
        return

    existing_vary_headers = _get_vary_headers(headers)
    if existing_vary_headers is None:
        return

    merged = _merge_header_names(existing_vary_headers, vary_headers)
    headers['vary'] = ', '.join(merged)
