# Garmin ingest — Path B: unofficial pull poller (BUILT)

The realistic route for this personal project (official Health API is entity-only + suspended,
see README §0). `tools/garmin_to_hec.py` logs into Garmin Connect with **your own credentials**
and pulls **your own** data — same shape/conventions as the Oura fetcher, different vendor.
**Against Garmin's API ToS and can break without notice; use only for your own data.**

> **Status: BUILT (TA-garmin 0.1.0).** Mappings written from a schema-only probe (test account
> had no synced device); **values pending validation** once a real Garmin syncs. Auth uses
> `python-garminconnect` **0.3.x**, which does its own SSO auth via `curl_cffi` — **no `garth`
> needed** (earlier drafts referenced garth; 0.3.x dropped it).

```
tools/garmin_to_hec.py (cron)  ──resume saved token──▶  Garmin Connect internal API
   per date: sleep, HR, dailies, spo2, stress, respiration, hrv, bodycomp, usermetrics, activities
        │  shape clean events; explode HR map -> 1 bpm/event
        ▼  fan-out to each target in garmin_targets.json (per-target dedup)
   sourcetype=garmin:<type>, vendor=garmin, person_id  ──HEC──▶  index=wearables
```

## On-disk conventions (mirror the Oura fetcher)
| file | purpose |
|---|---|
| `garmin_targets.json` | HEC target(s): `{targets:{name:{hec_url,hec_token,index,person_id,verify_ssl}}}` — multi-target fan-out. Env fallback: `SPLUNK_HEC_URL`/`_TOKEN` + `GARMIN_PERSON_ID` → single `default`. **gitignored (tokens)**; see `garmin_targets.example.json`. |
| `garmin_checkpoint.json` | last-run checkpoint (atomic `.tmp`→replace) |
| `garmin_dedup_store.json` | `"<sourcetype>::<date>" → {hash, date, sent_to[]}` — per-target dedup; adding a target backfills only it |
| `garmin_sync.lock` | `fcntl` lock; refuses concurrent runs (cron + manual) |
| `~/.garminconnect/` | session token — **managed by garminconnect** (its own oauth json files). Unlike Oura we don't hand-write a `garmin_tokens.json`. |

## Auth (garminconnect 0.3.x)
- One-time: `tools/garmin_probe.py` reads `GARMIN_EMAIL`/`GARMIN_PASSWORD` from env, prompts for
  **MFA**, and `Garmin(...).login("~/.garminconnect")` **auto-persists** the token.
- Poller: `Garmin().login("~/.garminconnect")` **resumes** — no password/MFA — until the token
  lapses (long-lived). Garmin **429-rate-limits the login endpoint** from a repeat IP, so the
  poller MUST resume the saved token, never re-login per run.

## Data-type → method → sourcetype (python-garminconnect 0.3.x)
| canonical dataset | method (per date) | sourcetype | tag(s) |
|---|---|---|---|
| Sleep | `get_sleep_data` → `dailySleepDTO.*` | `garmin:sleeps` | wearable_sleep |
| Heart rate | `get_heart_rates` → `heartRateValues [[ts_ms,bpm]]` | `garmin:heart_rate` | wearable_heartrate |
| Daily (activity + wellness) | `get_user_summary` | `garmin:dailies` | wearable_activity + wearable_daily |
| Workouts | `get_activities_by_date` | `garmin:activities` | wearable_workout |
| SpO2 | `get_spo2_data` | `garmin:pulseox` | wearable |
| Stress | `get_stress_data` | `garmin:stress` | wearable |
| Respiration | `get_respiration_data` | `garmin:respiration` | wearable_daily |
| HRV | `get_hrv_data` | `garmin:hrv` | wearable_daily |
| Body composition | `get_body_composition` | `garmin:bodycomp` | wearable_daily |
| VO2max / fitness age | `get_max_metrics` + `get_fitnessage_data` | `garmin:usermetrics` | wearable_daily |
| Device (model/firmware) | `get_devices` | `garmin:devices` | wearable_device |

`garmin:dailies` is the one-per-day summary → carries the daily stress/spo2/body-battery
averages, so intraday `garmin:stress`/`garmin:pulseox` stay `tag=wearable` only (no Daily
double-count). Field mappings live in `default/props.conf`.

## HR explosion
`get_heart_rates().heartRateValues` is `[[epoch_ms, bpm], …]`; the poller emits one event per
pair (`bpm`, `_time = ms/1000`) → the model's one-bpm-per-event HeartRate shape, no offset-map.

## Identity
Single user: `person_id` comes from the target (`garmin_targets.json`), stamped as an INDEXED
HEC field with `vendor="garmin"`. Adding Garmin next to your Oura ring = same `person_id` → they
merge in the model; the Device picker separates them by `device_id`.

## Caveats
1. **Python floor:** garminconnect 0.3.x needs **Python ≥ 3.10** (`str|None` signatures); the
   Oura script's 3.9 interpreter won't import it — run the Garmin poller with a 3.10+ python.
2. **Tolerance:** each `get_*` is wrapped — a failed data type logs a warning and is skipped,
   the run continues.
3. **Overlap dupes:** cleaned by this dedup store + the wearables app's "Wearables Dedup" saved
   searches.
4. **Secrets:** creds, `garmin_targets.json`, and the token dir are gitignored + PII-scanned;
   the poller is repo-only ingest tooling, never in the `.spl`.

## Remaining (when the test device syncs)
Run `garmin_to_hec.py --backfill <a-worn-day>`, MCP-validate each canonical field vs
`index=wearables sourcetype=garmin:*` (verify data-only keys like `dailySleepDTO.sleepScores`,
`hrvSummary.lastNightAvg` that were absent on the null probe day), confirm dashboards light up
for `vendor=garmin`, then decide Garmin-only tiles (README §5 / platform Task).
