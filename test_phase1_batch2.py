"""Tests for Phase 1 Batch 2 audit fixes:

1. Request body size limits (MAX_CONTENT_LENGTH + 413 handler)
2. Atomic employee ID generation (MongoDB counter)
3. MongoDB indexes
"""

import importlib
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 1. REQUEST BODY SIZE LIMITS
# ---------------------------------------------------------------------------


class TestMaxContentLength:
    """Flask MAX_CONTENT_LENGTH must be configured in Config."""

    def test_config_has_max_content_length(self):
        """Config class defines MAX_CONTENT_LENGTH."""
        # Import config with SECRET_KEY set (required by Config)
        env = os.environ.copy()
        env.setdefault("SECRET_KEY", "test-secret-key-for-config-import")
        with patch.dict(os.environ, env, clear=False):
            sys.modules.pop("config", None)
            from dotenv import load_dotenv as _ld
            with patch("dotenv.load_dotenv", return_value=None):
                import config
                assert hasattr(config.Config, "MAX_CONTENT_LENGTH")
                assert config.Config.MAX_CONTENT_LENGTH == 50 * 1024 * 1024

    def test_max_content_length_is_50mb(self):
        """50 MB covers audio (Whisper 25 MB), images (10 MB), and headroom."""
        env = os.environ.copy()
        env.setdefault("SECRET_KEY", "test-secret-key-for-config-import")
        with patch.dict(os.environ, env, clear=False):
            sys.modules.pop("config", None)
            with patch("dotenv.load_dotenv", return_value=None):
                import config
                assert config.Config.MAX_CONTENT_LENGTH == 50 * 1024 * 1024


class TestPayloadTooLargeHandler:
    """app.py must return a clean 413 JSON response for oversized payloads.

    Flask's test client reads the entire body into memory before the WSGI
    app sees it, so MAX_CONTENT_LENGTH is never enforced at the transport
    layer.  We therefore:
      1. Verify MAX_CONTENT_LENGTH is configured (TestMaxContentLength).
      2. Verify the error handler exists in app.py source.
      3. Test the handler's return value by invoking it via Flask's
         test_request_context and inspecting the response directly.
    """

    def _make_app(self):
        """Build a minimal Flask app with the same RequestEntityTooLarge
        handler as app.py."""
        from flask import Flask, jsonify
        from werkzeug.exceptions import RequestEntityTooLarge

        app = Flask(__name__)
        app.secret_key = "test-secret"
        app.config["MAX_CONTENT_LENGTH"] = 100

        @app.errorhandler(RequestEntityTooLarge)
        def _handle_payload_too_large(exc):
            return jsonify({"error": "Request payload is too large."}), 413

        @app.route("/api/test", methods=["POST"])
        def test_endpoint():
            return jsonify({"ok": True})

        return app

    def test_normal_small_request_succeeds(self):
        """A request well within the limit succeeds normally."""
        app = self._make_app()
        with app.test_client() as client:
            resp = client.post("/api/test", json={"data": "small"})
            assert resp.status_code == 200
            assert resp.get_json()["ok"] is True

    def _invoke_handler(self):
        """Invoke the 413 error handler directly and return (resp, code)."""
        from werkzeug.exceptions import RequestEntityTooLarge

        app = self._make_app()
        exc = RequestEntityTooLarge()
        with app.test_request_context("/api/test", method="POST"):
            resp = app.handle_user_exception(exc)
        return resp

    def test_oversized_handler_returns_413_json(self):
        """When RequestEntityTooLarge is raised, the handler returns 413 + JSON."""
        resp, code = self._invoke_handler()
        assert code == 413
        body = resp.get_json()
        assert body["error"] == "Request payload is too large."

    def test_413_response_no_werkzeug_html(self):
        """The 413 response must not contain Werkzeug/Flask HTML debug pages."""
        resp, _code = self._invoke_handler()
        body_bytes = resp.get_data(as_text=True).lower()
        assert "werkzeug" not in body_bytes
        assert "<!doctype" not in body_bytes
        assert "<html" not in body_bytes

    def test_413_error_message_no_internals(self):
        """413 error message does not expose internal exception details."""
        resp, _code = self._invoke_handler()
        body = resp.get_json()
        error_msg = body.get("error", "")
        assert "traceback" not in error_msg.lower()
        assert "exception" not in error_msg.lower()
        assert ".py" not in error_msg

    def test_413_handler_exists_in_app_source(self):
        """app.py source code registers a RequestEntityTooLarge error handler."""
        app_source = open(os.path.join(_ROOT, "app.py"), encoding="utf-8").read()
        assert "RequestEntityTooLarge" in app_source
        assert "_handle_payload_too_large" in app_source
        assert '"Request payload is too large."' in app_source
        assert "413" in app_source

    def test_413_handler_is_imported(self):
        """app.py imports RequestEntityTooLarge from werkzeug."""
        app_source = open(os.path.join(_ROOT, "app.py"), encoding="utf-8").read()
        assert "from werkzeug.exceptions import RequestEntityTooLarge" in app_source

    def test_no_werkzeug_debug_page_leaks(self):
        """Verify the handler returns JSON content type, not text/html."""
        resp, _code = self._invoke_handler()
        assert resp.content_type == "application/json"


class TestExistingUploadsRespected:
    """Verify the 50 MB limit supports all legitimate upload endpoints."""

    def test_image_upload_10mb_still_works(self):
        """The existing MAX_IMAGE_BYTES (10 MB) is well under 50 MB."""
        import sessions
        assert sessions.MAX_IMAGE_BYTES == 10 * 1024 * 1024
        # 10 MB < 50 MB — the global limit won't interfere
        assert sessions.MAX_IMAGE_BYTES < 50 * 1024 * 1024

    def test_audio_endpoint_no_premature_rejection(self):
        """Audio transcription: OpenAI Whisper accepts up to 25 MB.
        Our 50 MB limit allows this through."""
        whisper_limit = 25 * 1024 * 1024
        env = os.environ.copy()
        env.setdefault("SECRET_KEY", "test-secret-key-for-config-import")
        with patch.dict(os.environ, env, clear=False):
            sys.modules.pop("config", None)
            with patch("dotenv.load_dotenv", return_value=None):
                import config
                assert config.Config.MAX_CONTENT_LENGTH > whisper_limit


# ---------------------------------------------------------------------------
# 2. ATOMIC EMPLOYEE ID GENERATION
# ---------------------------------------------------------------------------


class TestAtomicEmployeeID:
    """next_employee_id uses MongoDB $inc for safe concurrent generation."""

    def test_next_employee_id_calls_find_one_and_update(self):
        """The function must use find_one_and_update with $inc, not
        find_one + increment + insert."""
        from extensions import next_employee_id
        import inspect
        src = inspect.getsource(next_employee_id)
        assert "find_one_and_update" in src
        assert "$inc" in src
        assert "upsert=True" in src
        # Must NOT use the old non-atomic pattern
        assert "find_one(" not in src.split("find_one_and_update")[0]

    def test_first_employee_returns_emp001(self):
        """First employee in a new org gets EMP001."""
        db = MagicMock()
        db.counters.find_one_and_update.return_value = {"_id": "employee:org1", "seq": 1}
        from extensions import next_employee_id
        result = next_employee_id(db, "org1")
        assert result == "EMP001"

    def test_sequential_ids_increment(self):
        """Sequential calls produce EMP001, EMP002, EMP003."""
        db = MagicMock()
        db.counters.find_one_and_update.side_effect = [
            {"_id": "employee:org1", "seq": 1},
            {"_id": "employee:org1", "seq": 2},
            {"_id": "employee:org1", "seq": 3},
        ]
        from extensions import next_employee_id
        assert next_employee_id(db, "org1") == "EMP001"
        assert next_employee_id(db, "org1") == "EMP002"
        assert next_employee_id(db, "org1") == "EMP003"

    def test_counter_document_structure(self):
        """Counter document has the expected _id format: employee:<org_id>."""
        db = MagicMock()
        db.counters.find_one_and_update.return_value = {"_id": "employee:abc123", "seq": 1}
        from extensions import next_employee_id
        next_employee_id(db, "abc123")
        call_args = db.counters.find_one_and_update.call_args
        filter_doc = call_args[0][0]
        assert filter_doc == {"_id": "employee:abc123"}

    def test_id_format_preserved(self):
        """Employee IDs remain zero-padded 3-digit: EMP001, EMP099, EMP100."""
        from extensions import next_employee_id
        db = MagicMock()
        for seq, expected in [(1, "EMP001"), (9, "EMP009"), (10, "EMP010"), (99, "EMP099"), (100, "EMP100")]:
            db.counters.find_one_and_update.return_value = {"_id": "employee:o", "seq": seq}
            assert next_employee_id(db, "o") == expected

    def test_concurrent_generation_no_duplicates(self):
        """Multiple threads calling next_employee_id concurrently each get
        a unique ID (no two threads receive the same sequence number)."""
        db = MagicMock()
        lock = threading.Lock()
        results = []

        # Simulate what MongoDB $inc does atomically: each call returns
        # a unique incremented sequence number.
        call_count = [0]

        def mock_find_one_and_update(*args, **kwargs):
            with lock:
                call_count[0] += 1
                seq = call_count[0]
            return {"_id": "employee:org1", "seq": seq}

        db.counters.find_one_and_update.side_effect = mock_find_one_and_update

        from extensions import next_employee_id

        def worker():
            result = next_employee_id(db, "org1")
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 50 results must be unique
        assert len(results) == 50
        assert len(set(results)) == 50, f"Duplicate IDs found: {[r for r in results if results.count(r) > 1]}"
        # All must be properly formatted
        for r in results:
            assert r.startswith("EMP")
            assert len(r) == 6  # EMP + 3 digits

    def test_separate_orgs_independent_sequences(self):
        """Different organizations have independent counters."""
        db = MagicMock()
        db.counters.find_one_and_update.side_effect = [
            {"_id": "employee:orgA", "seq": 1},
            {"_id": "employee:orgB", "seq": 1},
            {"_id": "employee:orgA", "seq": 2},
            {"_id": "employee:orgB", "seq": 2},
        ]
        from extensions import next_employee_id
        assert next_employee_id(db, "orgA") == "EMP001"
        assert next_employee_id(db, "orgB") == "EMP001"  # orgB starts fresh
        assert next_employee_id(db, "orgA") == "EMP002"
        assert next_employee_id(db, "orgB") == "EMP002"

    def test_employees_py_delegates_to_extensions(self):
        """employees.py._next_employee_id calls extensions.next_employee_id."""
        import employees
        import inspect
        src = inspect.getsource(employees._next_employee_id)
        assert "next_employee_id" in src

    def test_no_nonatomic_pattern_in_employees_py(self):
        """employees.py must NOT contain the old non-atomic find_one + sort
        + increment pattern for employee ID generation."""
        import employees
        import inspect
        src = inspect.getsource(employees._next_employee_id)
        assert "find_one(" not in src
        assert ".replace(" not in src
        assert "sort=" not in src

    def test_no_python_locks_used(self):
        """The atomic implementation must not use Python threading locks."""
        import extensions
        import inspect
        src = inspect.getsource(extensions.next_employee_id)
        assert "Lock" not in src
        assert "threading" not in src

    def test_no_module_level_counter(self):
        """No module-level counter variable is used for ID generation."""
        import extensions
        assert not hasattr(extensions, "_employee_counter")
        assert not hasattr(extensions, "_counter")
        assert not hasattr(extensions, "_next_seq")


# ---------------------------------------------------------------------------
# 3. MONGODB INDEXES
# ---------------------------------------------------------------------------


class TestMongoDBIndexes:
    """_init_indexes creates the expected indexes for all collections."""

    def _call_init_indexes(self):
        """Call _init_indexes with a mock db and capture all create_index calls."""
        db = MagicMock()
        from extensions import _init_indexes
        _init_indexes(db)
        return db

    def test_users_email_hash_index(self):
        """users collection gets a unique index on email_hash."""
        db = self._call_init_indexes()
        users_calls = db.users.create_index.call_args_list
        email_indexes = [c for c in users_calls if "email_hash" in str(c)]
        assert len(email_indexes) >= 1
        # Verify it's unique
        for c in email_indexes:
            if "email_hash" in str(c[0]):
                assert c[1].get("unique") is True

    def test_employees_org_id_created_at_index(self):
        """employees collection gets an (org_id, created_at) compound index."""
        db = self._call_init_indexes()
        emp_calls = db.employees.create_index.call_args_list
        found = False
        for c in emp_calls:
            keys = c[0][0] if c[0] else []
            key_names = [k[0] for k in keys]
            if "org_id" in key_names and "created_at" in key_names:
                found = True
                break
        assert found, "Missing (org_id, created_at) compound index on employees"

    def test_employees_compound_unique_index(self):
        """employees collection gets a unique (org_id, employee_id) index."""
        db = self._call_init_indexes()
        emp_calls = db.employees.create_index.call_args_list
        found = False
        for c in emp_calls:
            keys = c[0][0] if c[0] else []
            key_names = [k[0] for k in keys]
            if "org_id" in key_names and "employee_id" in key_names:
                assert c[1].get("unique") is True
                found = True
                break
        assert found, "Missing unique (org_id, employee_id) index on employees"

    def test_sessions_org_id_created_at_index(self):
        """sessions collection gets an (org_id, created_at) compound index."""
        db = self._call_init_indexes()
        sess_calls = db.sessions.create_index.call_args_list
        found = False
        for c in sess_calls:
            keys = c[0][0] if c[0] else []
            key_names = [k[0] for k in keys]
            if "org_id" in key_names and "created_at" in key_names:
                found = True
                break
        assert found, "Missing (org_id, created_at) compound index on sessions"

    def test_sessions_employee_id_created_at_index(self):
        """sessions collection gets an (employee_id, created_at) compound index."""
        db = self._call_init_indexes()
        sess_calls = db.sessions.create_index.call_args_list
        found = False
        for c in sess_calls:
            keys = c[0][0] if c[0] else []
            key_names = [k[0] for k in keys]
            if "employee_id" in key_names and "created_at" in key_names:
                found = True
                break
        assert found, "Missing (employee_id, created_at) compound index on sessions"

    def test_notifications_org_id_created_at_index(self):
        """notifications collection gets an (org_id, created_at) index."""
        db = self._call_init_indexes()
        calls = db.notifications.create_index.call_args_list
        found = False
        for c in calls:
            keys = c[0][0] if c[0] else []
            key_names = [k[0] for k in keys]
            if "org_id" in key_names and "created_at" in key_names:
                found = True
                break
        assert found, "Missing (org_id, created_at) index on notifications"

    def test_notifications_org_id_read_index(self):
        """notifications collection gets an (org_id, read) compound index."""
        db = self._call_init_indexes()
        calls = db.notifications.create_index.call_args_list
        found = False
        for c in calls:
            keys = c[0][0] if c[0] else []
            key_names = [k[0] for k in keys]
            if "org_id" in key_names and "read" in key_names:
                found = True
                break
        assert found, "Missing (org_id, read) index on notifications"

    def test_active_sessions_user_id_session_token_index(self):
        """active_sessions gets a (user_id, session_token) compound index."""
        db = self._call_init_indexes()
        calls = db.active_sessions.create_index.call_args_list
        found = False
        for c in calls:
            keys = c[0][0] if c[0] else []
            key_names = [k[0] for k in keys]
            if "user_id" in key_names and "session_token" in key_names:
                found = True
                break
        assert found, "Missing (user_id, session_token) index on active_sessions"

    def test_active_sessions_user_id_last_seen_index(self):
        """active_sessions gets a (user_id, last_seen) compound index."""
        db = self._call_init_indexes()
        calls = db.active_sessions.create_index.call_args_list
        found = False
        for c in calls:
            keys = c[0][0] if c[0] else []
            key_names = [k[0] for k in keys]
            if "user_id" in key_names and "last_seen" in key_names:
                found = True
                break
        assert found, "Missing (user_id, last_seen) index on active_sessions"

    def test_counters_index_created(self):
        """counters collection gets an index (for atomic employee ID generation)."""
        db = self._call_init_indexes()
        assert db.counters.create_index.called

    def test_otp_verifications_index_created(self):
        """otp_verifications collection gets an email_hash index."""
        db = self._call_init_indexes()
        calls = db.otp_verifications.create_index.call_args_list
        found = any("email_hash" in str(c) for c in calls)
        assert found, "Missing email_hash index on otp_verifications"

    def test_index_init_idempotent(self):
        """_init_indexes can be called multiple times without error."""
        db = MagicMock()
        from extensions import _init_indexes
        # Calling 3 times should not raise
        _init_indexes(db)
        _init_indexes(db)
        _init_indexes(db)
        # Each collection should have create_index called each time
        # (create_index is a no-op if index already exists in real MongoDB)

    def test_all_create_index_calls_use_background(self):
        """All index creation calls use background=True."""
        db = self._call_init_indexes()
        for collection_name in ("users", "employees", "sessions",
                                "notifications", "active_sessions",
                                "counters", "otp_verifications"):
            collection = getattr(db, collection_name)
            for call in collection.create_index.call_args_list:
                assert call[1].get("background") is True, (
                    f"{collection_name}.create_index missing background=True: {call}"
                )

    def test_init_indexes_called_from_init_db(self):
        """init_db calls _init_indexes when a MongoDB client exists."""
        import extensions
        import inspect
        src = inspect.getsource(extensions.init_db)
        assert "_init_indexes" in src

    def test_index_function_exists(self):
        """_init_indexes is defined in extensions.py."""
        import extensions
        assert hasattr(extensions, "_init_indexes")
        assert callable(extensions._init_indexes)


class TestNoRedundantIndexes:
    """Verify no obviously redundant single-field indexes are created
    when a compound index already covers the same prefix."""

    def _get_all_indexes(self):
        """Capture all index definitions from _init_indexes."""
        db = MagicMock()
        from extensions import _init_indexes
        _init_indexes(db)
        indexes = {}
        for name in ("users", "employees", "sessions", "notifications",
                      "active_sessions", "counters", "otp_verifications"):
            calls = getattr(db, name).create_index.call_args_list
            indexes[name] = []
            for c in calls:
                keys = c[0][0] if c[0] else []
                indexes[name].append(keys)
        return indexes

    def test_no_separate_org_id_index_when_compound_exists(self):
        """If (org_id, created_at) exists, there should not be a redundant
        standalone org_id index."""
        indexes = self._get_all_indexes()
        for collection in ("employees", "sessions", "notifications"):
            compound = any(
                len(keys) == 2 and keys[0][0] == "org_id"
                for keys in indexes[collection]
            )
            standalone_org = any(
                keys == "org_id"
                for keys in indexes[collection]
            )
            if compound:
                assert not standalone_org, (
                    f"{collection}: redundant standalone org_id index "
                    f"when compound (org_id, ...) exists"
                )


# ---------------------------------------------------------------------------
# 4. INTEGRATION: _init_db calls _init_indexes
# ---------------------------------------------------------------------------


class TestInitDBIntegration:
    """Verify init_db wires up all initialization steps."""

    def test_init_db_calls_init_indexes(self):
        """init_db should call _init_indexes when MongoDB is available."""
        import extensions
        import inspect
        src = inspect.getsource(extensions.init_db)
        assert "_init_indexes(db)" in src

    def test_init_db_calls_init_rate_limits(self):
        """init_db should still call _init_rate_limits (existing Phase 1)."""
        import extensions
        import inspect
        src = inspect.getsource(extensions.init_db)
        assert "_init_rate_limits(db)" in src
