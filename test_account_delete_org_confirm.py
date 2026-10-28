    # The caller must also type the organization's exact name, so an account
    # (and possibly the whole org) is never deleted by an accidental click.
    org_doc = db.organizations.find_one({"_id": user["org_id"]}) if user.get("org_id") else None
    if org_doc:
        typed_org_name = (data.get("orgName") or "").strip()
        if typed_org_name != (org_doc.get("name") or "").strip():
            return jsonify({"error": "org_name_mismatch"}), 400

                <div class="st-confirm-fields">
                  <div class="st-field" id="deleteOrgWrap">
                    <label for="deleteOrgName">Type your organization name to confirm: <strong id="deleteOrgExpected"></strong></label>
                    <input type="text" id="deleteOrgName" class="form-input" placeholder="Organization name" autocomplete="off" autocapitalize="off" spellcheck="false">
                  </div>
                </div>
                  <button class="btn btn-sm st-danger-btn" id="confirmDeleteBtn" type="button" disabled>Delete account</button>
  var orgWrap = document.getElementById('deleteOrgWrap');
  var orgInp = document.getElementById('deleteOrgName');
  var orgExpectedEl = document.getElementById('deleteOrgExpected');
  var expectedOrg = '';
  // Delete stays disabled until the typed organization name matches exactly.
  // The server checks it again, so this is only a convenience.
  function orgMatches(){
    if (!orgInp || !expectedOrg) return true;
    return orgInp.value.trim() === expectedOrg;
  }
  function refreshConfirmState(){
    if (confirmBtn) confirmBtn.disabled = !orgMatches();
  }
  function loadExpectedOrg(){
    var org = window.__voovrMeOrg;
    if (org && org.name) return Promise.resolve(String(org.name).trim());
    return fetch('/api/me').then(function(r){ return r.ok ? r.json() : null; })
      .then(function(d){ return (d && d.organization && d.organization.name) ? String(d.organization.name).trim() : ''; })
      .catch(function(){ return ''; });
  }
  if (orgInp) orgInp.addEventListener('input', refreshConfirmState);

      loadExpectedOrg().then(function(name){
        expectedOrg = name;
        if (orgExpectedEl) orgExpectedEl.textContent = name;
        if (orgWrap) orgWrap.style.display = name ? '' : 'none';
        refreshConfirmState();
        if (name && orgInp) orgInp.focus();
      });
    if (orgInp) orgInp.value = '';
    if (confirmBtn) confirmBtn.disabled = true;
          totpCode: totpInp ? totpInp.value : '',
          orgName: orgInp ? orgInp.value.trim() : ''
        refreshConfirmState();
        if (err && err.code === 'org_name_mismatch') {
          showToast('The organization name you typed doesn\u2019t match. Check it and try again.');
          return;
        }
"""DELETE /api/me must also require the organization's exact name."""
import os

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("HASH_INDEX_SECRET", "test-secret")

import pytest
from bson import ObjectId
from flask import Flask

import api as api_mod

ORG = "aaaaaaaaaaaaaaaaaaaaaaaa"
USER = "999999999999999999999999"


class _Result:
    def __init__(self, n=1):
        self.deleted_count = n


class _Col:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.deleted = False

    def find_one(self, q=None, *a, **k):
        for d in self.docs:
            if all(d.get(key) == val for key, val in (q or {}).items()):
                return d
        return None

    def count_documents(self, q=None, *a, **k):
        return len(self.docs)

    def delete_one(self, q):
        self.deleted = True
        return _Result()

    def delete_many(self, q):
        self.deleted = True
        return _Result()

    def insert_one(self, d):
        self.docs.append(d)


class _DB:
    def __init__(self):
        self.users = _Col([{"_id": ObjectId(USER), "org_id": ObjectId(ORG), "role": "admin"}])  # no password / TOTP
        self.organizations = _Col([{"_id": ObjectId(ORG), "name": "Acme Pvt Ltd"}])
        self.active_sessions = _Col()
        self.employees = _Col()
        self.sessions = _Col()
        self.meetings = _Col()
        self.conversation_memory = _Col()
        self.notifications = _Col()
        self.audit_log = _Col()


@pytest.fixture
def setup(monkeypatch):
    db = _DB()
    monkeypatch.setattr(api_mod, "get_db", lambda: db)
    monkeypatch.setattr(api_mod, "_check_auth", lambda: USER)
    monkeypatch.setattr(api_mod, "log_audit_event", lambda *a, **k: None)
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(api_mod.api_bp, url_prefix="/api")
    return app.test_client(), db


def test_missing_org_name_is_rejected(setup):
    client, db = setup
    r = client.delete("/api/me", json={})
    assert r.status_code == 400 and r.get_json()["error"] == "org_name_mismatch"
    assert db.users.deleted is False


def test_wrong_org_name_is_rejected(setup):
    client, db = setup
    r = client.delete("/api/me", json={"orgName": "acme pvt ltd"})
    assert r.status_code == 400 and r.get_json()["error"] == "org_name_mismatch"
    assert db.users.deleted is False and db.organizations.deleted is False


def test_exact_org_name_deletes(setup):
    client, db = setup
    r = client.delete("/api/me", json={"orgName": "  Acme Pvt Ltd "})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert db.users.deleted is True