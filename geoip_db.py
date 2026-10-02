"""Download and share the local DB-IP City Lite MMDB database."""

import gzip
import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import maxminddb
import requests


logger = logging.getLogger(__name__)
_reader = None
_reader_path = None
_reader_lock = threading.Lock()
_startup_lock = threading.Lock()
_download_started = False
_APP_ROOT = Path(__file__).resolve().parent


def get_db_path() -> Path:
    configured = Path(os.environ.get("GEOIP_DB_PATH", "data/dbip-city-lite.mmdb"))
    return configured if configured.is_absolute() else _APP_ROOT / configured


def _month_candidates(now=None):
    now = now or datetime.now(timezone.utc)
    current = (now.year, now.month)
    if now.month == 1:
        previous = (now.year - 1, 12)
    else:
        previous = (now.year, now.month - 1)
    return current, previous


def _month_label(year_month):
    return f"{year_month[0]:04d}-{year_month[1]:02d}"


def _validate_database(path) -> bool:
    reader = None
    try:
        reader = maxminddb.open_database(str(path))
        return reader.get("8.8.8.8") is not None
    except Exception:
        return False
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass


def download_db() -> bool:
    """Download and atomically install a validated monthly DB-IP database."""
    compressed_path = None
    database_path = None
    try:
        destination = get_db_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".dbip-", suffix=".mmdb.gz", dir=destination.parent, delete=False
        ) as compressed_file:
            compressed_path = Path(compressed_file.name)
        with tempfile.NamedTemporaryFile(
            prefix=".dbip-", suffix=".mmdb", dir=destination.parent, delete=False
        ) as database_file:
            database_path = Path(database_file.name)

        for year_month in _month_candidates():
            label = _month_label(year_month)
            url = (
                "https://download.db-ip.com/free/"
                f"dbip-city-lite-{label}.mmdb.gz"
            )
            response = requests.get(url, stream=True, timeout=(5, 30))
            try:
                if response.status_code == 404:
                    continue
                if response.status_code != 200:
                    raise RuntimeError("download returned non-200 status")
                with open(compressed_path, "wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()

            with gzip.open(compressed_path, "rb") as source, open(database_path, "wb") as output:
                shutil.copyfileobj(source, output)
            if not _validate_database(database_path):
                raise RuntimeError("downloaded database failed validation")

            os.replace(database_path, destination)
            database_path = None
            logger.info("geoip db updated (%s)", label)
            return True

        raise RuntimeError("current and previous monthly downloads returned 404")
    except Exception:
        logger.warning("geoip db update failed")
        return False
    finally:
        for temporary_path in (compressed_path, database_path):
            if temporary_path:
                try:
                    temporary_path.unlink(missing_ok=True)
                except Exception:
                    pass


def get_reader():
    """Lazily open the configured MMDB once; return None when it is absent."""
    global _reader, _reader_path

    path = get_db_path()
    if not path.is_file():
        return None
    with _reader_lock:
        if _reader is not None and _reader_path == path:
            return _reader
        if _reader is not None:
            try:
                _reader.close()
            except Exception:
                pass
        try:
            _reader = maxminddb.open_database(str(path))
            _reader_path = path
        except Exception:
            _reader = None
            _reader_path = None
        return _reader


def start_background_download():
    """Start one non-blocking download when no local database is present."""
    global _download_started

    if get_db_path().is_file():
        return
    with _startup_lock:
        if _download_started:
            return
        _download_started = True
        try:
            threading.Thread(
                target=download_db,
                name="geoip-db-download",
                daemon=True,
            ).start()
        except Exception:
            logger.warning("geoip db update failed")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    download_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())