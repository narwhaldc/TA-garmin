# TA-garmin — Garmin Add-on for the Wearables platform (FRAMING / pre-build)

Technology add-on that normalizes raw **Garmin Health API** data into the canonical
**Wearables** data model — the second vendor after `TA-oura`, and the real test of
whether the platform is vendor-neutral.

> **Status: DESIGN FRAME, not built.** Garmin's Health API is partner-gated (approval +
> OAuth consent + commercial licensing), and the developer program has intermittently
> been "on hold." Field-level mappings below are **provisional** — verify against the
> Garmin **Health REST API Specification** and real **sample/backfill payloads** (Garmin's
> dev tools provide both) before shipping. Do not cut 1.0 of anything until this frame is
> validated against live data.

---

## 0. Access reality (2026-07) — READ FIRST, it forks the whole design

The **official** Garmin Health API is effectively **closed to this personal project**:
not self-serve, **legal-entity applicants only** (no personal use), and **new sign-ups are
currently suspended**. A normal Garmin Connect (consumer) account does NOT grant developer
access — it's only useful later as the end-user OAuth account under an approved integration.

So there are two mutually-exclusive ingest paths, and **which one we can use is gated by
access, not preference:**

| | **Path A — Official Health API** (if ever approved as an entity) | **Path B — Unofficial library** (`garth`/`python-garminconnect`, personal use) |
|---|---|---|
| Model | **PUSH** — Garmin POSTs to a callback (see §1, §GARMIN-INGEST) | **PULL** — log in with your own Garmin creds, fetch (like the Oura poller) |
| Ingest component | always-on receiver service | a **poller**, reusing the `oura_to_hec_with_phi.py` pattern |
| Access | partner program, entity-only, suspended | your own account/data; **against Garmin API ToS**, can break, MFA/cred care |
| Effort | high (receiver + OAuth + approval) | low (reuse existing poller + this add-on's props) |

**For the personal project, Path B is the realistic route** — and it makes TA-garmin ingest a
POLLER, not the push receiver. The receiver design (§1, GARMIN-INGEST.md) only applies to
Path A; keep it for the "if we ever get official partner access" case. The **normalization in
this add-on (props/tags below) is the SAME either way** — only the ingest component differs.
Third-party aggregators (Terra/ROOK/Spike) are a paid Path C.

**➡ Path B is designed in [`GARMIN-PATHB-POLLER.md`](GARMIN-PATHB-POLLER.md)** — the pull poller
(`garth` + `python-garminconnect`) reusing the Oura fetcher pattern. Note: Path B returns
**Connect internal API** field names, which differ from the official Health API names used in
`default/props.conf` below — the canonical *targets* are identical, but each EVAL's right-hand
side must be re-derived from a real garth response (see that doc).

## 1. The big difference from Oura: PUSH, not pull

| | Oura (`TA-oura`) | Garmin (`TA-garmin`) |
|---|---|---|
| Delivery | **Pull** — our `oura_to_hec_with_phi.py` polls the REST API on a schedule | **Push** — Garmin POSTs each summary to a **callback URL** when a device syncs (Ping/Pull also offered) |
| Ingest component | a **poller** (cron script) | a **receiver/relay** web service (always-on HTTP endpoint) |
| Auth | single personal access token | **OAuth 2.0** per-user consent; partner approval + licensing |
| Identity | `person_id` from `oura_targets.json`, stamped by the poller | Garmin `userId` per callback → **map to `person_id` via `wearable_identity_map`** (this is finally where that lookup earns its keep) |
| Data shape | one event per reading | per-type callbacks; **HR/SpO2/stress arrive as offset-MAPS inside a daily payload** and must be exploded into per-sample events |
| History | `--backfill YYYY-MM-DD` (poller re-fetches) | **Backfill API** — request a window; Garmin pushes it to your callback |

**Consequence:** the Garmin analog of the Oura poller is a small **receiver service**
(Flask / FastAPI / Lambda / API Gateway) — see [`GARMIN-INGEST.md`](GARMIN-INGEST.md). The
Splunk-side normalization (this add-on) is otherwise the same pattern as `TA-oura`.

---

## 2. Sourcetype scheme

Receiver sets `sourcetype = garmin:<type>` and `index=wearables`, and stamps HEC indexed
fields `vendor="garmin"` + `person_id` (resolved from `userId` via the identity map).

`garmin:dailies` · `garmin:sleeps` · `garmin:epochs` · `garmin:activities` ·
`garmin:heart_rate` (exploded from the dailies HR map) · `garmin:pulseox` ·
`garmin:respiration` · `garmin:stress` (incl. Body Battery) · `garmin:hrv` ·
`garmin:userMetrics` · `garmin:bodycomp`

---

## 3. Canonical field mapping (PROVISIONAL — verify vs partner spec)

Garmin fields are camelCase; durations in **seconds**, distance in **meters**, times as
`startTimeInSeconds` (UTC) + `startTimeOffsetInSeconds`. Our model wants **minutes** and
canonical names — same conversions `TA-oura` already does.

### → Sleep root (`tag=wearable_sleep`, from `garmin:sleeps`)
| canonical | Garmin raw |
|---|---|
| `total_sleep_min` | `durationInSeconds`/60 (or Σ stage durations) |
| `deep_min` / `light_min` / `rem_min` / `awake_min` | `deepSleepDurationInSeconds` / `lightSleepDurationInSeconds` / `remSleepInSeconds` / `awakeDurationInSeconds` (÷60) |
| `sleep_score` | `overallSleepScore.value` (validation-dependent) |
| `spo2_avg` | avg of `timeOffsetSleepSpo2` map |
| `respiration_avg` | avg of `timeOffsetSleepRespiration` map |
| `hrv_avg` | from `garmin:hrv` `lastNightAvg` (separate callback, join by date) |
| `sleep_type` | `"long_sleep"` for main sleep; Garmin naps arrive as separate sleep records |
| `day` | `calendarDate` |
| `avg_hr` / `lowest_hr` | ⚠ not in Garmin sleep summary (HR lives in dailies) → likely null |
| `time_in_bed_min` | ⚠ Garmin has no explicit time-in-bed → null or approximate |

### → HeartRate root (`tag=wearable_heartrate`, from `garmin:heart_rate`)
Garmin embeds HR as `timeOffsetHeartRateSamples` = `{offsetSec: bpm}` **inside the daily
payload**. The **receiver explodes** this into one event per sample:
`bpm` + `_time = startTimeInSeconds + offset`. (Cannot be one-bpm-per-event via props alone.)

### → Activity root (`tag=wearable_activity`, from `garmin:dailies`)
| canonical | Garmin raw |
|---|---|
| `steps` | `steps` |
| `distance_m` | `distanceInMeters` |
| `active_calories` | `activeKilocalories` |
| `total_calories` | `activeKilocalories` + `bmrKilocalories` |
| `active_min` | (`moderateIntensityDurationInSeconds` + `vigorousIntensityDurationInSeconds`)/60 |
| `active_high_min` / `active_medium_min` | `vigorousIntensity…`/60 / `moderateIntensity…`/60 |
| `step_goal` | `stepsGoal` (Garmin device-provided; overrides the profile-lookup fallback) |
| `day` | `calendarDate` |
| `activity_score` | ⚠ Oura-only → **null for Garmin** |
| `active_low_min` / `sedentary_min` / `resting_min` / `calorie_goal` / `distance_goal_m` | ⚠ not in Garmin's model → null |

### → Workout root (`tag=wearable_workout`, from `garmin:activities`)
| canonical | Garmin raw |
|---|---|
| `workout_activity` | `activityType` |
| `workout_start_epoch` | `startTimeInSeconds` |
| `workout_end_epoch` | `startTimeInSeconds` + `durationInSeconds` |
| `workout_calories` | `activeKilocalories` |
| `workout_distance_m` | `distanceInMeters` |
| `workout_id` | `activityId` |

### → Daily root (`tag=wearable_daily`)
| canonical | Garmin raw / source |
|---|---|
| `spo2_avg` | `garmin:pulseox` |
| `respiration_avg` | `garmin:respiration` |
| resting HR | `restingHeartRateInBeatsPerMinute` (in `garmin:dailies`) — Garmin gives it DIRECTLY (Oura we compute it) |
| **new: `body_battery`** | `garmin:stress` `timeOffsetBodyBatteryValues` (Garmin-proprietary 0–100) |
| **new: `stress_avg`** | `garmin:dailies`/`garmin:stress` `averageStressLevel` |
| **new: `vo2max` / `fitness_age`** | `garmin:userMetrics` |
| **new: `weight_kg` / `bmi` / `body_fat_pct`** | `garmin:bodycomp` |
| `readiness_score` / `resilience_level` / `cardio_age` / `pwv` / `temp_deviation` | ⚠ Oura-only → **null for Garmin** |

### → Device root (`tag=wearable_device`)
⚠ **The Garmin Health API does not push device battery or ring-style hardware config.**
So the Device dashboard's battery/charge panels stay **empty for Garmin users** — the
clean, expected outcome of vendor-specific coverage. (A device *name/model* may be derivable
from activity payloads; TBD.)

---

## 4. What this validates about the platform

The mapping proves the model-lens design holds:
- **Shared canonical fields** (sleep stages, steps, calories, HR, workouts, SpO2,
  respiration) — Garmin fills them; dashboards work unchanged.
- **Garmin-only** metrics (Body Battery, stress, VO2 max, fitness age, body composition,
  beat-to-beat HRV) → **model extensions** to add when Garmin lands, exactly like we added
  Oura's readiness/resilience/cardio_age. Null for Oura users.
- **Oura-only** metrics (readiness, resilience, cardio age, ring battery, sleep-time
  guidance) → null for Garmin users.
- Adding a person's 2nd device (their Garmin next to their Oura ring) is **one row in
  `wearable_identity_map`** (same `person_id`) — no reindex, no authorize.conf change. The
  Device picker groundwork (wearables 0.1.13+) already anticipates this.

---

## 5. Model extensions needed for Garmin (do when building, not now)
Add to `Wearables.json` (+ TA-garmin props): `body_battery`, `stress_avg`, `vo2max`,
`fitness_age`, `weight_kg`, `bmi`, `body_fat_pct` on the **Daily** root (all optional/sparse
— null for Oura). Keep them model-lens (canonical fields, never raw-read in dashboards).

---

## 6. Open questions / decisions to resolve with partner access
1. **Receiver hosting** — Lambda+API Gateway vs a small always-on Flask/FastAPI box next to
   the Splunk host? (Push needs a public HTTPS endpoint Garmin can reach.)
2. **Callback verification** — how Garmin authenticates its POSTs to us (signature / token);
   reject spoofed pushes.
3. **HR-map explosion** — do it in the receiver (preferred, keeps model one-bpm-per-event) vs
   store the map and mvexpand at search time (breaks the model). Decision: receiver.
4. **Dedup** — key on `summaryId` + `updateTimeInSeconds` (Garmin re-sends corrected
   summaries); mirror the per-target dedup store concept.
5. **Backfill** — how far back Garmin allows, rate limits, and how to trigger it per user.
6. **Program status/licensing** — confirm the developer program is open + licensing terms
   before investing in the receiver.

---

## 6a. Testing without a real device — Garmin Developer Web Tools
Garmin's Developer Web Tools include **sample data**, **backfill user data**, and
**auto-verification of your integration before production** — i.e. a synthetic-summary
generator that emits payloads in the real shape, so the **receiver + these props can be
built and MCP-validated with NO real Garmin device or live user**. This is the de-risker for
Phase 1 below. Caveats (docs are partner-gated — confirm with Garmin): you still need a
developer-program **account/project** to reach the tools, and whether they're usable *before*
full partner approval vs. only after creating a project isn't publicly documented.

## 7. Build plan (phased, after partner approval)
1. Stand up the **receiver** (echo Garmin pushes → HEC `index=wearables`, stamping
   `vendor=garmin` + `person_id` via identity map; explode HR/SpO2/stress maps). Validate with
   Garmin's **sample data** tool first (no real user needed).
2. Write **`TA-garmin` props/eventtypes/tags** (this scaffold) against real sample payloads;
   validate each canonical field via MCP (same loop as TA-oura).
3. Add the **model extensions** (§5) + surface Garmin-only tiles on Wellness/Activity.
4. Register a real user via OAuth; **backfill**; verify per-person isolation still holds
   (RBAC srchFilter on `person_id`) with two vendors under one person.
5. Only then revisit **1.0** across the platform.

Apache-2.0. Part of the multi-vendor wearables platform (github.com/narwhaldc).
