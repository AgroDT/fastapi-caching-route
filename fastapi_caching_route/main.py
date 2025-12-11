"""Implementation for FastAPI Caching Route."""

from __future__ import annotations

import base64
from contextlib import AsyncExitStack
from hashlib import sha256
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NotRequired,
    ParamSpec,
    TypedDict,
    TypeVar,
    Unpack,
    cast,
)

from fastapi import FastAPI, Request, Response
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from fastapi.routing import APIRoute, get_request_handler
from starlette.responses import StreamingResponse
from starlette.status import HTTP_200_OK, HTTP_304_NOT_MODIFIED


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Generator, Sequence

    from aiocache import BaseCache
    from fastapi.params import Depends
    from fastapi.types import DependencyCacheKey
    from starlette.datastructures import MutableHeaders
    from typing_extensions import Doc

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

    _CacheEndpoints = dict[Callable, CacheParams]
    _CacheMethodParams = tuple[KeyBuilder, CacheParamsBase, Dependant | None]
    _DependencyCache = dict[DependencyCacheKey, Any]
    _T = TypeVar('_T')
    _P = ParamSpec('_P')


_CACHE_INSTANCE = '__cache_instance'
_DEFAULT_ACCEPTED_STATUS_CODES = {HTTP_200_OK}


class FastAPICache:
    """Manages cached routes.

    ## Example

    ```py
    from aiocache import SimpleMemoryCache
    from fastapi import APIRouter, FastAPI
    from fastapi_caching_route import CachingRoute, FastAPICache


    app = FastAPI()
    router = APIRouter(route_class=CachingRoute)
    cache = FastAPICache(SimpleMemoryCache())

    @cache()
    @router.get('/')
    def cached() -> str:
        return 'Hello, World!'

    app.include_router(router)
    cache.configure_app(app)
    ```
    """

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
            set[int],
            Doc(
                """Only cache responses with these HTTP status codes.

                By default only 200 is cached.
                """,
            ),
        ] = _DEFAULT_ACCEPTED_STATUS_CODES,
        cache_dependencies: Annotated[
            bool,
            Doc('Set to `False` to  disable dependency caching.'),
        ] = True,
    ) -> None:
        self._inner = cache
        self._endpoints: _CacheEndpoints = {}
        self._concat_namespace = namespace_policy == 'concat'
        self._cache_header = cache_header
        self._cache_header_hit = cache_header_hit
        self._cache_header_miss = cache_header_miss
        self.accepted_status_codes = accepted_status_codes
        self.cache_dependencies = cache_dependencies

    def __call__(
        self,
        **kwargs: 'Unpack[CacheParams]',  # noqa: UP037
    ) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
        """Decorate caching route.

        Does not actually wrap the endpoint, but marks it for caching.

        ```py hl_lines="3"
            cache = FastAPICache(SimpleMemoryCache())

            @cache()
            @router.get('/')
            def cached() -> str:
                ...
        ```
        """

        def decorator(endpoint: Callable[_P, _T]) -> Callable[_P, _T]:
            self._endpoints[endpoint] = kwargs
            return endpoint

        return decorator

    def configure_app(self, app: FastAPI) -> None:
        """Apply caching for decorated routes.

        Should be called once after all routes were mounted.
        """
        if _CACHE_INSTANCE in app.extra:
            raise CacheInitializationError
        self.routes = dict(_cache_routes(app, self._endpoints))
        app.extra[_CACHE_INSTANCE] = self

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

    def check_namespace(self, caching_params: CacheParamsBase) -> str | None:
        """Construct a full namespace according to the selected policy."""
        namespace = caching_params.get('namespace', None)
        root = self._inner.namespace
        if self._concat_namespace and root and namespace:
            namespace = f'{root}:{namespace}'
            caching_params['namespace'] = namespace
        return namespace

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

    def _get_original_route_handler(
        self,
        dependency_cache: _DependencyCache | None = None,
    ) -> RouteHandler:
        if dependency_cache:
            dependency_overrides_provider: Any = _CachedDependencyProvider(dependency_cache)
        else:
            dependency_overrides_provider = self.dependency_overrides_provider

        return get_request_handler(
            dependant=self.dependant,
            body_field=self.body_field,
            status_code=self.status_code,
            response_class=self.response_class,
            response_field=self.secure_cloned_response_field,
            response_model_include=self.response_model_include,
            response_model_exclude=self.response_model_exclude,
            response_model_by_alias=self.response_model_by_alias,
            response_model_exclude_unset=self.response_model_exclude_unset,
            response_model_exclude_defaults=self.response_model_exclude_defaults,
            dependency_overrides_provider=dependency_overrides_provider,
        )

    def get_route_handler(self) -> RouteHandler:  # noqa: D102
        async def app(request: Request) -> Response:
            try:
                cache: FastAPICache = request.app.extra[_CACHE_INSTANCE]
            except KeyError as exc:
                raise CacheInitializationError from exc

            if params := cache.routes.get((request.scope['route'].path, request.method), None):
                key_builder, caching_params, dependant = params
            else:
                return await self._get_original_route_handler()(request)

            dependency_cache = None
            if dependant:
                async with AsyncExitStack() as async_exit_stack:
                    solved_dependency = await solve_dependencies(
                        request=request,
                        dependant=dependant,
                        async_exit_stack=async_exit_stack,
                        embed_body_fields=False,
                    )
                if cache.cache_dependencies:
                    dependency_cache = solved_dependency.dependency_cache

            namespace = cache.check_namespace(caching_params)
            cache_key = key_builder(request)
            if cached := await cache.get_cached(cache_key, namespace):
                cache.set_cache_header(cached['headers'], hit=True)
                return _build_cached_response(request, cached)

            response = await self._get_original_route_handler(dependency_cache)(request)
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


class CacheInitializationError(RuntimeError):
    """`FastAPICache.configure_app` was not called or was called more then once."""

    def __init__(self) -> None:
        super().__init__(
            f'{__package__}.{FastAPICache.configure_app.__qualname__}'
            ' must be called once after the app initialization',
        )


class RouteClassError(TypeError):
    """`FastAPICache` decorator was applied to a route other then `CachingRoute`."""

    def __init__(self) -> None:
        super().__init__(
            f'{__package__}.{CachingRoute.__qualname__}'
            ' must be used for routes with'
            f' {__package__}.{FastAPICache.__qualname__}'
            ' decorator',
        )


class _CachedDependency:
    def __init__(self, result: Any) -> None:
        self._result = result

    def __call__(self) -> Any:
        return self._result


class _CachedDependencyProvider:
    def __init__(self, cache: _DependencyCache) -> None:
        self.dependency_overrides = {
            key[0]: _CachedDependency(res)
            for key, res in cache.items()
        }  # fmt: skip


def _cache_routes(
    app: FastAPI,
    endpoints: _CacheEndpoints,
) -> Generator[tuple[tuple[str, str], _CacheMethodParams], Any, None]:
    paths = app.openapi()['paths']
    for route in app.routes:
        if (
            isinstance(route, APIRoute)
            and (cache_params := endpoints.get(route.endpoint, None)) is not None
        ):
            if not isinstance(route, CachingRoute):
                raise RouteClassError

            route_path = route.path
            oapi_path = paths[route_path]
            key_builder = cache_params.pop('key_builder', None)
            if deps := cache_params.pop('dependencies', []):
                dependencies = [
                    get_dependant(
                        path=route.path,
                        call=d.dependency,
                        use_cache=d.use_cache,
                    )
                    for d in deps
                    if d.dependency
                ]
                dependant = Dependant(dependencies=dependencies)
            else:
                dependant = None

            for method in route.methods:
                if key_builder is None:
                    oapi_params = oapi_path[method.lower()].get('parameters', {})
                    key_builder = _key_builder_factory(oapi_params)
                yield (
                    (route_path, method),
                    (key_builder, cast('CacheParamsBase', cache_params), dependant),
                )


def _key_builder_factory(params: list[dict]) -> KeyBuilder:
    params_ = []
    for param in sorted(params, key=lambda p: p['name']):
        if param['in'] == 'query':
            default = param['schema'].get('default', '')
            params_.append((param['name'], default))

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
            chunk = chunk.encode(response.charset)  # noqa: PLW2901
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


def _cached_response(**kwargs: Unpack[CachedResponse]) -> CachedResponse:
    return kwargs


async def _content_stream(content: bytes) -> AsyncGenerator[bytes, None]:
    b = 0
    for e in range(b, len(content), 10240):
        yield content[b:e]
        b = e
    yield content[b:]


def _set_etag(headers: MutableHeaders | dict[str, str], content: bytes) -> None:
    etag = sha256(content, usedforsecurity=False).hexdigest()
    headers['etag'] = f'"{etag}"'
