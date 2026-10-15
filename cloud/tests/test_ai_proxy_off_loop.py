"""The AI proxy never runs a query on the event loop.

/v1/ai/analyze is async so the upstream call holds no thread, but its database
work used to run on the loop too. Under load that froze the service: a query
waiting for a pooled connection blocked the loop, and the requests holding the
connections needed the loop to give them back. Every statement now runs in
the threadpool, and none is in flight while the upstream call is.
"""
import asyncio
import io

from sqlalchemy import event

from app import forwarder as forwarder_mod
from app.database import get_engine
from app.forwarder import ForwardResult, StubForwarder
from app.routers import ai as ai_router


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _watch_statements():
    seen = []

    def listener(conn, cursor, statement, params, context, executemany):
        seen.append((_on_loop(), " ".join(statement.split())[:60]))

    event.listen(get_engine(), "before_cursor_execute", listener)
    return seen, lambda: event.remove(get_engine(), "before_cursor_execute", listener)


def _tiny_jpeg():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 90)).save(buf, "JPEG")
    return buf.getvalue()


_JPEG = _tiny_jpeg()


def _analyze(client, token, kind="enrich"):
    files = {}
    if kind in ("food", "receipt"):
        # A real (tiny) JPEG: Forager refuses scan bytes that are not a photo.
        files = {"image": ("photo.jpg", io.BytesIO(_JPEG), "image/jpeg")}
    return client.post("/v1/ai/analyze", data={"kind": kind, "text": "beans"},
                       files=files, headers={"Authorization": f"Bearer {token}"})


def test_no_statement_runs_on_the_event_loop(client, instance_token):
    seen, stop = _watch_statements()
    try:
        for kind in ("enrich", "food"):
            assert _analyze(client, instance_token, kind).status_code == 200
    finally:
        stop()
    assert seen, "the listener saw no statements at all"
    on_loop = [sql for loop, sql in seen if loop]
    assert on_loop == []


def test_a_refusal_releases_from_the_threadpool_too(client, instance_token, monkeypatch):
    class Failing(StubForwarder):
        async def forward(self, *a, **kw):
            raise forwarder_mod.ForwarderError(502, {"error": "upstream_error"})

    monkeypatch.setattr(ai_router, "get_forwarder", lambda: Failing())
    seen, stop = _watch_statements()
    try:
        assert _analyze(client, instance_token).status_code == 502
    finally:
        stop()
    assert [sql for loop, sql in seen if loop] == []
    assert any(sql.startswith("DELETE FROM usage_ledger") for _l, sql in seen)


def test_no_statement_is_in_flight_during_the_upstream_call(client, instance_token,
                                                             monkeypatch):
    seen, stop = _watch_statements()
    during = []

    class Watching(StubForwarder):
        async def forward(self, *a, **kw):
            during.append(len(seen))
            await asyncio.sleep(0.05)
            during.append(len(seen))
            return ForwardResult(result={"name": "Beans"}, tokens=10)

    monkeypatch.setattr(ai_router, "get_forwarder", lambda: Watching())
    try:
        assert _analyze(client, instance_token).status_code == 200
    finally:
        stop()
    # The reservation was committed before the call and nothing queried while
    # it was out, so the request held no pooled connection over it.
    assert during[0] == during[1]
    assert any(sql.startswith("INSERT INTO usage_ledger") for _l, sql in seen[:during[0]])
