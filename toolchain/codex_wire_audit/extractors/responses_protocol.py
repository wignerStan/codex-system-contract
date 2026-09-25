"""Canonical Responses request, Lite transformation, and event extraction.

Responses owns request-body and transport projection plus normalized response-event
semantics. Turn metadata, redirect/proxy routing, prompt composition, and provider
authority remain in their existing domains.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSnapshot
from .registry import ExtractorResult, register_extractor
from .responses_transport import (
    COOKIE_STORE, DEFAULT_CLIENT, HTTP_CLIENT, PROVIDER, RETRY, SESSION, STARTUP, WSS_CLIENT,
    build_transport_lifecycle, classify_http_response_diagnostics,
    classify_websocket_cookie_affinity, validate_transport_sources,
)

REQUEST_ID = "extractor.responses_request"
LITE_ID = "extractor.responses_lite"
EVENTS_ID = "extractor.response_events"
SCHEMA_VERSION = "1.0.0"
REQUEST_SCHEMA_VERSION = "2.0.0"
REQUEST_SCHEMA = "https://schemas.codex-system-contract.invalid/responses/responses-request-semantics-v2.schema.json"
LITE_SCHEMA = "https://schemas.codex-system-contract.invalid/responses/responses-lite-semantics-v1.schema.json"
EVENTS_SCHEMA = "https://schemas.codex-system-contract.invalid/responses/response-events-semantics-v1.schema.json"

COMMON = "source_spec.base.common"
CORE = "source_spec.base.core"
HTTP = "source_spec.base.http"
SSE = "source_spec.base.response_sse"
WS = "source_spec.base.ws"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _evidence(source: SourceFile, symbol: str) -> dict[str, Any]:
    return {
        "source_spec_id": source.spec_id,
        "path": source.selected_path,
        "sha256": source.content_sha256,
        "git_blob_sha": source.git_blob_sha,
        "symbol": symbol,
    }


def _missing(
    diagnostics: DiagnosticCollector,
    *,
    extractor_id: str,
    category: str,
    code: str,
    token: str,
    source: SourceFile | None,
    entity: str,
) -> None:
    diagnostics.emit(
        code=code,
        severity="error",
        category=category,
        message=f"Required Responses source token is missing: {token}",
        extractor_id=extractor_id,
        entity_id=entity,
        source_refs=[source.spec_id] if source else [],
        details={"path": source.selected_path, "token": token} if source else {"token": token},
        recoverable=False,
        strict_failure=True,
    )


def _require(
    diagnostics: DiagnosticCollector,
    *,
    extractor_id: str,
    category: str,
    source: SourceFile,
    tokens: tuple[tuple[str, str], ...],
    entity: str,
) -> bool:
    complete = True
    for code, token in tokens:
        if token not in source.text:
            complete = False
            _missing(
                diagnostics,
                extractor_id=extractor_id,
                category=category,
                code=code,
                token=token,
                source=source,
                entity=entity,
            )
    return complete


def _body(text: str, declaration: str) -> str | None:
    start = text.find(declaration)
    if start < 0:
        return None
    opening = text.find("{", start + len(declaration))
    if opening < 0:
        return None
    depth = 0
    for index in range(opening, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
    return None


def _struct_fields(text: str, name: str) -> list[str]:
    body = _body(text, f"pub struct {name}")
    if body is None:
        return []
    return re.findall(r"(?m)^\s{4}pub\s+(?:r#)?([A-Za-z_][A-Za-z0-9_]*)\s*:", body)


def _enum_variants(text: str, name: str) -> list[str]:
    body = _body(text, f"pub enum {name}")
    if body is None:
        return []
    variants = re.findall(r"(?m)^\s{4}([A-Z][A-Za-z0-9_]*)\b", body)
    return list(dict.fromkeys(variants))


def _event_kinds(text: str) -> list[str]:
    kinds = re.findall(r'"((?:response|codex)\.[A-Za-z0-9_.-]+)"\s*=>', text)
    return sorted(set(kinds))


def _endpoint_paths(text: str) -> dict[str, str]:
    pairs = re.findall(r'Self::([A-Za-z0-9_]+)\s*=>\s*"([^"]+)"', text)
    return {name: path for name, path in pairs}


def _string_const(text: str, name: str) -> str | None:
    match = re.search(rf'const\s+{re.escape(name)}\s*:\s*&str\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else None


def _result(
    extractor_id: str,
    source_ids: tuple[str, ...],
    body: dict[str, Any],
    complete: bool,
    schema_version: str = SCHEMA_VERSION,
) -> ExtractorResult:
    body["semantic_complete"] = complete
    body["semantic_digest"] = hashlib.sha256(_canonical(body)).hexdigest()
    return ExtractorResult(
        extractor_id=extractor_id,
        schema_version=schema_version,
        data=body,
        semantic_complete=complete,
        source_spec_ids=source_ids,
    )


class ResponsesRequestExtractor:
    extractor_id = REQUEST_ID
    source_spec_ids = (
        COMMON, CORE, HTTP, WS, STARTUP, SESSION, RETRY, PROVIDER,
        HTTP_CLIENT, DEFAULT_CLIENT, COOKIE_STORE, WSS_CLIENT,
    )

    def extract(self, snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> ExtractorResult:
        sources = {spec_id: snapshot.files.get(spec_id) for spec_id in self.source_spec_ids}
        missing = [spec_id for spec_id, source in sources.items() if source is None]
        if missing:
            for spec_id in missing:
                _missing(
                    diagnostics,
                    extractor_id=REQUEST_ID,
                    category="responses_request",
                    code="RESPONSES_REQUEST_SOURCE_UNAVAILABLE",
                    token=spec_id,
                    source=None,
                    entity=f"responses_request.source.{spec_id}",
                )
            return ExtractorResult(REQUEST_ID, REQUEST_SCHEMA_VERSION, {}, False, self.source_spec_ids)

        common = sources[COMMON]
        core = sources[CORE]
        http = sources[HTTP]
        ws = sources[WS]
        startup = sources[STARTUP]
        session = sources[SESSION]
        retry = sources[RETRY]
        provider = sources[PROVIDER]
        assert common and core and http and ws and startup and session and retry and provider
        complete = _require(
            diagnostics,
            extractor_id=REQUEST_ID,
            category="responses_request",
            source=common,
            entity="responses_request.shape",
            tokens=(
                ("RESPONSES_API_REQUEST_MISSING", "pub struct ResponsesApiRequest"),
                ("RESPONSES_WS_REQUEST_MISSING", "pub struct ResponseCreateWsRequest"),
                ("RESPONSES_WS_ENVELOPE_MISSING", "pub enum ResponsesWsRequest"),
                ("RESPONSES_CREATE_TAG_MISSING", '#[serde(rename = "response.create")]'),
            ),
        )
        complete &= _require(
            diagnostics,
            extractor_id=REQUEST_ID,
            category="responses_request",
            source=core,
            entity="responses_request.construction",
            tokens=(
                ("RESPONSES_BUILD_REQUEST_MISSING", "fn build_responses_request"),
                ("RESPONSES_PROPERTY_MATCH_MISSING", "fn responses_request_properties_match"),
                ("RESPONSES_PREVIOUS_RESPONSE_MISSING", "previous_response_id"),
                ("RESPONSES_WS_METADATA_MISSING", "build_ws_client_metadata"),
                ("RESPONSES_INCREMENTAL_ITEMS_MISSING", "fn get_incremental_items"),
                ("RESPONSES_PREWARM_GENERATE_FALSE_MISSING", "generate: if warmup { Some(false) } else { None }"),
                ("RESPONSES_PREWARM_COMPLETION_MISSING", "Ok(ResponseEvent::Completed { .. }) => break"),
                ("RESPONSES_WS_ENABLE_GATE_MISSING", "responses_websocket_enabled"),
                ("RESPONSES_WS_SESSION_FALLBACK_STATE_MISSING", "disable_websockets"),
                ("RESPONSES_WS_426_FALLBACK_MISSING", "StatusCode::UPGRADE_REQUIRED"),
                ("RESPONSES_WS_FALLBACK_OUTCOME_MISSING", "WebsocketStreamOutcome::FallbackToHttp"),
                ("RESPONSES_WS_SWITCH_FALLBACK_MISSING", "try_switch_fallback_transport"),
            ),
        )
        complete &= _require(
            diagnostics,
            extractor_id=REQUEST_ID,
            category="responses_request",
            source=http,
            entity="responses_request.http",
            tokens=(
                ("RESPONSES_ENDPOINT_MISSING", "pub enum ResponsesEndpoint"),
                ("RESPONSES_HTTP_POST_MISSING", "Method::POST"),
                ("RESPONSES_HTTP_STREAM_MISSING", "text/event-stream"),
                ("RESPONSES_HTTP_SESSION_HEADERS_MISSING", "build_session_headers"),
                ("RESPONSES_HTTP_SUBAGENT_MISSING", "x-openai-subagent"),
            ),
        )
        complete &= _require(
            diagnostics,
            extractor_id=REQUEST_ID,
            category="responses_request",
            source=ws,
            entity="responses_request.websocket",
            tokens=(
                ("RESPONSES_WS_CONNECTION_MISSING", "pub struct ResponsesWebsocketConnection"),
                ("RESPONSES_WS_SERIALIZE_MISSING", "serialize_websocket_request"),
                ("RESPONSES_WS_ENDPOINT_PATH_MISSING", "websocket_url_for_path(self.endpoint.path())"),
                ("RESPONSES_WS_STREAM_MISSING", "pub async fn stream_request"),
                ("RESPONSES_WS_TERMINAL_TAKE_MISSING", "let failed_stream = guard.take();"),
                ("RESPONSES_WS_DROP_ABORT_MISSING", "self.pump_task.abort();"),
                ("RESPONSES_WS_CONNECTION_LIMIT_MISSING", "websocket_connection_limit_reached"),
                ("RESPONSES_WS_PREVIOUS_NOT_FOUND_MISSING", "previous_response_not_found"),
            ),
        )

        transport_complete, prewarm_before_history_restore = validate_transport_sources(
            diagnostics=diagnostics,
            extractor_id=REQUEST_ID,
            startup=startup,
            session=session,
            retry=retry,
            provider=provider,
        )
        complete &= transport_complete
        diagnostics_complete, response_diagnostics = classify_http_response_diagnostics(
            diagnostics=diagnostics, extractor_id=REQUEST_ID,
            http_client=sources[HTTP_CLIENT], default_client=sources[DEFAULT_CLIENT],
            cookie_store=sources[COOKIE_STORE],
        )
        complete &= diagnostics_complete
        cookie_complete, websocket_cookie_affinity = classify_websocket_cookie_affinity(diagnostics=diagnostics, extractor_id=REQUEST_ID, websocket_client=sources[WSS_CLIENT], cookie_store=sources[COOKIE_STORE])
        complete &= cookie_complete

        http_fields = _struct_fields(common.text, "ResponsesApiRequest")
        ws_fields = _struct_fields(common.text, "ResponseCreateWsRequest")
        required_http = {"model", "input", "tool_choice", "parallel_tool_calls", "reasoning", "store", "stream", "include"}
        required_ws = required_http | {"previous_response_id", "generate"}
        if not required_http.issubset(http_fields) or not required_ws.issubset(ws_fields):
            complete = False
            _missing(
                diagnostics,
                extractor_id=REQUEST_ID,
                category="responses_request",
                code="RESPONSES_REQUEST_FIELD_INVENTORY_INCOMPLETE",
                token="required request fields",
                source=common,
                entity="responses_request.shape",
            )
        paths = _endpoint_paths(http.text)
        if paths.get("Responses") != "/responses":
            complete = False
            _missing(
                diagnostics,
                extractor_id=REQUEST_ID,
                category="responses_request",
                code="RESPONSES_ENDPOINT_PATH_DRIFT",
                token='Self::Responses => "/responses"',
                source=http,
                entity="responses_request.http",
            )

        body = {
            "$schema": REQUEST_SCHEMA,
            "schema_version": REQUEST_SCHEMA_VERSION,
            "ownership": {
                "owns": [
                    "Responses HTTP and WebSocket request body projection",
                    "Responses-compatible endpoint selection",
                    "HTTP/WS request transport semantics and continuation shape",
                    "WebSocket startup, socket-failure, retry, and session-scoped HTTP fallback lifecycle",
                ],
                "does_not_own": [
                    "turn metadata field derivation",
                    "prompt/context composition before request construction",
                    "proxy or redirect transport routing",
                    "provider authentication or permission authority",
                ],
            },
            "request_shape": {
                "http_fields": http_fields,
                "websocket_fields": ws_fields,
                "websocket_envelope_type": "response.create",
                "endpoint_paths": paths,
            },
            "construction": {
                "canonical_builder": "ModelClient::build_responses_request constructs one ResponsesApiRequest from the turn prompt/model controls",
                "http_and_ws_share_request": "ResponseCreateWsRequest derives from ResponsesApiRequest; transport-specific continuation fields are added for WebSocket use",
                "continuation": "WebSocket reuse may send previous_response_id only when request properties remain compatible and input advances as an incremental extension",
                "metadata_boundary": "request consumes already-derived client metadata; metadata key ownership remains extractor.turn_metadata",
            },
            "http_transport": {
                "method": "POST",
                "accept": "text/event-stream",
                "body": "encoded JSON ResponsesApiRequest",
                "headers": "session/thread, x-client-request-id, optional subagent, compatibility/attestation and turn-state headers are composed outside the body",
                "compression": ["none", "zstd"],
                "diagnostics": response_diagnostics,
            },
            "websocket_transport": {
                "message_type": "response.create",
                "body": "ResponseCreateWsRequest serialized as one WebSocket request frame",
                "connection": "provider WebSocket URL uses the same /responses path and maps http->ws, https->wss",
                "continuation_fields": ["previous_response_id", "generate", "client_metadata"],
                "cookie_affinity": websocket_cookie_affinity,
            },
            "transport_lifecycle": build_transport_lifecycle(
                prewarm_before_history_restore=prewarm_before_history_restore
            ),
            "evidence": {
                "common": _evidence(common, "ResponsesApiRequest/ResponseCreateWsRequest"),
                "core": _evidence(core, "build_responses_request/responses_request_properties_match"),
                "http": _evidence(http, "ResponsesClient::stream_request"),
                "websocket": _evidence(ws, "ResponsesWebsocketConnection::stream_request/WsStream::drop"),
                "startup": _evidence(startup, "schedule_startup_prewarm/prewarm_websocket"),
                "session": _evidence(session, "schedule_startup_prewarm/record_initial_history"),
                "retry": _evidence(retry, "handle_retryable_response_stream_error"),
                "provider": _evidence(provider, "websocket_url_for_path"),
                "http_client": _evidence(sources[HTTP_CLIENT], "HttpClient::log_response/RequestBuilder::send"),
                "default_client": _evidence(sources[DEFAULT_CLIENT], "create_client_for_route/default_http_client_builder"),
                "cookie_store": _evidence(sources[COOKIE_STORE], "ChatGptCloudflareCookieStore"),
                "websocket_client": _evidence(sources[WSS_CLIENT], "WebSocketConnector::connect_with_route"),
            },
        }
        return _result(REQUEST_ID, self.source_spec_ids, body, complete, REQUEST_SCHEMA_VERSION)


class ResponsesLiteExtractor:
    extractor_id = LITE_ID
    source_spec_ids = (CORE,)

    def extract(self, snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> ExtractorResult:
        core = snapshot.files.get(CORE)
        if core is None:
            _missing(
                diagnostics,
                extractor_id=LITE_ID,
                category="responses_lite",
                code="RESPONSES_LITE_SOURCE_UNAVAILABLE",
                token=CORE,
                source=None,
                entity="responses_lite.source",
            )
            return ExtractorResult(LITE_ID, SCHEMA_VERSION, {}, False, self.source_spec_ids)
        complete = _require(
            diagnostics,
            extractor_id=LITE_ID,
            category="responses_lite",
            source=core,
            entity="responses_lite.transform",
            tokens=(
                ("RESPONSES_LITE_GATE_MISSING", "model_info.use_responses_lite"),
                ("RESPONSES_LITE_INPUT_FORMAT_MISSING", "prompt.get_formatted_input_for_request(model_info.use_responses_lite)"),
                ("RESPONSES_LITE_TOOLS_MISSING", "create_tools_json_for_responses_lite"),
                ("RESPONSES_LITE_ADDITIONAL_TOOLS_MISSING", "ResponseItem::AdditionalTools"),
                ("RESPONSES_LITE_BASE_INSTRUCTIONS_MISSING", "ContextualUserFragment::into(BaseInstructionsFragment"),
                ("RESPONSES_LITE_PREFIX_INSERT_MISSING", "input.splice(0..0, prefix)"),
                ("RESPONSES_LITE_TOP_LEVEL_CLEAR_MISSING", "(String::new(), None)"),
                ("RESPONSES_LITE_PARALLEL_DISABLE_MISSING", "prompt.parallel_tool_calls && !model_info.use_responses_lite"),
                ("RESPONSES_LITE_REASONING_CONTEXT_MISSING", "ReasoningContext::AllTurns"),
                ("RESPONSES_LITE_HTTP_HEADER_MISSING", "X_OPENAI_INTERNAL_CODEX_RESPONSES_LITE_HEADER"),
                ("RESPONSES_LITE_WS_METADATA_MISSING", "WS_REQUEST_HEADER_RESPONSES_LITE_CLIENT_METADATA_KEY"),
            ),
        )
        body = {
            "$schema": LITE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "ownership": {
                "owns": ["Responses Lite request transformation and transport marker semantics"],
                "does_not_own": [
                    "base instruction composition before transformation",
                    "generic MCP/tool projection semantics",
                    "turn metadata derivation",
                    "network routing",
                ],
            },
            "activation": "model_info.use_responses_lite",
            "transformation": {
                "input_format": "prompt input is formatted in Responses Lite mode",
                "tools": "visible tools are materialized as a developer AdditionalTools response item prepended to input",
                "base_instructions": "base instructions are converted into a contextual developer-visible input item and prepended after AdditionalTools",
                "stable_prefix_ids": "tool and instruction prefix items receive deterministic UUIDv5-derived response item IDs scoped by thread identity and visible payload",
                "top_level_instructions": "empty string",
                "top_level_tools": None,
                "parallel_tool_calls": False,
                "reasoning_context": "all_turns",
            },
            "transport_markers": {
                "http_header": "x-openai-internal-codex-responses-lite: true",
                "websocket_client_metadata": "ws_request_header_x_openai_internal_codex_responses_lite=true",
            },
            "evidence": {"core": _evidence(core, "build_responses_request/add_responses_lite_header")},
        }
        return _result(LITE_ID, self.source_spec_ids, body, complete)


class ResponseEventsExtractor:
    extractor_id = EVENTS_ID
    source_spec_ids = (COMMON, SSE, WS)

    def extract(self, snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> ExtractorResult:
        common = snapshot.files.get(COMMON)
        sse = snapshot.files.get(SSE)
        ws = snapshot.files.get(WS)
        if common is None or sse is None or ws is None:
            for spec_id, source in ((COMMON, common), (SSE, sse), (WS, ws)):
                if source is None:
                    _missing(
                        diagnostics,
                        extractor_id=EVENTS_ID,
                        category="response_events",
                        code="RESPONSE_EVENTS_SOURCE_UNAVAILABLE",
                        token=spec_id,
                        source=None,
                        entity=f"response_events.source.{spec_id}",
                    )
            return ExtractorResult(EVENTS_ID, SCHEMA_VERSION, {}, False, self.source_spec_ids)
        complete = _require(
            diagnostics,
            extractor_id=EVENTS_ID,
            category="response_events",
            source=common,
            entity="response_events.normalized",
            tokens=(("RESPONSE_EVENT_ENUM_MISSING", "pub enum ResponseEvent"),),
        )
        complete &= _require(
            diagnostics,
            extractor_id=EVENTS_ID,
            category="response_events",
            source=sse,
            entity="response_events.sse",
            tokens=(
                ("RESPONSES_STREAM_EVENT_MISSING", "pub struct ResponsesStreamEvent"),
                ("RESPONSES_EVENT_DISPATCH_MISSING", "pub fn process_responses_event"),
                ("RESPONSES_EVENT_COMPLETED_MISSING", '"response.completed"'),
                ("RESPONSES_EVENT_FAILED_MISSING", '"response.failed"'),
                ("RESPONSES_EVENT_INCOMPLETE_MISSING", '"response.incomplete"'),
                ("RESPONSES_EVENT_CREATED_MISSING", '"response.created"'),
                ("RESPONSES_EVENT_METADATA_MISSING", '"response.metadata"'),
                ("RESPONSES_TURN_STATE_MISSING", "X_CODEX_TURN_STATE_HEADER"),
                ("RESPONSES_REQUEST_ID_MISSING", "REQUEST_ID_HEADER"),
            ),
        )
        complete &= _require(
            diagnostics,
            extractor_id=EVENTS_ID,
            category="response_events",
            source=ws,
            entity="response_events.websocket",
            tokens=(
                ("RESPONSES_WS_EVENT_REUSE_MISSING", "process_responses_event"),
                ("RESPONSES_WS_RATE_LIMIT_MISSING", "parse_rate_limit_event"),
                ("RESPONSES_WS_PREVIOUS_NOT_FOUND_MISSING", "previous_response_not_found"),
                ("RESPONSES_WS_CONNECTION_LIMIT_MISSING", "websocket_connection_limit_reached"),
            ),
        )
        variants = _enum_variants(common.text, "ResponseEvent")
        event_kinds = _event_kinds(sse.text)
        required_kinds = {
            "response.created",
            "response.completed",
            "response.failed",
            "response.incomplete",
            "response.output_item.done",
            "response.output_text.delta",
            "response.custom_tool_call_input.delta",
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done",
            "response.reasoning_text.delta",
        }
        if not required_kinds.issubset(event_kinds):
            complete = False
            _missing(
                diagnostics,
                extractor_id=EVENTS_ID,
                category="response_events",
                code="RESPONSE_EVENT_DISPATCH_INVENTORY_INCOMPLETE",
                token="required response.* dispatch arms",
                source=sse,
                entity="response_events.sse",
            )
        headers = {
            "reasoning_included": _string_const(sse.text, "X_REASONING_INCLUDED_HEADER"),
            "turn_state": _string_const(sse.text, "X_CODEX_TURN_STATE_HEADER"),
            "server_model": _string_const(sse.text, "OPENAI_MODEL_HEADER"),
            "request_id": _string_const(sse.text, "REQUEST_ID_HEADER"),
        }
        body = {
            "$schema": EVENTS_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "ownership": {
                "owns": [
                    "Responses SSE/WebSocket event normalization",
                    "terminal response error classification",
                    "Responses stream header-to-event projection",
                ],
                "does_not_own": [
                    "generic HTTP redirect routing",
                    "account/backend control-plane events",
                    "turn metadata construction",
                    "application thread/turn RPC lifecycle",
                ],
            },
            "normalized_event_variants": variants,
            "wire_event_kinds": event_kinds,
            "stream_headers": headers,
            "dispatch": {
                "sse": "ResponsesStreamEvent.kind dispatch normalizes wire response.* events into ResponseEvent or typed ApiError outcomes",
                "websocket": "WebSocket frames deserialize the same ResponsesStreamEvent and reuse process_responses_event for normalized semantics",
                "terminal_success": "response.completed emits response id, usage/usage metadata, and optional end_turn",
                "terminal_errors": ["response.failed", "response.incomplete"],
                "continuation_recovery": "WebSocket previous_response_not_found triggers full-request recovery rather than redefining event semantics",
            },
            "header_projection": {
                "server_model": "OpenAI-Model is emitted as a normalized ServerModel event",
                "rate_limits": "response headers or websocket rate-limit metadata become normalized RateLimits events",
                "turn_state": "x-codex-turn-state is captured for same-turn sticky continuation",
                "reasoning_included": "x-reasoning-included signals that server accounting already includes past reasoning",
                "upstream_request_id": "x-request-id is retained on the ResponseStream for diagnostics",
            },
            "evidence": {
                "common": _evidence(common, "ResponseEvent"),
                "sse": _evidence(sse, "ResponsesStreamEvent/process_responses_event"),
                "websocket": _evidence(ws, "run_websocket_response_stream/process_responses_event"),
            },
        }
        return _result(EVENTS_ID, self.source_spec_ids, body, complete)


@register_extractor(REQUEST_ID)
def _request_factory() -> ResponsesRequestExtractor:
    return ResponsesRequestExtractor()


@register_extractor(LITE_ID)
def _lite_factory() -> ResponsesLiteExtractor:
    return ResponsesLiteExtractor()


@register_extractor(EVENTS_ID)
def _events_factory() -> ResponseEventsExtractor:
    return ResponseEventsExtractor()
