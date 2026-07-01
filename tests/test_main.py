from collections.abc import Callable
from typing import Annotated

import pytest
from aiocache import SimpleMemoryCache
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.routing import APIRoute
from fastapi_caching_route.main import CachingRoute, FastAPICache
from starlette.testclient import TestClient

from examples import invalidate
from examples.complex import app as complex_app


class AppFactory:
    def __init__(
        self,
        *,
        route_class: type[APIRoute] = CachingRoute,
        cache: FastAPICache | None = None,
    ) -> None:
        self.router = APIRouter(route_class=route_class)
        self.cache = cache or FastAPICache(SimpleMemoryCache())

    def create_client(self) -> TestClient:
        app = FastAPI()
        app.include_router(self.router)

        return TestClient(app)


@pytest.fixture(name='anonymous_client', scope='session')
def anonymous_client_fixture() -> TestClient:
    return TestClient(complex_app)


@pytest.fixture(name='client', scope='session')
def client_fixture() -> TestClient:
    return TestClient(complex_app, headers={'X-Key': 'secret'})


def test_configure_app_not_required() -> None:
    af = AppFactory()

    @af.cache()
    @af.router.get('/')
    def cached() -> str:
        return 'Hello, World!'

    res = af.create_client().get('/')
    assert res.headers['x-cache'] == 'MISS'


def test_plain_apiroute_ignores_cache_config() -> None:
    af = AppFactory(route_class=APIRoute)

    @af.cache()
    @af.router.get('/')
    def cached() -> str:
        return 'Hello, World!'

    res = af.create_client().get('/')
    assert 'x-cache' not in res.headers


@pytest.mark.parametrize(
    ('ns_root', 'ns_method'),
    [
        pytest.param('root', None, id='root'),
        pytest.param(None, 'method', id='method'),
        pytest.param('root', 'method', id='root and method'),
    ],
)
def test_namespace(ns_root: str | None, ns_method: str | None) -> None:
    af = AppFactory(cache=FastAPICache(SimpleMemoryCache(namespace=ns_root)))

    @af.cache(namespace=ns_method)
    @af.router.get('/')
    def cached() -> str:
        return 'Hello, World!'

    af.create_client().get('/')


def test_non_authorized(anonymous_client: TestClient) -> None:
    res = anonymous_client.get('/cached')
    assert res.status_code == 401


@pytest.mark.parametrize('url', ['/cached', '/stream-cached'])
def test_cached(client: TestClient, url: str) -> None:
    res = client.get(url)
    assert res.status_code == 200
    assert res.headers['x-cache'] == 'MISS'

    res = client.get(url)
    assert res.status_code == 200
    assert res.headers['x-cache'] == 'HIT'


def test_cache_hit_skips_endpoint_after_early_dependencies() -> None:
    af = AppFactory()
    calls = {'early_dependency': 0, 'endpoint': 0}

    def early_dependency() -> None:
        calls['early_dependency'] += 1

    @af.cache(dependencies=[Depends(early_dependency)])
    @af.router.get('/')
    def cached() -> str:
        calls['endpoint'] += 1
        return 'Hello, World!'

    client = af.create_client()

    assert client.get('/').headers['x-cache'] == 'MISS'
    assert calls == {'early_dependency': 1, 'endpoint': 1}

    assert client.get('/').headers['x-cache'] == 'HIT'
    assert calls == {'early_dependency': 2, 'endpoint': 1}


def _test_valid_etag(client: TestClient, etag: str) -> None:
    res = client.get('/cached', headers={'if-none-match': etag})
    content_len = int(res.headers['content-length'])
    assert res.status_code == 304
    assert len(res.content) == content_len == 0


def _test_invalid_etag(client: TestClient, _etag: str) -> None:
    res = client.get('/cached', headers={'if-none-match': '"invalid"'})
    assert res.status_code == 200
    assert res.headers['x-cache'] == 'HIT'
    assert len(res.content) > 0


def _test_valid_etag_with_w(client: TestClient, etag: str) -> None:
    res = client.get('/cached', headers={'if-none-match': 'W/' + etag})
    content_len = int(res.headers['content-length'])
    assert res.status_code == 304
    assert len(res.content) == content_len == 0


def _test_valid_etag_in_list(client: TestClient, etag: str) -> None:
    res = client.get('/cached', headers={'if-none-match': f'"invalid", W/{etag}'})
    content_len = int(res.headers['content-length'])
    assert res.status_code == 304
    assert len(res.content) == content_len == 0


def _test_valid_etag_with_wildcard(client: TestClient, _etag: str) -> None:
    res = client.get('/cached', headers={'if-none-match': '*'})
    content_len = int(res.headers['content-length'])
    assert res.status_code == 304
    assert len(res.content) == content_len == 0


@pytest.mark.parametrize(
    'tester',
    [
        pytest.param(_test_valid_etag, id='valid'),
        pytest.param(_test_invalid_etag, id='invalid'),
        pytest.param(_test_valid_etag_with_w, id='valid with W/'),
        pytest.param(_test_valid_etag_in_list, id='valid in list'),
        pytest.param(_test_valid_etag_with_wildcard, id='wildcard'),
    ],
)
def test_etag(client: TestClient, tester: Callable[[TestClient, str], None]) -> None:
    res = client.get('/cached')
    etag = res.headers['etag']
    assert res.status_code == 200
    assert etag
    tester(client, etag)


def test_endpoint_etag_is_preserved() -> None:
    af = AppFactory()

    @af.cache()
    @af.router.get('/')
    def cached() -> Response:
        return Response(content='Hello, World!', headers={'ETag': 'W/"custom"'})

    client = af.create_client()

    res = client.get('/')
    assert res.status_code == 200
    assert res.headers['etag'] == 'W/"custom"'
    assert res.headers['x-cache'] == 'MISS'

    res = client.get('/')
    assert res.status_code == 200
    assert res.headers['etag'] == 'W/"custom"'
    assert res.headers['x-cache'] == 'HIT'

    res = client.get('/', headers={'if-none-match': '"custom"'})
    assert res.status_code == 304
    assert res.headers['etag'] == 'W/"custom"'
    assert not res.content


def test_cached_response_status_code_is_preserved() -> None:
    af = AppFactory(cache=FastAPICache(SimpleMemoryCache(), accepted_status_codes={201}))

    @af.cache()
    @af.router.post('/')
    def cached() -> Response:
        return Response(content='Created', status_code=201)

    client = af.create_client()

    res = client.post('/')
    assert res.status_code == 201
    assert res.headers['x-cache'] == 'MISS'

    res = client.post('/')
    assert res.status_code == 201
    assert res.headers['x-cache'] == 'HIT'


def test_vary_headers_are_part_of_default_cache_key() -> None:
    af = AppFactory()
    calls = {'count': 0}

    @af.cache(vary_headers=['Accept-Language'])
    @af.router.get('/')
    def cached(request: Request) -> str:
        calls['count'] += 1
        return request.headers.get('accept-language', 'missing')

    client = af.create_client()

    res = client.get('/', headers={'Accept-Language': 'en'})
    assert res.text == '"en"'
    assert res.headers['vary'] == 'accept-language'
    assert res.headers['x-cache'] == 'MISS'

    res = client.get('/', headers={'Accept-Language': 'ru'})
    assert res.text == '"ru"'
    assert res.headers['vary'] == 'accept-language'
    assert res.headers['x-cache'] == 'MISS'

    res = client.get('/', headers={'Accept-Language': 'en'})
    assert res.text == '"en"'
    assert res.headers['vary'] == 'accept-language'
    assert res.headers['x-cache'] == 'HIT'
    assert calls == {'count': 2}


def test_vary_wildcard_response_is_not_cached() -> None:
    af = AppFactory()
    calls = {'count': 0}

    @af.cache()
    @af.router.get('/')
    def cached() -> Response:
        calls['count'] += 1
        return Response(content=str(calls['count']), headers={'Vary': '*'})

    client = af.create_client()

    res = client.get('/')
    assert res.text == '1'
    assert 'x-cache' not in res.headers

    res = client.get('/')
    assert res.text == '2'
    assert 'x-cache' not in res.headers
    assert calls == {'count': 2}


def test_default_cache_key_includes_http_method() -> None:
    af = AppFactory()

    @af.cache()
    @af.router.get('/same')
    def get_same() -> str:
        return 'GET'

    @af.cache()
    @af.router.post('/same')
    def post_same() -> str:
        return 'POST'

    client = af.create_client()

    res = client.get('/same')
    assert res.text == '"GET"'
    assert res.headers['x-cache'] == 'MISS'

    res = client.post('/same')
    assert res.text == '"POST"'
    assert res.headers['x-cache'] == 'MISS'

    res = client.get('/same')
    assert res.text == '"GET"'
    assert res.headers['x-cache'] == 'HIT'

    res = client.post('/same')
    assert res.text == '"POST"'
    assert res.headers['x-cache'] == 'HIT'


def test_query(client: TestClient) -> None:
    res = client.get('/query', params={'a': 'a'})
    assert res.headers['x-cache'] == 'MISS'
    data = res.json()
    assert isinstance(data, dict)
    assert data['a'] == 'a'
    assert data['b'] == 'b'

    res = client.get('/query', params={'b': 'b', 'a': 'a'})
    assert res.headers['x-cache'] == 'HIT'
    data = res.json()
    assert isinstance(data, dict)
    assert data['a'] == 'a'
    assert data['b'] == 'b'


def test_default_cache_key_includes_repeated_query_values() -> None:
    af = AppFactory()
    calls = {'count': 0}

    @af.cache()
    @af.router.get('/items')
    def get_items(tag: Annotated[list[str], Query()]) -> dict[str, object]:
        calls['count'] += 1
        return {'tag': tag, 'calls': calls['count']}

    client = af.create_client()

    res = client.get('/items', params=[('tag', 'a'), ('tag', 'b')])
    assert res.headers['x-cache'] == 'MISS'
    assert res.json() == {'tag': ['a', 'b'], 'calls': 1}

    res = client.get('/items', params=[('tag', 'c'), ('tag', 'b')])
    assert res.headers['x-cache'] == 'MISS'
    assert res.json() == {'tag': ['c', 'b'], 'calls': 2}

    res = client.get('/items', params=[('tag', 'a'), ('tag', 'b')])
    assert res.headers['x-cache'] == 'HIT'
    assert res.json() == {'tag': ['a', 'b'], 'calls': 1}


def test_invalidation() -> None:
    client = TestClient(invalidate.app)
    user_id = client.post('/users', json={'name': 'Sasa'}).json()['id']
    user_url = f'/users/{user_id}'
    res = client.get(user_url)
    assert res.headers['x-cache'] == 'MISS'
    res = client.get(user_url)
    assert res.headers['x-cache'] == 'HIT'
    client.patch(user_url, json={'name': 'Sasha'})
    res = client.get(user_url)
    assert res.headers['x-cache'] == 'MISS'


@pytest.mark.parametrize(
    ('url', 'status_code'),
    [('/not-cached', 200), ('/404', 404)],
    ids=['not cached', 'not found'],
)
def test_other(client: TestClient, url: str, status_code: int) -> None:
    res = client.get(url)
    assert res.status_code == status_code
    assert 'x-cache' not in res.headers
