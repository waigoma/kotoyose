"""S3 / MinIO の conditional write と照合済み read による ArtifactStore。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Protocol

from . import process_env
from .artifact_blob_store import ArtifactBlob, ArtifactMissingError, ArtifactStorageError
from .artifact_storage_config import S3ArtifactStorageConfig


class S3Client(Protocol):
    """SDK 固有の transfer / URL 公開を保存契約へ漏らさない。"""

    def put_object(self, **kwargs: object) -> dict: ...
    def get_object(self, **kwargs: object) -> dict: ...


class S3ArtifactStore:
    """同一 key を上書きせず、保存応答の消失も bytes の照合で回収する。"""

    def __init__(self, config: S3ArtifactStorageConfig, client: S3Client | None = None):
        from botocore.exceptions import BotoCoreError, ClientError

        self._recoverable = (BotoCoreError, ClientError)
        self.config = config
        self.client = client if client is not None else self._client(config)
        address = {"kind": "s3", "version": "1", "endpoint": config.endpoint,
                   "bucket": config.bucket, "prefix": config.prefix, "region": config.region}
        self._identity = hashlib.sha256(json.dumps(address, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _client(config: S3ArtifactStorageConfig) -> S3Client:
        import boto3
        from botocore.config import Config

        access = process_env.get(config.access_key_env)
        secret = process_env.get(config.secret_key_env)
        if not access or not secret:
            raise ValueError("S3 artifact storage requires explicit credential environment variables")
        # default credential chain や user config による接続先・認証の暗黙変更を防ぐ。
        return boto3.client(
            "s3", endpoint_url=config.endpoint, region_name=config.region,
            aws_access_key_id=access, aws_secret_access_key=secret,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          connect_timeout=config.request_timeout_seconds,
                          read_timeout=config.request_timeout_seconds,
                          retries={"total_max_attempts": 1}, proxies={}),
        )

    @property
    def identity(self) -> str:
        """credential rotation と保存先変更を区別する。"""
        return self._identity

    def _key(self, blob: ArtifactBlob) -> str:
        if len(blob.key) != 64 or any(char not in "0123456789abcdef" for char in blob.key):
            raise ArtifactStorageError("Invalid artifact storage key")
        return f"{self.config.prefix}/{blob.key}"

    def _read(self, blob: ArtifactBlob) -> bytes:
        response = self.client.get_object(Bucket=self.config.bucket, Key=self._key(blob))
        body = response["Body"]
        try:
            if response.get("ContentLength") != blob.size or response.get("ContentType") != blob.media_type:
                raise ArtifactStorageError("S3 artifact metadata does not match the journal")
            return blob.verify(body.read(blob.size + 1))
        finally:
            body.close()

    def _put(self, blob: ArtifactBlob, content: bytes) -> None:
        blob.verify(content)
        try:
            self.client.put_object(Bucket=self.config.bucket, Key=self._key(blob), Body=content,
                                   ContentType=blob.media_type, IfNoneMatch="*")
        except self._recoverable:
            # 412 と送信後の応答消失は、同一 key の実 bytes が一致した場合だけ成功へ収束する。
            self._read(blob)
            return
        self._read(blob)

    async def put(self, blob: ArtifactBlob, content: bytes) -> None:
        """upload と read-back が完了するまで journal の成功確定を許可しない。"""
        try:
            await asyncio.to_thread(self._put, blob, content)
        except self._recoverable as exc:
            raise ArtifactStorageError("S3 artifact could not be saved and verified") from exc

    async def read(self, blob: ArtifactBlob) -> bytes:
        """保存済み metadata を上限とし、変更・消失・媒体不一致を拒否する。"""
        try:
            return await asyncio.to_thread(self._read, blob)
        except self._recoverable as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if str(code) in {"404", "NoSuchKey", "NotFound"}:
                raise ArtifactMissingError("S3 artifact is missing") from exc
            raise ArtifactStorageError("S3 artifact could not be read and verified") from exc

    def _delete(self, blob: ArtifactBlob) -> None:
        self.client.delete_object(Bucket=self.config.bucket, Key=self._key(blob))

    async def delete(self, blob: ArtifactBlob) -> None:
        """GC は journal の immutable key だけを対象にする。"""
        try:
            await asyncio.to_thread(self._delete, blob)
        except self._recoverable as exc:
            raise ArtifactStorageError("S3 artifact could not be deleted") from exc
