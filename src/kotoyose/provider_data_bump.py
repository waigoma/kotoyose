"""宣言のダウンロード境界を維持してバージョンと配布物のハッシュを更新する。"""

import hashlib
import json
import re
from tempfile import TemporaryFile

from . import provider_data_download
from .provider_data import check_url, parse_declaration
from .provider_data_runtime import MAX_ARTIFACT_BYTES


async def bump_declaration_version(
    document: str, new_version: str, *, url_overrides: dict[str, list[str]] | None = None, fetcher=None,
) -> dict:
    """固定版を更新し、再検証済み JSON とハッシュを返す。保存は行わない。

    fetcher は _fetch と同じ非同期契約 (URLs、許可ホスト、出力ストリーム) を使う。
    不正な版・URL・宣言は ValueError、安全に取得できない配布物は DownloadError。
    """
    if not isinstance(new_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9]+)*", new_version):
        raise ValueError("invalid version")
    old = parse_declaration(document.encode("utf-8"))
    doc = json.loads(document)
    if url_overrides is not None and (
        not isinstance(url_overrides, dict) or not url_overrides.keys() <= doc["platforms"].keys()
        or any(not isinstance(urls, list) or any(not isinstance(url, str) for url in urls)
               for urls in url_overrides.values())
    ):
        raise ValueError("invalid URL overrides")
    platforms = []
    # 全プラットフォームの URL を先に検証し、不正な後続 URL がある状態で通信しない。
    for key, platform in doc["platforms"].items():
        pin = platform["install"]
        urls = (url_overrides or {}).get(key)
        if not urls:
            if not any(old.install.version in url for url in pin["urls"]):
                raise ValueError(f"URL override required for {key}")
            urls = [url.replace(old.install.version, new_version) for url in pin["urls"]]
        if not 1 <= len(urls) <= 4 or len(set(urls)) != len(urls):
            raise ValueError("invalid download URLs")
        for url in urls:
            check_url(url, old.download_hosts)
        platforms.append({"platform": key, "urls": urls})
    for platform in platforms:
        with TemporaryFile() as output:
            await (fetcher or provider_data_download._fetch)(tuple(platform["urls"]), old.download_hosts, output)
            output.seek(0)
            hasher = hashlib.sha256()
            size = 0
            while chunk := output.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise ValueError("artifact exceeds its size limit")
                hasher.update(chunk)
            if not size:
                raise ValueError("empty artifact")
        platform["sha256"] = hasher.hexdigest()
        doc["platforms"][platform["platform"]]["install"].update(
            version=new_version, urls=platform["urls"], sha256=platform["sha256"],
        )
    updated = json.dumps(doc, indent=2) + "\n"
    try:
        row = parse_declaration(updated.encode("utf-8"))
    except ValueError as exc:
        if "native_skill_layout" not in doc or "native_skill_layout" not in str(exc):
            raise
        del doc["native_skill_layout"]
        updated = json.dumps(doc, indent=2) + "\n"
        row = parse_declaration(updated.encode("utf-8"))
    return {"document": updated, "sha256": row.sha256, "version": new_version, "platforms": platforms}
