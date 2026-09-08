import hashlib
import io
import os
import uuid
from pathlib import Path

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

import main
from main import EMPTY_SHA256, FileStore, hash_stream, iter_files, relative_key


# --------------------------------------------------------------------------- #
# Pure helpers (no database needed)
# --------------------------------------------------------------------------- #

def test_hash_stream_matches_hashlib():
    data = os.urandom(3 * 1024 + 17)
    digest, size = hash_stream(io.BytesIO(data), chunk_size=1024)
    assert digest == hashlib.sha256(data).hexdigest()
    assert size == len(data)


def test_hash_stream_distinguishes_content():
    a, _ = hash_stream(io.BytesIO(b"hello world"))
    b, _ = hash_stream(io.BytesIO(b"completely different bytes"))
    assert a != b
    assert a != EMPTY_SHA256


def test_hash_stream_empty():
    assert hash_stream(io.BytesIO(b"")) == (EMPTY_SHA256, 0)


def test_relative_key_uses_forward_slashes(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "c.txt"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")
    assert relative_key(tmp_path, nested) == "a/b/c.txt"


def test_iter_files_is_sorted_and_skips_directories(tmp_path: Path):
    (tmp_path / "z.txt").write_bytes(b"z")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")
    keys = [relative_key(tmp_path, p) for p in iter_files(tmp_path)]
    assert keys == ["b.txt", "z.txt", "sub/a.txt"]


def test_parser_rejects_missing_directory(tmp_path: Path):
    parser = main.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["upload", str(tmp_path / "nope")])


# --------------------------------------------------------------------------- #
# Integration (needs a reachable MongoDB; set TEST_MONGO_URI or MONGO_URI)
# --------------------------------------------------------------------------- #

@pytest.fixture
def store():
    uri = os.getenv("TEST_MONGO_URI") or os.getenv("MONGO_URI") or main.DEFAULT_MONGO_URI
    client = MongoClient(uri, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except PyMongoError:
        pytest.skip(f"MongoDB not reachable at {uri}")
    db_name = f"dedupe_test_{uuid.uuid4().hex[:8]}"
    store = FileStore(client[db_name])
    store.ensure_indexes()
    try:
        yield store
    finally:
        client.drop_database(db_name)
        client.close()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "one.txt").write_bytes(b"same content")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "two.txt").write_bytes(b"same content")
    (tmp_path / "three.bin").write_bytes(os.urandom(2 * 1024 * 1024 + 5))
    return tmp_path


def test_upload_dedupes_by_content(store: FileStore, tree: Path):
    counts = store.upload_directory(tree, "src", dry_run=False)
    assert (counts.new, counts.duplicate, counts.existing, counts.failed) == (2, 1, 0, 0)

    docs = {d["relative_path"]: d for d in store.files.find()}
    assert set(docs) == {"one.txt", "nested/two.txt", "three.bin"}
    assert docs["one.txt"]["file_hash"] == docs["nested/two.txt"]["file_hash"]
    assert docs["one.txt"]["file_id"] == docs["nested/two.txt"]["file_id"]
    assert docs["one.txt"]["file_hash"] != docs["three.bin"]["file_hash"]
    assert docs["three.bin"]["file_size"] == 2 * 1024 * 1024 + 5
    assert docs["one.txt"]["mime_type"] == "text/plain"
    assert store.db["fs.files"].count_documents({}) == 2  # only unique content stored


def test_upload_is_idempotent(store: FileStore, tree: Path):
    store.upload_directory(tree, "src", dry_run=False)
    counts = store.upload_directory(tree, "src", dry_run=False)
    assert (counts.new, counts.duplicate, counts.existing) == (0, 0, 3)
    assert store.files.count_documents({}) == 3
    assert store.db["fs.files"].count_documents({}) == 2


def test_dry_run_stores_nothing(store: FileStore, tree: Path):
    counts = store.upload_directory(tree, "src", dry_run=True)
    assert (counts.new, counts.duplicate) == (2, 1)
    assert store.files.count_documents({}) == 0
    assert store.db["fs.files"].count_documents({}) == 0


def test_check_directory(store: FileStore, tree: Path):
    before = store.check_directory(tree, "src")
    assert (before.new, before.existing) == (3, 0)
    store.upload_directory(tree, "src", dry_run=False)
    (tree / "four.txt").write_bytes(b"fresh")
    after = store.check_directory(tree, "src")
    assert (after.new, after.existing) == (1, 3)


def test_digest_repairs_legacy_documents(store: FileStore, tree: Path):
    """Documents written by the old tool: absolute path only, empty-sha hash."""
    path = tree / "one.txt"
    with path.open("rb") as handle:
        file_id = store.fs.put(handle, filename=path.name)
    store.files.insert_one({"file_id": file_id, "original_file_path": str(path),
                            "file_hash": EMPTY_SHA256})
    store.files.insert_one({"file_id": file_id, "original_file_path": "missing"})

    counts = store.digest()
    assert (counts.new, counts.failed) == (2, 0)
    expected = hashlib.sha256(b"same content").hexdigest()
    for doc in store.files.find():
        assert doc["file_hash"] == expected
        assert doc["file_size"] == len(b"same content")

    # A legacy document is recognised by absolute path, so re-upload skips it.
    counts = store.upload_directory(tree, "src", dry_run=False)
    assert counts.existing == 1


def test_digest_reports_missing_blob(store: FileStore):
    from bson import ObjectId
    store.files.insert_one({"file_id": ObjectId(), "original_file_path": "gone"})
    counts = store.digest()
    assert (counts.new, counts.failed) == (0, 1)


def test_summary_and_duplicates(store: FileStore, tree: Path):
    store.upload_directory(tree, "src", dry_run=False)
    store.files.insert_one({"file_id": None, "original_file_path": "unhashed"})

    summary = store.summary()
    assert summary.total_files == 4
    assert summary.hashed_files == 3
    assert summary.stored_blobs == 2
    assert summary.duplicate_groups == 1
    assert summary.redundant_files == 1
    assert summary.redundant_bytes == len(b"same content")
    assert sorted(Path(p).name for p in summary.example["paths"]) == ["one.txt", "two.txt"]

    groups = list(store.duplicate_groups(limit=5))
    assert len(groups) == 1
    assert groups[0]["count"] == 2
