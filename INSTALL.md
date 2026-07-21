# TA-garmin → Splunk — Installation Guide

Setup for the Garmin data pipeline into the Wearables platform.
**App version:** TA-garmin `0.1.0` · **Ingest:** `tools/garmin_to_hec.py` (Path B pull poller)

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
7. [Populate the registries](#5-populate-the-registries-kv-store)
8. [First run & backfill](#6-first-run--backfill)
9. [Cron automation](#7-cron-automation)
10. [Verify](#8-verify)
11. [State files](#state-files)
12. [Troubleshooting](#troubleshooting)

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
- A host with **Python ≥ 3.10** (garminconnect 0.3.x needs it — the Oura script's 3.9 will NOT
  import it; they can share a box but not the interpreter).
- Your Garmin Connect **email/password** (+ MFA if enabled).

---

## 1. Install the Splunk apps
Install both `.spl`s (Apps → Install app from file; check "Upgrade" if replacing):
- **`wearables-0_1_19.spl`** (or later) — model + dashboards + KV registries.
- **`TA-garmin-0_1_0.spl`** — Garmin normalization (props/eventtypes/tags).

(`TA-oura` is only needed if you also ingest Oura.) Restart Splunk after install.

## 2. Install the poller + libraries
The poller is repo-only ingest tooling — it is **not** in the `.spl`. Copy `tools/garmin_to_hec.py`
(and `tools/garmin_probe.py`) to your ingest host, then:
```bash
python3.10 -m pip install garminconnect curl_cffi requests
```

## 3. One-time Garmin auth
Create the saved session token (so the poller never needs your password/MFA again):
```bash
export GARMIN_EMAIL='you@example.com'
export GARMIN_PASSWORD='your-garmin-password'
python3 tools/garmin_probe.py            # enter the MFA code when prompted
unset GARMIN_EMAIL GARMIN_PASSWORD       # and clear it from shell history
```
This saves the token to `~/.garminconnect/` and dumps sample payloads (safe to delete).
The poller later does `login("~/.garminconnect")` which **resumes** that token.
> Garmin **429-rate-limits the login endpoint** from a repeat IP — always let the poller resume
> the saved token; don't script a fresh login per run.

## 4. HEC target config (`garmin_targets.json`)
Copy the example and fill in your HEC details (same format as `oura_targets.json`):
```bash
cp garmin_targets.example.json garmin_targets.json
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

> **Sourcetype — set automatically, not in the targets file.** Unlike `oura_targets.json`,
> `garmin_targets.json` has **no `sourcetype` field**. The poller stamps `sourcetype=garmin:<type>`
> **per event, per data type** (`garmin:sleeps`, `garmin:dailies`, `garmin:heart_rate`,
> `garmin:activities`, `garmin:pulseox`, `garmin:stress`, `garmin:respiration`, `garmin:hrv`,
> `garmin:bodycomp`, `garmin:usermetrics`, `garmin:devices`). **Do not add a per-target
> sourcetype** — TA-garmin's field normalization + tags key on these exact sourcetypes, so an
> override would silently break the canonical mappings. (Oura's per-target `sourcetype` is a
> vestigial, ignored field; this file just omits it.) `vendor` (`garmin`) and `person_id` are
> stamped as **indexed** HEC fields per target for RBAC.

## 5. Populate the registries (KV Store)
As a Splunk admin (KV registries live in the `wearables` app; writes are admin/sc_admin-locked):
```
| makeresults | eval person_id="P001", person_name="Tony", step_goal=10000
| table person_id person_name step_goal | outputlookup wearable_person_profile
```
The device row is derived from your data after the first pull — populate it once Garmin events
land: `index=wearables sourcetype=garmin:devices | head 1 | eval device_id="garmin:".deviceId,
device_name=productDisplayName, vendor="garmin", person_id="P001" | table device_id device_name
vendor person_id | outputlookup wearable_device_profile`.

## 6. First run & backfill
```bash
python3 tools/garmin_to_hec.py --dry-run --date 2026-07-18   # shape + count, no send
python3 tools/garmin_to_hec.py --backfill 2026-01-01          # history -> HEC
python3 tools/garmin_to_hec.py                                # incremental (checkpoint - overlap .. today)
```

## 7. Cron automation
Run a few times a day (Garmin syncs when the app opens). Example — hourly, with the 3.10 python:
```cron
15 * * * * cd /opt/garmin && /usr/bin/python3.10 tools/garmin_to_hec.py >> garmin_to_hec.log 2>&1
```
The `fcntl` lock (`garmin_sync.lock`) makes overlapping cron/manual runs safe. Overlap re-fetch
dupes are cleaned by the `wearables` app's "Wearables Dedup" scheduled searches.

## 8. Verify
```
index=wearables vendor=garmin | stats count by sourcetype
index=wearables tag=wearable_activity vendor=garmin | table _time steps active_calories step_goal
python3 tools/garmin_to_hec.py --status      # checkpoint + per-target coverage
```
Then open the Today / Sleep / Heart / Activity dashboards — pick your person; Garmin data should
populate the shared metrics (steps, sleep, HR, workouts).

## State files
All live next to the poller (all **gitignored**):
| file | purpose |
|---|---|
| `~/.garminconnect/` | session token (garminconnect-managed) |
| `garmin_targets.json` | HEC targets (**tokens** — keep private) |
| `garmin_checkpoint.json` | last-run date |
| `garmin_dedup_store.json` | per-`(sourcetype,date)` hash + `sent_to` targets |
| `garmin_sync.lock` | concurrency lock (auto-released on exit) |

## Troubleshooting
- **`no saved Garmin session`** → run step 3 (`garmin_probe.py`) first.
- **429 on login** → you re-logged in too often; wait, then rely on the saved token (`resume`).
- **`ModuleNotFoundError` / syntax error on import** → wrong Python; use **3.10+** for the poller.
- **Nothing in Splunk** → check HEC url/token/index in `garmin_targets.json`; `--dry-run` to see
  shaping; confirm the watch has actually synced that date to Garmin Connect.
- **Re-send a date** → `--reset-dedup` (all) or `--reset-dedup --target NAME` (one target), then
  re-run with `--backfill`/`--date`.
- **Values look wrong / fields empty** → mappings are pending real-device validation; capture a
  sample (`garmin_probe.py --date <day>`) and compare keys to `default/props.conf`.
