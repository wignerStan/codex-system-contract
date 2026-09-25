"""Deterministic Responses WebSocket/HTTP transport lifecycle classification."""
from __future__ import annotations

from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile

STARTUP = "source_spec.extra.responses_transport_startup"
SESSION = "source_spec.extra.responses_transport_session"
RETRY = "source_spec.extra.responses_transport_retry"
PROVIDER = "source_spec.extra.responses_transport_provider"
HTTP_CLIENT = "source_spec.extra.responses_http_client"
DEFAULT_CLIENT = "source_spec.base.default_client"
COOKIE_STORE = "source_spec.extra.responses_chatgpt_cookie_store"
WSS_CLIENT = "source_spec.extra.responses_websocket_client"


def _require(
    diagnostics: DiagnosticCollector,
    *,
    extractor_id: str,
    source: SourceFile,
    tokens: tuple[tuple[str, str], ...],
    entity: str,
) -> bool:
    complete = True
    for code, token in tokens:
        if token in source.text:
            continue
        complete = False
        diagnostics.emit(
            code=code,
            severity="error",
            category="responses_request",
            message=f"Required Responses transport source token is missing: {token}",
            extractor_id=extractor_id,
            entity_id=entity,
            source_refs=[source.spec_id],
            details={"path": source.selected_path, "token": token},
            recoverable=False,
            strict_failure=True,
        )
    return complete


def validate_transport_sources(
    *,
    diagnostics: DiagnosticCollector,
    extractor_id: str,
    startup: SourceFile,
    session: SourceFile,
    retry: SourceFile,
    provider: SourceFile,
) -> tuple[bool, bool]:
    complete = _require(
        diagnostics,
        extractor_id=extractor_id,
        source=startup,
        entity="responses_request.transport.startup",
        tokens=(
            ("RESPONSES_STARTUP_PREWARM_SCHEDULE_MISSING", "schedule_startup_prewarm"),
            ("RESPONSES_STARTUP_PREWARM_KIND_MISSING", "CodexResponsesRequestKind::Prewarm"),
            ("RESPONSES_STARTUP_PREWARM_CALL_MISSING", ".prewarm_websocket("),
            ("RESPONSES_STARTUP_EMPTY_INPUT_MISSING", "let startup_prompt = build_prompt(\n        Vec::new(),"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=session,
        entity="responses_request.transport.session",
        tokens=(
            ("RESPONSES_SESSION_PREWARM_MISSING", ".schedule_startup_prewarm("),
            ("RESPONSES_SESSION_HISTORY_RESTORE_MISSING", "record_initial_history(initial_history)"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=retry,
        entity="responses_request.transport.retry",
        tokens=(
            ("RESPONSES_RETRY_SWITCH_MISSING", "try_switch_fallback_transport"),
            ("RESPONSES_RETRY_HTTPS_WARNING_MISSING", "Falling back from WebSockets to HTTPS transport."),
            ("RESPONSES_UNBOUNDED_CONNECTION_RETRIES_MISSING", "Feature::UnboundedConnectionRetries"),
        ),
    )
    if not any(
        token in retry.text
        for token in ("handle_response_stream_error", "handle_retryable_response_stream_error")
    ):
        complete = False
        diagnostics.emit(
            code="RESPONSES_RETRY_HANDLER_MISSING",
            severity="error",
            category="responses_request",
            message="Required Responses retry handler is missing.",
            extractor_id=extractor_id,
            entity_id="responses_request.transport.retry",
            source_refs=[retry.spec_id],
            details={
                "path": retry.selected_path,
                "accepted_symbols": [
                    "handle_response_stream_error",
                    "handle_retryable_response_stream_error",
                ],
            },
            recoverable=False,
            strict_failure=True,
        )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=provider,
        entity="responses_request.transport.provider",
        tokens=(
            ("RESPONSES_PROVIDER_WS_URL_MISSING", "websocket_url_for_path"),
            ("RESPONSES_PROVIDER_HTTP_WS_MAPPING_MISSING", '"http" => "ws"'),
            ("RESPONSES_PROVIDER_HTTPS_WSS_MAPPING_MISSING", '"https" => "wss"'),
        ),
    )
    schedule_index = session.text.find(".schedule_startup_prewarm(")
    history_index = session.text.find("record_initial_history(initial_history)")
    ordered = 0 <= schedule_index < history_index
    if not ordered:
        complete = False
        diagnostics.emit(
            code="RESPONSES_PREWARM_HISTORY_ORDER_DRIFT",
            severity="error",
            category="responses_request",
            message="Responses startup prewarm no longer precedes initial history restoration.",
            extractor_id=extractor_id,
            entity_id="responses_request.transport.startup",
            source_refs=[session.spec_id],
            details={"path": session.selected_path},
            recoverable=False,
            strict_failure=True,
        )
    return complete, ordered


def classify_http_response_diagnostics(
    *,
    diagnostics: DiagnosticCollector,
    extractor_id: str,
    http_client: SourceFile,
    default_client: SourceFile,
    cookie_store: SourceFile,
) -> tuple[bool, dict[str, Any]]:
    complete = _require(
        diagnostics,
        extractor_id=extractor_id,
        source=http_client,
        entity="responses_request.http.diagnostics",
        tokens=(
            ("RESPONSES_HTTP_REQUEST_LOGGING_GATE_MISSING", "RequestLogging::Enabled"),
            ("RESPONSES_HTTP_RESPONSE_LOGGER_MISSING", "pub(crate) fn log_response"),
            ("RESPONSES_HTTP_RAW_HEADER_DIAGNOSTIC_MISSING", "headers = ?response.headers()"),
            ("RESPONSES_HTTP_DEBUG_DIAGNOSTIC_MISSING", "tracing::debug!"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=default_client,
        entity="responses_request.http.client_defaults",
        tokens=(
            ("RESPONSES_DEFAULT_ROUTE_CLIENT_MISSING", "pub fn create_client_for_route("),
            ("RESPONSES_DEFAULT_HTTP_BUILDER_MISSING", "fn default_http_client_builder()"),
            ("RESPONSES_CHATGPT_COOKIE_STORE_WIRING_MISSING", ".with_chatgpt_cloudflare_cookie_store()"),
            ("RESPONSES_NO_LOGGING_ESCAPE_HATCH_MISSING", ".without_request_logging()"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=cookie_store,
        entity="responses_request.http.cookie_store",
        tokens=(
            ("RESPONSES_COOKIE_STORE_IMPL_MISSING", "impl CookieStore for ChatGptCloudflareCookieStore"),
            ("RESPONSES_COOKIE_ALLOWLIST_MISSING", "is_allowed_cloudflare_set_cookie_header"),
            ("RESPONSES_COOKIE_HTTPS_SCOPE_MISSING", "fn is_chatgpt_cookie_url"),
        ),
    )

    raw_header_log_sites = http_client.text.count("headers = ?response.headers()")
    builder_tail = default_client.text.split("fn default_http_client_builder()", 1)
    builder_body = builder_tail[1].split("\n}", 1)[0] if len(builder_tail) == 2 else ""
    default_request_logging_enabled = ".without_request_logging()" not in builder_body
    configured_cookie_opt_out = (
        "create_client_with_chatgpt_cookies" in default_client.text
        and ".without_request_logging()" in default_client.text
    )
    cookie_persistence_allowlisted = (
        "filter(|header| is_allowed_cloudflare_set_cookie_header(header))" in cookie_store.text
    )
    exposed = raw_header_log_sites > 0 and default_request_logging_enabled
    if exposed:
        diagnostics.emit(
            code="RESPONSES_HTTP_SET_COOKIE_DIAGNOSTIC_EXPOSURE",
            severity="warning",
            category="responses_request",
            message=(
                "Logging-enabled HTTP clients render the complete response HeaderMap; HTTPS "
                "Set-Cookie values can therefore reach debug diagnostics independently of the "
                "ChatGPT cookie-store allowlist."
            ),
            extractor_id=extractor_id,
            entity_id="responses_request.http.diagnostics",
            source_refs=[http_client.spec_id, default_client.spec_id, cookie_store.spec_id],
            details={
                "http_client_path": http_client.selected_path,
                "default_client_path": default_client.selected_path,
                "cookie_store_path": cookie_store.selected_path,
                "raw_response_header_log_sites": raw_header_log_sites,
            },
            recoverable=True,
            strict_failure=False,
        )

    return complete, {
        "level": "debug",
        "request_logging_default": "enabled" if default_request_logging_enabled else "disabled",
        "raw_response_header_logging": raw_header_log_sites > 0,
        "raw_response_header_log_sites": raw_header_log_sites,
        "https_set_cookie_diagnostic_exposure": exposed,
        "set_cookie_redacted_before_diagnostics": False if exposed else None,
        "chatgpt_cookie_persistence_allowlisted": cookie_persistence_allowlisted,
        "cookie_store_filtering_sanitizes_diagnostics": False,
        "configured_cookie_client_disables_request_logging": configured_cookie_opt_out,
        "boundary": (
            "cookie persistence and cookie diagnostics are separate: rejecting a Set-Cookie from "
            "the shared jar does not remove it from the response HeaderMap rendered by diagnostics"
        ),
    }


def classify_websocket_cookie_affinity(
    *,
    diagnostics: DiagnosticCollector,
    extractor_id: str,
    websocket_client: SourceFile,
    cookie_store: SourceFile,
) -> tuple[bool, dict[str, Any]]:
    """Classify revision-sensitive ChatGPT routing-cookie behavior around the WS handshake."""

    # Cookie and Set-Cookie are HTTP Upgrade handshake headers; they are not WebSocket data frames.
    websocket_tokens = (
        "!request.headers().contains_key(COOKIE)",
        "chatgpt_cookie_header(&uri)",
        "store_chatgpt_response_cookies(&uri, response.headers())",
        "Ok((_, response))",
        "Err(WebSocketError::Http(response))",
    )
    cookie_store_tokens = (
        "pub fn chatgpt_cookie_header",
        "pub fn store_chatgpt_response_cookies",
        'if url.scheme() == "wss"',
    )
    websocket_hits = tuple(token in websocket_client.text for token in websocket_tokens)
    cookie_store_hits = tuple(token in cookie_store.text for token in cookie_store_tokens)
    bridge_observed = all(websocket_hits) and all(cookie_store_hits)
    partial_bridge = (
        (any(websocket_hits) and not all(websocket_hits))
        or (any(cookie_store_hits) and not all(cookie_store_hits))
    )
    complete = not partial_bridge
    if partial_bridge:
        diagnostics.emit(
            code="RESPONSES_WS_COOKIE_BRIDGE_PARTIAL_DRIFT",
            severity="error",
            category="responses_request",
            message=(
                "WebSocket ChatGPT cookie integration is partially present; the opening-handshake "
                "request/response bridge no longer matches a known legacy or shared-jar revision."
            ),
            extractor_id=extractor_id,
            entity_id="responses_request.websocket.cookie_affinity",
            source_refs=[websocket_client.spec_id, cookie_store.spec_id],
            details={
                "websocket_client_path": websocket_client.selected_path,
                "cookie_store_path": cookie_store.selected_path,
                "websocket_markers": dict(zip(websocket_tokens, websocket_hits, strict=True)),
                "cookie_store_markers": dict(zip(cookie_store_tokens, cookie_store_hits, strict=True)),
            },
            recoverable=False,
            strict_failure=True,
        )

    jar_delegation = "self.jar.set_cookies" in cookie_store.text
    return complete, {
        "revision_mode": (
            "shared_http_wss_opening_handshake_cookie_jar"
            if bridge_observed
            else "legacy_http_cookie_jar_without_websocket_bridge"
        ),
        "request_cookie_injected_before_upgrade": bridge_observed,
        "explicit_request_cookie_takes_precedence": (
            bridge_observed and "!request.headers().contains_key(COOKIE)" in websocket_client.text
        ),
        "successful_upgrade_set_cookie_captured": (
            bridge_observed and "Ok((_, response))" in websocket_client.text
        ),
        "rejected_upgrade_set_cookie_captured": (
            bridge_observed and "Err(WebSocketError::Http(response))" in websocket_client.text
        ),
        "wss_cookie_scope_mapped_to_https": (
            bridge_observed and 'if url.scheme() == "wss"' in cookie_store.text
        ),
        "http_and_wss_share_process_cookie_store": bridge_observed,
        "process_global_infrastructure_cookie_store": (
            "SHARED_CHATGPT_CLOUDFLARE_COOKIE_STORE" in cookie_store.text
        ),
        "oailb_routing_cookie_allowlisted": '"__oailb"' in cookie_store.text,
        "cookie_value_source": (
            "upstream Set-Cookie; Codex stores and replays allowlisted infrastructure values"
            if bridge_observed
            else "HTTP cookie jar may learn upstream Set-Cookie values; no WebSocket bridge is observed"
        ),
        "first_wss_without_preexisting_cookie": (
            "Codex inserts no Cookie header; injection occurs only when chatgpt_cookie_header returns Some"
            if bridge_observed
            else "no shared-cookie injection path is present in the WebSocket connector"
        ),
        "post_upgrade_scope": (
            "cookie integration is confined to the opening HTTP Upgrade handshake; WebSocket data frames do not carry cookie headers"
            if bridge_observed
            else "no WebSocket cookie bridge is observed"
        ),
        "expiry": {
            "codex_fixed_ttl_seconds": None,
            "jar_delegation_observed": jar_delegation,
            "owner": (
                "upstream Set-Cookie attributes interpreted by reqwest::cookie::Jar"
                if jar_delegation
                else "not established"
            ),
        },
    }


def build_transport_lifecycle(*, prewarm_before_history_restore: bool) -> dict[str, Any]:
    return {
        "selection": {
            "websocket_preferred_when": "provider supports_websockets and session fallback is not active",
            "http_fallback": "Responses HTTP transport; user warning names HTTPS, while the concrete scheme follows provider.base_url",
            "path": "/responses",
            "scheme_pairing": {
                "http_base": {"websocket": "ws", "fallback_http": "http"},
                "https_base": {"websocket": "wss", "fallback_http": "https"},
                "ws_base": {"websocket": "ws"},
                "wss_base": {"websocket": "wss"},
            },
        },
        "startup_prewarm": {
            "scheduled_during_session_initialization": True,
            "occurs_before_initial_history_restore": prewarm_before_history_restore,
            "logical_conversation_input": "empty",
            "uses_normal_base_instructions_and_tool_snapshot": True,
            "wire_type": "response.create",
            "generate": False,
            "waits_for": "response.completed",
            "continuation_baseline": "completed response id",
        },
        "continuation": {
            "eligibility": "non-input request properties match and current input extends the previous request plus server-returned response items",
            "eligible_wire": "send previous_response_id and only the incremental input suffix while serializing the other response.create fields",
            "ineligible_wire": "omit previous_response_id and send the full current input",
        },
        "state_scope": {
            "turn_state": "x-codex-turn-state is scoped to one ModelClientSession/turn",
            "websocket_connection": "a healthy connection may be cached by the session-scoped ModelClient for reuse",
            "fallback_state": "session-scoped disable_websockets state",
            "fallback_sticky_across_turns": True,
        },
        "fallback": {
            "upgrade_required_426": "switch immediately from WebSocket to Responses HTTP without ordinary WebSocket stream retries",
            "retryable_failure_before_budget": "discard failed socket, back off, and retry using a new/reopened WebSocket",
            "retry_budget_exhausted": "activate session HTTP fallback, reset the retry counter, and replay the Responses request over HTTP",
            "warning_prefix": "Falling back from WebSockets to HTTPS transport.",
            "unbounded_connection_retry_exception": "when UnboundedConnectionRetries applies to an eligible sampling connection failure, connection retries occur before the normal retry-budget fallback branch",
        },
        "terminal_socket": {
            "on_stream_error": "remove the current WsStream from connection state and drop it",
            "graceful_close_handshake": False,
            "drop_behavior": "WsStream::drop aborts the pump task",
            "special_retryable_codes": [
                "websocket_connection_limit_reached",
                "previous_response_not_found",
            ],
        },
    }
