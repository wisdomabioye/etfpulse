"""GzipMiddleware wiring assertions.

We don't test Starlette's compression logic (well-covered upstream). We test:
    - The middleware is present in the app's middleware stack.
    - The minimum_size threshold matches intent (500B).
    - The middleware order is correct (Gzip outermost, then CORS, then RequestID).

If any of these regress, compression silently stops working for the feed
response (the largest + most-hit endpoint), which would be invisible without
a test.
"""

from __future__ import annotations

from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from etfpulse.api.middleware.request_id import RequestIDMiddleware
from etfpulse.app import create_app


def _middleware_classes(app) -> list[type]:
    """Return middleware classes in the order Starlette will invoke them
    (outermost → innermost). `app.user_middleware` is LIFO vs add_middleware
    calls — last-added = index 0 = outermost."""
    return [m.cls for m in app.user_middleware]


def test_gzip_middleware_is_wired():
    app = create_app()
    assert GZipMiddleware in _middleware_classes(app)


def test_middleware_order_requestid_cors_gzip():
    """Outermost → innermost: RequestID → CORS → Gzip.

    CRITICAL: Gzip MUST be innermost so it sees the raw handler response
    (non-streaming, known Content-Length). If RequestID (BaseHTTPMiddleware)
    wraps around Gzip, Gzip sees a streaming response, hits its streaming-
    compression branch, and ignores `minimum_size` — causing every tiny
    response (like /api/health, 15 bytes) to be gzip-compressed INTO A
    LARGER BODY. The `test_small_response_not_compressed` below pins that
    behaviour; this order assertion stops it from silently regressing."""
    app = create_app()
    order = _middleware_classes(app)
    req_id_idx = order.index(RequestIDMiddleware)
    cors_idx = order.index(CORSMiddleware)
    gzip_idx = order.index(GZipMiddleware)

    assert req_id_idx < cors_idx < gzip_idx, (
        f"Expected RequestID → CORS → Gzip (outer→inner), got: {[c.__name__ for c in order]}"
    )


def test_small_response_not_compressed():
    """Raw ASGI inspection: /api/health (15 bytes) must NOT have
    Content-Encoding: gzip. Regression guard for the middleware-order bug."""
    import asyncio

    app = create_app()

    async def _probe():
        messages: list[dict] = []

        async def send(msg):
            messages.append(msg)

        async def recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        # Lifespan dance so app.state is ready.
        life_msgs: list[dict] = []

        async def life_send(m):
            life_msgs.append(m)

        async def life_recv():
            if not life_msgs:
                return {"type": "lifespan.startup"}
            return {"type": "lifespan.shutdown"}

        life_task = asyncio.create_task(app({"type": "lifespan", "app": app}, life_recv, life_send))
        await asyncio.sleep(0.1)

        await app(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/health",
                "headers": [(b"accept-encoding", b"gzip"), (b"host", b"t")],
                "query_string": b"",
                "server": ("t", 80),
                "scheme": "http",
                "http_version": "1.1",
                "raw_path": b"/api/health",
                "app": app,
            },
            recv,
            send,
        )

        life_msgs.append({})  # signal shutdown intent
        await life_task
        return messages

    messages = asyncio.run(_probe())

    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert headers.get("content-encoding") != "gzip", (
        "Small response (/api/health, 15 bytes) is below the 500-byte "
        "threshold and MUST NOT be gzip-compressed — compressing small "
        "bodies wastes CPU and actually increases payload size."
    )


def test_gzip_threshold_is_500_bytes():
    """Under-threshold responses (health probes ~30 bytes) must NOT pay the
    compression cost. Asserting the exact minimum_size stops anyone from
    accidentally dropping it to 0 (which would compress everything)."""
    app = create_app()
    gzip = next(m for m in app.user_middleware if m.cls is GZipMiddleware)
    assert gzip.kwargs.get("minimum_size") == 500
