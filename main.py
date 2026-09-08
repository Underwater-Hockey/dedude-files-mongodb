#!/usr/bin/env python3
"""Content-hash based file archive and duplicate finder backed by MongoDB GridFS.

Files are hashed (SHA-256) as they are read from disk. The bytes of a file are
stored in GridFS only the first time a given hash is seen; later files with the
same content get a metadata document that points at the existing blob.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import mimetypes
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

import gridfs
from gridfs.errors import NoFile
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

CHUNK_SIZE = 1024 * 1024
DEFAULT_MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "hash_index_db"
PROGRESS_EVERY = 100

# SHA-256 of zero bytes. Versions of this tool before the hashing fix stamped
# every document with this value, so documents carrying it are re-digested.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

log = logging.getLogger("dedupe")


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def hash_stream(stream: BinaryIO, chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
    """Return (sha256 hex digest, byte count) for a readable binary stream."""
    hasher = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        hasher.update(chunk)
        size += len(chunk)
    return hasher.hexdigest(), size


def hash_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        return hash_stream(handle)


def iter_files(root: Path) -> Iterator[Path]:
    """Yield every regular file under root, deterministically ordered."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_file():
                yield path


def relative_key(root: Path, path: Path) -> str:
    """Path of file relative to root, with forward slashes on every platform."""
    return path.relative_to(root).as_posix()


@dataclass
class Counts:
    new: int = 0
    duplicate: int = 0
    existing: int = 0
    failed: int = 0
    total: int = 0

    def bump(self, outcome: str) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)


@dataclass
class Summary:
    total_files: int = 0
    hashed_files: int = 0
    stored_blobs: int = 0
    duplicate_groups: int = 0
    redundant_files: int = 0
    redundant_bytes: int = 0
    example: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

class FileStore:
    def __init__(self, db: Database):
        self.db = db
        self.files: Collection = db["files"]
        self.fs = gridfs.GridFS(db)

    def ensure_indexes(self) -> None:
        # Documents written by older versions have no relative_path, so the
        # unique index is partial to avoid a null-key collision between them.
        self.files.create_index(
            [("source", ASCENDING), ("relative_path", ASCENDING)],
            unique=True,
            partialFilterExpression={"relative_path": {"$exists": True}},
            name="source_relative_path",
        )
        self.files.create_index("file_hash", name="file_hash")
        self.files.create_index("original_file_path", name="original_file_path")

    # -- lookups ----------------------------------------------------------- #

    def find_by_path(self, source: str, key: str, absolute: str) -> dict | None:
        return self.files.find_one(
            {"$or": [
                {"source": source, "relative_path": key},
                {"original_file_path": absolute},  # documents from older versions
            ]},
            {"_id": 1},
        )

    def find_by_hash(self, file_hash: str) -> dict | None:
        return self.files.find_one({"file_hash": file_hash}, {"file_id": 1})

    # -- upload ------------------------------------------------------------ #

    def upload_file(self, root: Path, path: Path, source: str, dry_run: bool,
                    seen: set[str] | None = None) -> str:
        """Store one file. Returns one of: existing, duplicate, new.

        In a dry run nothing is written, so `seen` (hashes encountered earlier
        in the same run) stands in for the database when spotting duplicates.
        """
        key = relative_key(root, path)
        absolute = str(path)
        if self.find_by_path(source, key, absolute):
            log.debug("Already indexed: %s", key)
            return "existing"

        file_hash, size = hash_file(path)
        match = self.find_by_hash(file_hash)
        outcome = "duplicate" if match or (seen is not None and file_hash in seen) else "new"
        if dry_run:
            if seen is not None:
                seen.add(file_hash)
            log.info("[dry run] %s (%s)", key, outcome)
            return outcome

        if match:
            file_id = match["file_id"]
        else:
            with path.open("rb") as handle:
                file_id = self.fs.put(handle, filename=path.name, file_hash=file_hash)

        stat = path.stat()
        document = {
            "file_id": file_id,
            "source": source,
            "relative_path": key,
            "original_file_path": absolute,
            "filename": path.name,
            "file_hash": file_hash,
            "file_size": size,
            "mime_type": mimetypes.guess_type(path.name)[0],
            "last_modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            "uploaded_at": datetime.now(tz=timezone.utc),
        }
        try:
            self.files.insert_one(document)
        except DuplicateKeyError:
            # Another process indexed the same path between our lookup and insert.
            if not match:
                self.fs.delete(file_id)
            return "existing"
        log.debug("%s: %s", outcome, key)
        return outcome

    def upload_directory(self, root: Path, source: str, dry_run: bool) -> Counts:
        paths = list(iter_files(root))
        counts = Counts(total=len(paths))
        log.info("Found %d files under %s (source=%s)", counts.total, root, source)
        seen: set[str] | None = set() if dry_run else None
        for index, path in enumerate(paths, start=1):
            try:
                counts.bump(self.upload_file(root, path, source, dry_run, seen))
            except (OSError, PyMongoError) as exc:
                counts.failed += 1
                log.warning("Skipping %s: %s", path, exc)
            if index % PROGRESS_EVERY == 0 or index == counts.total:
                log.info("Progress: %d/%d", index, counts.total)
        return counts

    def check_directory(self, root: Path, source: str) -> Counts:
        counts = Counts()
        for path in iter_files(root):
            counts.total += 1
            key = relative_key(root, path)
            if self.find_by_path(source, key, str(path)):
                counts.existing += 1
            else:
                counts.new += 1
        return counts

    # -- maintenance ------------------------------------------------------- #

    def digest(self, force: bool = False) -> Counts:
        """Compute hash and size for documents that lack them (or all, if force)."""
        query = {} if force else {
            "$or": [{"file_hash": {"$exists": False}}, {"file_hash": EMPTY_SHA256}]
        }
        counts = Counts(total=self.files.count_documents(query))
        log.info("Digesting %d documents", counts.total)
        cursor = self.files.find(query, {"file_id": 1}, no_cursor_timeout=True, batch_size=50)
        with cursor:
            for index, document in enumerate(cursor, start=1):
                try:
                    with self.fs.get(document["file_id"]) as grid_out:
                        file_hash, size = hash_stream(grid_out)
                except NoFile:
                    counts.failed += 1
                    log.warning("GridFS blob %s missing for document %s",
                                document["file_id"], document["_id"])
                    continue
                self.files.update_one(
                    {"_id": document["_id"]},
                    {"$set": {"file_hash": file_hash, "file_size": size}},
                )
                counts.new += 1
                if index % PROGRESS_EVERY == 0 or index == counts.total:
                    log.info("Progress: %d/%d", index, counts.total)
        return counts

    def duplicate_groups(self, limit: int | None = None) -> Iterator[dict]:
        pipeline: list[dict] = [
            {"$match": {"file_hash": {"$exists": True}}},
            {"$group": {
                "_id": "$file_hash",
                "count": {"$sum": 1},
                "size": {"$max": "$file_size"},
                "paths": {"$push": "$original_file_path"},
            }},
            {"$match": {"count": {"$gt": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ]
        if limit:
            pipeline.append({"$limit": limit})
        return self.files.aggregate(pipeline, allowDiskUse=True)

    def summary(self) -> Summary:
        result = Summary(
            total_files=self.files.count_documents({}),
            hashed_files=self.files.count_documents({"file_hash": {"$exists": True}}),
            stored_blobs=self.db["fs.files"].count_documents({}),
        )
        for group in self.duplicate_groups():
            result.duplicate_groups += 1
            extra = group["count"] - 1
            result.redundant_files += extra
            result.redundant_bytes += extra * (group.get("size") or 0)
            if not result.example:
                result.example = group
        return result


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def cmd_upload(store: FileStore, args: argparse.Namespace) -> int:
    counts = store.upload_directory(args.directory, args.source, args.dry_run)
    verb = "would be added" if args.dry_run else "added"
    log.info("Done: %d new, %d duplicate content, %d already indexed, %d failed (%d total %s)",
             counts.new, counts.duplicate, counts.existing, counts.failed, counts.total, verb)
    return 1 if counts.failed else 0


def cmd_check(store: FileStore, args: argparse.Namespace) -> int:
    counts = store.check_directory(args.directory, args.source)
    log.info("%d files: %d new, %d already indexed", counts.total, counts.new, counts.existing)
    return 0


def cmd_digest(store: FileStore, args: argparse.Namespace) -> int:
    counts = store.digest(force=args.force)
    log.info("Digested %d documents, %d missing blobs", counts.new, counts.failed)
    return 1 if counts.failed else 0


def cmd_summary(store: FileStore, args: argparse.Namespace) -> int:
    result = store.summary()
    log.info("Indexed files:        %d", result.total_files)
    log.info("  with a hash:        %d", result.hashed_files)
    log.info("Stored blobs:         %d", result.stored_blobs)
    log.info("Duplicate groups:     %d", result.duplicate_groups)
    log.info("Redundant files:      %d (%s)", result.redundant_files, human_bytes(result.redundant_bytes))
    if result.example:
        log.info("Example group %s:", result.example["_id"][:12])
        for path in result.example["paths"]:
            log.info("  %s", path)
    return 0


def cmd_duplicates(store: FileStore, args: argparse.Namespace) -> int:
    shown = 0
    for group in store.duplicate_groups(args.limit):
        shown += 1
        print(f"{group['_id']}  x{group['count']}  {human_bytes(group.get('size') or 0)}")
        for path in group["paths"]:
            print(f"    {path}")
    if not shown:
        print("No duplicate content found.")
    return 0


def existing_directory(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"not a directory: {value}")
    return path.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive files in MongoDB GridFS, deduplicated by content hash.")
    parser.add_argument("--mongo-uri", default=os.getenv("MONGO_URI", DEFAULT_MONGO_URI),
                        help="MongoDB connection string (default: $MONGO_URI or %(default)s)")
    parser.add_argument("--db", default=os.getenv("MONGO_DB", DB_NAME),
                        help="database name (default: $MONGO_DB or %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every file")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_directory(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("directory", type=existing_directory, help="directory to scan")
        sub.add_argument("--source", help="label for this directory; paths are stored relative "
                                          "to it (default: the directory's name)")

    upload = commands.add_parser("upload", help="index and store files from a directory")
    add_directory(upload)
    upload.add_argument("--dry-run", action="store_true", help="hash and report, but store nothing")
    upload.set_defaults(func=cmd_upload)

    check = commands.add_parser("check", help="count which files are new vs already indexed")
    add_directory(check)
    check.set_defaults(func=cmd_check)

    digest = commands.add_parser("digest", help="hash documents that are missing a hash")
    digest.add_argument("--force", action="store_true", help="re-hash every document")
    digest.set_defaults(func=cmd_digest)

    summary = commands.add_parser("summary", help="report totals and duplicate statistics")
    summary.set_defaults(func=cmd_summary)

    duplicates = commands.add_parser("duplicates", help="list groups of files with identical content")
    duplicates.add_argument("--limit", type=int, default=20,
                            help="groups to show, 0 for all (default: %(default)s)")
    duplicates.set_defaults(func=cmd_duplicates)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if getattr(args, "directory", None) is not None and not args.source:
        args.source = args.directory.name or str(args.directory)

    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=10_000)
    try:
        store = FileStore(client[args.db])
        store.ensure_indexes()
        return args.func(store, args)
    except ServerSelectionTimeoutError as exc:
        log.error("Cannot reach MongoDB at %s: %s", args.mongo_uri, exc)
        return 2
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
