"""A form body stuffed with junk fields is refused before any route reads it.

/signup takes a urlencoded form from anyone on the internet. FastAPI walks
every key of a form body for each declared Form parameter, and Starlette
releases before 1.3.1 set no cap on how many urlencoded fields it would parse,
so a few hundred kilobytes of junk keys held Forager's single worker for tens
of seconds of CPU. Starlette 1.3.1 and later stop at 1000 fields with a 400.

The status alone proves nothing here: an ordinary bad signup is a 400 too. The
refusal has to carry Starlette's own "Too many fields" message, which only the
parser limit produces.
"""
from app.database import SessionLocal
from app.models import Account


def test_signup_refuses_more_than_1000_form_fields(client):
    fields = [f"junk{i}=x" for i in range(1000)]
    fields.append("email=someone%40example.com")
    r = client.post(
        "/signup",
        content="&".join(fields),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Too many fields" in r.text
    with SessionLocal() as db:
        assert db.query(Account).count() == 0


def test_ordinary_signup_is_not_caught_by_the_field_limit(client):
    r = client.post("/signup",
                    data={"email": "dan@example.com", "password": "hunter2222",
                          "confirm_password": "hunter2222"},
                    follow_redirects=False)
    assert r.status_code == 303
