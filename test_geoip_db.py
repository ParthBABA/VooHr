"""Tests for local DB-IP lookup and atomic database downloads."""

import gzip
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import geoip_db
import login_flow


class _Reader:
    def __init__(self, record):
        self.record = record

    def get(self, _ip):
        return self.record

    def close(self):
        pass


def _local_record(city="Mumbai", region="Maharashtra", country="India"):
    return {
        "city": {"names": {"en": city}} if city else {},
        "subdivisions": [{"names": {"en": region}}] if region else [],
        "country": {"names": {"en": country}} if country else {},
    }


def _api_response(status=200, data=None):
    response = mock.MagicMock()
    response.status_code = status
    response.json.return_value = data or {}
    return response


class TestLookupOrder:
    def test_local_hit_never_calls_ipinfo(self, monkeypatch):
        monkeypatch.setattr(login_flow.geoip_db, "get_reader", lambda: _Reader(_local_record()))
        monkeypatch.setattr(login_flow.requests, "get", mock.Mock())

        location = login_flow._lookup_location("49.36.0.1")

        assert location == {"city": "Mumbai", "region": "Maharashtra", "country": "India"}
        login_flow.requests.get.assert_not_called()

    def test_local_miss_uses_ipinfo_once(self, monkeypatch):
        monkeypatch.setattr(login_flow.geoip_db, "get_reader", lambda: _Reader(None))
        monkeypatch.setenv("IP_API_KEY", "test-token")
        request = mock.Mock(return_value=_api_response(
            data={"city": "Mumbai", "region": "Maharashtra", "country": "IN"}
        ))
        monkeypatch.setattr(login_flow.requests, "get", request)

        location = login_flow._lookup_location("49.36.0.1")

        assert location == {"city": "Mumbai", "region": "Maharashtra", "country": "India"}
        request.assert_called_once()

    def test_both_sources_miss_returns_none(self, monkeypatch):
        monkeypatch.setattr(login_flow.geoip_db, "get_reader", lambda: _Reader(None))
        monkeypatch.setenv("IP_API_KEY", "test-token")
        monkeypatch.setattr(login_flow.requests, "get", mock.Mock(
            return_value=_api_response(data={"bogon": True})
        ))

        assert login_flow._lookup_location("49.36.0.1") is None

    def test_local_country_only_tries_fallback_then_preserves_local(self, monkeypatch):
        monkeypatch.setattr(
            login_flow.geoip_db, "get_reader", lambda: _Reader(_local_record(city=None, region=None))
        )
        monkeypatch.setenv("IP_API_KEY", "test-token")
        monkeypatch.setattr(login_flow.requests, "get", mock.Mock(
            return_value=_api_response(data={})
        ))

        assert login_flow._lookup_location("49.36.0.1") == {
            "city": None, "region": None, "country": "India",
        }
        login_flow.requests.get.assert_called_once()

    def test_private_or_empty_ip_skips_both_sources(self, monkeypatch):
        monkeypatch.setattr(login_flow.geoip_db, "get_reader", mock.Mock())
        monkeypatch.setattr(login_flow.requests, "get", mock.Mock())

        assert login_flow._lookup_location("") is None
        assert login_flow._lookup_location("192.168.1.20") is None
        assert login_flow._lookup_location(12345) is None
        login_flow.geoip_db.get_reader.assert_not_called()
        login_flow.requests.get.assert_not_called()

    def test_missing_db_and_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(login_flow.geoip_db, "get_reader", lambda: None)
        monkeypatch.delenv("IP_API_KEY", raising=False)
        monkeypatch.setattr(login_flow.requests, "get", mock.Mock())

        assert login_flow._lookup_location("49.36.0.1") is None
        login_flow.requests.get.assert_not_called()

    def test_fallback_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(login_flow.geoip_db, "get_reader", lambda: _Reader(None))
        monkeypatch.setenv("IP_API_KEY", "test-token")
        monkeypatch.setenv("GEOIP_FALLBACK_IPINFO", "false")
        monkeypatch.setattr(login_flow.requests, "get", mock.Mock())

        assert login_flow._lookup_location("49.36.0.1") is None
        login_flow.requests.get.assert_not_called()

    def test_lookup_never_raises_for_reader_or_network_errors(self, monkeypatch):
        def reader_failure():
            raise RuntimeError("reader unavailable")

        monkeypatch.setattr(login_flow.geoip_db, "get_reader", reader_failure)
        monkeypatch.setenv("IP_API_KEY", "test-token")
        monkeypatch.setattr(login_flow.requests, "get", mock.Mock(
            side_effect=RuntimeError("network unavailable")
        ))

        assert login_flow._lookup_location("49.36.0.1") is None

    def test_lookup_failure_does_not_log_ip_or_token(self, monkeypatch, caplog):
        monkeypatch.setattr(login_flow.geoip_db, "get_reader", lambda: None)
        monkeypatch.setenv("IP_API_KEY", "secret-test-token")
        monkeypatch.setattr(login_flow.requests, "get", mock.Mock(
            side_effect=RuntimeError("request failed")
        ))

        assert login_flow._lookup_location("49.36.0.1") is None
        assert "49.36.0.1" not in caplog.text
        assert "secret-test-token" not in caplog.text


class TestDatabaseDownload:
    def _setup(self, tmp_path, monkeypatch):
        destination = tmp_path / "data" / "dbip-city-lite.mmdb"
        monkeypatch.setenv("GEOIP_DB_PATH", str(destination))
        monkeypatch.setattr(geoip_db, "_month_candidates", lambda: ((2026, 10), (2026, 9)))
        monkeypatch.setattr(geoip_db.maxminddb, "open_database", lambda _path: _Reader({"ok": True}))
        return destination

    @staticmethod
    def _download_response(status, payload=b""):
        response = mock.MagicMock()
        response.status_code = status
        response.iter_content.return_value = [payload]
        return response

    def test_current_month_success_downloads_validated_file(self, tmp_path, monkeypatch, caplog):
        destination = self._setup(tmp_path, monkeypatch)
        compressed = gzip.compress(b"valid database bytes")
        monkeypatch.setattr(geoip_db.requests, "get", mock.Mock(
            return_value=self._download_response(200, compressed)
        ))

        with caplog.at_level(logging.INFO, logger="geoip_db"):
            assert geoip_db.download_db() is True

        assert destination.read_bytes() == b"valid database bytes"
        assert "geoip db updated (2026-10)" in caplog.text

    def test_404_retries_previous_month(self, tmp_path, monkeypatch):
        destination = self._setup(tmp_path, monkeypatch)
        compressed = gzip.compress(b"previous month database")
        requests = mock.Mock(side_effect=[
            self._download_response(404),
            self._download_response(200, compressed),
        ])
        monkeypatch.setattr(geoip_db.requests, "get", requests)

        assert geoip_db.download_db() is True

        assert requests.call_count == 2
        assert "2026-10" in requests.call_args_list[0].args[0]
        assert "2026-09" in requests.call_args_list[1].args[0]
        assert destination.read_bytes() == b"previous month database"

    def test_corrupt_download_keeps_old_database(self, tmp_path, monkeypatch, caplog):
        destination = self._setup(tmp_path, monkeypatch)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing database")
        monkeypatch.setattr(geoip_db.requests, "get", mock.Mock(
            return_value=self._download_response(200, b"not gzip")
        ))

        with caplog.at_level(logging.WARNING, logger="geoip_db"):
            assert geoip_db.download_db() is False

        assert destination.read_bytes() == b"existing database"
        assert "geoip db update failed" in caplog.text

    def test_download_failure_never_raises(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(geoip_db.requests, "get", mock.Mock(
            side_effect=RuntimeError("offline")
        ))

        assert geoip_db.download_db() is False

    def test_module_entrypoint_returns_zero_when_download_fails(self, monkeypatch):
        monkeypatch.setattr(geoip_db, "download_db", lambda: False)

        assert geoip_db.main() == 0

    def test_python_module_cli_exits_zero_on_download_failure(self, tmp_path):
        (tmp_path / "sitecustomize.py").write_text(
            "import requests\n"
            "def _blocked(*args, **kwargs):\n"
            "    raise requests.ConnectionError('network blocked for test')\n"
            "requests.get = _blocked\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(tmp_path), str(Path(__file__).parent), env.get("PYTHONPATH")])
        )
        env["GEOIP_DB_PATH"] = str(tmp_path / "missing" / "db.mmdb")
        result = subprocess.run(
            [sys.executable, "-m", "geoip_db"],
            cwd=Path(__file__).parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        assert result.returncode == 0
        assert "geoip db update failed" in result.stderr

    def test_reader_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEOIP_DB_PATH", str(tmp_path / "missing.mmdb"))

        assert geoip_db.get_reader() is None

    def test_missing_database_starts_only_one_background_download(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEOIP_DB_PATH", str(tmp_path / "missing.mmdb"))
        monkeypatch.setattr(geoip_db, "_download_started", False)
        thread = mock.Mock()
        monkeypatch.setattr(geoip_db.threading, "Thread", mock.Mock(return_value=thread))

        geoip_db.start_background_download()
        geoip_db.start_background_download()

        assert geoip_db.threading.Thread.call_count == 1
        thread.start.assert_called_once()

    def test_existing_database_does_not_start_background_download(self, tmp_path, monkeypatch):
        existing = tmp_path / "present.mmdb"
        existing.write_bytes(b"database")
        monkeypatch.setenv("GEOIP_DB_PATH", str(existing))
        thread = mock.Mock()
        monkeypatch.setattr(geoip_db.threading, "Thread", mock.Mock(return_value=thread))

        geoip_db.start_background_download()

        geoip_db.threading.Thread.assert_not_called()
        thread.start.assert_not_called()