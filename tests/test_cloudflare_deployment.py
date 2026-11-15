import hashlib
import io
import json

import pytest
from agrix.api import create_app
from agrix.config import Settings
from agrix.deployment import prepare_environment
from agrix.earth.object_store import CacheStorageError, ObjectStoreCache
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.fail_upload = False

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        if self.fail_upload and "/blobs/" in Key:
            raise OSError("simulated interrupted upload")
        self.objects[Key] = Body.read() if hasattr(Body, "read") else Body
        self.uploads.append(Key)


def test_checkpoint_survives_a_fresh_container_and_uploads_unchanged_blobs_once(
    tmp_path,
):
    client = FakeS3()
    directory = tmp_path / "first"
    (directory / "raw").mkdir(parents=True)
    (directory / "raw/weather.json").write_text('{"rain":12}')
    (directory / "assets.json").write_text('{"assets":[]}')
    (directory / "incomplete.tmp").write_text("unfinished")
    first = ObjectStoreCache(directory, "bucket", client=client)
    assert first.restore() == 0
    assert first.checkpoint() == 2
    uploads = list(client.uploads)
    first.checkpoint()
    assert client.uploads[len(uploads) :] == [first.index_key]
    restored = ObjectStoreCache(tmp_path / "fresh", "bucket", client=client)
    assert restored.restore() == 2
    assert (restored.directory / "raw/weather.json").read_text() == '{"rain":12}'
    assert not (restored.directory / "incomplete.tmp").exists()


def test_interrupted_upload_preserves_the_last_committed_checkpoint(tmp_path):
    client = FakeS3()
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.json").write_text("original")
    store = ObjectStoreCache(source, "bucket", client=client)
    store.checkpoint()
    committed = client.objects[store.index_key]
    (source / "data.json").write_text("changed")
    client.fail_upload = True
    with pytest.raises(OSError):
        store.checkpoint()
    assert client.objects[store.index_key] == committed
    recovered = ObjectStoreCache(tmp_path / "recovered", "bucket", client=client)
    recovered.restore()
    assert (recovered.directory / "data.json").read_text() == "original"


def test_restore_rejects_corruption_and_directory_traversal(tmp_path):
    client = FakeS3()
    store = ObjectStoreCache(tmp_path / "cache", "bucket", client=client)
    checksum = hashlib.sha256(b"valid").hexdigest()
    document = {"version": 1, "files": {"asset.tif": {"sha256": checksum, "size": 5}}}
    client.objects[store.index_key] = json.dumps(document).encode()
    client.objects[store.prefix + "/blobs/" + checksum] = b"wrong"
    with pytest.raises(CacheStorageError, match="checksum"):
        store.restore()
    assert not (store.directory / "asset.tif").exists()
    assert not list(store.directory.glob("*.tmp"))
    document["files"] = {"../escape": {"sha256": checksum, "size": 5}}
    client.objects[store.index_key] = json.dumps(document).encode()
    with pytest.raises(CacheStorageError, match="path"):
        store.restore()
    assert not (tmp_path / "escape").exists()


def test_restore_rejects_a_symlink_escape_before_reading_objects(tmp_path):
    client = FakeS3()
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    store = ObjectStoreCache(root, "bucket", client=client)
    document = {
        "version": 1,
        "files": {"linked/asset": {"sha256": "0" * 64, "size": 0}},
    }
    client.objects[store.index_key] = json.dumps(document).encode()
    with pytest.raises(CacheStorageError, match="escapes"):
        store.restore()
    assert not (outside / "asset").exists()


def test_cloudflare_startup_requires_tls_postgres_and_preserves_url_escaping(
    monkeypatch,
):
    values = {
        "AGRIX_DEPLOYMENT": "cloudflare",
        "AGRIX_ENV": "production",
        "DATABASE_URL": "postgresql://user:p%40ss@db.example/ghayth?sslmode=require",
        "AGRIX_ACCESS_PASSWORD": "workspace",
        "AGRIX_SESSION_SECRET": "s" * 32,
        "AGRIX_INTERNAL_TOKEN": "i" * 32,
        "AGRIX_ANALYSIS_WORKERS": "1",
        "AGRIX_R2_BUCKET": "bucket",
        "AGRIX_CACHE_GATEWAY": "http://ghayth-cache.internal",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    assert prepare_environment().database_url.startswith(
        "postgresql+psycopg://user:p%40ss@"
    )
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.sqlite3")
    with pytest.raises(ValueError, match="hosted PostgreSQL"):
        prepare_environment()
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example/ghayth")
    with pytest.raises(ValueError, match="sslmode"):
        prepare_environment()


def test_private_gateway_streams_checkpoints_and_aborts_failed_multipart_uploads(
    tmp_path, monkeypatch
):
    from urllib.error import HTTPError
    from urllib.parse import parse_qs, urlsplit

    from agrix.earth import cache_gateway

    class Gateway:
        def __init__(self):
            self.objects, self.parts, self.events = {}, {}, []
            self.fail = False

        def open(self, request, timeout):
            url = urlsplit(request.full_url)
            assert url.hostname == "ghayth-cache.internal"
            key, query = url.path[1:], parse_qs(url.query)
            method = request.get_method()
            body = request.data
            if body is not None:
                body = body.read() if hasattr(body, "read") else body
                assert len(body) == int(request.get_header("Content-length"))
            if method == "GET":
                if key not in self.objects:
                    raise HTTPError(request.full_url, 404, "Missing", {}, io.BytesIO())
                return io.BytesIO(self.objects[key])
            if method == "POST" and not query:
                self.parts = {}
                self.events.append("start")
                return io.BytesIO(b'{"upload_id":"test-upload"}')
            if method == "DELETE":
                self.events.append("abort")
                return io.BytesIO()
            if method == "POST":
                parts = json.loads(body)
                self.objects[key] = b"".join(self.parts[p["partNumber"]] for p in parts)
                self.events.append("complete")
            elif query:
                number = int(query["part_number"][0])
                if self.fail and number == 2:
                    raise HTTPError(
                        request.full_url, 500, "Unavailable", {}, io.BytesIO()
                    )
                self.parts[number] = body
                return io.BytesIO(
                    json.dumps({"partNumber": number, "etag": str(number)}).encode()
                )
            else:
                self.objects[key] = body
            return io.BytesIO()

    monkeypatch.setattr(cache_gateway, "PART_SIZE", 8)
    gateway = Gateway()
    client = cache_gateway.CacheGatewayClient("scientific", opener=gateway)
    source = tmp_path / "source"
    source.mkdir()
    (source / "weather.json").write_bytes(b"a small scientific asset")
    store = ObjectStoreCache(source, "bucket", "scientific", client=client)
    assert store.restore() == 0
    assert store.checkpoint() == 1
    assert gateway.events == ["start", "complete"]
    committed = gateway.objects["index.json"]
    restored = ObjectStoreCache(
        tmp_path / "fresh", "bucket", "scientific", client=client
    )
    assert restored.restore() == 1
    assert (
        restored.directory / "weather.json"
    ).read_bytes() == b"a small scientific asset"
    (source / "weather.json").write_bytes(b"a changed scientific asset")
    gateway.fail = True
    with pytest.raises(ClientError):
        store.checkpoint()
    assert gateway.events[-1] == "abort"
    assert gateway.objects["index.json"] == committed


def test_gateway_configuration_rejects_a_public_endpoint(tmp_path, monkeypatch):
    from agrix.earth.cache_gateway import CacheGatewayClient
    from agrix.earth.object_store import cache_store_from_env

    monkeypatch.setenv("AGRIX_R2_BUCKET", "bucket")
    monkeypatch.setenv("AGRIX_CACHE_GATEWAY", "http://ghayth-cache.internal")
    assert isinstance(cache_store_from_env(tmp_path).client, CacheGatewayClient)
    monkeypatch.setenv("AGRIX_CACHE_GATEWAY", "https://public.example")
    with pytest.raises(ValueError, match="private"):
        cache_store_from_env(tmp_path)


def test_private_runtime_probe_requires_its_own_secret(monkeypatch):
    monkeypatch.setenv("AGRIX_INTERNAL_TOKEN", "private-internal-token")
    app = create_app("sqlite+pysqlite:///:memory:", initialize=True)
    with TestClient(app) as client:
        assert client.get("/_internal/runtime").status_code == 404
        headers = {"X-Agrix-Internal-Token": "private-internal-token"}
        assert client.get("/_internal/runtime", headers=headers).json() == {
            "busy": False
        }
        app.state.speech_tasks = {object()}
        assert client.get("/_internal/runtime", headers=headers).json() == {
            "busy": True
        }


def test_cache_restore_precedes_runner_start_and_requires_serial_processing(tmp_path):
    events = []

    class Store:
        def restore(self):
            events.append("restore")

    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        cache_directory=tmp_path,
        analysis_workers=1,
    )
    app = create_app(settings=settings, initialize=True, cache_store=Store())
    app.state.runner.resume_pending = lambda: events.append("resume")
    with TestClient(app):
        assert events == ["restore", "resume"]
    with pytest.raises(ValueError, match="one analysis worker"):
        create_app("sqlite+pysqlite:///:memory:", initialize=True, cache_store=Store())


@pytest.mark.parametrize("storage_fails", [False, True])
def test_analysis_is_not_terminal_until_scientific_bytes_are_durable(
    tmp_path, storage_fails
):
    import time
    from datetime import date
    from threading import Event

    from agrix.demo import demo_plot
    from agrix.jobs import pending_profile
    from agrix.models import Availability

    entered, release = Event(), Event()

    class Store:
        def restore(self):
            pass

        def checkpoint(self):
            entered.set()
            assert release.wait(10)
            if storage_fails:
                raise CacheStorageError("Simulated unavailable storage")

    class Pipeline:
        def __init__(self, *args, **kwargs):
            self.progress_callback = None

        def run(self, plot, *args, **kwargs):
            profile = pending_profile(plot, "cloudflare-storage-test")
            profile.layers[0].availability = Availability.partial
            profile.layers[0].state = "partial"
            return profile

    settings = Settings(
        database_url="sqlite:///" + str(tmp_path / "runtime.sqlite3"),
        cache_directory=tmp_path / "cache",
        analysis_workers=1,
        network_enabled=False,
        analysis_start=date(2024, 10, 1),
        analysis_end=date(2024, 10, 31),
    )
    app = create_app(
        settings=settings,
        initialize=True,
        pipeline_factory=Pipeline,
        cache_store=Store(),
    )
    with TestClient(app) as client:
        payload = demo_plot().model_dump(mode="json")
        payload["analysis_mode"] = "live"
        plot = client.post("/plots", json=payload).json()
        job = client.post(f"/plots/{plot['id']}/analyses").json()
        try:
            assert entered.wait(5)
            assert client.get(f"/analyses/{job['id']}").json()["status"] == "running"
            assert app.state.runner.has_work()
        finally:
            release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get(f"/analyses/{job['id']}").json()
            if result["status"] not in {"pending", "running"}:
                break
            time.sleep(0.02)
        assert result["status"] == ("failed" if storage_fails else "partial")
        if storage_fails:
            assert result["error"]["values"]["error_type"] == "CacheStorageError"
