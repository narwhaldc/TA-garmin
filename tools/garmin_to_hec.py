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
    GARMIN_TOKENSTORE       garminconnect token store DIR (default: .garminconnect
                            next to this script, i.e. tools/.garminconnect — gitignored).
                            garth writes oauth1_token.json (long-lived, ~1yr) +
                            oauth2_token.json (short-lived, auto-refreshed) here.
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
    python3 garmin_to_hec.py --generate-sample-data   # synthetic test data (no device)
    python3 garmin_to_hec.py --dry-run

NOTE: field mappings written from a schema-only probe; verify values once a real
Garmin device syncs. Needs the python that can import garminconnect (0.3.x -> >=3.10).
"""
import argparse, atexit, collections, datetime, fcntl, hashlib, json, os, signal, sys, time
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
    from garminconnect import Garmin
except ImportError as e:
    sys.exit(f"missing dep ({e.name}): pip install garminconnect curl_cffi requests")

def load_dotenv():
    """Populate os.environ from a local .env (KEY=VALUE lines) next to this script,
    if present. Existing environment values win; a leading 'export ' and surrounding
    quotes are stripped. .env is gitignored (it holds credentials) — never commit it."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except IOError:
        pass


load_dotenv()  # pick up creds/config from .env next to this script (gitignored)

TOKENSTORE   = os.path.expanduser(os.getenv(
    "GARMIN_TOKENSTORE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".garminconnect")))
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


def generate_sample_events():
    """Synthetic 'one of each' dataset, timestamped ~now, for testing the pipeline
    end-to-end WITHOUT a real Garmin device. Every event carries synthetic="true"
    so it is trivially deletable and never mistaken for real data. Field NAMES match
    what props expect — this proves the plumbing/mappings, NOT that the real Garmin
    key names are correct (that still needs real data). Values are plausible & fixed
    (no randomness) so runs are reproducible.
    """
    now = time.time()
    cal = datetime.date.today().isoformat()
    S = {"synthetic": "true", "calendarDate": cal}
    ev = []
    ev.append(("garmin:sleeps", now, dict(S, sleepTimeSeconds=25680, deepSleepSeconds=5400,
        lightSleepSeconds=14880, remSleepSeconds=4800, awakeSleepSeconds=600, napTimeSeconds=0,
        sleepScore=82)))
    ev.append(("garmin:dailies", now, dict(S, totalSteps=8432, totalDistanceMeters=6100,
        activeKilocalories=520, bmrKilocalories=1650, totalKilocalories=2170,
        moderateIntensityMinutes=35, vigorousIntensityMinutes=15, dailyStepGoal=10000,
        restingHeartRate=54, minHeartRate=48, maxHeartRate=148, averageStressLevel=38,
        maxStressLevel=82, averageSpo2=96, lowestSpo2=92, bodyBatteryHighestValue=88,
        bodyBatteryLowestValue=22, bodyBatteryMostRecentValue=61)))
    for i, bpm in enumerate([58, 62, 71, 66, 84, 110, 95, 77, 64, 60, 59, 57]):  # last ~2h
        ev.append(("garmin:heart_rate", now - (11 - i) * 600, dict(S, bpm=bpm)))
    ev.append(("garmin:pulseox", now, dict(S, averageSpO2=96, lowestSpO2=92, latestSpO2=97)))
    ev.append(("garmin:stress", now, dict(S, avgStressLevel=38, maxStressLevel=82)))
    ev.append(("garmin:respiration", now, dict(S, avgSleepRespirationValue=14.2,
        avgWakingRespirationValue=16.1, lowestRespirationValue=11.0, highestRespirationValue=20.0)))
    ev.append(("garmin:hrv", now, dict(S, lastNightAvg=42, lastNight5MinHigh=68, weeklyAvg=45,
        status="BALANCED")))
    ev.append(("garmin:bodycomp", now, dict(S, weight=81200, bmi=24.5, bodyFat=18.2,
        bodyWater=55.1, muscleMass=64000, boneMass=3200, visceralFat=8, metabolicAge=52)))
    ev.append(("garmin:usermetrics", now, dict(S, vo2Max=44, chronologicalAge=62, fitnessAge=55)))
    wstart = datetime.datetime.fromtimestamp(now - 2400).strftime("%Y-%m-%d %H:%M:%S")
    ev.append(("garmin:activities", now - 2400, dict(S, activityId="SAMPLE-RUN",
        activityName="Morning Run", activityType="running", startTimeGMT=wstart, duration=2100,
        distance=5200, calories=340, averageHR=142, maxHR=168)))
    ev.append(("garmin:devices", now, dict(S, deviceId="SAMPLE123",
        productDisplayName="Garmin Forerunner 255 (SAMPLE)", softwareVersion="18.26",
        partNumber="006-B3956-00")))
    return ev


def pull_day(g, cal):
    def safe(name, *a):
        # Look the method up by name so a metric that a given garminconnect
        # version doesn't expose is skipped (not an AttributeError crash).
        fn = getattr(g, name, None)
        if fn is None:
            print(f"    [skip] {name}: not available in this garminconnect version")
            return None
        try:
            return fn(*a)
        except Exception as e:
            print(f"    [warn] {name}: {e.__class__.__name__}"); return None
    ev = []
    ev += shape_sleep(safe("get_sleep_data", cal), cal)
    ev += shape_dailies(safe("get_user_summary", cal), cal)
    ev += shape_heart_rate(safe("get_heart_rates", cal), cal)
    ev += shape_spo2(safe("get_spo2_data", cal), cal)
    ev += shape_stress(safe("get_stress_data", cal), cal)
    ev += shape_respiration(safe("get_respiration_data", cal), cal)
    ev += shape_hrv(safe("get_hrv_data", cal), cal)
    ev += shape_bodycomp(safe("get_body_composition", cal), cal)
    ev += shape_usermetrics(safe("get_max_metrics", cal), safe("get_fitnessage_data", cal), cal)
    ev += shape_activities(safe("get_activities_by_date", cal, cal), cal)
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
    ap.add_argument("--generate-sample-data", action="store_true",
                    help="send synthetic 'one of each' events (synthetic=true) to test the "
                         "pipeline without a real device; no Garmin login, no checkpoint/dedup")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--person", metavar="PERSON_ID",
                    help="override person_id from the targets file for all targets "
                         "(testing/demo — e.g. send synthetic data as a different person)")
    args = ap.parse_args()
    targets = load_targets(args.target)
    if args.person:
        for _tcfg in targets.values():
            _tcfg["person_id"] = args.person
        print(f"[override] person_id set to '{args.person}' for all targets (testing)")

    if args.status:
        print_status(targets); return

    if args.generate_sample_data:
        evs = generate_sample_events()
        sent = 0
        for tname, tcfg in targets.items():
            batch = [to_hec(tcfg, st, t, ev) for st, t, ev in evs]
            if not args.dry_run:
                try:
                    for i in range(0, len(batch), 200):
                        hec_send(tcfg, batch[i:i + 200])
                except Exception as e:
                    sys.exit(f"send to '{tname}' failed: {e}")
            sent += len(batch)
            print(f"  {tname}: {len(batch)} synthetic events "
                  f"({len(set(s for s, _, _ in evs))} sourcetypes) -> {tcfg['index']}")
        print(f"\n{'(dry-run) ' if args.dry_run else ''}sent {sent} synthetic events. "
              f"person_id={next(iter(targets.values())).get('person_id')}, all tagged synthetic=\"true\".")
        print('CLEANUP (admin, needs can_delete): index=wearables synthetic="true" | delete')
        return

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

    # exclusive lock: flock auto-releases on exit/crash; we also remove the lock
    # FILE on clean exit (atexit + SIGTERM/SIGINT) so no stale file lingers.
    # Registered only AFTER we hold the lock, so a losing instance can't delete it.
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another instance is already running (lock: {LOCK_FILE}). Exiting.")
    lock_fp.write(str(os.getpid())); lock_fp.flush()

    def _release_lock():
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_fp.close()
        except Exception:
            pass
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass
    atexit.register(_release_lock)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))

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

    # Resume the saved token; if creds are available (env or tools/.env), pass
    # them so login() can self-heal — garminconnect's login(tokenstore) resumes a
    # valid token (no network login, no 429), and only when that fails does it do
    # a fresh login and re-save. With no creds it stays resume-only (old behavior).
    email, pw = os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD")
    g = Garmin(email=email, password=pw) if (email and pw) else Garmin()
    os.makedirs(TOKENSTORE, exist_ok=True)
    os.chmod(TOKENSTORE, 0o700)
    try:
        g.login(TOKENSTORE)
    except Exception as e:
        sys.exit(f"Garmin login failed ({e}); set GARMIN_EMAIL/PASSWORD (env or "
                 f"tools/.env) or run tools/garmin_probe.py")

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
