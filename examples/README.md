Start the complex example:

```sh
uv run uvicorn examples.complex:app
```

The server listens on `http://127.0.0.1:8000` by default. Run the commands below
from the project root in another terminal.

The exact `Time total` values depend on your machine, but cache misses should be
close to one second and cache hits should be much faster.

## Cached response

Execute a first `GET /cached` request. It resolves the endpoint, so it takes about
one second and returns `X-Cache: MISS`:

```sh
curl -sS -w @examples/curl-format.txt http://127.0.0.1:8000/cached -H 'X-Key: secret'
# Hello, World!
#
# HTTP Code:  200
# Body Size:  13
# Time total: 1.001427
# ETag:       "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
# X-Cache:    MISS
```

Repeat the same request. The response is loaded from cache, so it is fast and
returns `X-Cache: HIT`:

```sh
curl -sS -w @examples/curl-format.txt http://127.0.0.1:8000/cached -H 'X-Key: secret'
# Hello, World!
#
# HTTP Code:  200
# Body Size:  13
# Time total: 0.001150
# ETag:       "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
# X-Cache:    HIT
```

Use the returned `ETag` with `If-None-Match`. A matching cached response returns
`304 Not Modified` without a response body:

```sh
curl -sS -w @examples/curl-format.txt http://127.0.0.1:8000/cached \
    -H 'X-Key: secret' \
    -H 'If-None-Match: "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"'
#
# HTTP Code:  304
# Body Size:  0
# Time total: 0.000973
# ETag:       "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
# X-Cache:    HIT
```

## Streaming response

The example also caches a streaming response. The first request is a miss:

```sh
curl -sS -w @examples/curl-format.txt http://127.0.0.1:8000/stream-cached -H 'X-Key: secret'
# Hello, World!
#
# HTTP Code:  200
# Body Size:  13
# Time total: 1.002778
# ETag:       "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
# X-Cache:    MISS
```

The repeated request is a hit:

```sh
curl -sS -w @examples/curl-format.txt http://127.0.0.1:8000/stream-cached -H 'X-Key: secret'
# Hello, World!
#
# HTTP Code:  200
# Body Size:  13
# Time total: 0.001167
# ETag:       "dffd6021bb2bd5b0af676290809ec3a53191dd81c7f70a4b28688a362182986f"
# X-Cache:    HIT
```

## Query parameters

Query parameters are part of the cache key, and their order is normalized. The
first request below is a miss:

```sh
curl -sS -w @examples/curl-format.txt 'http://127.0.0.1:8000/query?a=a'
# {"a":"a","b":"b"}
#
# HTTP Code:  200
# Body Size:  17
# Time total: 0.002067
# ETag:       "5b6fc73120d59ff048925bd03a11d53e1b1837a0f637569716a97a1ca96891b3"
# X-Cache:    MISS
```

The same parameters in another order hit the same cache entry:

```sh
curl -sS -w @examples/curl-format.txt 'http://127.0.0.1:8000/query?b=b&a=a'
# {"a":"a","b":"b"}
#
# HTTP Code:  200
# Body Size:  17
# Time total: 0.000944
# ETag:       "5b6fc73120d59ff048925bd03a11d53e1b1837a0f637569716a97a1ca96891b3"
# X-Cache:    HIT
```

## Non-cached responses

The `/not-cached` endpoint has the same slow data loading, but it is not decorated
with `@cache()`. It does not return cache headers:

```sh
curl -sS -w @examples/curl-format.txt http://127.0.0.1:8000/not-cached -H 'X-Key: secret'
# Hello, World!
#
# HTTP Code:  200
# Body Size:  13
# Time total: 1.032659
# ETag:
# X-Cache:
```

Responses with status codes outside the configured accepted status codes are not
cached either:

```sh
curl -sS -w @examples/curl-format.txt http://127.0.0.1:8000/404
#
# HTTP Code:  404
# Body Size:  0
# Time total: 0.001140
# ETag:
# X-Cache:
```

Requests to protected endpoints without the API key are rejected before caching:

```sh
curl -sS -w @examples/curl-format.txt http://127.0.0.1:8000/cached
# {"detail":"Not authenticated"}
#
# HTTP Code:  401
# Body Size:  30
# Time total: 0.001815
# ETag:
# X-Cache:
```
