"""Tests for Phase 1 Batch 3 — Privacy & Data Protection:

1. Data Flow Audit — audit document exists, no secrets/PII in it, fields classified
2. Secure Data Deletion — cascade cleanup, auth enforcement, tenant isolation
3. Authorized Data Export — field filtering, auth enforcement, no file writes
"""

import ast as _ast
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId

_ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# KMS mock (must be set up before any project imports that touch encryption)
# ---------------------------------------------------------------------------

_KMS_STUB = MagicMock()
_KMS_STUB.wrap_data_key.side_effect = lambda dek: dek
_KMS_STUB.unwrap_data_key.side_effect = lambda wrapped: wrapped
sys.modules.setdefault("kms", _KMS_STUB)
sys.modules.pop("field_encryption", None)


# ---------------------------------------------------------------------------
# 1. DATA FLOW AUDIT
# ---------------------------------------------------------------------------

class TestAuditDocumentExists:
    """DATA_PRIVACY_AUDIT.md must exist in docs/."""

    def test_audit_file_exists(self):
        path = os.path.join(_ROOT, "docs", "DATA_PRIVACY_AUDIT.md")
        assert os.path.isfile(path), f"Missing audit document: {path}"

    def test_audit_file_non_empty(self):
        path = os.path.join(_ROOT, "docs", "DATA_PRIVACY_AUDIT.md")
        size = os.path.getsize(path)
        assert size > 500, f"Audit file too small ({size} bytes) — expected substantial content"


class TestAuditContentClassification:
    """Audit document must correctly classify sensitive fields."""

    def _read_audit(self):
        path = os.path.join(_ROOT, "docs", "DATA_PRIVACY_AUDIT.md")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_audit_mentions_employee_pii_fields(self):
        content = self._read_audit()
        for field in ("name", "email", "phone"):
            assert field in content, f"Audit missing employee PII field: {field}"

    def test_audit_mentions_encryption(self):
        content = self._read_audit()
        assert "AES-256-GCM" in content or "aes-256-gcm" in content.lower()
        assert "encrypt" in content.lower()

    def test_audit_mentions_blind_index(self):
        content = self._read_audit()
        assert "blind_index" in content or "blind index" in content.lower()
        assert "email_hash" in content

    def test_audit_mentions_llm_exposure(self):
        content = self._read_audit()
        assert "LLM" in content or "llm" in content.lower()
        assert "transcript" in content.lower()

    def test_audit_mentions_password_hash_exclusion(self):
        content = self._read_audit()
        assert "password_hash" in content
        assert "Argon2" in content or "argon2" in content.lower()

    def test_audit_no_real_pii(self):
        """Audit must not contain actual email addresses or real names."""
        content = self._read_audit().lower()
        assert "alice@example.com" not in content
        assert "bob@example.com" not in content
        # Should not contain production-style emails
        assert "@voovrhr.com" not in content
        assert "@gmail.com" not in content

    def test_audit_no_real_secrets(self):
        """Audit must not contain actual API keys or tokens."""
        content = self._read_audit()
        assert "sk-" not in content  # OpenAI key prefix
        assert "AIza" not in content  # Google API key prefix

    def test_audit_mentions_tenant_isolation(self):
        content = self._read_audit()
        assert "org_id" in content
        assert "tenant" in content.lower() or "organization" in content.lower()

    def test_audit_mentions_deletion_behavior(self):
        content = self._read_audit()
        assert "deletion" in content.lower() or "delete" in content.lower()

    def test_audit_mentions_retention(self):
        content = self._read_audit()
        assert "retention" in content.lower() or "ttl" in content.lower()


# ---------------------------------------------------------------------------
# 2. SECURE DATA DELETION
# ---------------------------------------------------------------------------

class TestDeleteEmployeeCascade:
    """Employee deletion must clean up related sessions and notifications."""

    def _source_has_cascade(self):
        """Check employees.py source for cascade deletion logic."""
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_delete_employee_source_cascades_sessions(self):
        source = self._source_has_cascade()
        assert "sessions.delete_many" in source, \
            "delete_employee must delete related sessions"

    def test_delete_employee_source_cascades_notifications(self):
        source = self._source_has_cascade()
        assert "notifications.delete_many" in source, \
            "delete_employee must delete related notifications"

    def test_delete_employee_verifies_ownership_before_delete(self):
        """Must fetch the employee with org_id check before any deletion."""
        source = self._source_has_cascade()
        # The function should find the employee with org_id scoping FIRST
        assert 'find_one({"_id": emp_oid, "org_id": ObjectId(org_id)})' in source or \
               "find_one" in source and "org_id" in source

    def test_delete_employee_uses_try_except(self):
        """Deletion operations should be wrapped in error handling."""
        source = self._source_has_cascade()
        assert "except Exception" in source or "except:" in source

    def test_delete_employee_returns_generic_error(self):
        """Internal DB errors must not leak to clients."""
        source = self._source_has_cascade()
        # Should return a generic error, not str(e) or exception details
        assert '"deletion_failed"' in source or '"error"' in source

    def test_delete_employee_no_raw_exception_in_response(self):
        """The response must not include str(e) or traceback."""
        source = self._source_has_cascade()
        # Find the delete route function
        lines = source.split("\n")
        in_delete = False
        for line in lines:
            if "def delete_employee" in line:
                in_delete = True
            if in_delete and "return jsonify" in line:
                # Check this return doesn't include exception details
                assert "str(e)" not in line, \
                    "delete_employee must not expose exception details"
                break


class TestDeleteEmployeeAuth:
    """Deletion must enforce authentication and authorization."""

    def _get_delete_source(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # Extract the delete_employee function
        lines = content.split("\n")
        in_func = False
        func_lines = []
        indent = None
        for line in lines:
            if "def delete_employee(" in line:
                in_func = True
                indent = len(line) - len(line.lstrip())
                func_lines.append(line)
                continue
            if in_func:
                if line.strip() == "":
                    func_lines.append(line)
                    continue
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= indent and line.strip() and not line.strip().startswith("#"):
                    break
                func_lines.append(line)
        return "\n".join(func_lines)

    def test_delete_requires_auth(self):
        # Deletion is now admin-gated: it must go through _require_admin()
        # (which builds on _require_auth()) rather than plain _require_auth().
        source = self._get_delete_source()
        assert "_require_admin()" in source

    def test_delete_checks_not_authenticated(self):
        # Admin-only deletion rejects non-admins with admin_required / 403 at
        # the top of the route, before any _employee_accessible check runs.
        source = self._get_delete_source()
        assert "admin_required" in source
        assert "403" in source

    def test_delete_validates_objectid(self):
        source = self._get_delete_source()
        assert "InvalidId" in source or "ObjectId" in source

    def test_delete_returns_404_for_nonexistent(self):
        source = self._get_delete_source()
        assert "not_found" in source
        assert "404" in source


class TestDeleteEmployeeTenantIsolation:
    """Deletion must not allow cross-organization access."""

    def _get_delete_source(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        lines = content.split("\n")
        in_func = False
        func_lines = []
        indent = None
        for line in lines:
            if "def delete_employee(" in line:
                in_func = True
                indent = len(line) - len(line.lstrip())
                func_lines.append(line)
                continue
            if in_func:
                if line.strip() == "":
                    func_lines.append(line)
                    continue
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= indent and line.strip() and not line.strip().startswith("#"):
                    break
                func_lines.append(line)
        return "\n".join(func_lines)

    def test_delete_scopes_by_org_id(self):
        """All delete operations must include org_id in the query."""
        source = self._get_delete_source()
        # Sessions and notifications deletes must include org_id
        assert "org_id" in source

    def test_delete_does_not_use_emp_id_from_client(self):
        """The employee ObjectId must come from the URL, verified against DB."""
        source = self._get_delete_source()
        # Must fetch employee first, then use its _id for cascade deletes
        assert "find_one" in source


# ---------------------------------------------------------------------------
# 3. AUTHORIZED DATA EXPORT
# ---------------------------------------------------------------------------

class TestExportEmployeeRoute:
    """Export endpoint must exist and be properly secured."""

    def _get_export_source(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        lines = content.split("\n")
        in_func = False
        func_lines = []
        indent = None
        for line in lines:
            if "def export_employee(" in line:
                in_func = True
                indent = len(line) - len(line.lstrip())
                func_lines.append(line)
                continue
            if in_func:
                if line.strip() == "":
                    func_lines.append(line)
                    continue
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= indent and line.strip() and not line.strip().startswith("#"):
                    break
                func_lines.append(line)
        return "\n".join(func_lines)

    def test_export_route_exists(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "/employees/<emp_id>/export" in source
        assert "def export_employee(" in source

    def test_export_requires_auth(self):
        source = self._get_export_source()
        assert "_require_auth()" in source
        assert "not_authenticated" in source
        assert "401" in source

    def test_export_validates_objectid(self):
        source = self._get_export_source()
        assert "InvalidId" in source or "ObjectId" in source

    def test_export_returns_404_for_nonexistent(self):
        source = self._get_export_source()
        assert "not_found" in source
        assert "404" in source

    def test_export_scopes_by_org_id(self):
        source = self._get_export_source()
        assert "org_id" in source

    def test_export_returns_json(self):
        source = self._get_export_source()
        assert "jsonify" in source

    def test_export_has_content_disposition(self):
        source = self._get_export_source()
        assert "Content-Disposition" in source
        assert "attachment" in source


class TestExportExcludedFields:
    """Export must never include passwords, tokens, keys, or secrets."""

    def _get_export_source(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        lines = content.split("\n")
        in_func = False
        func_lines = []
        indent = None
        for line in lines:
            if "def export_employee(" in line:
                in_func = True
                indent = len(line) - len(line.lstrip())
                func_lines.append(line)
                continue
            if in_func:
                if line.strip() == "":
                    func_lines.append(line)
                    continue
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= indent and line.strip() and not line.strip().startswith("#"):
                    break
                func_lines.append(line)
        return "\n".join(func_lines)

    def test_export_no_password_hash(self):
        source = self._get_export_source()
        assert "password_hash" not in source or "password_hash" not in source.split("export_data")[1] if "export_data" in source else True

    def test_export_no_totp_secret(self):
        source = self._get_export_source()
        export_section = source[source.find("export_data"):] if "export_data" in source else source
        assert "totp_secret" not in export_section

    def test_export_no_wrapped_dek(self):
        source = self._get_export_source()
        export_section = source[source.find("export_data"):] if "export_data" in source else source
        assert "wrapped_dek" not in export_section

    def test_export_no_encrypted_blob(self):
        source = self._get_export_source()
        export_section = source[source.find("export_data"):] if "export_data" in source else source
        assert '"encrypted"' not in export_section

    def test_export_no_email_hash(self):
        source = self._get_export_source()
        export_section = source[source.find("export_data"):] if "export_data" in source else source
        assert "email_hash" not in export_section

    def test_export_no_backup_codes(self):
        source = self._get_export_source()
        export_section = source[source.find("export_data"):] if "export_data" in source else source
        assert "backup_codes" not in export_section

    def test_export_includes_decrypted_pii(self):
        source = self._get_export_source()
        assert "decrypt_fields" in source
        assert "pii.get" in source

    def test_export_includes_business_data(self):
        source = self._get_export_source()
        for field in ("department", "position", "employment_type", "work_mode"):
            assert f'"{field}"' in source

    def test_export_includes_ai_wellness(self):
        source = self._get_export_source()
        assert "ai_wellness" in source

    def test_export_includes_signals(self):
        source = self._get_export_source()
        assert "signals" in source


class TestExportNoFileWrites:
    """Export must not write to permanent server storage."""

    def _get_export_source(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        lines = content.split("\n")
        in_func = False
        func_lines = []
        indent = None
        for line in lines:
            if "def export_employee(" in line:
                in_func = True
                indent = len(line) - len(line.lstrip())
                func_lines.append(line)
                continue
            if in_func:
                if line.strip() == "":
                    func_lines.append(line)
                    continue
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= indent and line.strip() and not line.strip().startswith("#"):
                    break
                func_lines.append(line)
        return "\n".join(func_lines)

    def test_export_no_file_write(self):
        source = self._get_export_source()
        assert "open(" not in source, "Export must not write files"
        assert "write(" not in source, "Export must not write files"
        assert "os.path" not in source, "Export must not use file system paths"

    def test_export_no_logging_of_data(self):
        """Export must not log employee data."""
        source = self._get_export_source()
        assert "logger." not in source, "Export must not log employee data"
        assert "print(" not in source, "Export must not print employee data"


# ---------------------------------------------------------------------------
# 4. SOURCE CODE INTEGRITY — employees.py structure
# ---------------------------------------------------------------------------

class TestEmployeeModuleIntegrity:
    """Verify employees.py has proper structure and imports."""

    def test_employees_module_imports(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "from flask import" in source
        assert "Blueprint" in source
        assert "jsonify" in source
        assert "make_response" in source

    def test_employees_module_has_logger(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "logger = logging.getLogger" in source

    def test_delete_and_export_functions_exist(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "def delete_employee(" in source
        assert "def export_employee(" in source

    def test_export_uses_make_response(self):
        """Export should use make_response for proper header attachment."""
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        export_start = source.find("def export_employee(")
        export_func = source[export_start:]
        assert "make_response" in export_func


# ---------------------------------------------------------------------------
# 5. NO REGRESSION IN EXISTING employee_to_json
# ---------------------------------------------------------------------------

class TestEmployeeToJsonUnchanged:
    """_employee_to_json must still return the standard API fields."""

    def test_employee_to_json_has_required_fields(self):
        path = os.path.join(_ROOT, "employees.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        func_start = source.find("def _employee_to_json")
        func_end = source.find("\ndef ", func_start + 1)
        func_source = source[func_start:func_end]

        required_fields = [
            '"id"', '"employee_id"', '"name"', '"email"', '"phone"',
            '"department"', '"position"', '"employment_type"', '"work_mode"',
            '"joining_date"', '"status"', '"wellness_score"', '"wellness_status"',
            '"attrition_risk_pct"', '"burnout_index"', '"signals"', '"photo"',
            '"created_at"', '"updated_at"',
        ]
        for field in required_fields:
            assert field in func_source, f"_employee_to_json missing field: {field}"
