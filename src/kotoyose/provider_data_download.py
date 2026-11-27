"""HTTPS fetches for owner-approved data declarations."""

import asyncio
import ipaddress
from pathlib import Path
import socket
from tempfile import TemporaryFile
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

import httpx

from .cli_install_spec import platform_key
from .provider_data import Declaration, check_url
from .provider_data_runtime import MAX_ARTIFACT_BYTES, ensure_executable, install_stream, installed_path

_DOWNLOAD_TIMEOUT = 60
_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 4


class DownloadError(ValueError):
    """The artifact could not be fetched safely."""


async def _addresses(host: str) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise DownloadError("DNS lookup failed") from exc
    addresses = tuple(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise DownloadError("DNS returned no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_reserved or "%" in address:
            raise DownloadError("DNS resolved to a non-public IP address")
    return addresses


async def _body(response: httpx.Response, output: BinaryIO) -> None:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise DownloadError("artifact response has an invalid Content-Length") from None
        if declared > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds its download size limit")
    output.seek(0)
    output.truncate()
    size = 0
    async for chunk in response.aiter_raw(chunk_size=128 * 1024):
        size += len(chunk)
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds its download size limit")
        output.write(chunk)
    output.seek(0)


async def _hop(url: str, host: str, output: BinaryIO) -> str | None:
    addresses = await _addresses(host)
    # Connect to the checked IP, retaining the declared host for HTTP and certificate verification.
    # A new pool per hop prevents reuse of a connection authenticated for a different origin.
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        for address in addresses:
            target = httpx.URL(url).copy_with(host=address)
            try:
                async with client.stream(
                    "GET", target, headers={"Host": host, "Accept-Encoding": "identity"},
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.status_code in _REDIRECTS:
                        location = response.headers.get("location", "")
                        if not location or any(char.isspace() or ord(char) < 32 for char in location):
                            raise DownloadError("invalid download redirect")
                        return urljoin(url, location)
                    if response.status_code != 200:
                        raise DownloadError(f"server returned HTTP {response.status_code}")
                    await _body(response, output)
                    return None
            except (httpx.ConnectError, httpx.ConnectTimeout):
                continue
    raise DownloadError("could not connect over HTTPS")


async def _candidate(url: str, hosts: tuple[str, ...], output: BinaryIO) -> str:
    check_url(url, hosts)
    host = urlsplit(url).netloc
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            check_url(url, (host,))
        except ValueError as exc:
            raise DownloadError("redirect must use HTTPS on the original download host") from exc
        redirect = await _hop(url, host, output)
        if redirect is None:
            return url
        url = redirect
    raise DownloadError("too many download redirects")


async def _fetch(urls: tuple[str, ...], hosts: tuple[str, ...], output: BinaryIO) -> str:
    errors = []
    try:
        async with asyncio.timeout(_DOWNLOAD_TIMEOUT):
            for url in urls:
                host = urlsplit(url).netloc
                try:
                    return await _candidate(url, hosts, output)
                except DownloadError as exc:
                    errors.append(f"{host}: {exc}")
                except httpx.HTTPError:
                    errors.append(f"{host}: HTTPS transfer failed")
    except TimeoutError as exc:
        raise DownloadError("Artifact download timed out") from exc
    raise DownloadError("Artifact download failed (" + ", ".join(errors) + ")")


def install_download(row: Declaration, *, child_home: Path) -> None:
    ensure_executable(row, child_home)
    target = row.platform(platform_key())
    assert target is not None
    if target.artifact.format not in ("raw", "tar.gz"):
        raise ValueError("unsupported download artifact format")
    if installed_path(row, child_home) is not None:
        return
    with TemporaryFile() as payload:
        url = asyncio.run(_fetch(target.artifact.url_templates, row.download_hosts, payload))
        install_stream(row, payload, child_home=child_home, source=url)
