"""Server-side employee search must filter BEFORE paginating.

Name/email are encrypted, so MongoDB cannot match them. The previous
implementation paginated in Mongo first and then filtered the single returned
page in Python, which meant a search only ever matched employees that happened
to land on the current page, and reported `total`/`has_more` derived from the
UNFILTERED count.

These tests pin the corrected contract:
  - a match on any page is findable,
  - `total` counts matches, not the whole org,
  - `has_more` is computed from the filtered set,
  - the no-search path keeps its original Mongo-side pagination behaviour.
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("HASH_INDEX_SECRET", "test-secret")

import pytest
from bson import ObjectId
from flask import Flask

import employees as employees_mod

_ORG = "64b0000000000000000000c1"
_ADMIN = "64b0000000000000000000a1"
_MGR = "64b0000000000000000000a2"


class _Cursor:
    """Cursor with real sort/skip/limit so ordering and paging are exercised."""

    def __init__(self, items):
        self._items = list(items)

    def sort(self, key, direction=-1):
        # Newest first; missing/None created_at sorts last, deterministically.
        self._items.sort(
            key=lambda d: (d.get(key) is not None, d.get(key) or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=direction == -1,
        )
        return self

    def skip(self, n):
        return _Cursor(self._items[n:])

    def limit(self, n):
        return _Cursor(self._items[:n])

    def __iter__(self):
        return iter(self._items)


class _Collection:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    def _match(self, doc, filt):
        for k, v in (filt or {}).items():
            if isinstance(v, dict):
                if "$ne" in v and doc.get(k) == v["$ne"]:
                    return False
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def find(self, filt=None, *a, **kw):
        return _Cursor([d for d in self.docs if self._match(d, filt)])

    def find_one(self, filt=None, *a, **kw):
        for d in self.docs:
            if self._match(d, filt or {}):
                return d
        return None

    def count_documents(self, filt=None, *a, **kw):
        return sum(1 for d in self.docs if self._match(d, filt))

    def distinct(self, field, filt=None):
        return list({d.get(field) for d in self.docs if self._match(d, filt or {})})


class _DB:
    def __init__(self):
        self.employees = _Collection()
        self.users = _Collection()
        self.organizations = _Collection()

    def __getitem__(self, name):
        coll = getattr(self, name, None)
        if coll is None:
            coll = _Collection()
            setattr(self, name, coll)
        return coll


@pytest.fixture
def env(monkeypatch):
    db = _DB()
    # An org-admin user so _employee_scope_filter returns {} (full org access)
    # instead of its fail-closed _NEVER_MATCH when no session user is present.
    db.users.insert_one({
        "_id": ObjectId(_ADMIN),
        "org_id": ObjectId(_ORG),
        "role": "admin",
        "email": "admin@corp.com",
    })
    monkeypatch.setattr(employees_mod, "get_db", lambda: db)
    monkeypatch.setattr(employees_mod, "_require_auth", lambda: _ORG)
    # Identity "decryption" keeps the encrypted-blob shape while letting these
    # tests exercise only the search/pagination logic.
    monkeypatch.setattr(employees_mod, "decrypt_fields", lambda enc, dek: enc or {})
    return db


def _make_client(user_id=_ADMIN, org_id=_ORG):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test-secret-key")
    app.register_blueprint(employees_mod.employees_bp, url_prefix="/api")
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["org_id"] = org_id
        sess["session_token"] = "tok"
    return c


@pytest.fixture
def client(env):
    return _make_client(), env


def _seed(db, count, *, match_every=0, department="Engineering"):
    """Create *count* employees; the first *match_every* are named 'Ananya Patel'."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = []
    for i in range(count):
        is_match = i < match_every
        name = "Ananya Patel" if is_match else f"Person {i:03d}"
        email = f"ananya{i}@corp.com" if is_match else f"person{i}@corp.com"
        # Oldest created_at for i=0, so a -1 sort puts the LAST seeded first.
        doc = {
            "org_id": ObjectId(_ORG),
            "employee_id": f"EMP{i:04d}",
            "status": "active",
            "department": department,
            "encrypted": {"name": name, "email": email},
            "wrapped_dek": "dek",
            "created_at": base + timedelta(days=i),
        }
        db.employees.insert_one(doc)
        ids.append(name)
    return ids


class TestSearchFiltersBeforePaginating:
    def test_match_beyond_first_page_is_found(self, client):
        c, db = client
        # 30 employees, only the ones created LAST (deepest pages) match.
        _seed(db, 30, match_every=2)

        # Newest-first ordering puts the matching employees at the END, i.e.
        # past a single 10-per-page window.
        r = c.get("/api/employees?search=ananya&limit=10&page=1").get_json()
        assert r["total"] == 2
        assert {e["name"] for e in r["employees"]} == {"Ananya Patel"}
        assert r["has_more"] is False

    def test_old_bug_would_have_returned_nothing(self, client):
        """Regression guard: page-1-only filtering missed later matches."""
        c, db = client
        _seed(db, 40, match_every=1)
        # Unfiltered page 1 (newest 5) contains no 'ananya'.
        page1 = c.get("/api/employees?limit=5&page=1").get_json()
        assert not any("Ananya" in e["name"] for e in page1["employees"])
        # But the server-side search still finds it.
        found = c.get("/api/employees?search=ananya&limit=5&page=1").get_json()
        assert found["total"] == 1
        assert found["employees"][0]["name"] == "Ananya Patel"

    def test_total_counts_matches_not_whole_org(self, client):
        c, db = client
        _seed(db, 50, match_every=3)
        r = c.get("/api/employees?search=ananya&limit=20").get_json()
        assert r["total"] == 3

    def test_has_more_reflects_filtered_set(self, client):
        c, db = client
        _seed(db, 50, match_every=5)
        page1 = c.get("/api/employees?search=ananya&limit=2&page=1").get_json()
        assert len(page1["employees"]) == 2
        assert page1["has_more"] is True

        page3 = c.get("/api/employees?search=ananya&limit=2&page=3").get_json()
        assert len(page3["employees"]) == 1
        assert page3["has_more"] is False

    def test_page_beyond_filtered_end_is_empty_not_wrong(self, client):
        c, db = client
        _seed(db, 50, match_every=2)
        r = c.get("/api/employees?search=ananya&limit=10&page=9").get_json()
        assert r["employees"] == []
        assert r["total"] == 2
        assert r["has_more"] is False

    def test_no_matches_reports_zero_total(self, client):
        c, db = client
        _seed(db, 20, match_every=2)
        r = c.get("/api/employees?search=zzzznotfound").get_json()
        assert r["employees"] == []
        assert r["total"] == 0
        assert r["has_more"] is False

    def test_search_is_case_insensitive_and_partial(self, client):
        c, db = client
        _seed(db, 10, match_every=1)
        for term in ("ANANYA", "ananya", "AnYa", "patel"):
            r = c.get(f"/api/employees?search={term}").get_json()
            assert r["total"] == 1, f"search={term!r} did not match"


class TestSearchMatchesAllFieldsAndFilters:
    def test_matches_department(self, client):
        c, db = client
        _seed(db, 10, match_every=0, department="Engineering")
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "EMP9999", "status": "active",
            "department": "Legal", "encrypted": {"name": "Zed", "email": "z@corp.com"},
            "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        })
        r = c.get("/api/employees?search=legal").get_json()
        assert [e["name"] for e in r["employees"]] == ["Zed"]

    def test_search_respects_department_filter(self, client):
        c, db = client
        _seed(db, 5, match_every=5, department="Engineering")
        r = c.get("/api/employees?search=ananya&department=Legal").get_json()
        assert r["total"] == 0

    def test_search_respects_status_filter(self, client):
        c, db = client
        _seed(db, 4, match_every=4)
        for d in db.employees.docs:
            d["status"] = "inactive"
        r = c.get("/api/employees?search=ananya&status=inactive").get_json()
        assert r["total"] == 4
        r = c.get("/api/employees?search=ananya&status=active").get_json()
        assert r["total"] == 0

    def test_empty_search_uses_unfiltered_pagination(self, client):
        """A blank/absent search must not trigger the decrypt-and-scan path."""
        c, db = client
        _seed(db, 30, match_every=5)
        r = c.get("/api/employees?limit=10&page=2").get_json()
        assert r["total"] == 30
        assert len(r["employees"]) == 10
        assert r["has_more"] is True

    def test_unfiltered_last_page_has_more_false(self, client):
        c, db = client
        _seed(db, 30, match_every=5)
        r = c.get("/api/employees?limit=10&page=3").get_json()
        assert len(r["employees"]) == 10
        assert r["has_more"] is False

    def test_cross_org_isolation_preserved(self, client):
        """Search must never leak another org's employees."""
        c, db = client
        _seed(db, 3, match_every=1)
        db.employees.insert_one({
            "org_id": ObjectId("64b0000000000000000000c9"), "employee_id": "OTHER1",
            "status": "active", "department": "Engineering",
            "encrypted": {"name": "Ananya Outsider", "email": "out@other.com"},
            "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        })
        r = c.get("/api/employees?search=ananya").get_json()
        assert r["total"] == 1
        assert r["employees"][0]["name"] == "Ananya Patel"


class TestSearchRespectsRoleScope:
    """Search must run INSIDE the caller's scope, not org-wide then filtered."""

    def _manager_env(self, monkeypatch, reports_to):
        db = _DB()
        db.users.insert_one({
            "_id": ObjectId(_MGR),
            "org_id": ObjectId(_ORG),
            "role": "manager",
            "linked_employee_id": ObjectId(reports_to),
        })
        monkeypatch.setattr(employees_mod, "get_db", lambda: db)
        monkeypatch.setattr(employees_mod, "_require_auth", lambda: _ORG)
        monkeypatch.setattr(employees_mod, "decrypt_fields", lambda enc, dek: enc or {})
        return db

    def test_manager_search_only_sees_direct_reports(self, monkeypatch):
        mgr_emp = "64b0000000000000000000b1"
        db = self._manager_env(monkeypatch, mgr_emp)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 3 direct reports, all matching the search term.
        for i in range(3):
            db.employees.insert_one({
                "org_id": ObjectId(_ORG), "employee_id": f"RPT{i}", "status": "active",
                "department": "Engineering", "reports_to": ObjectId(mgr_emp),
                "encrypted": {"name": f"Ananya Report {i}", "email": f"r{i}@corp.com"},
                "created_at": base + timedelta(days=i),
            })
        # 4 same-name employees elsewhere in the org, not reporting to the manager.
        for i in range(4):
            db.employees.insert_one({
                "org_id": ObjectId(_ORG), "employee_id": f"OTH{i}", "status": "active",
                "department": "Sales",
                "encrypted": {"name": f"Ananya Stranger {i}", "email": f"o{i}@corp.com"},
                "created_at": base + timedelta(days=10 + i),
            })

        c = _make_client(user_id=_MGR)
        r = c.get("/api/employees?search=ananya&limit=50").get_json()
        assert r["total"] == 3
        assert all("Report" in e["name"] for e in r["employees"])

    def test_manager_search_total_excludes_outsiders(self, monkeypatch):
        mgr_emp = "64b0000000000000000000b1"
        db = self._manager_env(monkeypatch, mgr_emp)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "RPT0", "status": "active",
            "department": "Engineering", "reports_to": ObjectId(mgr_emp),
            "encrypted": {"name": "Ananya Report", "email": "r@corp.com"},
            "created_at": base,
        })
        for i in range(9):
            db.employees.insert_one({
                "org_id": ObjectId(_ORG), "employee_id": f"OTH{i}", "status": "active",
                "department": "Sales",
                "encrypted": {"name": f"Ananya Stranger {i}", "email": f"o{i}@corp.com"},
                "created_at": base + timedelta(days=10 + i),
            })

        c = _make_client(user_id=_MGR)
        r = c.get("/api/employees?search=ananya&limit=5").get_json()
        # Unfiltered org total is 10; the manager's visible+matching total is 1.
        assert r["total"] == 1
        assert r["has_more"] is False


class TestWellnessStatusFilter:
    """wellness_status is DERIVED, so it must be filtered server-side after
    derivation -- querying ai_wellness.status in Mongo would silently drop
    employees whose status is computed from HR signals."""

    def _ai(self, db, name, status, score, burnout=None, dept="Engineering"):
        doc = {
            "org_id": ObjectId(_ORG), "employee_id": name.replace(" ", ""),
            "status": "active", "department": dept,
            "encrypted": {"name": name, "email": name.replace(" ", ".").lower() + "@corp.com"},
            "ai_wellness": {"status": status, "score": score, "risk_factors": []},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        if burnout is not None:
            doc["ai_wellness"]["burnout_index"] = burnout
        db.employees.insert_one(doc)

    def test_filters_by_derived_status(self, client):
        c, db = client
        self._ai(db, "Crit One", "critical", 25)
        self._ai(db, "Warn One", "warning", 55)
        self._ai(db, "Good One", "healthy", 88)
        r = c.get("/api/employees?wellness_status=critical").get_json()
        assert r["total"] == 1
        assert r["employees"][0]["name"] == "Crit One"

    def test_not_assessed_rows_are_filterable(self, client):
        c, db = client
        self._ai(db, "Crit One", "critical", 25)
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "NONE1", "status": "active",
            "department": "Engineering",
            "encrypted": {"name": "No Data", "email": "nodata@corp.com"},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
        r = c.get("/api/employees?wellness_status=not_assessed").get_json()
        assert r["total"] == 1
        assert r["employees"][0]["name"] == "No Data"

    def test_signal_derived_status_is_matched(self, client):
        """A row with only HR signals (no ai_wellness) must still be filterable.

        NOTE the vocabulary: the signals branch of _employee_to_json() derives
        status from employee_scoring.score_employee(), which returns
        "healthy" | "watch" | "at_risk" -- NOT the "critical"/"warning" strings
        the AI-transcript branch emits. This test pins the real value.
        """
        c, db = client
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "SIG1", "status": "active",
            "department": "Engineering",
            "encrypted": {"name": "Signal Only", "email": "sig@corp.com"},
            # Real (non-zero) signals force the score_employee() branch.
            "signals": {"overtime_hours_last_3w": 40, "absences_last_30d": 6,
                        "missed_deadlines_last_30d": 5, "performance_delta_pct": -20},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
        r = c.get("/api/employees?wellness_status=at_risk").get_json()
        assert r["total"] == 1
        assert r["employees"][0]["name"] == "Signal Only"

    def test_status_vocabulary_differs_between_branches(self, client):
        """Documents the pre-existing split, so a future unification is a
        deliberate change rather than an accident.

        Signals -> "at_risk"/"watch"/"healthy"; AI transcript ->
        "critical"/"warning"/"healthy". The dashboard's status dropdown only
        offers the AI vocabulary, so signal-derived rows are reachable only
        under "All Status".
        """
        c, db = client
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "SIG1", "status": "active",
            "department": "Engineering",
            "encrypted": {"name": "Signal Only", "email": "sig@corp.com"},
            "signals": {"overtime_hours_last_3w": 40, "absences_last_30d": 6,
                        "missed_deadlines_last_30d": 5, "performance_delta_pct": -20},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
        rows = c.get("/api/employees").get_json()["employees"]
        assert rows[0]["wellness_status"] == "at_risk"
        # The dropdown values used by the dashboard.
        for offered in ("critical", "warning", "healthy"):
            r = c.get(f"/api/employees?wellness_status={offered}").get_json()
            assert r["total"] == 0

    def test_combines_with_search_and_pagination(self, client):
        c, db = client
        for i in range(4):
            self._ai(db, f"Crit {i}", "critical", 20 + i)
        self._ai(db, "Crit Extra", "warning", 60)
        r = c.get("/api/employees?search=crit&wellness_status=critical&limit=2&page=1").get_json()
        assert r["total"] == 4
        assert len(r["employees"]) == 2
        assert r["has_more"] is True
        r = c.get("/api/employees?search=crit&wellness_status=critical&limit=2&page=2").get_json()
        assert len(r["employees"]) == 2
        assert r["has_more"] is False

    def test_status_filter_respects_manager_scope(self, monkeypatch):
        mgr_emp = "64b0000000000000000000b1"
        db = _DB()
        db.users.insert_one({"_id": ObjectId(_MGR), "org_id": ObjectId(_ORG),
                             "role": "manager", "linked_employee_id": ObjectId(mgr_emp)})
        monkeypatch.setattr(employees_mod, "get_db", lambda: db)
        monkeypatch.setattr(employees_mod, "_require_auth", lambda: _ORG)
        monkeypatch.setattr(employees_mod, "decrypt_fields", lambda enc, dek: enc or {})
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "RPT", "status": "active",
            "department": "Engineering", "reports_to": ObjectId(mgr_emp),
            "encrypted": {"name": "My Report", "email": "r@corp.com"},
            "ai_wellness": {"status": "critical", "score": 20, "risk_factors": []},
            "created_at": base,
        })
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "OTH", "status": "active",
            "department": "Sales",
            "encrypted": {"name": "Someone Else", "email": "s@corp.com"},
            "ai_wellness": {"status": "critical", "score": 20, "risk_factors": []},
            "created_at": base,
        })
        c = _make_client(user_id=_MGR)
        r = c.get("/api/employees?wellness_status=critical").get_json()
        assert r["total"] == 1
        assert r["employees"][0]["name"] == "My Report"


class TestEmployeeStatsEndpoint:
    def test_aggregates_match_client_side_math(self, client):
        c, db = client
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        # 2 active (1 inactive), 2 departments, scores 80+60 -> avg 70,
        # 1 burnout (>=70), 1 created this week.
        rows = [
            ("A", "Engineering", "active", 80, 40, base),
            ("B", "Engineering", "active", 60, 75, now - timedelta(days=1)),
            ("C", "Sales", "inactive", None, None, base),
        ]
        for emp_id, dept, status, score, burnout_idx, created in rows:
            doc = {
                "org_id": ObjectId(_ORG), "employee_id": emp_id, "status": status,
                "department": dept,
                "encrypted": {"name": f"Name {emp_id}", "email": f"{emp_id}@corp.com"},
                "created_at": created,
            }
            if score is not None:
                doc["ai_wellness"] = {"status": "healthy", "score": score, "risk_factors": []}
                if burnout_idx is not None:
                    doc["ai_wellness"]["burnout_index"] = burnout_idx
            db.employees.insert_one(doc)

        r = c.get("/api/employees/stats").get_json()
        assert r["total"] == 3
        assert r["active"] == 2
        assert r["department_count"] == 2
        assert r["avg_wellness_score"] == 70
        assert r["scored_count"] == 2
        assert r["burnout_count"] == 1
        assert r["new_this_week"] == 1
        assert r["truncated"] is False

    def test_null_scores_excluded_from_average(self, client):
        c, db = client
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "A", "status": "active",
            "department": "Engineering",
            "encrypted": {"name": "Scored", "email": "a@corp.com"},
            "ai_wellness": {"status": "healthy", "score": 50, "risk_factors": []},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "B", "status": "active",
            "department": "Engineering",
            "encrypted": {"name": "Unscored", "email": "b@corp.com"},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
        r = c.get("/api/employees/stats").get_json()
        assert r["avg_wellness_score"] == 50
        assert r["scored_count"] == 1
        assert r["burnout_count"] == 0

    def test_empty_org_returns_nulls_not_zeros(self, client):
        c, _ = client
        r = c.get("/api/employees/stats").get_json()
        assert r["total"] == 0
        assert r["avg_wellness_score"] is None
        assert r["burnout_count"] == 0

    def test_stats_respects_manager_scope(self, monkeypatch):
        mgr_emp = "64b0000000000000000000b1"
        db = _DB()
        db.users.insert_one({"_id": ObjectId(_MGR), "org_id": ObjectId(_ORG),
                             "role": "manager", "linked_employee_id": ObjectId(mgr_emp)})
        monkeypatch.setattr(employees_mod, "get_db", lambda: db)
        monkeypatch.setattr(employees_mod, "_require_auth", lambda: _ORG)
        monkeypatch.setattr(employees_mod, "decrypt_fields", lambda enc, dek: enc or {})
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for emp_id, reports_to in (("RPT1", mgr_emp), ("RPT2", mgr_emp), ("OTH", None)):
            db.employees.insert_one({
                "org_id": ObjectId(_ORG), "employee_id": emp_id, "status": "active",
                "department": "Engineering",
                "reports_to": ObjectId(reports_to) if reports_to else None,
                "encrypted": {"name": f"Name {emp_id}", "email": f"{emp_id}@corp.com"},
                "created_at": base,
            })
        c = _make_client(user_id=_MGR)
        r = c.get("/api/employees/stats").get_json()
        assert r["total"] == 2
        assert r["active"] == 2


class TestDepartmentsAndManagerOptions:
    def test_departments_sorted_and_deduped(self, client):
        c, db = client
        for emp_id, dept in (("A", "Sales"), ("B", "Engineering"), ("C", "Sales"), ("D", "")):
            db.employees.insert_one({
                "org_id": ObjectId(_ORG), "employee_id": emp_id, "status": "active",
                "department": dept,
                "encrypted": {"name": f"N{emp_id}", "email": f"{emp_id}@corp.com"},
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            })
        r = c.get("/api/employees/departments").get_json()
        assert r["departments"] == ["Engineering", "Sales"]

    def test_departments_respects_manager_scope(self, monkeypatch):
        mgr_emp = "64b0000000000000000000b1"
        db = _DB()
        db.users.insert_one({"_id": ObjectId(_MGR), "org_id": ObjectId(_ORG),
                             "role": "manager", "linked_employee_id": ObjectId(mgr_emp)})
        monkeypatch.setattr(employees_mod, "get_db", lambda: db)
        monkeypatch.setattr(employees_mod, "_require_auth", lambda: _ORG)
        monkeypatch.setattr(employees_mod, "decrypt_fields", lambda enc, dek: enc or {})
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "R", "status": "active",
            "department": "Engineering", "reports_to": ObjectId(mgr_emp),
            "encrypted": {"name": "R", "email": "r@corp.com"},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "O", "status": "active",
            "department": "Legal",
            "encrypted": {"name": "O", "email": "o@corp.com"},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
        c = _make_client(user_id=_MGR)
        r = c.get("/api/employees/departments").get_json()
        assert r["departments"] == ["Engineering"]

    def test_manager_options_returns_minimal_fields(self, client):
        c, db = client
        db.employees.insert_one({
            "org_id": ObjectId(_ORG), "employee_id": "A", "status": "active",
            "department": "Engineering", "position": "Staff Engineer",
            "encrypted": {"name": "Ann Example", "email": "a@corp.com",
                          "phone": "+1-555-0100"},
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })
        r = c.get("/api/employees/manager-options").get_json()
        assert r["truncated"] is False
        assert len(r["options"]) == 1
        opt = r["options"][0]
        assert opt["name"] == "Ann Example"
        assert opt["position"] == "Staff Engineer"
        assert opt["id"]
        # Decrypted PII beyond what the dropdown needs must not be sent.
        assert "email" not in opt
        assert "phone" not in opt

    def test_manager_options_respects_manager_scope(self, monkeypatch):
        mgr_emp = "64b0000000000000000000b1"
        db = _DB()
        db.users.insert_one({"_id": ObjectId(_MGR), "org_id": ObjectId(_ORG),
                             "role": "manager", "linked_employee_id": ObjectId(mgr_emp)})
        monkeypatch.setattr(employees_mod, "get_db", lambda: db)
        monkeypatch.setattr(employees_mod, "_require_auth", lambda: _ORG)
        monkeypatch.setattr(employees_mod, "decrypt_fields", lambda enc, dek: enc or {})
        for emp_id, rt in (("R", mgr_emp), ("O", None)):
            db.employees.insert_one({
                "org_id": ObjectId(_ORG), "employee_id": emp_id, "status": "active",
                "department": "Engineering",
                "reports_to": ObjectId(rt) if rt else None,
                "encrypted": {"name": f"N {emp_id}", "email": f"{emp_id}@corp.com"},
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            })
        c = _make_client(user_id=_MGR)
        r = c.get("/api/employees/manager-options").get_json()
        assert [o["name"] for o in r["options"]] == ["N R"]
