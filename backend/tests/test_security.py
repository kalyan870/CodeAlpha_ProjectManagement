import os
import sys
import tempfile
from pathlib import Path

os.environ["APP_DATA_DIR"] = tempfile.mkdtemp(prefix="codealpha-pm-tests-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.main import app, get_db, tokens

client = TestClient(app)


def setup_function():
    tokens.clear()
    conn = get_db()
    for table in ("comments", "tasks", "project_members", "projects", "users"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


def register(email):
    result = client.post("/api/register", json={"name": email.split("@")[0], "email": email, "password": "long-secure-pass"})
    assert result.status_code == 200
    return result.json()


def test_auth_uses_authorization_header_and_protects_users():
    user = register("one@example.com")
    headers = {"Authorization": f"Bearer {user['token']}"}
    assert client.get("/api/me", headers=headers).status_code == 200
    assert client.get("/api/users").status_code == 401
    users = client.get("/api/users", headers=headers)
    assert users.status_code == 200
    assert "email" not in users.json()[0]


def test_project_data_is_private_and_membership_is_owner_controlled():
    owner = register("owner@example.com")
    guest = register("guest@example.com")
    owner_headers = {"Authorization": f"Bearer {owner['token']}"}
    guest_headers = {"Authorization": f"Bearer {guest['token']}"}
    project = client.post("/api/projects", headers=owner_headers, json={"name": "Private"}).json()
    pid = project["id"]
    assert client.get(f"/api/projects/{pid}", headers=guest_headers).status_code == 404
    assert client.get(f"/api/projects/{pid}/tasks", headers=guest_headers).status_code == 404
    assert client.post(f"/api/projects/{pid}/members?user_id={guest['user']['id']}", headers=guest_headers).status_code == 403
    assert client.post(f"/api/projects/{pid}/members?user_id={guest['user']['id']}", headers=owner_headers).status_code == 200
    assert client.get(f"/api/projects/{pid}", headers=guest_headers).status_code == 200


def test_task_access_validates_membership_and_enums():
    owner = register("owner@example.com")
    headers = {"Authorization": f"Bearer {owner['token']}"}
    project = client.post("/api/projects", headers=headers, json={"name": "Tasks"}).json()
    pid = project["id"]
    bad = client.post(f"/api/projects/{pid}/tasks", headers=headers, json={"title": "T", "priority": "urgent"})
    assert bad.status_code == 422
    task = client.post(f"/api/projects/{pid}/tasks", headers=headers, json={"title": "Valid"}).json()
    assert client.patch(f"/api/tasks/{task['id']}", headers=headers, json={"status": "deleted"}).status_code == 422
    assert client.patch(f"/api/tasks/{task['id']}", json={"status": "done"}).status_code == 401


def test_passwords_are_salted_and_legacy_hash_upgrades_on_login():
    user = register("password@example.com")
    conn = get_db()
    stored = conn.execute("SELECT password FROM users WHERE email=?", ("password@example.com",)).fetchone()[0]
    assert stored.startswith("pbkdf2_sha256$")
    # Replace with the legacy SHA digest, then verify transparent migration.
    import hashlib
    conn = get_db()
    conn.execute("UPDATE users SET password=? WHERE email=?", (hashlib.sha256(b"long-secure-pass").hexdigest(), "password@example.com"))
    conn.commit()
    conn.close()
    assert client.post("/api/login", json={"email": "password@example.com", "password": "long-secure-pass"}).status_code == 200
    conn = get_db()
    migrated = conn.execute("SELECT password FROM users WHERE email=?", ("password@example.com",)).fetchone()[0]
    conn.close()
    assert migrated.startswith("pbkdf2_sha256$")
