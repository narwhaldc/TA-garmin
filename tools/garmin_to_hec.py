#!/usr/bin/env python3
"""
garmin_to_hec.py — Path B pull poller: Garmin Connect -> Splunk HEC (index=wearables).

The Garmin analog of oura_to_hec_with_phi.py, and mirrors its on-disk conventions:
a separate checkpoint file, a dedup store keyed "<sourcetype>::<date>" with content
hashes, and an fcntl lock file so a cron run and a manual run can't corrupt state.

Resumes the saved garminconnect session token (created by tools/garmin_probe.py —
no password/MFA needed here), pulls each data type per day, shapes clean events,
and POSTs them to HEC with indexed fields vendor="garmin" + person_id. Overlap
re-fetch dupes are cleaned by (a) this dedup store and (b) the wearables app's
"Wearables Dedup" maintenance saved searches.

  Config (env):
    SPLUNK_HEC_URL          e.g. https://splunk:8088/services/collector
    SPLUNK_HEC_TOKEN        HEC token with access to index=wearables
    SPLUNK_HEC_VERIFY       "0" to skip TLS verify (self-signed)
    GARMIN_PERSON_ID        canonical person_id to stamp (default P001)
    WEARABLES_INDEX         default "wearables"
    GARMIN_TOKENSTORE       garminconnect token store DIR (default ~/.garminconnect)
                            NOTE: garminconnect manages its own oauth json files here;
                            unlike Oura we don't hand-write a garmin_tokens.json.
    GARMIN_CHECKPOINT_FILE  default ./garmin_checkpoint.json
    GARMIN_DEDUP_FILE       default ./garmin_dedup_store.json
    GARMIN_LOCK_FILE        default ./garmin_sync.lock
    GARMIN_OVERLAP_DAYS     default 3  (re-fetch recent days to catch late syncs)

  Usage:
    python3 garmin_to_hec.py                 # incremental (checkpoint-overlap .. today)
    python3 garmin_to_hec.py --backfill 2026-01-01
    python3 garmin_to_hec.py --date 2026-07-18
    python3 garmin_to_hec.py --status
    python3 garmin_to_hec.py --reset-dedup   # re-send everything in the fetch window
    python3 garmin_to_hec.py --dry-run       # shape + count, don't send

NOTE: field mappings were written from a schema-only (no-data) probe; verify values
once a real Garmin device syncs. Requires the python that can import garminconnect
(0.3.x needs Python >= 3.10 — may differ from the Oura script's 3.9 interpreter).
"""
import argparse, datetime, fcntl, hashlib, json, os, sys, time
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
    from garminconnect import Garmin
except ImportError as e:
    sys.exit(f"missing dep ({e.name}): pip install garminconnect curl_cffi requests")

PID          = os.getenv("GARMIN_PERSON_ID", "P001")
INDEX        = os.getenv("WEARABLES_INDEX", "wearables")
TOKENSTORE   = os.path.expanduser(os.getenv("GARMIN_TOKENSTORE", "~/.garminconnect"))
OVERLAP_DAYS = int(os.getenv("GARMIN_OVERLAP_DAYS", "3"))
CHECKPOINT_FILE = Path(os.getenv("GARMIN_CHECKPOINT_FILE", "./garmin_checkpoint.json"))
DEDUP_FILE      = Path(os.getenv("GARMIN_DEDUP_FILE",      "./garmin_dedup_store.json"))
LOCK_FILE       = Path(os.getenv("GARMIN_LOCK_FILE",       "./garmin_sync.lock"))
HEC_URL      = os.getenv("SPLUNK_HEC_URL", "")
HEC_TOKEN    = os.getenv("SPLUNK_HEC_TOKEN", "")
HEC_VERIFY   = os.getenv("SPLUNK_HEC_VERIFY", "1") != "0"


# ---------------------------------------------------------------- state (mirrors Oura)
def load_checkpoint():
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except Exception as e:
            print(f"[warn] could not read checkpoint ({e}); starting fresh")
    return {}

def save_checkpoint(cp):
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cp, indent=2, default=str))
    tmp.replace(CHECKPOINT_FILE)

def load_dedup_store():
    """{"<sourcetype>::<date>": {"hash": "<sha>", "date": "YYYY-MM-DD"}}"""
    if DEDUP_FILE.exists():
        try:
            return json.loads(DEDUP_FILE.read_text())
        except Exception as e:
            print(f"[warn] could not read dedup store ({e}); starting fresh")
    return {}

def save_dedup_store(store):
    tmp = DEDUP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, separators=(",", ":"), default=str))
    tmp.replace(DEDUP_FILE)

def prune_dedup_store(store, max_age_days=None):
    if max_age_days is None:
        max_age_days = OVERLAP_DAYS + 400          # keep ~a year + overlap
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    return {k: v for k, v in store.items() if v.get("date", "") >= cutoff}

def midnight_epoch(cal_date):
    return time.mktime(datetime.datetime.strptime(cal_date, "%Y-%m-%d").timetuple())

def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- shapers
# Each returns a list of (sourcetype, event_time_epoch, event_dict). Defensive:
# returns [] when the day has no data. Values verified against real device TBD.
def shape_sleep(d, cal):
    dto = (d or {}).get("dailySleepDTO") or {}
    if not dto.get("sleepTimeSeconds"):
        return []
    ev = {k: dto.get(k) for k in (
        "sleepTimeSeconds", "deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds",
        "awakeSleepSeconds", "napTimeSeconds", "sleepStartTimestampGMT",
        "sleepEndTimestampGMT", "calendarDate")}
    ev["sleepScore"] = (((dto.get("sleepScores") or {}).get("overall") or {}).get("value"))
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:sleeps", midnight_epoch(cal), ev)]

def shape_dailies(d, cal):
    if not d or not d.get("totalSteps") and not d.get("activeKilocalories"):
        return []
    keep = ("totalSteps", "totalDistanceMeters", "activeKilocalories", "bmrKilocalories",
            "totalKilocalories", "moderateIntensityMinutes", "vigorousIntensityMinutes",
            "dailyStepGoal", "intensityMinutesGoal", "restingHeartRate", "minHeartRate",
            "maxHeartRate", "averageStressLevel", "maxStressLevel", "averageSpo2",
            "lowestSpo2", "bodyBatteryMostRecentValue", "bodyBatteryHighestValue",
            "bodyBatteryLowestValue", "highlyActiveSeconds", "activeSeconds",
            "sedentarySeconds", "sleepingSeconds", "floorsAscended", "calendarDate")
    ev = {k: d.get(k) for k in keep}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:dailies", midnight_epoch(cal), ev)]

def shape_heart_rate(d, cal):
    vals = (d or {}).get("heartRateValues") or []
    out = []
    for pair in vals:                       # [[ts_ms, bpm], ...]
        if not pair or len(pair) < 2 or pair[1] is None:
            continue
        out.append(("garmin:heart_rate", pair[0] / 1000.0,
                    {"bpm": pair[1], "calendarDate": cal}))
    return out

def shape_spo2(d, cal):
    if not d or d.get("averageSpO2") is None:
        return []
    ev = {k: d.get(k) for k in ("averageSpO2", "lowestSpO2", "latestSpO2", "calendarDate")}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:pulseox", midnight_epoch(cal), ev)]

def shape_stress(d, cal):
    if not d or d.get("avgStressLevel") in (None, -1):
        return []
    ev = {k: d.get(k) for k in ("avgStressLevel", "maxStressLevel", "calendarDate")}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:stress", midnight_epoch(cal), ev)]

def shape_respiration(d, cal):
    if not d or (d.get("avgSleepRespirationValue") is None
                 and d.get("avgWakingRespirationValue") is None):
        return []
    ev = {k: d.get(k) for k in ("avgSleepRespirationValue", "avgWakingRespirationValue",
                                "lowestRespirationValue", "highestRespirationValue", "calendarDate")}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:respiration", midnight_epoch(cal), ev)]

def shape_hrv(d, cal):
    summ = (d or {}).get("hrvSummary") or {}
    if not summ.get("lastNightAvg"):
        return []
    ev = {k: summ.get(k) for k in ("lastNightAvg", "lastNight5MinHigh", "weeklyAvg", "status")}
    ev["calendarDate"] = cal
    return [("garmin:hrv", midnight_epoch(cal), ev)]

def shape_bodycomp(d, cal):
    ta = (d or {}).get("totalAverage") or {}
    if ta.get("weight") is None:
        return []
    ev = {k: ta.get(k) for k in ("weight", "bmi", "bodyFat", "bodyWater", "muscleMass",
                                 "boneMass", "visceralFat", "metabolicAge")}
    ev["calendarDate"] = cal
    return [("garmin:bodycomp", midnight_epoch(cal), ev)]

def shape_usermetrics(mx, fa, cal):
    ev = {"calendarDate": cal}
    generic = (mx[0].get("generic") if isinstance(mx, list) and mx else (mx or {}).get("generic")) or {}
    ev["vo2Max"] = generic.get("vo2MaxValue")
    if isinstance(fa, dict):
        ev["chronologicalAge"] = fa.get("chronologicalAge")
        ev["fitnessAge"] = fa.get("achievableFitnessAge") or fa.get("fitnessAge")
    if ev.get("vo2Max") is None and ev.get("fitnessAge") is None:
        return []
    return [("garmin:usermetrics", midnight_epoch(cal), ev)]

def shape_activities(acts, cal):
    out = []
    for a in acts or []:
        st = a.get("startTimeGMT")            # "YYYY-MM-DD HH:MM:SS"
        try:
            t = time.mktime(datetime.datetime.strptime(st, "%Y-%m-%d %H:%M:%S").timetuple())
        except Exception:
            t = midnight_epoch(cal)
        out.append(("garmin:activities", t, {
            "activityId": a.get("activityId"), "activityName": a.get("activityName"),
            "activityType": (a.get("activityType") or {}).get("typeKey"),
            "startTimeGMT": st, "duration": a.get("duration"), "distance": a.get("distance"),
            "calories": a.get("calories"), "averageHR": a.get("averageHR"),
            "maxHR": a.get("maxHR"), "calendarDate": cal}))
    return out


# ---------------------------------------------------------------- pull one day
def pull_day(g, cal):
    def safe(fn, *a):
        try: return fn(*a)
        except Exception as e:
            print(f"    [warn] {fn.__name__}: {e.__class__.__name__}")
            return None
    ev = []
    ev += shape_sleep(safe(g.get_sleep_data, cal), cal)
    ev += shape_dailies(safe(g.get_user_summary, cal), cal)
    ev += shape_heart_rate(safe(g.get_heart_rates, cal), cal)
    ev += shape_spo2(safe(g.get_spo2_data, cal), cal)
    ev += shape_stress(safe(g.get_stress_data, cal), cal)
    ev += shape_respiration(safe(g.get_respiration_data, cal), cal)
    ev += shape_hrv(safe(g.get_hrv_data, cal), cal)
    ev += shape_bodycomp(safe(g.get_body_composition, cal), cal)
    ev += shape_usermetrics(safe(g.get_max_metrics, cal), safe(g.get_fitnessage_data, cal), cal)
    ev += shape_activities(safe(g.get_activities_by_date, cal, cal), cal)
    return ev


# ---------------------------------------------------------------- HEC
def hec_send(batch):
    if not HEC_URL or not HEC_TOKEN:
        sys.exit("set SPLUNK_HEC_URL and SPLUNK_HEC_TOKEN")
    body = "".join(json.dumps(e) for e in batch)
    r = requests.post(HEC_URL, data=body, headers={"Authorization": f"Splunk {HEC_TOKEN}"},
                      verify=HEC_VERIFY, timeout=60)
    r.raise_for_status()

def to_hec(sourcetype, t, ev):
    return {"time": t if t else time.time(), "event": ev, "sourcetype": sourcetype,
            "index": INDEX, "fields": {"vendor": "garmin", "person_id": PID}}


# ---------------------------------------------------------------- main
def daterange(start, end):
    d = start
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", metavar="YYYY-MM-DD")
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset-dedup", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.status:
        cp = load_checkpoint(); store = load_dedup_store()
        print(f"checkpoint : {cp.get('checkpoint')}   (last_run {cp.get('last_run')})")
        print(f"person_id  : {PID}   index: {INDEX}")
        print(f"dedup file : {DEDUP_FILE}  ({len(store)} entries)")
        return

    if args.reset_dedup:
        if DEDUP_FILE.exists(): DEDUP_FILE.unlink()
        print(f"dedup store reset ({DEDUP_FILE})")
        if not (args.backfill or args.date):
            return

    # -- exclusive lock: fcntl.flock auto-releases on process exit/crash (no stale
    #    cleanup). Prevents a cron run + manual run from racing on checkpoint/dedup.
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another instance is already running (lock: {LOCK_FILE}). Exiting.")
    lock_fp.write(str(os.getpid())); lock_fp.flush()

    today = date.today()
    cp = load_checkpoint()
    if args.date:
        dates = [args.date]
    elif args.backfill:
        dates = list(daterange(date.fromisoformat(args.backfill), today))
    else:
        last = cp.get("checkpoint")
        start = (date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS)
                 if last else today - timedelta(days=OVERLAP_DAYS))
        dates = list(daterange(start, today))

    g = Garmin()
    try:
        g.login(TOKENSTORE)                    # resume saved token; no re-login/MFA
    except Exception as e:
        sys.exit(f"no saved Garmin session ({e}); run tools/garmin_probe.py first")

    store = load_dedup_store()
    sent_total, skipped = 0, 0
    for cal in dates:
        print(f"[{cal}]")
        buckets = {}
        for stype, t, ev in pull_day(g, cal):
            buckets.setdefault(stype, []).append((t, ev))
        batch = []
        for stype, items in buckets.items():
            key = f"{stype}::{cal}"
            h = _hash(items)
            if store.get(key, {}).get("hash") == h:
                skipped += len(items); continue
            for t, ev in items:
                batch.append(to_hec(stype, t, ev))
            store[key] = {"hash": h, "date": cal}
            print(f"    {stype}: {len(items)} event(s)")
        if batch and not args.dry_run:
            for i in range(0, len(batch), 200):
                hec_send(batch[i:i + 200])
        sent_total += len(batch)

    if not args.dry_run:
        store = prune_dedup_store(store)
        save_dedup_store(store)
        cp["checkpoint"] = today.isoformat()
        cp["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
        save_checkpoint(cp)
    print(f"\n{'(dry-run) ' if args.dry_run else ''}sent {sent_total} events, "
          f"skipped {skipped} unchanged. checkpoint={cp.get('checkpoint')}")


if __name__ == "__main__":
    main()
