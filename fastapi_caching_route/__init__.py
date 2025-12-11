"""FastAPI Caching Route."""

__all__ = ['CachingRoute', 'FastAPICache', '__version__']

from fastapi_caching_route._version import version as __version__
from fastapi_caching_route.main import CachingRoute, FastAPICache
