# dedupe-files-mongodb

Archive the files in a directory into MongoDB GridFS, deduplicated by content.

Every file is hashed with SHA-256 as it is read. The bytes are stored in GridFS
only the first time a hash is seen; any later file with identical content gets a
metadata document that points at the blob already stored. That makes it cheap to
find duplicates across large collections and keeps storage proportional to the
amount of *unique* content, not the number of files.

## Requirements

- Python 3.12 or newer, or Docker
- MongoDB 5 or newer

## Quick start (local Python)

```bash
pip install -r requirements.txt
```

Start MongoDB however you like, for example:

```bash
docker run -d --name mongodb -p 127.0.0.1:27017:27017 mongo:7
```

Then index a directory:

```bash
python main.py upload ~/Downloads
```

The connection string comes from `MONGO_URI` or `--mongo-uri` and defaults to
`mongodb://localhost:27017/`. The database name comes from `MONGO_DB` or `--db`
and defaults to `hash_index_db`.

## Commands

| Command | What it does |
| --- | --- |
| `upload DIR [--dry-run] [--source NAME]` | Index every file under `DIR`. Already indexed paths are skipped, so re-running is safe. `--dry-run` hashes and reports without writing. |
| `check DIR [--source NAME]` | Count how many files under `DIR` are new versus already indexed. |
| `digest [--force]` | Compute hash and size for documents that lack them, such as those written by older versions of this tool. `--force` re-hashes everything. |
| `summary` | Totals: indexed files, stored blobs, duplicate groups, redundant files and bytes, plus one example group. |
| `duplicates [--limit N]` | List groups of files with identical content, largest groups first. `--limit 0` shows all. |

Add `-v` before the command to log every file.

### Sources and paths

Paths are stored relative to the directory you scan, labelled with a *source*
name that defaults to the directory's own name. The pair (source, relative path)
is unique. Pass `--source` explicitly when the same content is reachable under
different roots, for example a host directory mounted at `/data` inside Docker,
so re-runs recognise files that were already indexed.

## Docker Compose

Copy `.env.example` to `.env` and set `SOURCE_DIR` to the host directory you want
to scan. It is mounted read-only at `/data` inside the app container.

```bash
docker compose up --build
```

`APP_ARGS` in `.env` selects the command. Examples:

```
APP_ARGS=upload /data --source Downloads
APP_ARGS=upload /data --dry-run
APP_ARGS=summary
APP_ARGS=duplicates --limit 0
```

The compose file publishes MongoDB on `127.0.0.1:27017` so you can inspect the
database with local tools. The app container waits for MongoDB's healthcheck
before starting.

## Data model

Collection `files`, one document per indexed path:

| Field | Meaning |
| --- | --- |
| `file_id` | GridFS id of the stored bytes. Shared by every file with the same content. |
| `source`, `relative_path` | Where the file came from. Unique together. |
| `original_file_path` | Absolute path at upload time, for humans. |
| `file_hash` | SHA-256 hex digest of the content. Indexed. |
| `file_size` | Size in bytes. |
| `mime_type` | Guessed from the file name, may be null. |
| `last_modified`, `uploaded_at` | UTC timestamps. |

## Upgrading from the previous version

Earlier releases computed the hash after upload and, due to a bug, recorded the
hash of empty input for every file. Run `python main.py digest` once. It finds
documents carrying that value or no hash at all and recomputes them from the
stored bytes. Duplicate blobs that the old version stored in full are left in
place; only new uploads are deduplicated.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The integration tests need a reachable MongoDB and skip themselves otherwise.
Point them at a specific instance with `TEST_MONGO_URI`. Each test uses a
throwaway database that is dropped afterwards.

## License

Creative Commons Attribution 4.0 International (CC BY 4.0).
