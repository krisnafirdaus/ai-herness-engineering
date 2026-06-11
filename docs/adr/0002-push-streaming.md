# ADR 0002 — Push-based streaming (SSE + WebSocket) instead of polling

**Status:** accepted

## Context

Run visibility used to be `GET /runs/{id}` polling: the client guessed a
refresh interval, paid latency between transitions, and hammered the API
under fan-out. Production agent platforms stream progress.

## Decision

Three pieces (`src/events.py`, `src/api/server.py`):

1. **Event bus.** `Repository.add_event` persists the event, then publishes
   it to a bus: in-process fan-out for single-process deployments, Redis
   pub/sub (`harness:events:{run_id}`) when workers and API are separate
   processes. `HARNESS_EVENT_BUS=auto` keys off the queue mode.
2. **SSE endpoint** `GET /runs/{id}/stream`: emits a `snapshot` frame, then
   replays the durable event log from `Last-Event-ID` (or `?after=`), then
   pushes live bus events — all deduplicated by event id because the
   subscription is opened *before* the replay query, so no event can fall in
   the gap. Heartbeats keep proxies alive; the stream closes itself shortly
   after the run reaches a terminal state.
3. **WebSocket** `WS /runs/{id}/ws` with the same merge logic for clients
   that want bidirectional framing.

The events table is the replay log; pub/sub is allowed to be lossy because a
quiet bus is backstopped by a durable-log catch-up read inside the stream
loop.

## Why not "just WebSockets" or "just poll faster"?

* SSE is HTTP/1.1, proxy-friendly, auto-reconnecting (browsers resend
  `Last-Event-ID`), and exactly fits a one-way progress feed; WS is provided
  for richer clients.
* Faster polling burns DB reads at fan-out scale and still has worst-case
  latency = interval. Push has ~0 idle cost per subscriber and transition
  latency in milliseconds.

## Consequences

* Live UIs (and `curl -N`) see every transition as it commits, with lossless
  reconnect.
* Cross-process without Redis degrades to the catch-up read (documented),
  never to silence.
