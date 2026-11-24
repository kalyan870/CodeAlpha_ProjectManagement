import os
import sys
import tempfile
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{(Path(tempfile.mkdtemp()) / 'pm.db').as_posix()}"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from app.auth import require_user
from app.database import get_engine, initialize_local_database
from app.main import app

client = TestClient(app)
initialize_local_database()


def auth_override(request: Request):
    users = {"owner-token": "user_owner", "guest-token": "user_guest"}
    token = request.headers.get("authorization", "").replace("Bearer ", "")
    if token not in users:
        raise HTTPException(401, "Not authenticated")
    return users[token]


app.dependency_overrides[require_user] = auth_override


def headers(token="owner-token", name="Project member", email="member@example.com"):
    return {"Authorization": f"Bearer {token}", "X-User-Name": name, "X-User-Email": email}


def setup_function():
    with get_engine().begin() as conn:
        for table in ("comments", "tasks", "project_members", "projects", "users"):
            conn.exec_driver_sql(f"DELETE FROM {table}")


def profile(token, name):
    response = client.get("/api/me", headers=headers(token, name, f"{name.lower().replace(' ', '.')}@example.com"))
    assert response.status_code == 200
    return response.json()


def test_clerk_dependency_uses_verified_subject_and_fails_closed(monkeypatch):
    import app.auth as auth

    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_unit")
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "http://localhost:7860")
    class State:
        is_signed_in = True
        payload = {"sub": "user_verified_by_clerk"}
    seen = {}
    monkeypatch.setattr(auth, "authenticate_request", lambda request, options: (seen.setdefault("options", options), State())[1])
    scope = {"type":"http", "method":"GET", "path":"/api/me", "headers":[], "query_string":b"", "server":("test",80), "client":("test",1), "scheme":"http"}
    assert require_user(Request(scope)) == "user_verified_by_clerk"
    assert seen["options"].accepts_token == ["session_token"]
    monkeypatch.delenv("CLERK_SECRET_KEY")
    try:
        require_user(Request(scope))
        assert False, "missing Clerk config must fail closed"
    except HTTPException as exc:
        assert exc.status_code == 503


def test_projects_are_private_and_only_owner_can_add_members():
    profile("owner-token", "Owner")
    guest = profile("guest-token", "Guest")
    project = client.post("/api/projects", headers=headers(), json={"name":"Private"}).json()
    pid = project["id"]
    assert client.get(f"/api/projects/{pid}", headers=headers("guest-token")).status_code == 404
    assert client.get(f"/api/projects/{pid}/tasks", headers=headers("guest-token")).status_code == 404
    assert client.post(f"/api/projects/{pid}/members?user_id_to_add={guest['id']}", headers=headers("guest-token")).status_code == 403
    assert client.post(f"/api/projects/{pid}/members?user_id_to_add={guest['id']}", headers=headers()).status_code == 200
    assert client.get(f"/api/projects/{pid}", headers=headers("guest-token")).status_code == 200


def test_tasks_validate_membership_fields_and_comment_identity():
    profile("owner-token", "Owner")
    project = client.post("/api/projects", headers=headers(), json={"name":"Board"}).json()
    pid = project["id"]
    assert client.post(f"/api/projects/{pid}/tasks", headers=headers(), json={"title":"x", "priority":"urgent"}).status_code == 422
    task = client.post(f"/api/projects/{pid}/tasks", headers=headers(), json={"title":"Review"}).json()
    assert client.patch(f"/api/tasks/{task['id']}", headers=headers(), json={"status":"deleted"}).status_code == 422
    comment = client.post(f"/api/tasks/{task['id']}/comments", headers=headers(name="Owner"), json={"content":"First note"})
    assert comment.status_code == 200 and comment.json()["user_name"] == "Owner"
    assert client.patch(f"/api/tasks/{task['id']}", json={"status":"done"}).status_code == 401


def test_unauthenticated_routes_and_legacy_password_auth_are_removed():
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/register", json={"email":"x","password":"long-long-password","name":"x"}).status_code == 404
    assert client.post("/api/login", json={"email":"x","password":"x"}).status_code == 404


def test_frontend_script_parses_and_does_not_store_session_tokens():
    import re
    html = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(encoding="utf-8")
    scripts = [script for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL) if script.strip()]
    import subprocess
    result = subprocess.run(["node", "--check"], input=scripts[-1], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "localStorage.setItem('token'" not in html
