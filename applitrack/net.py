"""Outbound HTTP, with the proxy wired in explicitly.

Some hosts -- PythonAnywhere's free tier among them -- have no direct route
to the internet. Everything must go through their proxy, and a direct
connection fails with `[Errno 101] Network is unreachable`.

Setting `http_proxy`/`https_proxy` is *supposed* to be enough: requests reads
them, and httplib2 claims to. In practice it is not reliable here -- the
OAuth token exchange still went out direct and failed. Rather than keep
guessing which library honours which variable, this module hands every
caller a transport with the proxy already attached.

Two transports are needed because the Google stack uses both:

* `requests` -- the OAuth token exchange and credential refresh
* `httplib2` -- everything built by googleapiclient.discovery.build

If no proxy is configured, both fall back to ordinary direct connections, so
this is a no-op on a normal machine.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

log = logging.getLogger("applitrack.net")

_PROXY_VARS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")


def proxy_url() -> str | None:
    """The proxy to use, if any. APPLITRACK_PROXY wins over the environment."""
    explicit = os.environ.get("APPLITRACK_PROXY", "").strip()
    if explicit:
        return explicit
    for var in _PROXY_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return None


def proxies() -> dict[str, str]:
    """A `requests`-style proxy mapping. Empty when none is configured."""
    url = proxy_url()
    return {"http": url, "https": url} if url else {}


def session():
    """A `requests.Session` that will actually go through the proxy."""
    import requests

    sess = requests.Session()
    mapping = proxies()
    if mapping:
        sess.proxies.update(mapping)
        # Do not let a stray no_proxy entry send us back out directly.
        sess.trust_env = False
    return sess


# httplib2 does its proxying through the `socks` module. Versions up to
# ~0.22 bundled a copy; 0.32 does not, and sets `httplib2.socks = None` when
# the import fails -- silently, so every request then goes out direct. On a
# host with no direct route that surfaces as "[Errno 101] Network is
# unreachable" from deep inside the Google client, which is a long way from
# the actual cause. PySocks supplies the module.
PROXY_TYPE_HTTP = 3


def _socks_available() -> bool:
    import httplib2

    return getattr(httplib2, "socks", None) is not None


def _proxy_info():
    """Translate the proxy URL into what httplib2 expects."""
    url = proxy_url()
    if not url:
        return None

    import httplib2

    if not _socks_available():
        raise RuntimeError(
            "A proxy is configured but httplib2 cannot use one: its `socks` "
            "module is missing.\n"
            "Install it:  pip install PySocks\n"
            "Without it every Google API call ignores the proxy and fails "
            "with [Errno 101] Network is unreachable."
        )

    parsed = urlparse(url if "://" in url else f"http://{url}")
    return httplib2.ProxyInfo(
        proxy_type=getattr(httplib2.socks, "PROXY_TYPE_HTTP", PROXY_TYPE_HTTP),
        proxy_host=parsed.hostname,
        proxy_port=parsed.port or 3128,
        proxy_user=parsed.username or None,
        proxy_pass=parsed.password or None,
    )


def http(timeout: int = 90):
    """An `httplib2.Http` with the proxy attached."""
    import httplib2

    return httplib2.Http(proxy_info=_proxy_info(), timeout=timeout)


def authorized_http(credentials, timeout: int = 90):
    """An authorised, proxied transport for googleapiclient."""
    import google_auth_httplib2

    return google_auth_httplib2.AuthorizedHttp(credentials, http=http(timeout))


def build_service(api: str, version: str, credentials):
    """`googleapiclient.discovery.build`, but guaranteed to use the proxy.

    Passing `http=` means the credentials travel on the transport rather than
    being handed to build() separately -- the two are mutually exclusive.
    """
    from googleapiclient.discovery import build

    if proxy_url():
        return build(api, version, http=authorized_http(credentials),
                     cache_discovery=False)
    return build(api, version, credentials=credentials, cache_discovery=False)


def auth_request():
    """A `google.auth` transport request that honours the proxy.

    Used when refreshing an expired token.
    """
    from google.auth.transport.requests import Request

    return Request(session=session())


def describe() -> str:
    url = proxy_url()
    return url or "direct (no proxy)"
