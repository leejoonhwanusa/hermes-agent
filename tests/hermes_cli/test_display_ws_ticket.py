"""The display bridge admits only a display ticket minted for THIS profile's socket: a gateway ticket,
an expired ticket, or one for another provider is refused before any socket is dialled."""

from __future__ import annotations

from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.web_routers import display


class _Ws:
    def __init__(self, **params):
        self.query_params = params


def test_display_observe_inherits_basic_logout_for_pending_and_idle_sockets(monkeypatch, tmp_path):
    import asyncio
    import threading
    from types import SimpleNamespace

    import pytest
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from hermes_cli import web_server, web_server_chat
    from hermes_cli.dashboard_auth import clear_providers, register_provider
    from hermes_constants import get_hermes_home
    from plugins.dashboard_auth.basic import BasicAuthProvider, hash_password
    from tools.bot_desktop import runtime
    from tui_gateway import server
    from tui_gateway.ws import WSTransport

    clear_providers()
    ws_tickets._reset_for_tests()
    register_provider(BasicAuthProvider(username="admin", password_hash=hash_password("hunter2"),
                                        secret=b"test-display-revocation-secret!!!"))
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "fly-app.fly.dev", raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_port", 443, raising=False)
    monkeypatch.setattr(runtime, "rfb_socket_path", lambda: tmp_path / "rfb.sock")

    cleaned = threading.Event()
    expected_home = get_hermes_home()

    async def open_rfb(profile_home):
        assert profile_home == expected_home
        class Reader:
            started = False
            cancelled = False

            async def read(self, size):
                if not self.started:
                    self.started = True
                    return b"RFB 003.008\n"
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled = True

        reader = Reader()

        class Writer:
            def close(self):
                assert reader.cancelled

            async def wait_closed(self):
                cleaned.set()

        return reader, Writer(), None

    monkeypatch.setattr(display, "_open_rfb", open_rfb)
    config, _ = web_server._build_uvicorn_server("fly-app.fly.dev", 443)
    client = TestClient(config.app, base_url="https://fly-app.fly.dev")
    loop = asyncio.new_event_loop()
    try:
        assert client.post("/auth/password-login", json={
            "provider": "basic", "username": "admin", "password": "hunter2",
        }).status_code == 200
        parent = client.post("/api/auth/ws-ticket").json()["ticket"]
        ws = SimpleNamespace(scope={}, query_params={"ticket": parent}, headers={})
        assert web_server_chat._ws_auth_ok(ws)
        transport = WSTransport(ws, loop, auth_identity=ws._hermes_auth_identity)
        tickets = []
        for rid in (1, 2):
            result = server.dispatch({"jsonrpc": "2.0", "id": rid,
                                      "method": "display.observe", "params": {}}, transport)
            tickets.append(result["result"]["ticket"])
        with client.websocket_connect(
                f"wss://fly-app.fly.dev/api/display/ws?display_ticket={tickets[0]}") as viewer:
            assert viewer.receive_bytes() == b"RFB 003.008\n"
            assert client.post("/auth/logout", follow_redirects=False).status_code == 302
            with pytest.raises(ws_tickets.TicketInvalid):
                ws_tickets.consume_ticket(tickets[1])
            with pytest.raises(WebSocketDisconnect) as disconnected:
                viewer.receive_text()
            assert disconnected.value.code == 4401
            assert cleaned.wait(5), "RFB pumps and socket must be reaped after logout"
    finally:
        client.close()
        loop.close()
        clear_providers()
        ws_tickets._reset_for_tests()


def test_display_ticket_must_be_a_bot_desktop_ticket_pinned_to_a_profile_home(monkeypatch):
    ws_tickets._reset_for_tests()
    gateway_ticket = ws_tickets.mint_ticket(user_id="u", provider="google")
    assert display._consume_display_ticket(_Ws(display_ticket=gateway_ticket)) is None

    unpinned = ws_tickets.mint_ticket(user_id="display:v", provider="bot-desktop")
    assert display._consume_display_ticket(_Ws(display_ticket=unpinned)) is None

    good = ws_tickets.mint_ticket(user_id="display:v", provider="bot-desktop",
                                  extra={"hermes_home": "/srv/hermes/bot-a", "viewer_id": "v"})
    info = display._consume_display_ticket(_Ws(display_ticket=good))
    assert info and info["hermes_home"] == "/srv/hermes/bot-a" and info["viewer_id"] == "v"
    assert display._consume_display_ticket(_Ws(display_ticket=good)) is None, "single use"


def test_a_bad_ticket_is_refused_with_a_close_frame_the_renderer_can_read(monkeypatch):
    """Closing BEFORE accept surfaces to a browser as an HTTP 403 handshake failure with no close
    code; the renderer's close listener never learns WHY (4401 = ticket, 4001 = desktop gone) and
    cannot pick between re-observing and showing "not running". The handshake must complete and
    the reason travel in the close frame."""
    import pytest
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from hermes_cli import web_server

    ws_tickets._reset_for_tests()
    prev = {k: getattr(web_server.app.state, k, None) for k in ("auth_required", "bound_host")}
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = None
    client = TestClient(web_server.app)
    try:
        used = ws_tickets.mint_ticket(user_id="display:v", provider="bot-desktop",
                                      extra={"hermes_home": "/srv/hermes/bot-a", "viewer_id": "v"})
        ws_tickets.consume_ticket(used)
        with client.websocket_connect(f"/api/display/ws?display_ticket={used}") as conn:  # handshake completes
            with pytest.raises(WebSocketDisconnect) as exc:
                conn.receive_bytes()
        assert exc.value.code == 4401
    finally:
        client.close()
        ws_tickets._reset_for_tests()
        for k, v in prev.items():
            if v is None:
                if hasattr(web_server.app.state, k):
                    delattr(web_server.app.state, k)
            else:
                setattr(web_server.app.state, k, v)
