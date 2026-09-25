# Responses transport architecture

This note is intentionally architectural. Exact request fields, error mappings,
retry codes, fork flags, and revision-specific behavior belong in the generated
system contract.

## One logical agent loop, two transport strategies

Codex has to support a repeated model/tool loop:

```text
model step
   ↓
local tool work
   ↓
model step
   ↓
...
```

There are two useful transport extremes:

```text
HTTPS
  replayable request boundary
  simple fallback/recovery path
  no correctness dependency on one long-lived socket

WebSocket
  long-lived fast path
  amortized connection setup
  server-side continuation can make later model steps incremental
```

Keeping those roles distinct is simpler than making HTTPS partially stateful.

## Why HTTP keep-alive is not the turn contract

An HTTP implementation may reuse TCP/TLS connections, but connection reuse is a
performance detail rather than the identity of a Codex turn.

Making one turn depend on one particular HTTP connection would couple several
otherwise independent concerns:

```text
agent state
routing affinity
proxy selection
authentication recovery
transport failure
connection lifetime
```

That coupling makes recovery harder. A proxy restart, route change, connection
close, or network migration should not erase the client's ability to reconstruct
the next model request.

The safer boundary is:

```text
logical committed history lives above the transport
transport-local acceleration may be discarded and rebuilt
```

HTTP connection pooling can still be used where useful; it simply should not be
what makes a turn semantically continuous.

## Why WebSocket is worth being stateful

For an agent workload, the gain from WebSocket is larger than saving an HTTP
header or handshake. It gives Codex one explicit place to put the stateful fast
path:

```text
persistent connection
+ server-side continuation state
+ small incremental requests between tool steps
```

That is materially different from merely keeping an HTTP/2 connection open.
HTTP/2 connection reuse multiplexes requests over one transport connection; it
does not by itself define a model-context continuation protocol.

The stateful optimization is therefore concentrated in the transport that can
actually exploit it.


## Model identity has a capability layer

The request `model` slug and server-reported `OpenAI-Model` are names, not stable
capability identities. Model discovery carries a separate capability vector.

Current catalog revisions can advertise:

```text
models[].available_access_programs.cyber
models[].comp_hash
tool/context/reasoning capability fields
```

Legacy catalog revisions may omit `available_access_programs` entirely. Its
presence and values are therefore useful as a catalog-generation or alias
fingerprint, but not as a unique proof of one model family. `comp_hash` is also
not unique model identity; it identifies compaction-compatible configurations.

The inference request field is separate:

```text
turn/start.cyberAccessProgram
        ↓
access_programs.cyber
```

It is a per-response selection, not model discovery metadata.

Startup WebSocket prewarm currently builds its turn with
`NewTurnContextOptions::default()`, so `cyber_access_program` is null and
`access_programs` is omitted from that prewarm request. The prewarm response may
still carry `x-models-etag`, which is a catalog invalidation/version signal.


## Prewarm belongs to the fast path

A WebSocket fast path benefits from doing connection and server-side setup before
the first expensive generation. A non-generating prewarm can establish the
transport and continuation baseline without turning setup into model inference.

The architectural purpose is latency hiding and fast-path preparation, not
conversation persistence. Durable/reconstructible history remains a separate
layer.


## Routing cookies belong to the opening handshake

ChatGPT infrastructure cookies such as routing-affinity cookies are transport setup
state rather than WebSocket frame state. On revisions with the shared cookie bridge,
Codex may learn an allowlisted cookie from an HTTPS response or a WebSocket opening
handshake and replay it on a later matching HTTPS/WSS request.

For WSS, both directions are ordinary HTTP headers before the protocol switches:

```text
GET ... Upgrade: websocket
Cookie: <routing affinity, if already known>
        ↓
101 Switching Protocols
Set-Cookie: <routing affinity, if supplied upstream>
        ↓
WebSocket data frames
```

A cookie returned with the successful upgrade cannot change the routing of that
already-established socket; it only affects later matching requests/reconnects.
Codex does not assign a fixed routing-cookie TTL here; expiry remains an upstream
`Set-Cookie` attribute interpreted by the shared cookie jar.
The runtime contract is revision-sensitive because older Codex revisions had the
HTTP cookie jar without the WebSocket handshake bridge.


## Failure should fall back toward reconstructible state

A useful invariant is:

```text
optimization state may be lost
committed conversation state must remain reconstructible
```

If an incremental WebSocket chain cannot continue, recovery can discard that
transport-local state and rebuild from committed history. This is preferable to
trying to make a half-failed socket or partially emitted model stream the new
source of truth.

That also gives transport fallback a clean direction:

```text
stateful optimized path
        ↓ failure
reconstruct request from logical state
        ↓
replayable transport path
```

## Forks reinforce the same separation

A fork is fundamentally a history/identity operation, not a transport-cloning
operation. The child can inherit a selected history prefix while establishing
fresh transport-local state.

This separation allows durable forks, ephemeral side branches, subagent history
forks, and resumed threads to reuse the same underlying history concepts without
requiring them to inherit a parent's live connection or routing token.

## Design rule

The useful division is therefore:

```text
HTTPS: robust replay boundary
WebSocket: stateful latency/context optimization
history: transport-independent source of recoverable model context
```

Avoiding a semantic middle layer where HTTP connection lifetime partly owns the
turn keeps failure handling and fork/resume behavior much easier to reason about.
