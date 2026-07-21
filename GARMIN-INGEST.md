# Garmin ingest — Path A: the push receiver (SHELVED)

> **Not the built path.** This is the *official Health API* push-receiver design, kept for the
> "if we ever get partner/entity approval" case. The project uses **Path B** (unofficial pull
> poller) instead — see [GARMIN-PATHB-POLLER.md](GARMIN-PATHB-POLLER.md). Access reality: README §0.



Oura is pulled by a cron poller; **Garmin pushes**, so the ingest component is a small
always-on HTTPS service that Garmin POSTs to. It is the Garmin analog of
`oura_to_hec_with_phi.py`, but a **server**, not a scheduler.

```
Garmin Health API ──push (per-type callbacks)──▶  TA-garmin receiver  ──HEC──▶  index=wearables
   dailies/sleeps/epochs/activities/                 (public HTTPS)              (vendor=garmin,
   pulseox/respiration/stress/hrv/...                                            person_id, sourcetype=garmin:<type>)
```

## Responsibilities of the receiver
1. **Expose a public HTTPS callback** per data type (or one endpoint that dispatches on
   payload type). Garmin registers these callback URLs per project.
2. **Verify the request** is really from Garmin (signature/token — TBD, see README §6.2).
   Reject anything unverified — treat the payload as untrusted input.
3. **Resolve identity:** each payload carries Garmin `userId` (+ `userAccessToken`). Look up
   `person_id` from **`wearable_identity_map`** (`vendor=garmin, vendor_user_id=<userId>`).
   Drop/park payloads with no mapping (unknown user).
4. **Explode offset-maps** the model needs per-sample:
   `timeOffsetHeartRateSamples` → N `garmin:heart_rate` events (`bpm`, `_time = start+offset`);
   same for SpO2 / respiration / stress / body-battery series where a per-sample view is wanted.
5. **Stamp + forward to HEC:** set `sourcetype=garmin:<type>`, `index=wearables`, and HEC
   indexed `fields:{vendor:"garmin", person_id:"<resolved>"}`. Keep raw JSON in `_raw`.
6. **Dedup / corrections:** Garmin re-sends corrected summaries — key on
   `summaryId` + `updateTimeInSeconds`; mirror the per-target dedup concept from the Oura poller.
7. **Backfill:** expose an admin trigger that calls Garmin's Backfill API for a user/date-range;
   Garmin pushes the history back through the same callbacks.

## Identity & access (unchanged platform guarantees)
- Identity is stamped **at ingest** (here), exactly like Oura — `vendor` + `person_id` as HEC
  indexed fields. This add-on only *reads/normalizes*.
- RBAC stays `authorize.conf` `srchFilter` on the indexed `person_id`. A Garmin device for an
  existing person is **one row** in `wearable_identity_map` (same `person_id`) — no reindex.
- Registry lookups remain KV Store, admin/sc_admin-write-locked, in the `wearables` app.

## Hosting options (decide with partner access)
- **Lambda + API Gateway** — no always-on box, scales, good if Splunk is Cloud.
- **Small Flask/FastAPI service** beside the Splunk host — simplest if self-managed; must be
  reachable from the public internet over HTTPS.

## Security notes
- The receiver handles OAuth tokens (`userAccessToken`) and PII → never log raw tokens; store
  secrets in env/secret manager (same rule as the Oura token). Runs through the same
  **PII/secret bundling scan** before any code is packaged/published.
- Validate + size-limit payloads; the callback is public-facing attack surface.
