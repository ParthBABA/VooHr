"""DELETE /api/me must also require the organization's exact name."""
import os

os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('HASH_INDEX_SECRET', 'test-secret')

import pytest
from bson import ObjectId
from flask import Flask

import api as api_mod

ORG = 'aaaaaaaaaaaaaaaaaaaaaaaa'
USER = '999999999999999999999999'


class FakeUsers:
    def __init__(self):
        self.doc = {
            '_id': ObjectId(USER),
            'email': 'admin@example.com',
            'org_id': ObjectId(ORG),
            'password_hash': 'hash',
            'totp_enabled': False,
            'name': 'Admin',
        }
        self.deleted = False

    def find_one(self, query):
        if '_id' in query and query['_id'] == ObjectId(USER):
            return dict(self.doc)
        if 'email' in query and query['email'] == self.doc['email']:
            return dict(self.doc)
        return None

    def count_documents(self, query):
        if query.get('org_id') == ObjectId(ORG):
            return 1
        return 0

    def delete_one(self, query):
        if '_id' in query and query['_id'] == ObjectId(USER):
            self.deleted = True
        return type('R', (), {'deleted_count': 1})()


class FakeOrgs:
    def __init__(self):
        self.doc = {'_id': ObjectId(ORG), 'name': 'Acme Pvt Ltd'}
        self.deleted = False

    def find_one(self, query):
        if '_id' in query and query['_id'] == ObjectId(ORG):
            return dict(self.doc)
        return None

    def delete_one(self, query):
        if '_id' in query and query['_id'] == ObjectId(ORG):
            self.deleted = True
        return type('R', (), {'deleted_count': 1})()


class FakeColl:
    def __init__(self):
        self.deleted_count = 0

    def delete_many(self, query):
        self.deleted_count += 1
        return type('R', (), {'deleted_count': self.deleted_count})()


class FakeDB:
    def __init__(self):
        self.users = FakeUsers()
        self.organizations = FakeOrgs()
        self.active_sessions = FakeColl()
        self.employees = FakeColl()
        self.sessions = FakeColl()
        self.meetings = FakeColl()
        self.conversation_memory = FakeColl()
        self.notifications = FakeColl()
        self.audit_log = FakeColl()


@pytest.fixture
def setup(monkeypatch):
    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(api_mod.api_bp, url_prefix='/api')
    db = FakeDB()
    monkeypatch.setattr(api_mod, 'get_db', lambda: db)
    monkeypatch.setattr(api_mod, '_check_auth', lambda: str(USER))
    monkeypatch.setattr(api_mod, 'log_audit_event', lambda *a, **k: None)
    monkeypatch.setattr(api_mod, 'verify_password', lambda p, h: p == 'pass')
    client = app.test_client()
    with app.test_request_context():
        from flask import session
        session['user_id'] = USER
    return client, db


def test_missing_org_name_rejected(setup):
    client, db = setup
    r = client.delete('/api/me', json={'password': 'pass'})
    assert r.status_code == 400
    assert r.get_json()['error'] == 'org_name_mismatch'
    assert db.users.deleted is False
    assert db.organizations.deleted is False


def test_wrong_case_org_name_rejected(setup):
    client, db = setup
    r = client.delete('/api/me', json={'password': 'pass', 'orgName': 'acme pvt ltd'})
    assert r.status_code == 400
    assert r.get_json()['error'] == 'org_name_mismatch'
    assert db.users.deleted is False and db.organizations.deleted is False


def test_exact_org_name_with_spaces_deletes(setup):
    client, db = setup
    r = client.delete('/api/me', json={'password': 'pass', 'orgName': '  Acme Pvt Ltd '})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    assert db.users.deleted is True
