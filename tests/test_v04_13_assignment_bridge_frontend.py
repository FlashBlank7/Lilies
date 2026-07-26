from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "platform" / "frontend"


def _read(relative: str) -> str:
    return (FRONTEND / relative).read_text(encoding="utf-8")


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"async function {name}")
    end = source.index(f"async function {next_name}", start)
    return source[start:end]


def test_frontend_client_uses_only_the_local_lilies_bridge_namespace() -> None:
    source = _read("lib/platform.ts")

    required_paths = {
        "/api/v1/local-lilies/status",
        "/api/v1/local-lilies/connections",
        "/api/v1/local-lilies/connections/${encodeURIComponent(connectionId)}/refresh",
        "/api/v1/local-lilies/connections/${encodeURIComponent(connectionId)}/reconnect",
        "/api/v1/local-lilies/applications/${encodeURIComponent(applicationId)}/builds",
        "/api/v1/local-lilies/applications/${encodeURIComponent(applicationId)}/assignments",
        "/api/v1/local-lilies/assignments/${encodeURIComponent(assignmentId)}",
        "/api/v1/local-lilies/builds/${encodeURIComponent(buildId)}",
        "/api/v1/local-lilies/sessions/${encodeURIComponent(sessionId)}",
        "/api/v1/local-lilies/assignments/${encodeURIComponent(assignmentId)}/cancel",
        "/api/v1/local-lilies/assignments/${encodeURIComponent(assignmentId)}/resume",
        "/api/platform/api/v1/local-lilies/assignments/${encodeURIComponent(assignmentId)}/events?after=",
    }
    for path in required_paths:
        assert path in source
    assert "enabled: boolean" in source
    assert "default_route: false" in source
    assert "connections: LocalLiliesConnection[]" in source
    assert "export type LocalLiliesDiscoveryAvailable" in source
    assert "export type LocalLiliesDiscoveryUnavailable" in source
    assert "status: 'available'" in source
    assert "status: 'unavailable'" in source
    assert "discovery?: LocalLiliesDiscovery" in source
    assert "relay_cursor: number" in source
    assert "ack_cursor: number" in source


def test_home_local_launch_does_not_prebuild_or_silently_fall_back() -> None:
    source = _read("app/page.tsx")
    local_route = _function(source, "launchLocalLilies", "launchLegacyBuilder")
    legacy_route = source[source.index("async function launchLegacyBuilder"):source.index("const JAPANESE_LEARNING", source.index("async function launchLegacyBuilder"))]

    assert "createLocalLiliesAssignment" in local_route
    assert "applyCodexWorkspaceScenario" not in local_route
    assert "seedSafeDraftSkeleton" not in local_route
    assert "applyDraftOperation" not in local_route
    assert "/draft" not in local_route
    assert "auto_publish" not in local_route
    assert "applyCodexWorkspaceScenario" in legacy_route
    assert "/api/v1/applications/${application.id}/builds" in legacy_route
    assert "catch {\n        window.location.href = `/applications/${app.id}?safeDraft=1`" not in source
    assert "no implicit fallback was run" in source
    assert "cause instanceof PlatformApiError && cause.assignment_id" in source
    assert "?assignment=${encodeURIComponent(cause.assignment_id)}" in source
    assert 'data-builder-route={builderRoute || \'unselected\'}' in source
    assert "useState<BuilderRoute | null>(null)" in source


def test_home_requires_feature_enablement_pairing_and_an_explicit_route() -> None:
    source = _read("app/page.tsx")
    connection = _read("app/local-lilies-connection-panel.tsx")

    assert "enabled: false" in source
    assert "default_route: false" in source
    assert "connection.status === 'connected'" in source
    assert "!builderRoute" in source
    assert "Legacy Builder (developer)" in source
    assert "never a Local Lilies fallback" in source
    assert "expected_daemon_fingerprint" in connection
    assert "pairing_code" in connection
    assert "idempotency_key: pairAttemptRef.current.current()" in connection
    assert "reconnectAttemptRef.current.current()" in connection
    assert "Keep the known connection ID and daemon fingerprint visible" in connection
    assert "selectedConnectionId" in connection
    assert "onConnectionSelect?.(selectedConnectionId)" in connection
    assert "status.connections.find(item => item.connection_id === selectedConnectionId)" in connection
    assert "status.connections[0]" not in connection
    assert "Manage connection" in connection
    assert "Pair another daemon" in connection
    assert "next.discovery?.status === 'available'" in connection
    assert "setDaemonUrl(next.discovery.base_url)" in connection
    assert "setFingerprint(next.discovery.daemon_fingerprint)" in connection
    assert 'data-local-lilies-discovery-state={discoveryState}' in connection
    assert "pairing still requires a one-time code entered by you" in connection
    discovery_prefill = connection[
        connection.index("const commitStatus ="):
        connection.index("const refresh =", connection.index("const commitStatus ="))
    ]
    assert "setPairingCode" not in discovery_prefill
    assert "pairLocalLilies" not in discovery_prefill
    assert "selectedLocalConnectionId" in source
    assert "setSelectedLocalConnectionId(connectionId)" in source
    assert "setBuildIntentConfirmed(false)" in source
    assert "onConnectionSelect={selectLocalConnection}" in source
    refresh_body = connection[connection.index("async function refreshConnection"):connection.index("const connection =", connection.index("async function refreshConnection"))]
    assert "catch (cause)" in refresh_body
    assert "await refresh()" in refresh_body


def test_pairing_and_reconnect_retries_reuse_one_in_memory_operation_key() -> None:
    connection = _read("app/local-lilies-connection-panel.tsx")
    build_panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")
    client = _read("lib/platform.ts")
    attempt = _read("lib/local-lilies-operation-attempt.ts")

    pair_body = _function(connection, "pair", "reconnect")
    pair_success = pair_body[:pair_body.index("} catch (cause) {")]
    pair_failure = pair_body[pair_body.index("} catch (cause) {"):]
    reconnect_body = _function(connection, "reconnect", "refreshConnection")
    reconnect_success = reconnect_body[:reconnect_body.index("} catch (cause) {")]
    reconnect_failure = reconnect_body[reconnect_body.index("} catch (cause) {"):]
    detail_reconnect = build_panel[
        build_panel.index("async function reconnect()"):
        build_panel.index("const connection =", build_panel.index("async function reconnect()"))
    ]
    client_reconnect = client[
        client.index("export function reconnectLocalLilies"):
        client.index("export function createLocalLiliesAssignment")
    ]

    assert "useRef(new LocalLiliesOperationAttempt(idempotency))" in connection
    assert "idempotency_key: pairAttemptRef.current.current()" in pair_success
    assert "pairAttemptRef.current.reset()" in pair_success
    assert "pairAttemptRef.current.reset()" not in pair_failure
    assert "reconnectAttemptRef.current.current()" in reconnect_success
    assert "reconnectAttemptRef.current.reset()" in reconnect_success
    assert "reconnectAttemptRef.current.reset()" not in reconnect_failure
    assert "reconnectAttemptRef.current.current()" in detail_reconnect
    assert "reconnectAttemptRef.current.reset()" in detail_reconnect

    for state_change in (
        "pairAttemptRef.current.reset(); setDaemonUrl(event.target.value)",
        "pairAttemptRef.current.reset(); setFingerprint(event.target.value)",
        "reconnectAttemptRef.current.reset(); setSelectedConnectionId(event.target.value)",
    ):
        assert state_change in connection
    assert "value={pairingCode} onChange={event => setPairingCode(event.target.value)}" in connection
    assert "pairAttemptRef.current.reset(); setPairingCode(event.target.value)" not in connection
    assert "reconnectAttemptRef.current.reset(); setPairingCode(event.target.value)" not in connection
    assert "value={reconnectCode} onChange={event => setReconnectCode(event.target.value)}" in build_panel
    assert "reconnectAttemptRef.current.reset(); setReconnectCode(event.target.value)" not in build_panel
    pair_another = connection[
        connection.index('onClick={() => { pairAttemptRef.current.reset()'):
        connection.index("Pair another daemon")
    ]
    assert "reconnectAttemptRef.current.reset()" in pair_another
    assert "setPairingCode('')" in pair_another

    assert "idempotencyKey: string" in client_reconnect
    assert "idempotency_key: idempotencyKey" in client_reconnect
    assert "idempotency()" not in client_reconnect
    assert "pairing" not in attempt.lower()
    assert "token" not in attempt.lower()
    assert "localStorage" not in connection
    assert "sessionStorage" not in connection
    assert "URLSearchParams" not in connection
    assert "console." not in connection
    assert "localStorage" not in detail_reconnect
    assert "sessionStorage" not in detail_reconnect
    assert "URLSearchParams" not in detail_reconnect
    assert "console." not in detail_reconnect


def test_studio_recovers_assignment_and_keeps_all_four_correlation_ids() -> None:
    page = _read("app/applications/[id]/page.tsx")
    panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")

    assert "query.get('assignment')" in page
    assert "requestedAssignmentId={requestedAssignmentId}" in page
    assert "localLiliesAssignment(requestedAssignmentId)" not in panel  # recovery is centralized
    assert "if (requestedAssignmentId) return loadAssignment(requestedAssignmentId)" in panel
    assert "localLiliesApplicationAssignments(applicationId)" in panel
    assert 'data-correlation-ids="application,build,assignment,session"' in panel
    for label in ("Application ID", "Build ID", "Assignment ID", "Session ID"):
        assert label in panel
    assert "Keep an already-rendered assignment and all correlation IDs visible" in panel
    assert "cancelLocalLiliesAssignment(assignment.assignment_id)" in panel
    assert "next.application_id !== applicationId" in panel
    assert "assignment_application_mismatch" in panel
    assert "const hasNonterminalAssignment = Boolean(assignment && !terminal)" in panel
    assert "busy || hasNonterminalAssignment || !status.enabled" in panel
    assert "['completed', 'cancelled'].includes(assignment.phase)" in panel


def test_studio_event_stream_deduplicates_replay_and_preserves_cursor() -> None:
    panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")

    assert "openLocalLiliesAssignmentEventStream(assignmentId, streamCursor, controller.signal)" in panel
    assert "response.body.getReader()" in panel
    assert "new TextDecoder()" in panel
    assert "new AbortController()" in panel
    assert "controller.abort()" in panel
    assert "streamAbortRef.current?.abort()" in panel
    assert "parseLocalLiliesSseFrame" in panel
    assert "field === 'event'" in panel
    assert "seenEvents.current.has(key)" in panel
    assert "seenEvents.current.add(key)" in panel
    assert "projected.seq <= replayBoundary.current" in panel
    assert "setCursor(current => Math.max(current, normalized.seq))" in panel
    assert 'data-event-dedupe="event_id"' in panel
    assert "IDs and cursor are preserved while reconnecting" in panel
    assert "setStreamError('')" in panel
    assert "streamedAssignmentId.current !== assignmentId" in panel
    assert "seenEvents.current.clear()" in panel
    assert "setEvents([])" in panel
    assert "setCursor(0)" in panel
    assert 'role="log"' in panel
    assert 'aria-live="polite"' in panel
    assert "setAssignment(null)" not in panel
    assert "new EventSource" not in panel


def test_platform_proxy_preserves_sse_resume_headers_and_cancellation() -> None:
    source = _read("app/api/platform/[...path]/route.ts")
    client = _read("lib/platform.ts")

    assert "request.headers.get('x-lilies-platform-api-token')" in source
    assert "request.headers.get('accept')" in source
    assert "headers.set('accept', accept)" in source
    assert "request.headers.get('last-event-id')" in source
    assert "headers.set('last-event-id', lastEventId)" in source
    assert "signal: request.signal" in source
    assert "new Response(response.body" in source
    stream_client = client[client.index("export function localLiliesAssignmentEventsUrl"):client.index("export function idempotency")]
    assert "X-Lilies-Platform-API-Token" in stream_client
    assert "Last-Event-ID" in stream_client
    assert "signal" in stream_client
    assert "withFrontendToken" not in stream_client
    assert "frontend_token" not in stream_client


def test_platform_proxy_rejects_local_lilies_query_secrets_before_auth_forwarding() -> None:
    source = _read("app/api/platform/[...path]/route.ts")

    for key in (
        "access_token",
        "api_key",
        "api_token",
        "authorization",
        "bootstrap_credential",
        "credential",
        "frontend_token",
        "pairing_code",
        "password",
        "prepared_access_token",
        "previous_access_token",
        "secret",
        "token",
    ):
        assert f"'{key}'" in source
    assert "isLocalLiliesPath(path) && containsQuerySecret(searchParams)" in source
    assert "code: 'query_secret_rejected'" in source
    assert "authentication is accepted only in request headers" in source
    assert source.index("isLocalLiliesPath(path) && containsQuerySecret(searchParams)") < source.index(
        "const browserToken ="
    )


def test_api_errors_preserve_structured_detail_and_bridge_identifiers() -> None:
    source = _read("lib/platform.ts")
    api_body = source[source.index("export async function api<"):source.index("export function localLiliesStatus")]

    assert "export class PlatformApiError extends Error" in source
    for field in (
        "readonly status: number",
        "readonly statusText: string",
        "readonly body: unknown",
        "readonly detail: unknown",
        "readonly code: string",
        "readonly application_id?: string",
        "readonly assignment_id?: string",
        "readonly build_id?: string",
        "readonly session_id?: string",
    ):
        assert field in source
    assert "parseApiErrorBody(await response.text())" in api_body
    assert "throw new PlatformApiError(response.status, response.statusText, body)" in api_body
    assert "throw new Error" not in api_body
    assert "error instanceof PlatformApiError" in source
    assert "error.status === 401" in source


def test_bridge_errors_are_typed_and_never_rendered_as_react_objects() -> None:
    client = _read("lib/platform.ts")
    home_panel = _read("app/local-lilies-connection-panel.tsx")
    studio_panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")

    assert "export type LocalLiliesBridgeError" in client
    assert "last_error?: LocalLiliesBridgeError | null" in client
    assert "export function localLiliesErrorMessage(value: unknown)" in client
    assert "localLiliesErrorMessage(connection?.last_error)" in home_panel
    assert "localLiliesErrorMessage(assignment?.last_error)" in studio_panel
    assert "{connection?.last_error}" not in home_panel
    assert "{assignment?.last_error}" not in studio_panel


def test_interrupted_assignments_have_an_explicit_resume_without_auto_publish() -> None:
    client = _read("lib/platform.ts")
    panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")
    start = panel.index("async function startLocalAssignment")
    end = panel.index("async function cancel", start)
    local_start = panel[start:end]

    assert "export function resumeLocalLiliesAssignment" in client
    assert "resumeLocalLiliesAssignment(assignment.assignment_id)" in panel
    for state in ("interrupted", "error", "unavailable"):
        assert f"'{state}'" in panel
    resumable = panel[panel.index("const resumable ="):panel.index("const visibleError =")]
    assert "waiting" not in resumable
    assert "Resume assignment explicitly" in panel
    assert "auto_publish" not in local_start
    assert "cause instanceof PlatformApiError && cause.assignment_id" in local_start
    assert "loadAssignment(cause.assignment_id)" in local_start


def test_cancel_failure_reloads_the_durable_assignment_before_connection_status() -> None:
    panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")
    cancel_body = panel[panel.index("async function cancel()"):panel.index("async function resume()")]
    catch_body = cancel_body[cancel_body.index("} catch (cause) {"):]

    assert "await loadAssignment(assignment.assignment_id)" in catch_body
    assert catch_body.index("await loadAssignment(assignment.assignment_id)") < catch_body.index(
        "await loadStatus()"
    )


def test_local_events_refresh_the_parent_draft_and_canvas() -> None:
    page = _read("app/applications/[id]/page.tsx")
    panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")

    assert "onApplicationChanged: () => unknown | Promise<unknown>" in panel
    assert "['tool.completed', 'turn.finished', 'assignment.completed']" in panel
    assert "applicationChangedRef.current = onApplicationChanged" in panel
    assert "Promise.resolve(applicationChangedRef.current())" in panel
    assert "onApplicationChanged={refresh}" in page


def test_state_events_reload_assignment_and_terminal_turn_stops_the_stream() -> None:
    panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")

    assert "['assignment.', 'session.', 'turn.', 'permission.', 'bridge.']" in panel
    assert ".some(prefix => normalized.event_type.startsWith(prefix))" in panel
    assert "loadAssignment(assignmentId).then(next =>" in panel
    assert "['completed', 'cancelled'].includes(next.phase)" in panel
    assert "controller.abort()" in panel


def test_assignment_business_context_is_derived_from_requirement_and_contract() -> None:
    client = _read("lib/platform.ts")
    home = _read("app/page.tsx")
    panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")
    helper = client[client.index("export function deriveLocalLiliesBusinessContext"):client.index("const root =")]

    assert "target_user" in helper
    assert "business_goal" in helper
    assert "external_contracts" in helper
    assert "runtime_guarantees" in helper
    assert "unresolved_decisions" in helper
    assert "localLiliesRequirementSections(requirement)" in helper
    assert "customer_roles:" in helper
    assert "inputs:" in helper
    assert "outputs:" in helper
    assert "constraints," in helper
    assert "deriveLocalLiliesBusinessContext(requirement, capabilityContext" in home
    assert "deriveLocalLiliesBusinessContext(requirement, capabilityContext" in panel
    assert "customer_roles: ['workflow owner']" not in home
    assert "customer_roles: ['workflow owner']" not in panel
    assert "The current application requirement" not in panel
    assert "The complete workflow requirement submitted in this form" not in home


def test_studio_marks_legacy_builder_as_a_separate_developer_route() -> None:
    page = _read("app/applications/[id]/page.tsx")
    panel = _read("app/applications/[id]/local-lilies-build-panel.tsx")

    assert 'data-builder-route="legacy_builder"' in page
    assert 'data-build-action="detail-start-legacy-builder"' in page
    assert "never used as a Local Lilies fallback" in page
    assert 'data-local-lilies-build="explicit"' in panel
    start = panel.index("async function startLocalAssignment")
    end = panel.index("async function cancel", start)
    local_start = panel[start:end]
    assert "createLocalLiliesAssignment" in local_start
    assert "/draft" not in local_start
    assert "legacy" not in local_start.lower()
