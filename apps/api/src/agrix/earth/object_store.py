"""Durable R2 checkpoints for the local POSIX scientific cache.

Blobs are immutable and content-addressed. The index is published only after
every referenced blob has uploaded; an interrupted checkpoint retains the last
committed index. PostgreSQL, not this cache, owns farm records and job state.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from threading import RLock

from agrix.earth.manifest import file_hash

DEFAULT_PREFIX = "ghayth/production/scientific"


class CacheStorageError(RuntimeError):
    pass


class ObjectStoreCache:
    def __init__(self, directory, bucket, prefix=DEFAULT_PREFIX, *, client):
        self.directory = Path(directory).resolve()
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if not bucket or not self.prefix or ".." in self.prefix.split("/"):
            raise ValueError("A bucket and a dedicated object-storage prefix are required")
        self.client = client
        self._lock = RLock()
        self._committed = set()

    @property
    def index_key(self):
        return self.prefix + "/index.json"

    def _path(self, relative):
        if not isinstance(relative, str) or "\\" in relative:
            raise CacheStorageError("Invalid cache checkpoint path")
        name = PurePosixPath(relative)
        if name.is_absolute() or not name.parts or any(p in {".", ".."} for p in name.parts):
            raise CacheStorageError("Invalid cache checkpoint path")
        path = self.directory.joinpath(*name.parts)
        if not path.resolve().is_relative_to(self.directory):
            raise CacheStorageError("Cache checkpoint escapes its directory")
        return path

    def _index(self):
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.index_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                return {}
            raise CacheStorageError("Cannot read the scientific cache checkpoint") from exc
        body = response["Body"]
        try:
            raw = body.read(16 * 1024 * 1024 + 1)
        finally:
            body.close()
        if len(raw) > 16 * 1024 * 1024:
            raise CacheStorageError("Scientific cache checkpoint index is too large")
        try:
            document = json.loads(raw)
            records = document["files"]
            if document["version"] != 1 or not isinstance(records, dict):
                raise ValueError("Invalid checkpoint schema")
            for name, item in records.items():
                self._path(name)
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                    or type(item.get("size")) is not int
                    or item["size"] < 0
                ):
                    raise ValueError("Invalid checkpoint record")
        except (KeyError, TypeError, ValueError) as exc:
            raise CacheStorageError("Invalid scientific cache checkpoint") from exc
        return records

    def restore(self):
        with self._lock:
            records = self._index()
            self.directory.mkdir(parents=True, exist_ok=True)
            for relative, item in sorted(records.items()):
                path = self._path(relative)
                if (
                    path.is_file()
                    and path.stat().st_size == item["size"]
                    and file_hash(path) == item["sha256"]
                ):
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                response = self.client.get_object(
                    Bucket=self.bucket, Key=self.prefix + "/blobs/" + item["sha256"]
                )
                body = response["Body"]
                temporary = None
                try:
                    digest, size = hashlib.sha256(), 0
                    with tempfile.NamedTemporaryFile(
                        dir=path.parent, suffix=".tmp", delete=False
                    ) as f:
                        temporary = Path(f.name)
                        for chunk in iter(lambda body=body: body.read(1024 * 1024), b""):
                            size += len(chunk)
                            if size > item["size"]:
                                raise CacheStorageError("Scientific asset size mismatch")
                            digest.update(chunk)
                            f.write(chunk)
                    if size != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise CacheStorageError("Scientific asset checksum mismatch")
                    temporary.replace(path)
                finally:
                    body.close()
                    if temporary:
                        temporary.unlink(missing_ok=True)
            self._committed = {item["sha256"] for item in records.values()}
            return len(records)

    def checkpoint(self):
        with self._lock:
            records, uploaded = {}, set(self._committed)
            self.directory.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.directory.rglob("*")):
                if path.is_symlink():
                    raise CacheStorageError("Scientific cache checkpoints cannot contain symlinks")
                if not path.is_file() or path.name.endswith((".tmp", ".incomplete")):
                    continue
                relative = path.relative_to(self.directory).as_posix()
                self._path(relative)
                # Snapshot each file before hashing/uploading so metadata describes
                # the exact transmitted bytes, even if an atomic local replace occurs.
                with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as source:
                    with path.open("rb") as original:
                        shutil.copyfileobj(original, source)
                    source.seek(0)
                    digest, size = hashlib.sha256(), 0
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                    checksum = digest.hexdigest()
                    if checksum not in uploaded:
                        source.seek(0)
                        self.client.put_object(
                            Bucket=self.bucket,
                            Key=self.prefix + "/blobs/" + checksum,
                            Body=source,
                            ContentLength=size,
                            ContentType="application/octet-stream",
                        )
                        uploaded.add(checksum)
                records[relative] = {"sha256": checksum, "size": size}
            self.client.put_object(
                Bucket=self.bucket,
                Key=self.index_key,
                Body=json.dumps({"version": 1, "files": records}, sort_keys=True).encode(),
                ContentType="application/json",
            )
            self._committed = uploaded
            return len(records)


def cache_store_from_env(directory):
    bucket = os.getenv("AGRIX_R2_BUCKET")
    if not bucket:
        return None
    gateway = os.getenv("AGRIX_CACHE_GATEWAY")
    if gateway:
        from agrix.earth.cache_gateway import GATEWAY_URL, CacheGatewayClient

        if gateway != GATEWAY_URL:
            raise ValueError("The cache gateway must use the private container binding")
        prefix = os.getenv("AGRIX_R2_PREFIX", DEFAULT_PREFIX)
        return ObjectStoreCache(directory, bucket, prefix, client=CacheGatewayClient(prefix))
    from botocore.config import Config
    from botocore.session import get_session

    names = ("AGRIX_R2_ENDPOINT", "AGRIX_R2_ACCESS_KEY_ID", "AGRIX_R2_SECRET_ACCESS_KEY")
    if not all(os.getenv(name) for name in names):
        raise ValueError("R2 cache persistence requires its endpoint and scoped API credentials")
    client = get_session().create_client(
        "s3",
        endpoint_url=os.environ["AGRIX_R2_ENDPOINT"],
        aws_access_key_id=os.environ["AGRIX_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AGRIX_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"mode": "standard", "max_attempts": 3},
            connect_timeout=10,
            read_timeout=60,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return ObjectStoreCache(
        directory,
        bucket,
        os.getenv("AGRIX_R2_PREFIX", DEFAULT_PREFIX),
        client=client,
    )
