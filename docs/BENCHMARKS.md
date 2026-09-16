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
| End-to-end p50 | 150.98 s (single run) | < 5 s | **0.855 s** |
| End-to-end p95 | — | < 10 s | **1.459 s** |
| End-to-end min / max | — | — | 0.627 s / 1.459 s |
| Tool calls | 51 | ≤ 2 | **1** |
| Telegram RPC | 1221 | — | **18** |
| Messages returned | 869 | — | 195 |
| Payload | 605 575 B | — | 67 944 B |
| Transcriptions run inside the request | many | 0 | **0** |
| FloodWait events | — | 0 | **0** |
| Errors | — | 0 | **0** |

Six consecutive valid samples, host load average 2.21 at start and 3.99 at end.
Samples taken while the host was loaded are excluded from these figures and
recorded separately — see *Invalid samples* below.

**Where the time goes**, from one representative run (0.834 s end to end):

| Phase | Seconds | What it is |
|---|---|---|
| dialog state | 0.527 | one live pass for unread counters and read markers |
| live top-up | 0.179 | reading the chats the archive does not cover |
| archive | 0.063 | two database queries |
| assembly | 0.065 | merging and serialising |

Roughly 85% of the remaining time is waiting on Telegram, not our own work. That
is the floor for this design: dialog state cannot be served from a snapshot
without making it wrong.

**Where the RPC count comes from**, and why it is not a returning N+1:

    1   get_me
    2   dialog pages (200 dialogs, 100 per page)
    15  chats the archive could not answer for
    ─────
    18

Every call above the fixed three is one chat the archive did not cover. A run
observed at 32 RPC had *29* such chats rather than 15 — the archive had fallen
behind during a host overload — so the extra calls were coverage, not a loop that
crept back in.

**Coverage in that run:** 15 of 29 chats and 172 of 195 messages came from the
archive; 14 chats were topped up live because they had only just entered
collection scope. Fourteen voice transcripts were served ready, two were still
pending, and none were computed during the request.

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

## Invalid samples

Two runs are excluded from the numbers above and kept here instead, because
averaging them in would describe the host rather than the code:

| Observation | Host state | What it was |
|---|---|---|
| one iteration at 190.6 s, 32 RPC | load average 53, 352 MB free | host overloaded by unrelated background jobs; SSH to the box was also timing out |
| occasional ~29 s responses | host healthy | Telegram rate limiting under a burst — see below |

A run is treated as valid only when load average is below 5 and more than 300 MB
of memory is available, recorded before and after each sample.

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
