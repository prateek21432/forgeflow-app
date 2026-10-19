"""Serve one selected preview on a separate origin, never on the dashboard origin."""

from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.background import BackgroundTask

from ..config import settings
from .demo_service import demos

router = APIRouter()
COOKIE = "forgeflow_preview"
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def signer():
    return URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt="preview-access-v1")


def is_preview_request(request: Request) -> bool:
    return (
        bool(settings.preview_origin)
        and request.headers.get("host") == urlsplit(settings.preview_origin).netloc
    )


def preview_url(project_id: int) -> str | None:
    demo = demos.running.get(project_id)
    if demo is None:
        return None
    if not settings.preview_origin:
        return demo.url
    if demo.access_token:
        try:
            signer().loads(demo.access_token, max_age=43200)
        except BadSignature:
            demo.access_token = ""
    if not demo.access_token:
        demo.access_token = signer().dumps({"project": project_id, "snapshot": demo.snapshot.name})
    return settings.preview_origin.rstrip("/") + "/__open?token=" + demo.access_token


def resolve_preview(token: str):
    try:
        data = signer().loads(token, max_age=43200)
        demo = demos.running.get(data["project"])
        if demo is not None and demo.snapshot.name == data["snapshot"]:
            return demo
    except (BadSignature, KeyError, TypeError):
        pass
    raise HTTPException(
        401, "Preview access expired or stopped. Open it again from your project dashboard."
    )


@router.api_route(
    "/_preview/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)
async def proxy_preview(path: str, request: Request):
    if not is_preview_request(request):
        raise HTTPException(404, "Not found.")
    if path == "__open":
        token = request.query_params.get("token", "")
        resolve_preview(token)
        response = RedirectResponse("/", 303)
        response.set_cookie(
            COOKIE, token, secure=True, httponly=True, samesite="lax", max_age=43200
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
    demo = resolve_preview(request.cookies.get(COOKIE, ""))
    # The destination is always a live, owned process. No user-supplied URL is accepted.
    url = demo.url.rstrip("/") + "/" + path
    if request.url.query:
        url += "?" + request.url.query
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_HEADERS | {"host", "cookie", "authorization"}
    }
    headers["host"] = urlsplit(settings.preview_origin).netloc
    headers["x-forwarded-proto"] = "https"
    cookies = {
        key: value
        for key, value in request.cookies.items()
        if key not in {COOKIE, "forgeflow_session"}
    }
    client = httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False)
    try:
        upstream = await client.send(
            client.build_request(
                request.method, url, headers=headers, cookies=cookies, content=request.stream()
            ),
            stream=True,
        )
    except httpx.HTTPError as error:
        await client.aclose()
        raise HTTPException(
            502, "The preview process is unavailable. Restart it from the dashboard."
        ) from error

    async def close():
        await upstream.aclose()
        await client.aclose()

    response = StreamingResponse(
        upstream.aiter_raw(), status_code=upstream.status_code, background=BackgroundTask(close)
    )
    for key, value in upstream.headers.multi_items():
        if key.lower() in HOP_HEADERS | {
            "set-cookie",
            "content-security-policy",
            "x-frame-options",
        }:
            continue
        if key.lower() == "location":
            value = value.replace(demo.url, settings.preview_origin)
            value = value.replace(
                "http://" + urlsplit(settings.preview_origin).netloc, settings.preview_origin
            )
        response.headers[key] = value
    for value in upstream.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        try:
            parsed.load(value)
            for name, cookie in parsed.items():
                if name in {COOKIE, "forgeflow_session"}:
                    continue
                cookie["domain"] = ""
                cookie["secure"] = True
                response.raw_headers.append(
                    (b"set-cookie", cookie.OutputString().encode("latin-1"))
                )
        except (ValueError, UnicodeError):
            continue
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "frame-ancestors " + (
        settings.public_origin or "'none'"
    )
    return response
