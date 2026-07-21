# Garmin ingest — Path B: unofficial pull poller (personal use)

The realistic route for this personal project (official Health API is entity-only + suspended,
see README §0). A scheduled Python **poller** logs into Garmin Connect with **your own
credentials** via the `garth` library and pulls **your own** data — the same shape as the Oura
fetcher, just a different vendor. **This is against Garmin's API ToS and can break without
notice; use only for your own data.**

```
garmin_to_hec.py (cron)  ──garth login (your creds)──▶  Garmin Connect internal API
        │  pulls per date: sleep, HR, dailies, stress, body battery, activities, spo2, ...
        ▼
   build events (sourcetype=garmin:<type>, vendor=garmin, person_id)  ──HEC──▶  index=wearables
```

## Reuse, don't reinvent
Mirror `oura_to_hec_with_phi.py`: keep the **HEC send**, the **`wearable_fields()`** stamping
(`vendor` + `person_id` indexed), the **per-target dedup store**, the **`CHECKPOINT_OVERLAP_DAYS`
re-fetch window**, `--backfill YYYY-MM-DD`, and `--status`. Only the auth + the fetch calls are
new. Recommend a **separate `garmin_to_hec.py`** (different auth/endpoints) that imports the
shared HEC/dedup helpers rather than bolting onto the Oura script.

## Auth (garth) — do this carefully
- First run: `garth.login(email, password)` → handles Garmin SSO; **prompts for MFA OTP** if
  enabled → exchanges for OAuth Bearer tokens → `garth.save("~/.garminconnect")`
  (tokens land at `~/.garminconnect/garmin_tokens.json`).
- Every run after: `garth.resume("~/.garminconnect")` — no password, no MFA, until the token
  expires (OAuth2 refresh is long-lived, ~1 yr); re-login when it lapses.
- **Credentials via env** (`GARMIN_EMAIL` / `GARMIN_PASSWORD`), never hardcoded. The token dir
  and any creds are **gitignored + caught by the bundling PII scan**. Never ship in a `.spl`
  (the poller is repo-only ingest tooling, like the Oura fetcher — excluded from the bundle).

## Data-type → library method → sourcetype (via `python-garminconnect`, which wraps garth)
| canonical dataset | `Garmin(...)` method (per date) | sourcetype |
|---|---|---|
| Sleep | `get_sleep_data(date)` | `garmin:sleeps` |
| Heart rate (intraday) | `get_heart_rates(date)` → `heartRateValues` `[[ts_ms, bpm], …]` | `garmin:heart_rate` |
| Daily activity/wellness | `get_stats(date)` / `get_user_summary(date)` (steps, distance, calories, intensity min, **resting HR**, stress avg) | `garmin:dailies` |
| Workouts | `get_activities(start, limit)` | `garmin:activities` |
| Stress / Body Battery | `get_stress(date)` / `get_body_battery(start,end)` | `garmin:stress` |
| SpO2 | `get_spo2(date)` | `garmin:pulseox` |
| Respiration | `get_respiration(date)` | `garmin:respiration` |
| HRV | `get_hrv_data(date)` | `garmin:hrv` |
| Body composition | `get_body_composition(date)` | `garmin:bodycomp` |
| VO2max / fitness age | `get_max_metrics(date)` | `garmin:userMetrics` |

## HR explosion (same requirement as Path A, but easier here)
`get_heart_rates(date).heartRateValues` is already an array `[[epoch_ms, bpm], …]`. The poller
emits **one HEC event per pair** (`bpm`, `_time = epoch_ms/1000`) at `sourcetype=garmin:heart_rate`
— no offset-map math. This gives the model its one-bpm-per-event HeartRate shape directly.

## ⚠ Field names differ from the scaffolded props
The `default/props.conf` in this add-on was written against the **official Health API** field
names (`deepSleepDurationInSeconds`, etc.). The **Connect internal API** (what garth returns)
uses **different keys and nesting** (e.g. `deepSleepSeconds`, `sleepTimeSeconds`, `dailySleepDTO`).
So for Path B, the props **canonical TARGET stays identical**, but every EVAL right-hand-side
must be **re-derived from a real garth response**. Plan: dump one sample of each
`get_*` response, then write the props from those actual keys (MCP-validate each canonical field,
same loop as TA-oura).

## Identity (single user → trivial)
One person (you): stamp `vendor="garmin"`, `person_id="P001"` directly in the poller (like an
Oura target). `wearable_identity_map` isn't needed for a single-user pull, but add a row for
tidiness. Adding Garmin next to your Oura ring = both stamped `person_id="P001"` → they merge
in the model automatically (Device picker separates by `device_id`).

## Caveats to resolve at build time
1. **Python floor:** confirm `garth` / `python-garminconnect`'s minimum Python vs the Splunk
   box's 3.9 — the poller can run wherever the Oura fetcher runs; if garth needs 3.10+, run it
   there (it doesn't have to match the Oura script's 3.9 target unless co-located).
2. **Rate limiting / politeness:** Garmin may throttle; backfill day-by-day with small sleeps.
3. **MFA token lifetime + breakage:** unofficial API can change; keep the poller tolerant
   (log + skip a failed data type, don't crash the whole run).
4. **Secrets:** creds + `garmin_tokens.json` never committed; PII-scan gate applies.

## Build steps (Path B)
1. `pip install garth python-garminconnect`; one-time `login()` (with MFA) → save token; confirm
   `resume()` works headless.
2. Write `garmin_to_hec.py` (reuse Oura HEC/dedup/backfill/status scaffolding); pull one date,
   dump raw responses.
3. Rewrite `default/props.conf` RHS from the real Connect-API keys; MCP-validate each canonical
   field against `index=wearables sourcetype=garmin:*`.
4. Add Daily-root model extensions (body_battery, stress_avg, vo2max, fitness_age, weight_kg,
   bmi, body_fat_pct) — see README §5.
5. Backfill; verify the existing dashboards light up for `person_id=P001, vendor=garmin`, and
   that RBAC/Device-picker behave with two vendors under one person.
