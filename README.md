# pack-companions-sdk

Python SDK for the [Pack Companions Service](https://github.com/pack-companions/pack-companions) — AI companions across the Pack ecosystem.

## What this is

A thin, async Python client that lets your FastAPI (or any server-side Python)
app talk to the Pack Companions service without reimplementing HMAC signing or
comment-decision retries.

This SDK is **server-only**. Its HMAC secret must never be embedded in a browser,
mobile app, public bundle, log, or error response.

Browser and mobile code must call **your own authenticated backend**. That
backend may use this SDK to call Pack. Never call an HMAC-authenticated Pack
endpoint directly from a frontend and never pass the HMAC secret to a
JavaScript bundle or APK:

```text
browser/mobile -> your backend -> this Python SDK -> Pack Companions
```

The [`@pack-companions/companions`](https://github.com/pack-companions/pack-companions-js)
and
[`@pack-companions/companions-react`](https://github.com/pack-companions/pack-companions-js)
packages may provide frontend types and renderers, but any authenticated Pack
transport must still originate in your backend. A package does not make a
frontend a safe place for a Pack HMAC secret.

Persist the `link_incarnation_id` and `privacy_generation` returned by Pack
identify atomically with the host account. Place them in runtime snapshots as
`expected_link_incarnation_id` and `expected_privacy_generation`. Capture both
inside every durable queued event. If Pack returns `stale_account_incarnation`
or `stale_privacy_generation`, discard the old operation: never fetch new
tokens and replay old content against them.

## Install

```bash
# From PyPI (once published)
pip install pack-companions-sdk

# Local dev (editable install — changes show up live)
pip install -e ~/projects/pack-companions-sdk/
```

## Quickstart

```python
from uuid import uuid4

from pack_companions import (
    CommentEvent,
    CompanionSnapshot,
    CompanionsClient,
    SnapshotCompanionPreferences,
    SnapshotLocation,
    SnapshotSession,
    SnapshotUser,
)

client = CompanionsClient(
    api_key="your_app_api_key",
    secret="your_app_hmac_secret",
    service_url="https://companions.example.internal",
)

session_id = uuid4()
snapshot = CompanionSnapshot(
    user=SnapshotUser(
        id="your-server-side-user-id",
        companion_id="puppy",
    ),
    location=SnapshotLocation(
        route="/practice/two-sum",
        route_intent="practice",
    ),
    session=SnapshotSession(
        id=session_id,
        started=True,
        absence_seconds=7200,
        timezone="America/Chicago",
    ),
    preferences=SnapshotCompanionPreferences(
        ai_personalization_enabled=True,
        care_reminders_enabled=True,
        hydration_enabled=True,
        food_enabled=True,
        break_reminders_enabled=True,
        late_night_banter_enabled=True,
        quiet_hours_active=False,
        quiet_hours_allow_session_greeting=True,
        reduced_motion=False,
    ),
)

# Create this ONCE for the logical fact. Persist the complete exact payload
# before sending; a UUID without its original facts is not enough to recover
# safely after process death.
event = CommentEvent.create(
    snapshot=snapshot,
    event_type="session_started",
)
persist_pending_event(event.client_event_id, event.request_bytes)
result = await client.get_comment_event(event)

assert result.decision_id is not None

# Pack makes decision processing idempotent. Visible delivery belongs to the
# host, so persist a presentation key and make this block idempotent too.
if not was_presented(result.decision_id):
    if result.speaks:
        present_plain_text(result.text)

    # Speaking and acting are independent presentation legs. A speak_and_act
    # result must do both; action_only performs only the act_only leg.
    if (
        result.expression is not None
        and result.expression.response_mode in {"speak_and_act", "act_only"}
    ):
        render_semantic_expression(result.expression)

    if result.deliberately_silent and result.outcome != "action_only":
        # throttled, declined, and no_opportunity are intentional no-speech
        # decisions. Do not invent fallback companion copy.
        pass

    mark_presented(result.decision_id)

delete_pending_event(event.client_event_id)
```

`SnapshotCompanionPreferences` is immutable and always present in the wire
snapshot. Omitting it applies the same defaults shown above: every control is
enabled except `quiet_hours_active` and `reduced_motion`. The host must populate
these facts from authenticated settings; Pack decides the resulting wording,
opportunity, and semantic expression.

The v1 event types are:

- `session_started`
- `navigation`
- `learning_event`
- `hint_requested`
- `activity_tick`
- `settings_changed`

The v1 terminal outcomes are `delivered`, `action_only`, `throttled`,
`declined`, and `no_opportunity`. `action_only` is reserved by the initial v1
contract and is not emitted yet, but clients preserve it without inventing
speech. The decision also preserves:

- Semantic expression intent, intensity, response mode, optional bubble intent,
  and optional behavior intent (`drink`, `eat`, `stretch`, `rest`, `tired`,
  `sleeping`).
- `response_locale` and `speech_locale`.
- Combined opportunity facets.
- `next_evaluation_at`.
- `deduplicated`, which identifies recovery of the same stable decision
  content and ID. This replay marker itself intentionally differs from the
  original response.

Expression values are semantic only. They never contain asset paths, filenames,
URLs, or executable renderer instructions.

## Retry and compatibility rules

Typed comment calls retry a bounded number of ambiguous transport/502/503/504
failures with the exact same request bytes and `client_event_id`. The default is
two total attempts and the configurable maximum is three. Public SDK failures
are sanitized: they never expose the signed HTTP request, response body, HMAC
headers, or snapshot payload.

For a retry performed by your own queue, HTTP handler, or worker, reuse the same
`CommentEvent`; do not create a new UUID for the same logical fact. Reusing an ID
with a changed payload is a service conflict. `CommentEvent` and its complete
snapshot tree are immutable, and the event captures its canonical request bytes
when constructed.

For durable recovery, store `event.request_bytes` before the first network
attempt. Restore it with:

```python
event = CommentEvent.from_request_bytes(stored_request_bytes)
result = await client.get_comment_event(event)
```

Pack guarantees exactly-once **decision processing**, not exactly-once visible
delivery. A response may be lost after Pack commits it, and the recovered
response then carries `deduplicated=true`. Do not use that flag alone to skip
presentation—the original may never have reached the user. Instead, make
presentation idempotent with durable `decision_id`/`client_event_id` records.
Marking after presentation gives at-least-once behavior and retains a small
crash window where a visual can repeat; marking before presentation trades that
for a possible missed visual. A strict delivery acknowledgement is a future
contract.

`get_comment(snapshot)` and `get_legacy_comment(snapshot)` retain the historical
snapshot-only request for one compatibility window. Because that request has no
idempotency key, the SDK intentionally gives it only one network attempt. New
integrations should use `CommentEvent`.

## Durable app-account erasure (ID-4)

Account erasure uses its own incarnation-bound protocol and is deliberately
independent of privacy generation:

```python
from pack_companions import EraseForAppEvent

# state is the AccountLinkState persisted with the authenticated local account.
event = EraseForAppEvent.create_for_state(
    user_id="your-server-side-user-id",
    state=state,
)

# The active job needs the exact bytes in access-controlled encrypted storage.
persist_encrypted_pending_erasure(
    event.client_erasure_event_id,
    event.request_bytes,
)
result = await client.erase_for_app(event)
# The external deletion ledger must outlive the user row. Store a
# replay-capable protected user reference, never routine plaintext analytics.
persist_terminal_erasure_receipt(
    event_id=event.client_erasure_event_id,
    erasure_id=result.erasure_id,
    erased=result.erased,
    identity_deleted=result.identity_deleted,
)
```

`client_erasure_event_id` is created once and reused with the exact immutable
request bytes for every retry. `erase_for_app(...)` performs exactly one
network attempt; the durable host queue owns retry timing and observability.
The SDK never auto-identifies, replaces `expected_link_incarnation_id`, or
creates a replacement deletion event.
`expected_link_incarnation_id=None` must be supplied explicitly and is valid
only when Pack never established a link for that host account; it is not a
fallback for a lost token.

The returned opaque `erasure_id` is the capability for a later explicit,
genuine re-registration ceremony. Retain it outside the deleted user row.
Never turn an erasure retry into registration automatically.

## Privacy operations (platform contract 2.0)

Identify the authenticated host account before creating a privacy mutation:

```python
from pack_companions import (
    AccountLinkState,
    IdentifyRequest,
    MemoryForgetEvent,
)

identified = await client.identify(
    IdentifyRequest(host_user_id="your-server-side-user-id")
)
state = identified.account_link_state
persist_account_link_state_atomically(state)

event = MemoryForgetEvent.create(
    snapshot_user=state.snapshot_user(
        host_user_id="your-server-side-user-id",
        tier="pro",
    ),
    companion_id="puppy",
)

# Persist the complete bytes before sending. A retry restores this exact event;
# it does not mint a replacement UUID or borrow a newer generation.
persist_pending_privacy_event(event.client_privacy_event_id, event.request_bytes)
result = await client.forget_memory(event)

# Concurrent responses may arrive out of order. Persist only the maximum
# generation observed for this same incarnation.
state = state.observe_result(result)
persist_account_link_state_max_only(state)
delete_pending_privacy_event(event.client_privacy_event_id)
```

The seven generation-bound event types are:

- `MemoryExcludeEvent`
- `MemoryForgetEvent`
- `MemoryDeleteChatHistoryEvent`
- `ObservationDeleteEvent`
- `ShareMemoryEvent`
- `PerceptionConsentEvent`
- `LinkStatusEvent`

Every event has immutable canonical `request_bytes`, a non-nil
`client_privacy_event_id`, and `from_request_bytes(...)` for durable recovery.
All seven client methods retry ambiguous transport/502/503/504 failures using
those same exact bytes.

`PrivacyOperationError` exposes only an optional allowlisted machine `code`,
safe `disposition`, optional `operation_completed`, bounded
`retry_after_seconds`, and status code. Unknown error bodies and raw headers
are never reflected:

- `retry_same_event`: preserve the complete event and retry it unchanged after
  the indicated delay or operator reconciliation. `Retry-After` is accepted
  only as validated delta-seconds from 0 through 3,600.
- `discard_event`: the captured incarnation/generation is stale or the UUID was
  reused incorrectly. Never refresh tokens and replay it.
- `start_new_read`: the old receipt is superseded, expired, or unreadable.
  Discard it and obtain current state with a fresh authenticated read before
  creating any new mutation.

Use `AccountLinkState.reconcile_identify(...)` only for a genuinely fresh
identify ceremony. It retains the maximum generation within the same
incarnation and resets generation only when identify proves a different
incarnation. The SDK intentionally keeps no global per-user state.

## Versioning

This SDK follows independent semver from the service.

| SDK version | Minimum service version |
|---|---|
| 0.1.x | Service with snapshot-only `/v1/comment` |
| 0.2.x | Platform contract 2.0 (`/v1/comment` v1 + privacy operations v1) |

A compatibility table will be maintained here as the service stabilizes.

## Status

- `0.2.0` is a locally verified, unpublished platform-contract 2.0 candidate.
- HMAC signing, typed comment decisions, exact privacy-operation retries, and
  durable app-erasure events are implemented.
- General chat/streaming methods are not part of this SDK candidate.
- Publication and production compatibility are not claimed until coordinated
  service and consumer release gates pass.

## License

MIT — see [LICENSE](LICENSE).
