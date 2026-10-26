# Review — adversarial divergence lens

**Method:** construct pairs of units one level down that obey every AD to the letter yet build incompatibly.

**Verdict: 2 findings, both fixed at Finalize.**

## F1 (high, FIXED) — `token_spend` unit ambiguity

The python SDK and the rust SDK both satisfy AD-8 (`metrics: {token_spend?}`) while reporting incompatible quantities: one reports token count, the other reports USD cost. CAP-3's "token spend" is genuinely ambiguous and the braindump doesn't resolve it. **Fix:** AD-8 rule amended — `token_spend` is total tokens consumed (input + output) as an integer; an optional `cost_usd` field may accompany it. Prometheus counter `candybar_tokens_total` sums token count; cost is derived downstream.

## F2 (high, FIXED) — cron evaluation timezone

The dispatcher (evaluating fires per tick) and the API (computing upcoming `fireTime` for `/schedule`) both use croniter per AD-13, but nothing pinned the timezone: one builds against container-local time, the other against UTC, and every row in the Holocene view disagrees with actual fires by the TZ offset. **Fix:** AD-13 rule amended — all cron evaluation and fire-time computation is in UTC; the tick minute is a UTC minute; local-time rendering is the consumer's job.

## Pairs checked, no hole

- SDK-vs-SDK result-file shape: pinned verbatim in AD-8. Env names pinned. Exit-code fallback pinned.
- Registry writers: AD-10 + AD-2 make the daemon the sole writer; API-role mutations (noise, skips) go through the same registry layer in the same process — no second writer class possible.
- Event producers: all `bloodbank.cron.*` facts are emitted by daemon roles; vocabulary and envelope fields pinned in AD-3 against a locked external contract.
- Dispatcher-vs-CLI double invoke: AD-12 routes manual invokes through the same command subject with a distinct idempotency_key shape (`manual:<uuid>` vs `tick:<tick_id>`); a manual invoke cannot be deduped against a scheduled one, and a scheduled one cannot be skipped by a manual one. Correct.
- Skip application: dispatcher and API both read the same `skips` table through the same registry layer; AD-13 binds both paths to honor skips.
