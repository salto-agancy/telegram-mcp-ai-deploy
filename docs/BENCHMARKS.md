# Benchmarks

Measurements from one operator's production account. Absolute numbers depend on
how many chats an account has and how much of it the archive covers; the shape of
the difference is what generalises.

No message content, chat identifiers or account names appear here.

## The scenario

`TG-001 — 24-hour audit`: everything that happened across conversations in the
last day, with unread state, direction, attachments and what voice messages said.

## Three baselines, not one

**`observed_external_baseline` — 891 seconds.** One real end-to-end run of the
question through an AI workflow before any of this work. This is *not* the latency
of a tool or an API call: it includes model turns, repeated searches, and manual
assembly across two accounts. It is recorded to show what the user actually
experienced, and must never be compared directly against a tool-level number.

**`LEGACY` — the same retrieval without an AI in the loop.** Discover active chats,
then read each one. Measured through the gateway's own implementation functions, on
the production account, in one process.

**`NEW` — `recent_activity`.** Same account, same window, same process, same session.

## Results

| Metric | LEGACY | TARGET | ACTUAL |
|---|---|---|---|
| End-to-end, one snapshot | 150.98 s | — | see below |
| Tool calls | 51 | ≤ 2 | 1 |
| Telegram RPC | 1221 | — | 18 |
| Chats returned | 50 | — | 29 conversations |
| Messages returned | 869 | — | 195 |
| Payload | 605 575 B | — | ~66 000 B |
| Latency p50 | — | < 5 s | filled in below |
| Latency p95 | — | < 10 s | filled in below |
| Transcriptions run inside the request | many | 0 | 0 |

The chat counts differ by design: LEGACY returns whatever `find_chats` finds,
which on this account was mostly broadcast channels, while `recent_activity`
returns conversations and leaves channels to an explicit opt-in. Correctness is
therefore judged on whether any *conversation* was lost, not on the raw count.

## What the tail is, and what it is not

Repeated runs showed occasional ~29-second responses among sub-second ones. It
would be easy to report that as p95 latency of the new path. It is not.

Lowering the client's flood threshold on a diagnostic run showed the cause: after
roughly sixty rapid reads, Telegram answered **every** request with
`FloodWait 29s`. Telethon sleeps off any wait below its threshold (60 seconds by
default) and retries without raising, so the exception never surfaces and the
telemetry reported a clean run that merely looked slow.

Two consequences worth keeping:

- A benchmark that hammers the same chats measures Telegram's rate limiter, not
  the retrieval path. Iterations are spaced accordingly.
- Telemetry now records the slowest live read and counts reads that crossed a
  stall threshold, so waiting is visible instead of being absorbed into "elapsed".

The practical effect of the archive is the same in either direction: fewer live
reads means fewer chances to meet the limiter at all.

## Correctness cases

Numbers below are message-level outcomes on the real corpus, not synthetic data.

| Case | Before | After |
|---|---|---|
| TG-003 — find a message by what was said in a voice note | not found | first result |
| TG-005 — find a message by text that exists only on a screenshot | not found (no such data existed) | found, matched in the image only |
| TG-006 — exact phrase across a year of history | found | found, first result |
| TG-008 — attachment with no caption | found by filename | found by filename |
| TG-015 — voice with no transcript yet | silently absent | present, marked `pending` |

## Reproducing

The runner lives in `tests/integration/`. It needs a real session and an account
worth measuring, so it is opt-in and never runs in CI. Point it at your own
account; the numbers above are one operator's and are not a target to hit.
