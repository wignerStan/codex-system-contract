"""Declarative source registry shared by all source providers and extractors."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from . import SOURCE_REGISTRY_VERSION
from .models import SourceGroup, SourceSpec


class SourceRegistryError(ValueError):
    pass


class SourceRegistry:
    def __init__(self, specs: Iterable[SourceSpec]) -> None:
        self._specs: dict[str, SourceSpec] = {}
        legacy_slots: set[tuple[SourceGroup, str]] = set()
        for spec in specs:
            if spec.id in self._specs:
                raise SourceRegistryError(f"duplicate source spec id: {spec.id}")
            slot = (spec.group, spec.legacy_key)
            if slot in legacy_slots:
                raise SourceRegistryError(
                    f"duplicate legacy source slot: {spec.group.value}:{spec.legacy_key}"
                )
            if not spec.path_candidates:
                raise SourceRegistryError(f"source spec has no path candidates: {spec.id}")
            for path in spec.path_candidates:
                if path.startswith("/") or ".." in Path(path).parts:
                    raise SourceRegistryError(f"unsafe source path candidate: {path}")
            self._specs[spec.id] = spec
            legacy_slots.add(slot)

    @property
    def specs(self) -> tuple[SourceSpec, ...]:
        return tuple(sorted(self._specs.values(), key=lambda item: item.id))

    def get(self, spec_id: str) -> SourceSpec:
        try:
            return self._specs[spec_id]
        except KeyError as error:
            raise SourceRegistryError(f"unknown source spec: {spec_id}") from error

    def for_group(self, group: SourceGroup) -> tuple[SourceSpec, ...]:
        return tuple(spec for spec in self.specs if spec.group == group)

    def with_overlay(self, overlay: Mapping[str, Any]) -> "SourceRegistry":
        if set(overlay) - {"schema_version", "sources"}:
            raise SourceRegistryError(
                "source registry overlay contains unsupported top-level keys: "
                + ", ".join(sorted(set(overlay) - {"schema_version", "sources"}))
            )
        rows = overlay.get("sources")
        if not isinstance(rows, Mapping):
            raise SourceRegistryError("source registry overlay must contain an object named 'sources'")
        updated = dict(self._specs)
        for spec_id, patch in rows.items():
            if not isinstance(patch, Mapping):
                raise SourceRegistryError(f"source overlay must be an object: {spec_id}")
            creating = spec_id not in updated
            allowed = {
                "legacy_key",
                "group",
                "path_candidates",
                "required",
                "roles",
                "expected_symbols",
                "extractor_ids",
                "exclusion_reason",
            }
            unknown = set(patch) - allowed
            if unknown:
                raise SourceRegistryError(
                    f"unsupported overlay fields for {spec_id}: {', '.join(sorted(unknown))}"
                )
            changes: dict[str, Any] = {}
            for key in allowed:
                if key not in patch:
                    continue
                value = patch[key]
                if key in {"path_candidates", "roles", "expected_symbols", "extractor_ids"}:
                    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                        raise SourceRegistryError(f"{spec_id}.{key} must be an array of strings")
                    changes[key] = tuple(value)
                elif key == "required":
                    if not isinstance(value, bool):
                        raise SourceRegistryError(f"{spec_id}.required must be boolean")
                    changes[key] = value
                elif key == "group":
                    try:
                        changes[key] = SourceGroup(str(value))
                    except (TypeError, ValueError) as error:
                        raise SourceRegistryError(f"{spec_id}.group must be base, surface, or extra") from error
                else:
                    if value is not None and not isinstance(value, str):
                        raise SourceRegistryError(f"{spec_id}.{key} must be string or null")
                    changes[key] = value
            if creating:
                required_fields = {"legacy_key", "group", "path_candidates", "required"}
                missing = sorted(required_fields - set(changes))
                if missing:
                    raise SourceRegistryError(
                        f"new source spec {spec_id} is missing required fields: {', '.join(missing)}"
                    )
                updated[spec_id] = SourceSpec(
                    id=spec_id,
                    legacy_key=changes["legacy_key"],
                    group=changes["group"],
                    path_candidates=changes["path_candidates"],
                    required=changes["required"],
                    roles=changes.get("roles", ()),
                    expected_symbols=changes.get("expected_symbols", ()),
                    extractor_ids=changes.get("extractor_ids", ()),
                    exclusion_reason=changes.get("exclusion_reason"),
                )
            else:
                if "legacy_key" in changes or "group" in changes:
                    raise SourceRegistryError(
                        f"existing source spec identity cannot be changed by overlay: {spec_id}"
                    )
                updated[spec_id] = replace(updated[spec_id], **changes)
        return SourceRegistry(updated.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_REGISTRY_VERSION,
            "sources": {spec.id: spec.to_dict() for spec in self.specs},
            "counts": {
                "total": len(self._specs),
                "required": sum(spec.required for spec in self._specs.values()),
                "optional": sum(not spec.required for spec in self._specs.values()),
                "base": len(self.for_group(SourceGroup.BASE)),
                "surface": len(self.for_group(SourceGroup.SURFACE)),
                "extra": len(self.for_group(SourceGroup.EXTRA)),
            },
        }

    @classmethod
    def load_overlay(cls, path: str | Path) -> Mapping[str, Any]:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SourceRegistryError(f"cannot read source registry overlay {path}: {error}") from error
        if not isinstance(value, Mapping):
            raise SourceRegistryError("source registry overlay root must be an object")
        return value


def _spec_id(group: SourceGroup, key: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return f"source_spec.{group.value}.{safe}"


def _append_optional_specs(
    specs: list[SourceSpec],
    rows: Iterable[tuple[str, str, str, tuple[str, ...]]],
    *,
    role: str,
    extractor_id: str,
) -> None:
    existing_ids = {spec.id for spec in specs}
    for spec_id, legacy_key, path, symbols in rows:
        if spec_id in existing_ids:
            continue
        specs.append(SourceSpec(
            id=spec_id,
            legacy_key=legacy_key,
            group=SourceGroup.EXTRA,
            path_candidates=(path,),
            required=False,
            roles=(role,),
            expected_symbols=tuple(symbols),
            extractor_ids=(extractor_id,),
        ))
        existing_ids.add(spec_id)


def _merge_optional_specs(
    specs: list[SourceSpec],
    rows: Iterable[tuple[str, str, str, tuple[str, ...]]],
    *,
    role: str,
    extractor_id: str,
) -> None:
    positions = {spec.id: index for index, spec in enumerate(specs)}
    for spec_id, legacy_key, path, symbols in rows:
        index = positions.get(spec_id)
        if index is None:
            specs.append(SourceSpec(
                id=spec_id,
                legacy_key=legacy_key,
                group=SourceGroup.EXTRA,
                path_candidates=(path,),
                required=False,
                roles=(role,),
                expected_symbols=tuple(symbols),
                extractor_ids=(extractor_id,),
            ))
            positions[spec_id] = len(specs) - 1
            continue
        current = specs[index]
        specs[index] = SourceSpec(
            id=current.id,
            legacy_key=current.legacy_key,
            group=current.group,
            path_candidates=current.path_candidates,
            required=current.required,
            roles=tuple(dict.fromkeys((*current.roles, role))),
            expected_symbols=tuple(dict.fromkeys((*current.expected_symbols, *symbols))),
            extractor_ids=tuple(sorted(set((*current.extractor_ids, extractor_id)))),
            exclusion_reason=current.exclusion_reason,
        )


_ROUTING_TRANSPORT_SPECS = (
    ("source_spec.extra.routing_proxy_spec", "routing_proxy_spec", "codex-rs/core/src/config/network_proxy_spec.rs", ("NetworkProxySpec", "start_proxy", "environment_policy")),
    ("source_spec.extra.routing_proxy_config", "routing_proxy_config", "codex-rs/network-proxy/src/config.rs", ("NetworkProxyConfig", "NetworkMode", "resolve_runtime")),
    ("source_spec.extra.routing_requirements", "routing_requirements", "codex-rs/config/src/config_requirements.rs", ("NetworkRequirementsToml", "managed_allowed_domains_only", "header_injections")),
    ("source_spec.extra.routing_outbound_proxy", "routing_outbound_proxy", "codex-rs/http-client/src/outbound_proxy.rs", ("OutboundProxyPolicy", "HttpClientFactory", "resolve_proxy_route")),
)

_REDIRECT_HEADER_SPECS = (
    ("source_spec.extra.route_aware_redirect", "route_aware_redirect", "codex-rs/http-client/src/route_aware_redirect.rs", ("redirect_request", "remove_sensitive_headers", "insert_referer")),
    ("source_spec.extra.mcp_http_redirect", "mcp_http_redirect", "codex-rs/rmcp-client/src/http_client_redirect.rs", ("SameOriginRedirectHttpClient", "MAX_REDIRECTS", "HttpRedirectPolicy::Stop")),
    ("source_spec.extra.mcp_http_headers", "mcp_http_headers", "codex-rs/rmcp-client/src/http_headers.rs", ("HttpHeadersProvider", "HttpHeadersClient", "with_http_headers_helper")),
)

def _merge_routing_domain_specs(specs: list[SourceSpec]) -> None:
    for rows, role, extractor_id in (
        (_ROUTING_TRANSPORT_SPECS, "routing_transport", "extractor.routing_transport"),
        (_REDIRECT_HEADER_SPECS, "redirect_headers", "extractor.redirect_headers"),
    ):
        _merge_optional_specs(specs, rows, role=role, extractor_id=extractor_id)


_APP_SERVER_RPC_SPECS = (
    ("source_spec.extra.app_server_common", "app_server_common", "codex-rs/app-server-protocol/src/protocol/common.rs", ("client_request_definitions", "server_notification_definitions", "ClientRequestSerializationScope")),
    ("source_spec.extra.app_server_thread", "app_server_thread", "codex-rs/app-server-protocol/src/protocol/v2/thread.rs", ("ThreadStartParams", "ThreadResumeParams", "ThreadStatus", "ThreadTurnsListParams", "ThreadItemsListParams")),
    ("source_spec.extra.app_server_thread_data", "app_server_thread_data", "codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs", ("ThreadHistoryMode", "Thread", "Turn", "TurnItemsView")),
    ("source_spec.extra.app_server_turn", "app_server_turn", "codex-rs/app-server-protocol/src/protocol/v2/turn.rs", ("TurnStatus", "TurnStartParams", "TurnSteerParams", "TurnInterruptParams")),
    ("source_spec.extra.app_server_item", "app_server_item", "codex-rs/app-server-protocol/src/protocol/v2/item.rs", ("ThreadItem",)),
    ("source_spec.extra.app_server_rpc", "app_server_rpc", "codex-rs/app-server-protocol/src/rpc.rs", ("JSONRPCMessage", "JSONRPCRequest", "JSONRPCNotification", "JSONRPCResponse", "JSONRPCError")),
    ("source_spec.extra.app_server_thread_processor", "app_server_thread_processor", "codex-rs/app-server/src/request_processors/thread_processor.rs", ("thread_fork_inner", "thread_revert_response", "reload_paginated_thread", "prepare_fork", "revert_thread", "ServerNotification::ThreadStarted", "ServerNotification::ThreadReverted")),
    ("source_spec.extra.app_server_thread_manager", "app_server_thread_manager", "codex-rs/core/src/thread_manager.rs", ("fork_prepared_thread", "fork_thread_with_initial_history", "a fresh id", "forked_from_thread_id")),
    ("source_spec.extra.app_server_tui_session", "app_server_tui_session", "codex-rs/tui/src/app_server_session.rs", ("fork_thread_at", "ThreadForkParams", "ClientRequest::ThreadFork", "ThreadHistorySupport::Paginated")),
    ("source_spec.extra.local_storage_tui_backtrack", "local_storage_tui_backtrack", "codex-rs/tui/src/app_backtrack.rs", ("backtrack_fork_before_turn_id", "ForkSessionForPromptEdit", "source-preserving branch", "cannot fork in the middle of a turn")),
    ("source_spec.extra.local_storage_paginated_fork", "local_storage_paginated_fork", "codex-rs/thread-store/src/local/paginated_fork.rs", ("HistoryPosition", "ForkBoundary::Latest", "ForkBoundary::ThroughTurn", "ForkBoundary::BeforeTurn", "history_base_at_boundary")),
    ("source_spec.extra.local_storage_revert_thread", "local_storage_revert_thread", "codex-rs/thread-store/src/local/revert_thread.rs", ("let rollout_id = ThreadId::new();", "with_rollout_id", "replace_rollout_path_if_current")),
)

def _merge_app_server_domain_specs(specs: list[SourceSpec]) -> None:
    _merge_optional_specs(
        specs,
        _APP_SERVER_RPC_SPECS,
        role="app_server_rpc",
        extractor_id="extractor.app_server_rpc",
    )


_MCP_SPECS = (
    ("source_spec.extra.mcp_catalog", "mcp_catalog", "codex-rs/codex-mcp/src/catalog.rs", ("McpServerSource", "McpCatalogBuilder", "ResolvedMcpCatalog")),
    ("source_spec.extra.mcp_runtime", "mcp_runtime", "codex-rs/codex-mcp/src/mcp/mod.rs", ("McpConfig", "effective_mcp_servers", "ToolPluginProvenance")),
    ("source_spec.extra.mcp_tools", "mcp_tools", "codex-rs/codex-mcp/src/tools.rs", ("ToolInfo", "ToolFilter", "normalize_tools_for_model_with_prefix")),
    ("source_spec.extra.mcp_tool_exposure", "mcp_tool_exposure", "codex-rs/core/src/mcp_tool_exposure.rs", ("McpHandlerCache", "append_mcp_tools", "tool_is_model_visible")),
    ("source_spec.extra.mcp_tool_plan", "mcp_tool_plan", "codex-rs/core/src/tools/spec_plan.rs", ("build_tool_router", "apply_mcp_tool_exposure_policy", "omit_tools_from")),
    ("source_spec.extra.mcp_handler", "mcp_handler", "codex-rs/core/src/tools/handlers/mcp.rs", ("McpHandler", "prepare_mcp_call", "handle_mcp_tool_call")),
)

_RESPONSES_REQUEST_SPECS = (
    ("source_spec.base.common", "common", "codex-rs/codex-api/src/common.rs", ("ResponsesApiRequest", "ResponseCreateWsRequest", "ResponsesWsRequest")),
    ("source_spec.base.core", "core", "codex-rs/core/src/client.rs", ("build_responses_request", "responses_request_properties_match", "build_ws_client_metadata")),
    ("source_spec.base.http", "http", "codex-rs/codex-api/src/endpoint/responses.rs", ("ResponsesEndpoint", "ResponsesClient", "stream_request")),
    ("source_spec.base.ws", "ws", "codex-rs/codex-api/src/endpoint/responses_websocket.rs", ("ResponsesWebsocketConnection", "ResponsesWebsocketClient", "stream_request")),
    ("source_spec.base.default_client", "default_client", "codex-rs/login/src/auth/default_client.rs", ("create_client_for_route", "default_http_client_builder", "without_request_logging")),
    ("source_spec.extra.responses_http_client", "responses_http_client", "codex-rs/http-client/src/client.rs", ("RequestLogging", "log_response", "response.headers")),
    ("source_spec.extra.responses_chatgpt_cookie_store", "responses_chatgpt_cookie_store", "codex-rs/http-client/src/chatgpt_cloudflare_cookies.rs", ("is_chatgpt_cookie_url", "is_allowed_cloudflare_set_cookie_header", "CookieStore")),
    ("source_spec.extra.responses_websocket_client", "responses_websocket_client", "codex-rs/websocket-client/src/lib.rs", ("WebSocketConnector", "connect_with_route")),
    ("source_spec.extra.responses_transport_startup", "responses_transport_startup", "codex-rs/core/src/session_startup_prewarm.rs", ("schedule_startup_prewarm", "CodexResponsesRequestKind::Prewarm", "prewarm_websocket")),
    ("source_spec.extra.responses_transport_session", "responses_transport_session", "codex-rs/core/src/session/session.rs", ("schedule_startup_prewarm", "record_initial_history")),
    ("source_spec.extra.responses_transport_retry", "responses_transport_retry", "codex-rs/core/src/responses_retry.rs", ("try_switch_fallback_transport", "Falling back from WebSockets to HTTPS transport", "retry_state")),
    ("source_spec.extra.responses_transport_provider", "responses_transport_provider", "codex-rs/codex-api/src/provider.rs", ("websocket_url_for_path", "http", "https", "ws", "wss")),
)

_RESPONSES_LITE_SPECS = (
    ("source_spec.base.core", "core", "codex-rs/core/src/client.rs", ("use_responses_lite", "ResponseItem::AdditionalTools", "add_responses_lite_header")),
)

_RESPONSE_EVENT_SPECS = (
    ("source_spec.base.common", "common", "codex-rs/codex-api/src/common.rs", ("ResponseEvent",)),
    ("source_spec.base.response_sse", "response_sse", "codex-rs/codex-api/src/sse/responses.rs", ("ResponsesStreamEvent", "process_responses_event", "spawn_response_stream")),
    ("source_spec.base.ws", "ws", "codex-rs/codex-api/src/endpoint/responses_websocket.rs", ("process_responses_event", "run_websocket_response_stream")),
)

_RESPONSES_SERVER_RESPONSE_SPECS = (
    ("source_spec.extra.models_manager", "models_manager", "codex-rs/models-manager/src/manager.rs", ("refresh_if_new_etag", "fetch_and_update_models", "RefreshStrategy::Online", "ModelsCacheEntry")),
    ("source_spec.extra.responses_server_model_protocol", "responses_server_model_protocol", "codex-rs/protocol/src/openai_models.rs", ("pub struct ModelInfo", "comp_hash")),
    ("source_spec.extra.responses_server_app_model_protocol", "responses_server_app_model_protocol", "codex-rs/app-server-protocol/src/protocol/v2/model.rs", ("pub struct Model",)),
    ("source_spec.extra.responses_server_prewarm_turn_context", "responses_server_prewarm_turn_context", "codex-rs/core/src/session/turn_context.rs", ("NewTurnContextOptions", "new_startup_prewarm_turn_from_configuration")),
)

_RUNTIME_BEHAVIOR_SPECS = (
    ("source_spec.base.core", "core", "codex-rs/core/src/client.rs", ("turn_state: Arc::new(OnceLock::new())", "client_metadata.insert(X_CODEX_TURN_STATE_HEADER")),
    ("source_spec.extra.response_api_bridge", "response_api_bridge", "codex-rs/codex-api/src/api_bridge.rs", ("map_api_error", "server_is_overloaded", "slow_down", "StatusCode::TOO_MANY_REQUESTS")),
    ("source_spec.extra.response_protocol_error", "response_protocol_error", "codex-rs/protocol/src/error.rs", ("pub enum CodexErrorDetails", "pub fn retry_delay", "ServerOverloaded", "RateLimitExceeded")),
    ("source_spec.extra.app_server_error_notification", "app_server_error_notification", "codex-rs/app-server-protocol/src/protocol/v2/notification.rs", ("pub struct ErrorNotification", "will_retry", "automatically retry")),
    ("source_spec.extra.app_server_bespoke_events", "app_server_bespoke_events", "codex-rs/app-server/src/bespoke_event_handling.rs", ("EventMsg::StreamError", "will_retry: true", "will_retry: false")),
    ("source_spec.extra.tui_side", "tui_side", "codex-rs/tui/src/app/side.rs", ("fork_config.ephemeral = true", "fork_side_thread")),
    ("source_spec.extra.tui_slash_command", "tui_slash_command", "codex-rs/tui/src/slash_command.rs", ("SlashCommand::Side", "SlashCommand::Btw", "ephemeral fork")),
)

def _merge_protocol_domain_specs(specs: list[SourceSpec]) -> None:
    for rows, role, extractor_id in (
        (_MCP_SPECS, "mcp_projection", "extractor.mcp_projection"),
        (_RESPONSES_REQUEST_SPECS, "responses_request", "extractor.responses_request"),
        (_RESPONSES_LITE_SPECS, "responses_lite", "extractor.responses_lite"),
        (_RESPONSE_EVENT_SPECS, "response_events", "extractor.response_events"),
        (_RESPONSES_SERVER_RESPONSE_SPECS, "responses_server_response", "extractor.responses_server_response"),
        (_RUNTIME_BEHAVIOR_SPECS, "runtime_behavior", "extractor.runtime_behavior"),
    ):
        _merge_optional_specs(specs, rows, role=role, extractor_id=extractor_id)


def from_legacy_maps(
    base_files: Mapping[str, str],
    surface_files: Mapping[str, str],
    extra_files: Mapping[str, str],
) -> SourceRegistry:
    specs: list[SourceSpec] = []
    for group, mapping, required in (
        (SourceGroup.BASE, base_files, True),
        (SourceGroup.SURFACE, surface_files, False),
        (SourceGroup.EXTRA, extra_files, False),
    ):
        for key, path in mapping.items():
            roles = [f"legacy_{group.value}"]
            expected_symbols: tuple[str, ...] = ()
            extractors: tuple[str, ...] = ()
            candidates = (path,)
            if group == SourceGroup.BASE and key == "metadata":
                roles.extend(("responses_metadata", "turn_metadata_semantics"))
                expected_symbols = (
                    "CodexResponsesMetadata",
                    "CodexTurnMetadataPayload",
                    "turn_metadata_payload",
                )
                extractors = ("extractor.turn_metadata",)
                candidates = (
                    path,
                    "codex-rs/core/src/responses/metadata.rs",
                )
            elif group == SourceGroup.SURFACE and key == "endpoint_mod":
                roles.append("endpoint_discovery")
                expected_symbols = ("pub mod",)
            elif group == SourceGroup.EXTRA and key in {
                "feature_configs", "feature_registry", "config_toml", "model_protocol", "provider_runtime"
            }:
                roles.append("context_management_support")
                extractors = tuple(sorted(set((*extractors, "extractor.context_management"))))
            elif group == SourceGroup.BASE and key == "provider_info":
                roles.append("context_management_routing")
                extractors = tuple(sorted(set((*extractors, "extractor.context_management"))))
            if (group, key) in {
                (SourceGroup.BASE, "provider_info"),
                (SourceGroup.EXTRA, "config_toml"),
                (SourceGroup.EXTRA, "core_config"),
                (SourceGroup.EXTRA, "feature_configs"),
                (SourceGroup.EXTRA, "feature_registry"),
                (SourceGroup.EXTRA, "provider_runtime"),
                (SourceGroup.EXTRA, "turn_metadata"),
            }:
                roles.append("config_surface_support")
                extractors = tuple(sorted(set((*extractors, "extractor.config_effects"))))
            if group == SourceGroup.EXTRA and key == "config_toml":
                roles.append("local_storage_configuration")
                extractors = tuple(sorted(set((*extractors, "extractor.local_storage"))))
            specs.append(
                SourceSpec(
                    id=_spec_id(group, key),
                    legacy_key=key,
                    group=group,
                    path_candidates=candidates,
                    required=required,
                    roles=tuple(roles),
                    expected_symbols=expected_symbols,
                    extractor_ids=extractors,
                )
            )
    context_specs = (
        ("source_spec.extra.context_management_activation", "context_management_activation", "codex-rs/core/src/session/token_budget.rs", ("apply_experimental_context", "supports_experimental_context")),
        ("source_spec.extra.history_notes_tools", "history_notes_tools", "codex-rs/ext/history-notes/src/tools.rs", ("HistoryNotesAction", "alpha/history/v2/list_windows", "alpha/notes/v2/write_file")),
        ("source_spec.extra.history_notes_backend", "history_notes_backend", "codex-rs/ext/history-notes/src/backend.rs", ("HistoryNotesBackend", "x-openai-tool-output-truncation-policy")),
        ("source_spec.extra.history_notes_extension", "history_notes_extension", "codex-rs/ext/history-notes/src/extension.rs", ("alpha/notes/v2/thread_hint", "use_history_notes_extension")),
        ("source_spec.extra.new_context_window", "new_context_window", "codex-rs/core/src/tools/handlers/new_context_window.rs", ("NEW_CONTEXT_WINDOW_MESSAGE", "request_new_context_window")),
        ("source_spec.extra.new_context_window_spec", "new_context_window_spec", "codex-rs/core/src/tools/handlers/new_context_window_spec.rs", ("NEW_CONTEXT_WINDOW_TOOL_NAME", "Start a new context window")),
        ("source_spec.extra.compact_token_budget", "compact_token_budget", "codex-rs/core/src/compact_token_budget.rs", ("skips model/server summarization", "start_new_context_window")),
        ("source_spec.extra.context_management_tests", "context_management_tests", "codex-rs/core/tests/suite/token_budget.rs", ("experimental_context_requires", "supports_experimental_context")),
        ("source_spec.extra.history_notes_tests", "history_notes_tests", "codex-rs/ext/history-notes/tests/history_notes_extension.rs", ("Recent notes (up to 5, most-recent first)", "thread_hint")),
    )
    local_storage_specs = (
        ("source_spec.extra.local_storage_home_dir", "local_storage_home_dir", "codex-rs/utils/home-dir/src/lib.rs", ("find_codex_home", "CODEX_HOME", ".codex")),
        ("source_spec.extra.local_storage_thread_types", "local_storage_thread_types", "codex-rs/thread-store/src/types.rs", ("CreateThreadParams", "session_id", "thread_id", "RevertThreadParams")),
        ("source_spec.extra.local_storage_rollout_lib", "local_storage_rollout_lib", "codex-rs/rollout/src/lib.rs", ("SESSIONS_SUBDIR", "ARCHIVED_SESSIONS_SUBDIR")),
        ("source_spec.extra.local_storage_rollout_filename", "local_storage_rollout_filename", "codex-rs/rollout/src/rollout_file_name.rs", ("RolloutFileName", "rollout_id", "split_once('_')")),
        ("source_spec.extra.local_storage_rollout_recorder", "local_storage_rollout_recorder", "codex-rs/rollout/src/recorder.rs", ("RolloutRecorderParams", "with_session_id", "with_rollout_id")),
        ("source_spec.extra.local_storage_rollout_compression", "local_storage_rollout_compression", "codex-rs/rollout/src/compression.rs", ("COMPRESSED_SUFFIX", "open_rollout_line_reader", "plain_rollout_path")),
        ("source_spec.extra.local_storage_session_index", "local_storage_session_index", "codex-rs/rollout/src/session_index.rs", ("SESSION_INDEX_FILE", "SessionIndexEntry", "append_thread_name")),
        ("source_spec.extra.local_storage_revert_thread", "local_storage_revert_thread", "codex-rs/thread-store/src/local/revert_thread.rs", ("replace_rollout_path_if_current", "with_rollout_id", "history_base")),
        ("source_spec.extra.local_storage_paginated_fork", "local_storage_paginated_fork", "codex-rs/thread-store/src/local/paginated_fork.rs", ("HistoryPosition", "end_ordinal_exclusive", "end_byte_offset")),
        ("source_spec.extra.local_storage_archive_thread", "local_storage_archive_thread", "codex-rs/thread-store/src/local/archive_thread.rs", ("ARCHIVED_SESSIONS_SUBDIR", "mark_archived", "rename")),
        ("source_spec.extra.local_storage_unarchive_thread", "local_storage_unarchive_thread", "codex-rs/thread-store/src/local/unarchive_thread.rs", ("rollout_date_parts", "mark_unarchived", "SESSIONS_SUBDIR")),
        ("source_spec.extra.local_storage_delete_thread", "local_storage_delete_thread", "codex-rs/thread-store/src/local/delete_thread.rs", ("RolloutReferenceIndex", "remove_thread_name_entries", "delete_rollout_file")),
        ("source_spec.extra.local_storage_writer_lock", "local_storage_writer_lock", "codex-rs/thread-store/src/local/writer_lock.rs", ("WRITER_LOCK_DIR", "COORDINATION_LOCK_FILE", "try_lock")),
        ("source_spec.extra.local_storage_state_sqlite", "local_storage_state_sqlite", "codex-rs/state/src/sqlite.rs", ("RUNTIME_DBS", "STATE_DB_FILENAME", "THREAD_HISTORY_DB_FILENAME")),
        ("source_spec.extra.local_storage_state_threads", "local_storage_state_threads", "codex-rs/state/src/runtime/threads.rs", ("replace_rollout_path_if_current", "UPDATE threads SET rollout_path")),
        ("source_spec.extra.local_storage_threads_migration", "local_storage_threads_migration", "codex-rs/state/migrations/0001_threads.sql", ("CREATE TABLE threads", "id TEXT PRIMARY KEY", "rollout_path TEXT NOT NULL")),
        ("source_spec.extra.local_storage_history_materialization", "local_storage_history_materialization", "codex-rs/thread-store/src/local/thread_history_materialization.rs", ("materialize_to_sqlite", "next_byte_offset", "next_ordinal")),
        ("source_spec.extra.local_storage_local_store", "local_storage_local_store", "codex-rs/thread-store/src/local/mod.rs", ("LocalThreadStore", "live_recorders", "ensure_live_recorder_absent")),
        ("source_spec.extra.local_storage_live_writer", "local_storage_live_writer", "codex-rs/thread-store/src/local/live_writer.rs", ("SQLite is a rebuildable view.", "durable_write", "materialize_to_sqlite")),
        ("source_spec.extra.local_storage_rollout_resolver", "local_storage_rollout_resolver", "codex-rs/thread-store/src/local/thread_rollout_resolver.rs", ("resolve_current", "LookupScope", "rollout_path")),
        ("source_spec.extra.local_storage_model_context", "local_storage_model_context", "codex-rs/thread-store/src/local/model_context.rs", ("load_latest_model_context", "ReverseJsonlScanner", "resolve_rollout_lineage")),
        ("source_spec.extra.local_storage_history_read", "local_storage_history_read", "codex-rs/thread-store/src/local/thread_history/read.rs", ("list_turns", "list_items", "thread_history_db")),
        ("source_spec.extra.local_storage_tui_backtrack", "local_storage_tui_backtrack", "codex-rs/tui/src/app_backtrack.rs", ("backtrack_fork_before_turn_id", "cannot be branched independently", "cannot fork in the middle of a turn")),
        ("source_spec.extra.local_storage_shell_snapshot", "local_storage_shell_snapshot", "codex-rs/core/src/shell_snapshot.rs", ("SNAPSHOT_DIR", "SNAPSHOT_RETENTION", "session_id")),
        ("source_spec.extra.local_storage_visualization", "local_storage_visualization", "codex-rs/tui/src/inline_visualization.rs", ("visualizations", "visualization-viewers", "thread_id")),
    )
    prompt_specs = (
        ("source_spec.extra.prompt_world_state", "prompt_world_state", "codex-rs/core/src/session/world_state.rs", ("build_world_state_for_step", "AgentsMdState::new", "PluginsInstructionsState::new")),
        ("source_spec.extra.prompt_session_context", "prompt_session_context", "codex-rs/core/src/session/mod.rs", ("get_prompt_base_instructions", "build_initial_context_with_world_state", "world_state.render_full()")),
        ("source_spec.extra.prompt_turn", "prompt_turn", "codex-rs/core/src/session/turn.rs", ("build_prompt", "model_visible_specs", "build_skills_and_plugins")),
        ("source_spec.extra.prompt_debug", "prompt_debug", "codex-rs/core/src/prompt_debug.rs", ("build_prompt_input_from_session", "capture_step_context", "for_prompt")),
    )
    policy_specs = (
        ("source_spec.extra.policy_protocol", "policy_protocol", "codex-rs/protocol/src/models.rs", ("pub enum PermissionProfile", "pub enum SandboxEnforcement", "pub struct ActivePermissionProfile")),
        ("source_spec.extra.policy_permissions", "policy_permissions", "codex-rs/core/src/config/permissions.rs", ("default_builtin_permission_profile_name", "compile_permission_profile_selection", "resolve_permission_profile")),
        ("source_spec.extra.policy_requirements", "policy_requirements", "codex-rs/config/src/config_requirements.rs", ("pub struct ConfigRequirements", "pub approval_policy", "pub permission_profile")),
        ("source_spec.extra.policy_config_resolution", "policy_config_resolution", "codex-rs/core/src/config/mod.rs", ("resolve_effective_permission_selection", "resolve_default_permissions", "allowed_permission_profiles")),
        ("source_spec.extra.policy_profile_state", "policy_profile_state", "codex-rs/core/src/config/resolved_permission_profile.rs", ("PermissionProfileState", "active_permission_profile", "profile_workspace_roots")),
        ("source_spec.extra.policy_turn_context", "policy_turn_context", "codex-rs/core/src/session/turn_context.rs", ("fn approval_policy", "fn permission_profile", "fn allow_prefix_rules")),
        ("source_spec.extra.policy_exec_policy", "policy_exec_policy", "codex-rs/core/src/exec_policy.rs", ("ExecApprovalRequest", "prompt_is_rejected_by_policy", "Decision::Forbidden")),
    )
    plugin_specs = (
        ("source_spec.extra.plugin_model", "plugin_model", "codex-rs/plugin/src/lib.rs", ("PluginCapabilitySummary", "AppDeclaration", "PluginTelemetryMetadata")),
        ("source_spec.extra.plugin_load_outcome", "plugin_load_outcome", "codex-rs/plugin/src/load_outcome.rs", ("LoadedPlugin", "PluginLoadOutcome", "effective_plugin_skill_roots")),
        ("source_spec.extra.plugin_manifest", "plugin_manifest", "codex-rs/core-plugins/src/manifest.rs", ("PluginManifestFormat", "RawPluginManifest", "load_plugin_manifest_with_format")),
        ("source_spec.extra.plugin_loader", "plugin_loader", "codex-rs/core-plugins/src/loader.rs", ("load_plugins_from_layer_stack", "PluginLoadScope", "load_plugin_skill_inventory")),
        ("source_spec.extra.plugin_manager", "plugin_manager", "codex-rs/core-plugins/src/manager.rs", ("PluginsConfigInput", "plugins_for_config", "plugin_skill_snapshots_for_config")),
        ("source_spec.extra.plugin_mentions", "plugin_mentions", "codex-rs/core/src/plugins/mentions.rs", ("collect_explicit_plugin_mentions", "collect_explicit_plugin_ids", "PLUGIN_TEXT_MENTION_SIGIL")),
        ("source_spec.extra.plugin_injection", "plugin_injection", "codex-rs/core/src/plugins/injection.rs", ("build_plugin_injections", "PluginInstructions::new", "CODEX_APPS_MCP_SERVER_NAME")),
        ("source_spec.extra.plugin_render", "plugin_render", "codex-rs/core/src/plugins/render.rs", ("render_explicit_plugin_instructions", "MAX_EXPLICIT_PLUGIN_INSTRUCTIONS_BYTES")),
    )
    environment_specs = (
        ("source_spec.extra.environment_shell_policy", "environment_shell_policy", "codex-rs/protocol/src/config_types.rs", ("ShellEnvironmentPolicyInherit", "ShellEnvironmentPolicy", "use_profile")),
        ("source_spec.extra.environment_shell_builder", "environment_shell_builder", "codex-rs/protocol/src/shell_environment.rs", ("NON_INHERITABLE_ENV_VARS", "create_env_from_vars", "CODEX_THREAD_ID_ENV_VAR")),
        ("source_spec.extra.environment_selection", "environment_selection", "codex-rs/core/src/environment_selection.rs", ("EnvironmentConfigOrigin", "ThreadEnvironments", "default_thread_environment_selections")),
        ("source_spec.extra.environment_shell_snapshot", "environment_shell_snapshot", "codex-rs/core/src/shell_snapshot.rs", ("ShellSnapshot", "SNAPSHOT_TIMEOUT", "cleanup_stale_snapshots")),
        ("source_spec.extra.environment_manager", "environment_manager", "codex-rs/exec-server/src/environment.rs", ("EnvironmentManager", "EnvironmentObservedStatus", "default_environment_ids")),
        ("source_spec.extra.environment_turn_context", "environment_turn_context", "codex-rs/core/src/session/turn_context.rs", ("TurnEnvironment", "shell_environment_policy", "workspace_roots")),
    )
    specs.append(SourceSpec(
        id="source_spec.extra.generated_config_schema",
        legacy_key="generated_config_schema",
        group=SourceGroup.EXTRA,
        path_candidates=("codex-rs/core/config.schema.json",),
        required=False,
        roles=("generated_config_schema", "config_surface"),
        expected_symbols=("\"title\": \"ConfigToml\"", "\"features\"", "\"model_providers\""),
        extractor_ids=("extractor.config_effects",),
    ))
    specs.append(SourceSpec(
        id="source_spec.extra.config_schema_generator",
        legacy_key="config_schema_generator",
        group=SourceGroup.EXTRA,
        path_candidates=("codex-rs/config/src/schema.rs",),
        required=False,
        roles=("config_schema_generation_policy", "config_surface"),
        expected_symbols=("features_schema", "Feature::Artifact", "Feature::GuardianThreadContext"),
        extractor_ids=("extractor.config_effects",),
    ))
    _append_optional_specs(
        specs, context_specs, role="context_management", extractor_id="extractor.context_management"
    )
    _append_optional_specs(
        specs, local_storage_specs, role="local_storage", extractor_id="extractor.local_storage"
    )
    _append_optional_specs(
        specs, prompt_specs, role="prompt_context", extractor_id="extractor.prompt_context"
    )
    _append_optional_specs(
        specs, policy_specs, role="execution_policy", extractor_id="extractor.execution_policy"
    )
    _append_optional_specs(
        specs, plugin_specs, role="plugin_runtime", extractor_id="extractor.plugin_runtime"
    )
    _append_optional_specs(
        specs,
        environment_specs,
        role="execution_environment",
        extractor_id="extractor.execution_environment",
    )
    _merge_routing_domain_specs(specs)
    _merge_protocol_domain_specs(specs)
    _merge_app_server_domain_specs(specs)
    return SourceRegistry(specs)
