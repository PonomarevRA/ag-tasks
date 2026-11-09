# Agent Activity HTTP API v1

Canonical read-only HTTP contract for Atlas, AgMemory, Small Yuki, Ag Design and Ag Tasks. The service base URL
is the only application-specific client setting. Existing unversioned mini-chat
routes and WebSocket frames keep their existing behavior. MCP is unchanged.

## Routes

- `GET /api/agent-activity/v1/status`: durable bounds, quota and normalized health.
- `GET /api/agent-activity/v1/events`: one bounded page of permitted events.

Responses use `Content-Type: application/json` and `Cache-Control: no-store`.
`schemaVersion` is `agent-activity-http/v1`; individual events keep the existing
`agent-activity/v1` schema. The colocated OpenAPI 3.1 document and JSON Schema are
portable artifacts; they do not require global Swagger or a new runtime package.

## Query validation

Only `limit`, `journalEpoch`, `afterSequence`, `beforeSequence`, `sessionId`,
`operation`, `q` and `projectId` are accepted on events. Status accepts no query.
Unknown or repeated keys, malformed percent escapes and invalid URL-decoded UTF-8
are HTTP 400 `invalid_query`. Limit defaults to 100;
valid values are canonical decimal integers 1..100. Sequence values are canonical
decimal strings in 0..9223372036854775807, never JSON floating-point numbers.

`afterSequence` and `beforeSequence` are mutually exclusive. Either requires a
nonempty `journalEpoch` (at most 128 Unicode scalar values); epoch without a
sequence is invalid. No sequence means the latest backward page. `sessionId`,
`operation` and `projectId` are exact filters, at most 128 Unicode scalar values;
`q` is at most 512. Empty filter strings mean no filter. Reject invalid Unicode.

The common `q` search is a **case-sensitive literal substring** in operation,
query.preview, request.summary or response.summary. It is neither regex nor a
new tool/model call. This deliberately avoids locale/Unicode case-fold divergence
between Python and .NET. The existing mini-chat search remains unchanged.
Project filtering matches captured permitted code-project metadata. A removed
project may subsequently produce an unavailable display target. Memory events
have no code project and do not match a nonempty projectId. No filter changes authority.

## Page and continuation

Every page returns `direction` (`forward` with afterSequence, otherwise backward),
`events`, `nextCursor`, `highWatermark`, `oldestSequence`, `hasMore`, `truncated`
and `health`, plus schemaVersion/application. Events within a page are always in
ascending commit sequence, regardless of traversal direction. No event count,
byte truncation or filter may silently skip a matching event between pages.

- Continue forward with afterSequence=nextCursor.sequence and its journalEpoch.
- Continue backward with beforeSequence=nextCursor.sequence and its journalEpoch.
- nextCursor is an **exclusive** continuation boundary, distinct from the head.
- highWatermark is the durable head captured with that page, not continuation.
- Forward: if hasMore, nextCursor is the last included event; otherwise it advances
  to highWatermark, including an empty filtered page. Reusing this cursor polls
  later committed events safely.
- Backward: nextCursor is the earliest included event while hasMore; at the end
  it is oldestSequence minus one, including an empty page.
- hasMore means another matching retained event existed in that direction at
  the captured highWatermark. It does not predict future appends.

Pages contain at most 100 events and **262144 UTF-8 bytes including the envelope**.
At the byte boundary stop, never fit smaller events beyond the skipped event.
`truncated=true` means this page was shortened by the byte limit; hasMore is true.
Count pagination alone is not byte truncation. Health counters separately describe
producer losses; they are not pagination omissions.

All page decisions/bounds use one coherent committed window. A cursor with a
different epoch, below oldestSequence-1 or above the captured latestSequence
returns HTTP 409 `cursor_expired` and the current head as `resetCursor`. An
already rolled-out prefix is never silently accepted. An empty journal has
oldestSequence=latestSequence+1. Cursor is valid at that boundary minus one.

## Status and errors

Status returns schemaVersion/application, eventSchemaVersion, journalEpoch,
oldestSequence/latestSequence, maxSizeBytes/usedBytes, `cursor` (the durable head),
`limits` (maxPageEvents=100, maxPageBytes=262144) and health.
Normalized health is `{status:"ok"|"degraded",droppedTotal,queueItems,queueBytes,errorCode}`.
Unknown counters/gauges are explicit null, never invented zero. AgMemory's live
cross-process queue gauges are unknown; its committed dropped total is known.

Handled errors use the same envelope in both services:

```json
{"schemaVersion":"agent-activity-http/v1","application":"atlas","error":{"code":"invalid_query","message":"Invalid query parameters.","retryable":false},"resetCursor":null}
```

Codes: 400 invalid_query; 403 forbidden; 409 cursor_expired; 503
journal_unavailable. Only 409 includes a non-null resetCursor. 503 is retryable;
the others require correcting the request/access or explicitly resetting a cursor.
Fixed messages are respectively `Invalid query parameters.`, `Access forbidden.`,
`Cursor has expired.` and `Journal is unavailable.`
Messages are fixed safe descriptions, without echoed queries, storage paths,
actor/scope, exception text or private payloads. Transport/host rejection before
the API and method-level 405 may use the host's existing response body; clients
must honor HTTP status even without a JSON API envelope.

## Access and compatibility

Local HTTP clients may omit Origin. If present it must match the service origin.
Keep loopback peer/Host checks, cross-site rejection and AgMemory's existing
Development/fixed-authority policy. No CORS widening, new access token or remote
listener is introduced. Projection never exposes internal partition/target tokens,
actor/scope, memory bodies, CRC or storage paths. These endpoints do not clear or
write the journal. Old UI/WS routes keep their current cursor/filter semantics.
