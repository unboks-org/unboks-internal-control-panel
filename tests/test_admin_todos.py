from fastapi.testclient import TestClient

from app.main import app
from app.todos import list_todos


def _login(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)
    response = client.post("/login", data={"password": "test-password"})
    assert response.status_code == 200
    return client


def test_todos_require_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)

    response = client.get("/admin/todos", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_todo_page_renders_rich_paste_editor(monkeypatch, tmp_path):
    client = _login(tmp_path, monkeypatch)

    response = client.get("/admin/todos")

    assert response.status_code == 200
    assert "Private working list for Calvin" in response.text
    assert "data-todo-editor" in response.text
    assert "data-todo-html" in response.text
    assert "Rich paste" in response.text


def test_create_toggle_and_delete_rich_todo(monkeypatch, tmp_path):
    client = _login(tmp_path, monkeypatch)

    created = client.post(
        "/admin/todos",
        data={
            "content_html": (
                "<p><strong>Call lawyer</strong></p>"
                "<script>alert('bad')</script>"
                "<img src=\"data:image/png;base64,AAAA\" onerror=\"bad()\">"
            ),
            "content_plain": "Call lawyer",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/admin/todos"

    page = client.get("/admin/todos")
    assert page.status_code == 200
    assert "<strong>Call lawyer</strong>" in page.text
    assert "<script>" not in page.text
    assert "onerror" not in page.text
    assert "data:image/png;base64,AAAA" in page.text

    todos = list_todos()
    assert len(todos) == 1
    todo = todos[0]
    assert not todo.is_done

    toggled = client.post(
        f"/admin/todos/{todo.id}/toggle",
        follow_redirects=False,
    )
    assert toggled.status_code == 303
    assert list_todos()[0].is_done

    deleted = client.post(
        f"/admin/todos/{todo.id}/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert list_todos() == ()


def test_admin_js_contains_todo_paste_handlers():
    js = open("app/static/js/admin.js", encoding="utf-8").read()

    assert "initTodoEditor" in js
    assert "data-todo-editor" in js
    assert "readAsDataURL" in js
