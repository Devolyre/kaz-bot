"""Use an explicitly configured system proxy, without reading .netrc."""

from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass


def proxy_for(url: str):
    parsed = urlsplit(url)
    if not parsed.hostname or proxy_bypass(parsed.hostname):
        return None
    return getproxies().get(parsed.scheme)
