"""Stream scientific objects through the container's private Cloudflare binding."""

import io
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from botocore.exceptions import ClientError

GATEWAY_URL = "http://ghayth-cache.internal"
PART_SIZE = 32 * 1024 * 1024


class CacheGatewayClient:
    def __init__(self, prefix, *, opener=None):
        self.prefix = prefix.strip("/") + "/"
        # The virtual hostname is intercepted by Cloudflare; ignore host proxies.
        self.opener = opener or build_opener(ProxyHandler({}))

    def _request(self, method, key, body=None, *, query=None, length=None):
        if not key.startswith(self.prefix):
            raise ValueError("Cache key is outside the configured prefix")
        relative = key[len(self.prefix) :]
        url = GATEWAY_URL + "/" + relative
        if query:
            url += "?" + urlencode(query)
        headers = {}
        if body is not None:
            headers["Content-Length"] = str(length if length is not None else len(body))
        try:
            return self.opener.open(
                Request(url, data=body, headers=headers, method=method), timeout=120
            )
        except HTTPError as exc:
            exc.close()
            code = "NoSuchKey" if exc.code == 404 else str(exc.code)
            raise ClientError({"Error": {"Code": code}}, method) from None

    def get_object(self, *, Bucket, Key):
        return {"Body": self._request("GET", Key)}

    def put_object(self, *, Bucket, Key, Body, ContentLength=None, **kwargs):
        source = Body if hasattr(Body, "read") else io.BytesIO(Body)
        length = ContentLength
        if length is None:
            position = source.tell()
            source.seek(0, 2)
            length = source.tell() - position
            source.seek(position)
        if Key.endswith("/index.json") or length <= PART_SIZE:
            with self._request("PUT", Key, source, length=length):
                return
        with self._request("POST", Key) as response:
            upload_id = json.load(response)["upload_id"]
        parts = []
        try:
            while chunk := source.read(PART_SIZE):
                with self._request(
                    "PUT",
                    Key,
                    chunk,
                    query={"upload_id": upload_id, "part_number": len(parts) + 1},
                ) as response:
                    parts.append(json.load(response))
            payload = json.dumps(parts).encode()
            with self._request("POST", Key, payload, query={"upload_id": upload_id}):
                pass
        except Exception:
            try:
                with self._request("DELETE", Key, query={"upload_id": upload_id}):
                    pass
            except Exception:
                pass
            raise
