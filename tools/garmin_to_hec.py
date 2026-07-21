#!/usr/bin/env python3
"""
garmin_to_hec.py — Path B pull poller: Garmin Connect -> Splunk HEC (index=wearables).

The Garmin analog of oura_to_hec_with_phi.py, mirroring its on-disk conventions:
  garmin_targets.json     HEC target(s) config (multi-target fan-out; per-target dedup)
  garmin_checkpoint.json  last-run checkpoint (atomic writes)
  garmin_dedup_store.json dedup store, "<sourcetype>::<date>" -> {hash, date, sent_to}
  garmin_sync.lock        fcntl lock so cron + manual runs can't corrupt state

Resumes the saved garminconnect session token (created by tools/garmin_probe.py —
no password/MFA here), pulls each data type per day, shapes clean events, and POSTs
them to each active target with indexed fields vendor="garmin" + person_id. Adding a
new target re-sends only what that target hasn't seen (sent_to tracking). Overlap
re-fetch dupes are also cleaned by the wearables app's "Wearables Dedup" searches.

  garmin_targets.json:
  {
    "targets": {
      "personal": {
        "hec_url":   "https://splunk:8088/services/collector/event",
        "hec_token": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "index":     "wearables",
        "person_id": "P001",
        "verify_ssl": false
      }
    }
  }
  (falls back to env SPLUNK_HEC_URL/SPLUNK_HEC_TOKEN + GARMIN_PERSON_ID as a single
   "default" target for quick single-target use — same as the Oura script.)

  Env (paths/behavior):
    GARMIN_TOKENSTORE       garminconnect token store DIR (default ~/.garminconnect).
                            garminconnect manages its own oauth json files here;
                            unlike Oura we don't hand-write a garmin_tokens.json.
    GARMIN_TARGETS_FILE     default ./garmin_targets.json
    GARMIN_CHECKPOINT_FILE  default ./garmin_checkpoint.json
    GARMIN_DEDUP_FILE       default ./garmin_dedup_store.json
    GARMIN_LOCK_FILE        default ./garmin_sync.lock
    GARMIN_OVERLAP_DAYS     default 3

  Usage:
    python3 garmin_to_hec.py                       # incremental, all targets
    python3 garmin_to_hec.py --target personal     # one target only
    python3 garmin_to_hec.py --backfill 2026-01-01
    python3 garmin_to_hec.py --date 2026-07-18
    python3 garmin_to_hec.py --status              # per-target coverage
    python3 garmin_to_hec.py --reset-dedup [--target NAME]
    python3 garmin_to_hec.py --dry-run

NOTE: field mappings written from a schema-only probe; verify values once a real
Garmin device syncs. Needs the python that can import garminconnect (0.3.x -> >=3.10).
"""
import argparse, collections, datetime, fcntl, hashlib, json, os, sys, time
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
    from garminconnect import Garmin
except ImportError as e:
    sys.exit(f"missing dep ({e.name}): pip install garminconnect curl_cffi requests")

TOKENSTORE   = os.path.expanduser(os.getenv("GARMIN_TOKENSTORE", "~/.garminconnect"))
OVERLAP_DAYS = int(os.getenv("GARMIN_OVERLAP_DAYS", "3"))
TARGETS_FILE    = Path(os.getenv("GARMIN_TARGETS_FILE",    "./garmin_targets.json"))
CHECKPOINT_FILE = Path(os.getenv("GARMIN_CHECKPOINT_FILE", "./garmin_checkpoint.json"))
DEDUP_FILE      = Path(os.getenv("GARMIN_DEDUP_FILE",      "./garmin_dedup_store.json"))
LOCK_FILE       = Path(os.getenv("GARMIN_LOCK_FILE",       "./garmin_sync.lock"))


# ---------------------------------------------------------------- targets (mirrors Oura)
def load_targets(target_filter=None):
    """File first (garmin_targets.json), else env-var single 'default' target."""
    targets = {}
    if TARGETS_FILE.exists():
        try:
            raw = json.loads(TARGETS_FILE.read_text()).get("targets", {})
        except Exception as e:
            sys.exit(f"failed to read {TARGETS_FILE}: {e}")
        for name, cfg in raw.items():
            if not cfg.get("hec_url") or not cfg.get("hec_token"):
                print(f"[warn] target '{name}' missing hec_url/hec_token — skipping"); continue
            if not cfg.get("person_id"):
                print(f"[warn] target '{name}' has no person_id — required for wearables RBAC")
            targets[name] = {
                "hec_url": cfg["hec_url"], "hec_token": cfg["hec_token"],
                "index": cfg.get("index", "wearables"), "vendor": cfg.get("vendor", "garmin"),
                "person_id": cfg.get("person_id"), "verify_ssl": cfg.get("verify_ssl", True)}
    else:
        url, tok = os.getenv("SPLUNK_HEC_URL"), os.getenv("SPLUNK_HEC_TOKEN")
        if url and tok:
            targets["default"] = {
                "hec_url": url, "hec_token": tok,
                "index": os.getenv("WEARABLES_INDEX", "wearables"), "vendor": "garmin",
                "person_id": os.getenv("GARMIN_PERSON_ID", "P001"),
                "verify_ssl": os.getenv("SPLUNK_HEC_VERIFY", "1") != "0"}
    if not targets:
        sys.exit(f"no targets: create {TARGETS_FILE} or set SPLUNK_HEC_URL + SPLUNK_HEC_TOKEN")
    if target_filter:
        if target_filter not in targets:
            sys.exit(f"target '{target_filter}' not found. have: {list(targets)}")
        targets = {target_filter: targets[target_filter]}
    return targets


# ---------------------------------------------------------------- state (mirrors Oura)
def load_json(path, default):
    if path.exists():
        try: return json.loads(path.read_text())
        except Exception as e: print(f"[warn] could not read {path} ({e}); fresh")
    return default

def save_json(path, obj, compact=False):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":") if compact else None,
                             indent=None if compact else 2, default=str))
    tmp.replace(path)

def prune_dedup_store(store, max_age_days=None):
    if max_age_days is None:
        max_age_days = OVERLAP_DAYS + 400
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    return {k: v for k, v in store.items() if v.get("date", "") >= cutoff}

def midnight_epoch(cal):
    return time.mktime(datetime.datetime.strptime(cal, "%Y-%m-%d").timetuple())

def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- shapers
# (sourcetype, event_time_epoch, event_dict); [] when the day has no data.
def shape_sleep(d, cal):
    dto = (d or {}).get("dailySleepDTO") or {}
    if not dto.get("sleepTimeSeconds"): return []
    ev = {k: dto.get(k) for k in ("sleepTimeSeconds", "deepSleepSeconds", "lightSleepSeconds",
          "remSleepSeconds", "awakeSleepSeconds", "napTimeSeconds", "sleepStartTimestampGMT",
          "sleepEndTimestampGMT", "calendarDate")}
    ev["sleepScore"] = (((dto.get("sleepScores") or {}).get("overall") or {}).get("value"))
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:sleeps", midnight_epoch(cal), ev)]

def shape_dailies(d, cal):
    if not d or not d.get("totalSteps") and not d.get("activeKilocalories"): return []
    keep = ("totalSteps", "totalDistanceMeters", "activeKilocalories", "bmrKilocalories",
            "totalKilocalories", "moderateIntensityMinutes", "vigorousIntensityMinutes",
            "dailyStepGoal", "intensityMinutesGoal", "restingHeartRate", "minHeartRate",
            "maxHeartRate", "averageStressLevel", "maxStressLevel", "averageSpo2", "lowestSpo2",
            "bodyBatteryMostRecentValue", "bodyBatteryHighestValue", "bodyBatteryLowestValue",
            "highlyActiveSeconds", "activeSeconds", "sedentarySeconds", "sleepingSeconds",
            "floorsAscended", "calendarDate")
    ev = {k: d.get(k) for k in keep}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:dailies", midnight_epoch(cal), ev)]

def shape_heart_rate(d, cal):
    out = []
    for pair in (d or {}).get("heartRateValues") or []:
        if pair and len(pair) >= 2 and pair[1] is not None:
            out.append(("garmin:heart_rate", pair[0] / 1000.0, {"bpm": pair[1], "calendarDate": cal}))
    return out

def shape_spo2(d, cal):
    if not d or d.get("averageSpO2") is None: return []
    ev = {k: d.get(k) for k in ("averageSpO2", "lowestSpO2", "latestSpO2", "calendarDate")}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:pulseox", midnight_epoch(cal), ev)]

def shape_stress(d, cal):
    if not d or d.get("avgStressLevel") in (None, -1): return []
    ev = {k: d.get(k) for k in ("avgStressLevel", "maxStressLevel", "calendarDate")}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:stress", midnight_epoch(cal), ev)]

def shape_respiration(d, cal):
    if not d or (d.get("avgSleepRespirationValue") is None and d.get("avgWakingRespirationValue") is None):
        return []
    ev = {k: d.get(k) for k in ("avgSleepRespirationValue", "avgWakingRespirationValue",
          "lowestRespirationValue", "highestRespirationValue", "calendarDate")}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:respiration", midnight_epoch(cal), ev)]

def shape_hrv(d, cal):
    summ = (d or {}).get("hrvSummary") or {}
    if not summ.get("lastNightAvg"): return []
    ev = {k: summ.get(k) for k in ("lastNightAvg", "lastNight5MinHigh", "weeklyAvg", "status")}
    ev["calendarDate"] = cal
    return [("garmin:hrv", midnight_epoch(cal), ev)]

def shape_bodycomp(d, cal):
    ta = (d or {}).get("totalAverage") or {}
    if ta.get("weight") is None: return []
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
    if ev.get("vo2Max") is None and ev.get("fitnessAge") is None: return []
    return [("garmin:usermetrics", midnight_epoch(cal), ev)]

def shape_activities(acts, cal):
    out = []
    for a in acts or []:
        st = a.get("startTimeGMT")
        try: t = time.mktime(datetime.datetime.strptime(st, "%Y-%m-%d %H:%M:%S").timetuple())
        except Exception: t = midnight_epoch(cal)
        out.append(("garmin:activities", t, {
            "activityId": a.get("activityId"), "activityName": a.get("activityName"),
            "activityType": (a.get("activityType") or {}).get("typeKey"), "startTimeGMT": st,
            "duration": a.get("duration"), "distance": a.get("distance"),
            "calories": a.get("calories"), "averageHR": a.get("averageHR"),
            "maxHR": a.get("maxHR"), "calendarDate": cal}))
    return out


def pull_day(g, cal):
    def safe(fn, *a):
        try: return fn(*a)
        except Exception as e:
            print(f"    [warn] {fn.__name__}: {e.__class__.__name__}"); return None
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


# ---------------------------------------------------------------- HEC (per target)
def hec_send(target, batch):
    body = "".join(json.dumps(e) for e in batch)
    verify = target.get("verify_ssl", True) if target["hec_url"].startswith("https") else False
    r = requests.post(target["hec_url"], data=body,
                      headers={"Authorization": f"Splunk {target['hec_token']}"},
                      verify=verify, timeout=60)
    r.raise_for_status()

def to_hec(target, sourcetype, t, ev):
    return {"time": t if t else time.time(), "event": ev, "sourcetype": sourcetype,
            "index": target["index"],
            "fields": {"vendor": target.get("vendor", "garmin"), "person_id": target["person_id"]}}


# ---------------------------------------------------------------- status
def print_status(targets):
    store = load_json(DEDUP_FILE, {})
    cp = load_json(CHECKPOINT_FILE, {})
    print(f"checkpoint : {cp.get('checkpoint')}   (last_run {cp.get('last_run')})")
    print(f"targets    : {', '.join(targets)}")
    if not store:
        print(f"dedup store empty/missing ({DEDUP_FILE})"); return
    per_t = collections.Counter()
    for e in store.values():
        for t in e.get("sent_to", []): per_t[t] += 1
    print(f"dedup      : {len(store)} (sourcetype,date) buckets")
    for t in targets:
        print(f"  {t}: {per_t.get(t,0)} buckets delivered")


# ---------------------------------------------------------------- main
def daterange(start, end):
    d = start
    while d <= end:
        yield d.isoformat(); d += timedelta(days=1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target")
    ap.add_argument("--backfill", metavar="YYYY-MM-DD")
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset-dedup", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    targets = load_targets(args.target)

    if args.status:
        print_status(targets); return

    if args.reset_dedup:
        store = load_json(DEDUP_FILE, {})
        if args.target:                      # selective: drop this target from sent_to
            for e in store.values():
                e["sent_to"] = [t for t in e.get("sent_to", []) if t != args.target]
            save_json(DEDUP_FILE, store, compact=True)
            print(f"removed target '{args.target}' from dedup sent_to lists")
        elif DEDUP_FILE.exists():
            DEDUP_FILE.unlink(); print(f"dedup store reset ({DEDUP_FILE})")
        if not (args.backfill or args.date): return

    # exclusive lock (auto-releases on exit/crash)
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another instance is already running (lock: {LOCK_FILE}). Exiting.")
    lock_fp.write(str(os.getpid())); lock_fp.flush()

    today = date.today()
    cp = load_json(CHECKPOINT_FILE, {})
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
        g.login(TOKENSTORE)
    except Exception as e:
        sys.exit(f"no saved Garmin session ({e}); run tools/garmin_probe.py first")

    store = load_json(DEDUP_FILE, {})
    tnames = list(targets)
    sent_total, skipped = 0, 0
    for cal in dates:
        print(f"[{cal}]")
        buckets = {}
        for stype, t, ev in pull_day(g, cal):
            buckets.setdefault(stype, []).append((t, ev))
        for stype, items in buckets.items():
            key = f"{stype}::{cal}"
            h = _hash(items)
            entry = store.get(key)
            changed = not entry or entry.get("hash") != h
            already = set() if changed else set(entry.get("sent_to", []))
            needed = [t for t in tnames if t not in already]
            if not needed:
                skipped += len(items); continue
            succeeded = list(already)
            for tname in needed:
                batch = [to_hec(targets[tname], stype, t, ev) for t, ev in items]
                if not args.dry_run:
                    try:
                        for i in range(0, len(batch), 200):
                            hec_send(targets[tname], batch[i:i + 200])
                    except Exception as e:
                        print(f"    [warn] {stype} -> {tname}: send failed ({e})"); continue
                succeeded.append(tname); sent_total += len(items)
            print(f"    {stype}: {len(items)} event(s) -> {needed}")
            if not args.dry_run:
                store[key] = {"hash": h, "date": cal, "sent_to": sorted(set(succeeded))}

    if not args.dry_run:
        store = prune_dedup_store(store)
        save_json(DEDUP_FILE, store, compact=True)
        cp["checkpoint"] = today.isoformat()
        cp["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
        save_json(CHECKPOINT_FILE, cp)
    print(f"\n{'(dry-run) ' if args.dry_run else ''}sent {sent_total} events, "
          f"skipped {skipped}. checkpoint={cp.get('checkpoint')}")


if __name__ == "__main__":
    main()
