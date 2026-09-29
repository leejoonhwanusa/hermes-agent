"""WS-upgrade auth credentials for gated mode.

Browsers cannot set ``Authorization`` on a WebSocket upgrade, and gated mode has no token
injected into the SPA, so two credential shapes exist: (1) single-use browser tickets
(``mint_ticket`` / ``consume_ticket``) fetched via authenticated ``POST /api/auth/ws-ticket`` and
passed as ``?ticket=`` on the upgrade — 30 s TTL, a leak is uninteresting; (2) a process-lifetime
internal credential (``internal_ws_credential`` / ``consume_internal_credential``) for
*server-spawned* WS clients (the embedded-TUI PTY child on ``/api/ws`` + ``/api/pub``), which
reuse their attach URL on every reconnect, possibly >30 s after boot — minted once, never expires,
multi-use, never injected into any HTML/SPA (leaves the process only via the child's environment,
so browser XSS cannot read it; grants no more than a ticket). In-memory; ``time.time`` patchable.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

#: Long enough for ``getWsTicket()`` -> open WS, short enough that a leaked ticket is uninteresting.
TTL_SECONDS = 30
_WS_SESSION_SCOPE_KEY = "_hermes_ws_session_binding"

_lock = threading.Lock()
_tickets: Dict[str, Tuple[int, Dict[str, Any]]] = {}  # ticket -> (expires_at, info)
_internal_credential: Optional[str] = None  # lazily minted; guarded by ``_lock``

#: Identity recorded for internal-credential connections (audit logs distinguish them from tickets).
INTERNAL_USER_ID = "server-internal"
INTERNAL_PROVIDER = "server-internal"


class TicketInvalid(Exception):
    """Ticket missing, expired, or already consumed."""


def mint_ticket(*, user_id: str, provider: str, access_token: str = "",
                extra: Optional[Dict[str, Any]] = None, session_binding=None) -> str:
    """One-shot base64url ticket (32 random bytes) bound to this identity; ``consume_ticket``
    hands the ``info`` dict back to the WS handler."""
    ticket = secrets.token_urlsafe(32)
    info = {**(extra or {}), "user_id": user_id, "provider": provider, "minted_at": int(time.time())}
    info.pop("_session_binding", None)
    if session_binding is not None:
        _verify_binding(*session_binding)
        info["_session_binding"] = session_binding
    from .registry import get_provider
    bound_provider = get_provider(provider)
    if access_token and bound_provider is None:
        raise TicketInvalid("session provider unavailable")
    if bound_provider is not None and bound_provider.bind_ws_ticket_session:
        if not access_token:
            raise TicketInvalid("session required")
        info["_session_binding"] = ((bound_provider, access_token), user_id, provider)
    with _lock:
        _tickets[ticket] = (int(time.time()) + TTL_SECONDS, info)
        _gc_expired_locked()
    return ticket


def _verify_binding(binding, user_id: str, provider: str) -> None:
    from .registry import get_provider

    bound_provider, access_token = binding
    try:
        session = (bound_provider.verify_session(access_token=access_token)
                   if get_provider(provider) is bound_provider else None)
    except Exception:
        raise TicketInvalid("session unavailable") from None
    if session is None or session.user_id != user_id or session.provider != provider:
        raise TicketInvalid("session revoked or changed")


def consume_ticket(ticket: str, *, scope=None) -> Dict[str, Any]:
    """Validate and consume (single-use). Raises :class:`TicketInvalid` on missing/expired/used."""
    now = int(time.time())
    with _lock:
        entry = _tickets.pop(ticket, None)
        if entry is None:
            # Truncated so misuse never logs the secret in full.
            truncated = (ticket[:8] + "…") if ticket else "<empty>"
            raise TicketInvalid(f"unknown ticket: {truncated}")
        expires_at, info = entry
        if expires_at < now:
            raise TicketInvalid("expired")
    binding = info.pop("_session_binding", None)
    from .registry import get_provider
    current = get_provider(info["provider"])
    if binding is not None:
        _verify_binding(*binding)
        if scope is not None:
            scope[_WS_SESSION_SCOPE_KEY] = binding
            arm_watch = scope.get("_hermes_ws_session_watch")
            if arm_watch is not None:
                arm_watch()
    elif current is not None and current.bind_ws_ticket_session:
        raise TicketInvalid("session binding missing")
    return info


def wrap_asgi_with_ws_sessions(app):
    """Revalidate session-bound sockets, including idle sockets, until disconnect.

    The credential stays in the connection scope; public identity and frames never
    contain it. In-flight work already authorized before logout is not rolled back.
    """
    from starlette.websockets import WebSocketDisconnect

    async def wrapped(scope, receive, send):
        if scope["type"] != "websocket":
            return await app(scope, receive, send)

        closed = False
        revoked = False
        monitor = None
        accepted = False
        send_lock = asyncio.Lock()

        async def valid():
            binding = scope.get(_WS_SESSION_SCOPE_KEY)
            if binding is None:
                return True
            try:
                await asyncio.to_thread(_verify_binding, *binding)
                return True
            except TicketInvalid:
                return False

        async def revoke():
            nonlocal closed, revoked
            async with send_lock:
                if not closed:
                    closed = revoked = True
                    try:
                        with suppress(OSError, RuntimeError):
                            await send({"type": "websocket.close", "code": 4401})
                    finally:
                        if asyncio.current_task() is not handler:
                            handler.cancel()
            raise WebSocketDisconnect(code=4401)

        async def watch():
            while not closed:
                await asyncio.sleep(1)
                if not await valid():
                    with suppress(WebSocketDisconnect):
                        await revoke()
                    return

        def arm_watch():
            nonlocal monitor
            if accepted and monitor is None and _WS_SESSION_SCOPE_KEY in scope:
                monitor = asyncio.create_task(watch())

        scope["_hermes_ws_session_watch"] = arm_watch

        async def guarded_receive():
            nonlocal closed
            message = await receive()
            if message["type"] == "websocket.disconnect":
                closed = True
            elif message["type"] == "websocket.receive" and not await valid():
                await revoke()
            return message

        async def guarded_send(message):
            nonlocal closed, accepted
            kind = message["type"]
            if kind in ("websocket.accept", "websocket.send") and not await valid():
                await revoke()
            async with send_lock:
                if closed:
                    raise WebSocketDisconnect(code=4401 if revoked else 1000)
                await send(message)
                if kind == "websocket.close":
                    closed = True
                elif kind == "websocket.accept":
                    accepted = True
                    arm_watch()

        handler = asyncio.create_task(app(scope, guarded_receive, guarded_send))
        try:
            await handler
        except asyncio.CancelledError:
            if not revoked:
                raise
        finally:
            closed = True
            if monitor is not None:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            scope.pop(_WS_SESSION_SCOPE_KEY, None)
            scope.pop("_hermes_ws_session_watch", None)

    return wrapped


def _gc_expired_locked() -> None:
    """Drop expired tickets. Caller must hold ``_lock``."""
    now = int(time.time())
    for t in [t for t, (exp, _) in _tickets.items() if exp < now]:
        _tickets.pop(t, None)


def internal_ws_credential() -> str:
    """Process-lifetime internal WS credential, minted once. Never injected into the SPA or
    returned over REST — only passed to a spawned child via its environment."""
    global _internal_credential
    with _lock:
        if _internal_credential is None:
            _internal_credential = secrets.token_urlsafe(32)
        return _internal_credential


def consume_internal_credential(value: str) -> Dict[str, Any]:
    """Validate an internal credential (NOT single-use); returns the fixed server-internal
    ``{user_id, provider}`` info dict, mirroring ``consume_ticket``. Constant-time compare; any
    value is rejected until a credential has been minted."""
    with _lock:
        expected = _internal_credential
    if not value or expected is None:
        raise TicketInvalid("no internal credential")
    if not secrets.compare_digest(value.encode(), expected.encode()):
        raise TicketInvalid("internal credential mismatch")
    return {"user_id": INTERNAL_USER_ID, "provider": INTERNAL_PROVIDER}


def _reset_for_tests() -> None:
    """Test-only: drop all tickets and the internal credential."""
    global _internal_credential
    with _lock:
        _tickets.clear()
        _internal_credential = None
