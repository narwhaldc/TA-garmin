# TA-garmin → Splunk — Installation Guide

Setup for the Garmin data pipeline into the Wearables platform.
**App version:** TA-garmin `0.1.10` · **Ingest:** `tools/garmin_to_hec.py` (Path B pull poller)

> Ingest uses **Path B** — the unofficial `python-garminconnect` library logging in with **your
> own** Garmin credentials to pull **your own** data (the official Health API is legal-entity-only
> and suspended, see README §0). **This is against Garmin's API ToS and can break without notice;
> use only for your own data.** Values are pending validation against a real synced device.

---

## Table of Contents
1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Install the Splunk apps](#1-install-the-splunk-apps)
4. [Install the poller + libraries](#2-install-the-poller--libraries)
5. [One-time Garmin auth](#3-one-time-garmin-auth)
6. [HEC target config](#4-hec-target-config-garmin_targetsjson)
7. [Optional: mirror ingest logs to Splunk](#4b-optional-mirror-ingest-logs-to-splunk-ingest-health-dashboard)
8. [Populate the registries](#5-populate-the-registries-kv-store)
9. [First run & backfill](#6-first-run--backfill)
10. [Cron automation](#7-cron-automation)
11. [Verify](#8-verify)
12. [State files](#state-files)
13. [Troubleshooting](#troubleshooting)

---

## Architecture
```
Garmin watch → Garmin Connect (cloud)
                    ↓  (your credentials, saved session token)
        tools/garmin_to_hec.py            (cron; python >= 3.10)
        resume token · pull per day · shape events · HR-explode
        per-target dedup (sent_to) · fcntl lock · checkpoint
                    ↓  multi-target fan-out (garmin_targets.json)
        Splunk HEC → index=wearables, sourcetype=garmin:<type>,
                     indexed fields vendor=garmin + person_id
                    ↓
        TA-garmin (search-time normalization → canonical Wearables model)
        wearables app (data model + dashboards, shared with Oura)
```
The poller runs on a Linux host on your LAN (the same box as the Oura fetcher is fine). Splunk
need not be internet-facing; the poller pushes to HEC. Garmin Connect is the only external call.

## Prerequisites
- **`index=wearables`** exists (Settings → Indexes, or ACS on Cloud).
- **`wearables` app** installed (≥ 0.1.19 — provides the data model incl. the Garmin Daily-root
  fields, the KV registries, and the dashboards).
- A Splunk **HEC token** with access to `index=wearables`.
- *(Optional — for log mirroring, §4b)* a second index **`wearables_log`** for ingest logs, and
  the **same HEC token** granted write access to it too (one token, two indexes).
- A host with **Python ≥ 3.10** (garminconnect 0.3.x needs it — the Oura script's 3.9 will NOT
  import it; they can share a box but not the interpreter).
- Your Garmin Connect **email/password** (+ MFA if enabled).

---

## 1. Install the Splunk apps
Install both `.spl`s (Apps → Install app from file; check "Upgrade" if replacing):
- **`wearables-0_1_19.spl`** (or later) — model + dashboards + KV registries.
- **`TA-garmin-0_1_6.spl`** — Garmin normalization (props/eventtypes/tags).

(`TA-oura` is only needed if you also ingest Oura.) Restart Splunk after install.

## 2. Install the poller + libraries
The poller is repo-only ingest tooling — it is **not** in the `.spl` (keeps the app Splunk-Cloud
vetted). Get both scripts onto your ingest host with a one-liner (no git required — just `curl`):
```bash
base=https://raw.githubusercontent.com/narwhaldc/TA-garmin/main/tools
curl -O $base/garmin_to_hec.py -O $base/garmin_probe.py
```
(Or copy them from a checkout / `wget` the same URLs.)

**Pick your Python first — this is where setups go sideways.** Use a **standard Python 3.10+**
(recommended: gets garminconnect **0.3.x** + the full metric set). macOS: `brew install python@3.11`.
Linux: your distro's `python3.11`/`python3.10`. Python **3.9 also works** but caps at garminconnect
**0.2.x** (fewer metrics; writes a different token format — see step 3).
> **Do NOT use Splunk's bundled Python** (`/opt/splunk/bin/python3`). `pip` will "install" into it,
> but garminconnect's **compiled deps (`curl_cffi`, `pydantic-core`) fail to *load*** in Splunk's
> custom build — you get a confusing "cannot import garminconnect" even though pip reports
> everything satisfied. (It's also wiped by Splunk upgrades.) Use a real system/Homebrew Python
> or a `venv` built from one.

**Install the libs with the *exact* interpreter you'll run the scripts with** — the #1 mistake is
installing into one Python and running with another (bare `python3` may not be the one you think).
Verify, then install, then use that **same** command everywhere below:
```bash
which python3.11 && python3.11 --version                        # confirm your interpreter
python3.11 -m pip install garminconnect curl_cffi requests
```
> Throughout this guide, **`python3.11` means the interpreter you just installed into** — swap in
> `python3.10` (or a venv's `python`) if that's your choice, but keep it consistent.

## 3. One-time Garmin auth
Create the saved session token (so the poller never needs your password/MFA again). Just run the
probe with `--login-only` — it prompts for your email, then your **password with no echo** (via
`getpass`, so it never lands in shell history) and the MFA code, creates the token, and exits
(no sample-file dump):
```bash
python3.11 tools/garmin_probe.py --login-only
```
For an **unattended** first login (e.g. a headless box) you can instead supply creds via env or a
gitignored `tools/.env` (`GARMIN_EMAIL` / `GARMIN_PASSWORD`); interactive use needs neither.
This saves the token to `tools/.garminconnect/` (gitignored; `chmod 700`/`600`) and the poller
resumes from the same store. Override the location with `GARMIN_TOKENSTORE`.
> **Token filename differs by library version:** garminconnect **0.3.x** (Python 3.10+,
> recommended) writes a single **`garmin_tokens.json`**; the older **0.2.x**/garth backend
> (the newest that installs on Python 3.9) writes **`oauth1_token.json` + `oauth2_token.json`**.
> The two formats are **not** interchangeable — if you switch Python/library versions, clear the
> store (`rm -f tools/.garminconnect/*`) and re-auth so the new backend writes its own format.
> Garmin **429-rate-limits the login endpoint** from a repeat IP — always let the poller resume
> the saved token; don't script a fresh login per run.

## 4. HEC target config (`garmin_targets.json`)
Copy the example and fill in your HEC details (same format as `oura_targets.json`):
```bash
cp tools/garmin_targets.example.json tools/garmin_targets.json
```
```json
{
  "targets": {
    "personal": {
      "hec_url":   "https://splunk:8088/services/collector/event",
      "hec_token": "YOUR-HEC-TOKEN",
      "index":     "wearables",
      "person_id": "P001",
      "verify_ssl": false
    }
  }
}
```
Add more named targets to fan out to several Splunk instances. `garmin_targets.json` holds a
token → it is **gitignored; never commit it**. (Quick single-target alternative: skip the file
and set `SPLUNK_HEC_URL`, `SPLUNK_HEC_TOKEN`, `GARMIN_PERSON_ID` env vars.)

> **Index (recommended: `wearables`).** Set each target's `index` here. If you use a different
> index, it must match the **`widx` macro** in the dashboard apps (`wearables` and `oura_health`
> each define `widx` — see their INSTALLs), so the dashboards read the same index your ingest
> writes to. Change it in both places (target `index` + `widx`).

> **Sourcetype — set automatically, not in the targets file.** Unlike `oura_targets.json`,
> `garmin_targets.json` has **no `sourcetype` field**. The poller stamps `sourcetype=garmin:<type>`
> **per event, per data type** (`garmin:sleeps`, `garmin:dailies`, `garmin:heart_rate`,
> `garmin:activities`, `garmin:pulseox`, `garmin:stress`, `garmin:respiration`, `garmin:hrv`,
> `garmin:bodycomp`, `garmin:usermetrics`, `garmin:devices`). **Do not add a per-target
> sourcetype** — TA-garmin's field normalization + tags key on these exact sourcetypes, so an
> override would silently break the canonical mappings. (Oura's per-target `sourcetype` is a
> vestigial, ignored field; this file just omits it.) `vendor` (`garmin`) and `person_id` are
> stamped as **indexed** HEC fields per target for RBAC.

## 4b. Optional: mirror ingest logs to Splunk (Ingest Health dashboard)
The poller always writes **logfmt** logs to **stderr** (`<ts> level=… comp=garmin msg="…" …`) — the
cron redirect (`>> /var/log/garmin_sync.log`, below) captures them. To also **mirror those logs into
Splunk** so the wearables **Ingest Health** dashboard can show real success/failure/duration (not just
"had new data"), add a top-level `logging` block to `garmin_targets.json`:
```json
"logging": { "method": "hec", "hec_logging_index": "wearables_log" }
```
- With `method: "hec"`, logs go to **each target's own HEC** (reusing that target's `hec_url` +
  `hec_token`) into `hec_logging_index`. Fan out to several Splunks → **each gets its own ingest
  logs** (run-level lines everywhere; per-target lines only to that target's Splunk).
- **Create a second index for the logs** — `wearables_log` — separate from the `wearables` data
  index. Splunk retention is **per-index**, so a separate index lets you keep logs ~30 days while
  health data stays for years.
- **The same HEC token must have write access to BOTH indexes** — the data index (`wearables`) and
  `hec_logging_index` (`wearables_log`). One token, two indexes.
- stderr stays on regardless; **remove the block to log to stderr only.** Logs arrive as sourcetype
  `wearables:ingest`. Endpoint overridable per target with `hec_logging_url` / `hec_logging_token`.
- **Per-person RBAC on the log index:** `person_id` is stamped as an **indexed field** on each per-target log line (`sent events` / `send failed`), and on run-level lines (`run started` / `run complete`) **only when the run is a single person**. So a person-scoped `srchFilter` on `wearables_log` shows a self-manager their own ingest health (including run start/stop/duration), while multi-person aggregator runs keep run-level lines admin-only (the aggregate `events=N` total is not leaked to individuals). To scope logs by person, add `wearables_log` to the wearables role's `srchFilter` (same person_id key as the data index).

## 5. Populate the registries (KV Store)
In the **wearables** app, open **Admin → People & Defaults** to add/edit the person (person_id,
display name, default units, goals/height, and optionally the mapped Splunk login). The page is
admin-only (KV writes are admin/sc_admin-locked); blank fields keep existing values. Equivalent
raw SPL if you prefer:
```
| makeresults | eval person_id="P001", person_name="Tony", step_goal=10000
| table person_id person_name step_goal | outputlookup wearable_person_profile
```
The device row is derived from your data after the first pull — populate it once Garmin events
land: `index=wearables sourcetype=garmin:devices | head 1 | eval device_id="garmin:".deviceId,
device_name=productDisplayName, vendor="garmin", person_id="P001" | table device_id device_name
vendor person_id | outputlookup wearable_device_profile`.

## Test the pipeline without a device
No Garmin device yet? Send a synthetic "one of each" dataset (timestamped ~now) through the
whole pipeline to prove HEC → normalization → model → dashboards work:
```bash
python3.11 tools/garmin_to_hec.py --generate-sample-data            # sends to all targets
python3.11 tools/garmin_to_hec.py --generate-sample-data --dry-run  # preview, no send
```
Every event is tagged **`synthetic="true"`** (no Garmin login, no checkpoint/dedup writes — safe
to re-run). Requires `TA-garmin` + `wearables` installed so the props/tags/model fire. It proves
the plumbing and mappings — **not** that the real Garmin field *names* are correct (that needs
real data). **Clean up when done** (admin, needs `can_delete`; **set the time range to All
time** — the *search* honors the time picker and only the events it returns reach `| delete`, so a narrow range finds nothing to delete):
```
index=wearables synthetic="true" | delete
```
> Heads-up: synthetic events are timestamped *now* and carry Oura-shaped markers
> (`sleep_type=long_sleep`, a sleep score), so while present they can win the "most recent
> record" in capability panels (hypnogram, contributors) and blank them. Delete them before
> judging those panels against real data.

## 6. First run & backfill
```bash
python3.11 tools/garmin_to_hec.py --dry-run --date 2026-07-18   # shape + count, no send
python3.11 tools/garmin_to_hec.py --backfill 2026-01-01          # history -> HEC
python3.11 tools/garmin_to_hec.py --backfill 2026-01-01 --backfill-end 2026-03-31   # bounded range
python3.11 tools/garmin_to_hec.py                                # incremental (checkpoint - overlap .. today)
```
`--backfill-end` matters more here than for the other TAs: Garmin's fetcher pulls **one day at a
time** (not a single ranged query), so a long `--backfill` means many sequential API calls, and
Garmin's login/data endpoints are rate-limited with no documented recovery window. Splitting a
large backfill into a few smaller `--backfill`/`--backfill-end` chunks — run on separate days if
you can — is meaningfully safer than one open-ended request. The checkpoint correctly advances to
the *later* of its existing value vs. this run's actual end (never backward), so a bounded chunk
never creates a gap for the next incremental run to miss.

## 7. Cron automation
Run a few times a day (Garmin syncs when the app opens). Edit your crontab with `crontab -e` and add
one line. No `cd` needed — the script finds its own state files (targets/checkpoint/dedup/token)
next to itself regardless of the working directory. **Replace the two ALLCAPS placeholders with your
real paths** — they are NOT literal:
- `YOUR_PYTHON311` = the interpreter from step 2 (run `which python3.11` to get it).
- `YOUR_TOOLS_DIR` = where you put the scripts in step 2 (run `pwd` there to get it).
```cron
15 * * * * YOUR_PYTHON311 YOUR_TOOLS_DIR/garmin_to_hec.py >> /var/log/garmin_sync.log 2>&1
```
The `fcntl` lock (`garmin_sync.lock`) makes overlapping cron/manual runs safe. Overlap re-fetch
dupes are cleaned by the `wearables` app's "Wearables Dedup" scheduled searches.

## 8. Verify
```
index=wearables vendor=garmin | stats count by sourcetype
index=wearables tag=wearable_activity vendor=garmin | table _time steps active_calories step_goal
python3.11 tools/garmin_to_hec.py --status      # checkpoint + per-target coverage
```
Then open the Today / Sleep / Heart / Activity dashboards — pick your person; Garmin data should
populate the shared metrics (steps, sleep, HR, workouts).

### A metric is missing? Check the WATCH first, not the pipeline
Several Garmin metrics are **off by default on the watch**, so the poller faithfully ingests
nothing because nothing was ever recorded. The pipeline looks healthy (runs succeed, other
sourcetypes populate) — the data simply doesn't exist upstream. Check these before debugging
ingest:

| Missing | Turn on (watch or Garmin Connect) |
|---|---|
| **SpO₂** (`garmin:pulseox`, `spo2_avg`) | **Pulse Ox** — **off by default on many models** to save battery. Walkthrough below. |
| **Body Battery / stress** (`garmin:stress`) | All-day **heart-rate** tracking (both are derived from continuous HR). |
| **HRV status** (`garmin:hrv`) | Consistent **overnight wear**, on a supported model. |
| Data split across two Garmins | **Physio TrueUp** — *Garmin Connect → Settings → User Settings → Physio TrueUp* — syncs metrics between devices so one watch isn't siloed. |

#### Enabling Pulse Ox (SpO₂) — step by step
Menu wording varies a little by model; this is the common path on modern watches (Forerunner,
Venu, fēnix, vívoactive). If yours differs, search "Pulse Ox" in Garmin's support site for your
exact model.

**On the watch:**
1. Hold the **MENU** button (or press **Start → Settings** on touch-only models).
2. **Settings → Health & Wellness → Pulse Ox** *(older firmware: Settings → Sensors & Accessories
   → Pulse Oximeter)*.
3. Choose a **tracking mode**:
   - **Off** — the default on many models. No SpO₂ is ever recorded.
   - **During Sleep** — ✅ **recommended.** Samples only while you sleep. This is what populates
     `spo2_avg` and costs little battery.
   - **All Day** — continuous spot checks. Richest data, but a **noticeable battery hit** (often
     cited around a day or more of runtime on some models).
4. Optionally enable **Acclimation** if you want altitude-adjusted readings while travelling.

**Or from your phone** (some models only expose it here): *Garmin Connect app → **⋯ More** →
**Garmin Devices** → your watch → **Health & Wellness** (or **Sensors**) → **Pulse Ox** → pick the
mode.* Changes apply on the watch's next sync.

**What to expect afterward:**
- **It is not retroactive.** Garmin can only report what the watch recorded, so `--backfill` will
  **not** fill in SpO₂ for days the setting was off. Data starts from the moment you enable it.
- With **During Sleep**, readings appear after your next full night + sync — not immediately.
- Verify it reached Splunk once a night has passed:
  ```
  index=wearables vendor=garmin sourcetype=garmin:dailies | where isnum(spo2_avg)
  | table _time spo2_avg | sort - _time
  ```
  Still empty after a night's sync? Confirm the mode actually stuck on the watch (a factory reset
  or firmware update can silently revert it to Off).

> **Real example:** a user here had respiration and HRV flowing from their Garmin while SpO₂ stayed
> completely empty — Pulse Ox was simply never enabled. Confirmed in the data: that account's
> `garmin:pulseox` has **zero** events all-time, and SpO₂ appeared in `garmin:dailies` only during a
> two-week window (the period the setting happened to be on) and stopped. The Wellness → Vitals
> glossary in the `wearables` app carries the same caveat for dashboard viewers.

## State files
All live next to the poller (all **gitignored**):
| file | purpose |
|---|---|
| `.garminconnect/` | session token — `garmin_tokens.json` (0.3.x) or `oauth1_token.json`+`oauth2_token.json` (0.2.x/garth); `GARMIN_TOKENSTORE` to relocate |
| `garmin_targets.json` | HEC targets (**tokens** — keep private) |
| `garmin_checkpoint.json` | last-run date |
| `garmin_dedup_store.json` | per-`(sourcetype,date)` hash + `sent_to` targets |
| `garmin_sync.lock` | concurrency lock (auto-released + removed on exit) |

## Troubleshooting
- **`no saved Garmin session`** → run step 3 (`garmin_probe.py`) first.
- **`Username and password are required` / can't resume** → the token store holds a token from a
  *different* library version (e.g. 0.2.x/garth `oauth1_token.json` files while running 0.3.x, or
  vice versa). Clear it and re-auth: `rm -f tools/.garminconnect/* && python3.x ./garmin_probe.py --login-only`.
- **429 on login** → you re-logged in too often; wait ~15–60 min, then rely on the saved token
  (routine runs *resume* and never hit the login endpoint). With creds in `tools/.env`, the poller
  self-heals a stale token on its own.
- **"cannot import garminconnect" even though pip says it's installed** → you're running a
  *different* Python than you installed into, **or** you used **Splunk's bundled Python**
  (`/opt/splunk/bin/python3`), whose custom build can't *load* the compiled deps
  (`curl_cffi`/`pydantic-core`). See the real error with
  `python3.11 -c "from garminconnect import Garmin"`, then install **and** run with the *same*
  standard Python 3.10+ (step 2). `ModuleNotFoundError`/syntax errors on import = same root cause.
- **Nothing in Splunk** → check HEC url/token/index in `garmin_targets.json`; `--dry-run` to see
  shaping; confirm the watch has actually synced that date to Garmin Connect.
- **Re-send a date** → `--reset-dedup` (all) or `--reset-dedup --target NAME` (one target), then
  re-run with `--backfill`/`--date`.
- **Values look wrong / fields empty** → mappings are pending real-device validation; capture a
  sample (`garmin_probe.py --date <day>`) and compare keys to `default/props.conf`.
