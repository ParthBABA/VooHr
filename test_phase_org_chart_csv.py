"""
Phase — bulk org-chart `reports_to` CSV support.

Verifies (via a hand-rolled in-memory Mongo facade + Flask test client, no
live DB):
  - csv-template exposes the `reports_to_email` column
  - import resolves `reports_to_email` to a manager's _id, both when the
    manager shares the same file (before or after the report) and when the
    manager already exists in the org
  - unresolved managers produce a `warnings` entry (employee still created),
    self-reference is rejected, and cross-org matches are never resolved
  - export-csv round-trips `reports_to_email` from the manager's email
"""
import io
import os

import pytest
from bson import ObjectId
from flask import Flask

import employees as employees_mod
from blind_index import blind_index

ORG_A = "aaaaaaaaaaaaaaaaaaaaaaaa"
ORG_B = "bbbbbbbbbbbbbbbbbbbbbbbb"
ADMIN_USER = "999999999999999999999999"

os.environ.setdefault("HASH_INDEX_SECRET", "test-secret")


class FakeCollection:
    def __init__(self):
        self._docs = []

    def _match(self, doc, filt):
        for k, v in filt.items():
            if isinstance(v, dict) and "$in" in v:
                if doc.get(k) not in v["$in"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def find_one(self, filt, *args, **kw):
        for d in self._docs:
            if self._match(d, filt):
                return dict(d)
        return None

    def find(self, filt=None, *args, **kw):
        filt = filt or {}
        wrapped = [dict(d) for d in self._docs if self._match(d, filt)]

        class Cursor:
            def sort(self, key, direction=None):
                return self
            def __iter__(self):
                return iter(wrapped)
            def __len__(self):
                return len(wrapped)

        return Cursor()

    def insert_one(self, doc):
        d = dict(doc)
        d["_id"] = d.get("_id") or ObjectId()
        self._docs.append(d)
        return type("R", (), {"inserted_id": d["_id"]})()

    def update_one(self, filt, update):
        for d in self._docs:
            if self._match(d, filt):
                if "$set" in update:
                    d.update(update["$set"])
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()


class FakeDB:
    def __init__(self):
        self.employees = FakeCollection()
        self.audit_logs = FakeCollection()
        self.users = FakeCollection()


@pytest.fixture
def client(monkeypatch):
    db = FakeDB()
    audit_calls = []

    db.users.insert_one({
        "_id": ObjectId(ADMIN_USER), "role": "admin",
        "org_id": ObjectId(ORG_A),
    })
    monkeypatch.setattr(employees_mod, "get_db", lambda: db)
    monkeypatch.setattr(employees_mod, "_require_auth", lambda: ORG_A)
    emp_seq = {"n": 0}

    def fake_next(db, org_id):
        emp_seq["n"] += 1
        return f"EMP{emp_seq['n']:03d}"
    monkeypatch.setattr(employees_mod, "_next_employee_id", fake_next)

    monkeypatch.setattr(
        employees_mod, "encrypt_fields",
        lambda pii: (dict(pii), "dek"),
    )
    monkeypatch.setattr(
        employees_mod, "decrypt_fields",
        lambda enc, dek: enc if isinstance(enc, dict) else {},
    )
    monkeypatch.setattr(
        employees_mod, "log_audit_event",
        lambda *a, **kw: audit_calls.append(kw.get("meta")),
    )

    app = Flask(__name__)
    app.register_blueprint(employees_mod.employees_bp, url_prefix="/api")
    app.config["TESTING"] = True
    app.secret_key = "test"
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = ADMIN_USER
        c._fake_db = db
        c._audit = audit_calls
        yield c


def _import(client, rows):
    header = (
        "name,email,phone,department,position,employment_type,work_mode,"
        "joining_date,reports_to_email"
    )
    lines = [header] + list(rows)
    csv_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    return client.post(
        "/api/employees/import",
        data={"file": (io.BytesIO(csv_bytes), "employees.csv")},
        content_type="multipart/form-data",
    )


def _find_emp(db, employee_id):
    for d in db.employees._docs:
        if d["employee_id"] == employee_id:
            return d
    return None


def test_template_has_reports_to_email_column(client):
    body = client.get("/api/employees/csv-template").get_data(as_text=True)
    assert "reports_to_email" in body


def test_import_same_file_manager_below_report(client):
    db = client._fake_db
    r = _import(client, [
        "Rana,rana@corp.com,555,Design,Designer,FT,Office,2026-01-01,hr@corp.com",
        "HR,hr@corp.com,555,HR,Manager,FT,Office,2025-01-01,",
    ])
    data = r.get_json()
    assert data["created"] == 2
    assert data["reports_to_resolved"] == 1
    assert data["warnings"] == []
    rana = _find_emp(db, "EMP001")
    hr = _find_emp(db, "EMP002")
    assert rana["reports_to"] == hr["_id"]


def test_import_same_file_manager_above_report(client):
    db = client._fake_db
    r = _import(client, [
        "HR,hr@corp.com,555,HR,Manager,FT,Office,2025-01-01,",
        "Rana,rana@corp.com,555,Design,Designer,FT,Office,2026-01-01,hr@corp.com",
    ])
    data = r.get_json()
    assert data["reports_to_resolved"] == 1
    rana = _find_emp(db, "EMP002")
    hr = _find_emp(db, "EMP001")
    assert rana["reports_to"] == hr["_id"]


def test_import_resolves_existing_employee_in_org(client):
    db = client._fake_db
    db.employees.insert_one({
        "_id": ObjectId(), "employee_id": "EMP100", "org_id": ObjectId(ORG_A),
        "status": "active", "email_hash": blind_index("manager@corp.com"),
        "encrypted": {"email": "manager@corp.com"}, "wrapped_dek": "dek",
    })
    r = _import(client, [
        "Rana,rana@corp.com,555,Design,Designer,FT,Office,2026-01-01,manager@corp.com",
    ])
    data = r.get_json()
    assert data["created"] == 1
    assert data["reports_to_resolved"] == 1
    rana = _find_emp(db, "EMP001")
    mng = db.employees.find_one({"employee_id": "EMP100"})
    assert rana["reports_to"] == mng["_id"]


def test_import_unresolved_manager_warns_but_creates(client):
    db = client._fake_db
    r = _import(client, [
        "Rana,rana@corp.com,555,Design,Designer,FT,Office,2026-01-01,ghost@corp.com",
    ])
    data = r.get_json()
    assert data["created"] == 1
    assert data["reports_to_unresolved"] == 1
    assert data["warnings"] == [{
        "row": 2, "warning": "reports_to_email_not_found", "email": "ghost@corp.com",
    }]
    rana = _find_emp(db, "EMP001")
    assert "reports_to" not in rana


def test_import_self_reference_is_rejected(client):
    db = client._fake_db
    r = _import(client, [
        "Rana,rana@corp.com,555,Design,Designer,FT,Office,2026-01-01,rana@corp.com",
    ])
    data = r.get_json()
    assert data["created"] == 1
    assert data["reports_to_unresolved"] == 1
    rana = _find_emp(db, "EMP001")
    assert "reports_to" not in rana


def test_import_never_resolves_manager_from_other_org(client):
    db = client._fake_db
    # Same email, but belonging to ORG_B — must NOT be linked from ORG_A import.
    db.employees.insert_one({
        "_id": ObjectId(), "employee_id": "EMP200", "org_id": ObjectId(ORG_B),
        "status": "active", "email_hash": blind_index("shared@corp.com"),
        "encrypted": {"email": "shared@corp.com"}, "wrapped_dek": "dek",
    })
    r = _import(client, [
        "Rana,rana@corp.com,555,Design,Designer,FT,Office,2026-01-01,shared@corp.com",
    ])
    data = r.get_json()
    assert data["created"] == 1
    assert data["reports_to_unresolved"] == 1
    rana = _find_emp(db, "EMP001")
    assert "reports_to" not in rana


def test_export_missing_reports_to_column_blank(client):
    db = client._fake_db
    db.employees.insert_one({
        "_id": ObjectId(), "employee_id": "EMP500", "org_id": ObjectId(ORG_A),
        "status": "active",
        "encrypted": {"name": "Solo", "email": "solo@corp.com"}, "wrapped_dek": "dek",
    })
    body = client.get("/api/employees/export-csv").get_data(as_text=True)
    row = body.strip().splitlines()[1]
    assert ",," not in row or "solo@corp.com" in row
    # The reports_to_email cell (last column) is empty for a manager-less emp.
    assert row.rstrip().endswith(",")


def test_export_round_trips_reports_to_email(client):
    db = client._fake_db
    manager = db.employees.insert_one({
        "_id": ObjectId(), "employee_id": "EMP300", "org_id": ObjectId(ORG_A),
        "status": "active",
        "encrypted": {"name": "HR", "email": "hr@corp.com"}, "wrapped_dek": "dek",
    })
    db.employees.insert_one({
        "_id": ObjectId(), "employee_id": "EMP301", "org_id": ObjectId(ORG_A),
        "status": "active", "reports_to": manager.inserted_id,
        "encrypted": {"name": "Rana", "email": "rana@corp.com"}, "wrapped_dek": "dek",
    })
    body = client.get("/api/employees/export-csv").get_data(as_text=True)
    lines = body.strip().splitlines()
    assert lines[0].endswith("reports_to_email")
    rana_row = next(L for L in lines[1:] if "rana@corp.com" in L)
    assert rana_row.rstrip().endswith("hr@corp.com")
