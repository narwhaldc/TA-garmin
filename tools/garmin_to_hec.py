#!/usr/bin/env python3
"""
garmin_to_hec.py — Path B pull poller: Garmin Connect -> Splunk HEC (index=wearables).

The Garmin analog of oura_to_hec_with_phi.py. Resumes the saved garminconnect
session token (created by tools/garmin_probe.py — no password/MFA needed here),
pulls each data type per day, shapes clean events, and POSTs them to HEC with
indexed fields vendor="garmin" + person_id. Duplicates from the overlap re-fetch
are handled by (a) a content-hash dedup store here and (b) the wearables app's
"Wearables Dedup" maintenance saved searches.

  Config (env):
    SPLUNK_HEC_URL      e.g. https://splunk:8088/services/collector
    SPLUNK_HEC_TOKEN    HEC token with access to index=wearables
    GARMIN_PERSON_ID    canonical person_id to stamp (default P001)
    WEARABLES_INDEX     default "wearables"
    GARMIN_TOKENSTORE   default ~/.garminconnect
    GARMIN_STATE        default ~/.garmin_to_hec_state.json  (checkpoint + dedup)
    GARMIN_OVERLAP_DAYS default 3  (re-fetch recent days to catch late syncs)
    SPLUNK_HEC_VERIFY   "0" to skip TLS verify (self-signed)

  Usage:
    python3 garmin_to_hec.py                 # incremental (checkpoint-overlap .. today)
    python3 garmin_to_hec.py --backfill 2026-01-01
    python3 garmin_to_hec.py --date 2026-07-18
    python3 garmin_to_hec.py --status
    python3 garmin_to_hec.py --dry-run       # shape + count, don't send

NOTE: field mappings were written from a schema-only (no-data) probe; verify values
once a real Garmin device syncs. Requires the python that can import garminconnect
(0.3.x needs Python >= 3.10 — may differ from the Oura script's 3.9 interpreter).
"""
import argparse, datetime, hashlib, json, os, sys, time

try:
    import requests
    from garminconnect import Garmin
except ImportError as e:
    sys.exit(f"missing dep ({e.name}): pip install garminconnect curl_cffi requests")

PID          = os.getenv("GARMIN_PERSON_ID", "P001")
INDEX        = os.getenv("WEARABLES_INDEX", "wearables")
TOKENSTORE   = os.path.expanduser(os.getenv("GARMIN_TOKENSTORE", "~/.garminconnect"))
STATE_PATH   = os.path.expanduser(os.getenv("GARMIN_STATE", "~/.garmin_to_hec_state.json"))
OVERLAP_DAYS = int(os.getenv("GARMIN_OVERLAP_DAYS", "3"))
HEC_URL      = os.getenv("SPLUNK_HEC_URL", "")
HEC_TOKEN    = os.getenv("SPLUNK_HEC_TOKEN", "")
HEC_VERIFY   = os.getenv("SPLUNK_HEC_VERIFY", "1") != "0"


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"checkpoint": None, "dedup": {}}

def save_state(st):
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, indent=2)

def midnight_epoch(cal_date):
    return time.mktime(datetime.datetime.strptime(cal_date, "%Y-%m-%d").timetuple())

def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


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
    # sleep score only present on capable devices: dailySleepDTO.sleepScores.overall.value
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

def shape_stress(d, cal):
    if not d or d.get("avgStressLevel") in (None, -1):
        return []
    ev = {k: d.get(k) for k in ("avgStressLevel", "maxStressLevel", "calendarDate")}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:stress", midnight_epoch(cal), ev)]

def shape_spo2(d, cal):
    if not d or d.get("averageSpO2") is None:
        return []
    ev = {k: d.get(k) for k in ("averageSpO2", "lowestSpO2", "latestSpO2", "calendarDate")}
    ev["calendarDate"] = ev.get("calendarDate") or cal
    return [("garmin:pulseox", midnight_epoch(cal), ev)]

def shape_respiration(d, cal):
    if not d or d.get("avgSleepRespirationValue") is None and d.get("avgWakingRespirationValue") is None:
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
        ev = {"activityId": a.get("activityId"),
              "activityName": a.get("activityName"),
              "activityType": (a.get("activityType") or {}).get("typeKey"),
              "startTimeGMT": st, "duration": a.get("duration"),
              "distance": a.get("distance"), "calories": a.get("calories"),
              "averageHR": a.get("averageHR"), "maxHR": a.get("maxHR"),
              "calendarDate": cal}
        out.append(("garmin:activities", t, ev))
    return out

def shape_devices(devs):
    out = []
    for dv in devs or []:
        ev = {"deviceId": dv.get("deviceId") or dv.get("unitId"),
              "productDisplayName": dv.get("productDisplayName"),
              "partNumber": dv.get("partNumber"),
              "softwareVersion": dv.get("currentFirmwareVersion") or dv.get("softwareVersion")}
        out.append(("garmin:devices", None, ev))   # time set at send (now)
    return out


# ---------------------------------------------------------------- pull one day
def pull_day(g, cal):
    """Return list of (sourcetype, time, event) for one calendar date."""
    def safe(fn, *a):
        try: return fn(*a)
        except Exception as e:
            print(f"    [warn] {fn.__name__}: {e.__class__.__name__}")
            return None
    events = []
    events += shape_sleep(safe(g.get_sleep_data, cal), cal)
    events += shape_dailies(safe(g.get_user_summary, cal), cal)
    events += shape_heart_rate(safe(g.get_heart_rates, cal), cal)
    events += shape_stress(safe(g.get_stress_data, cal), cal)
    events += shape_spo2(safe(g.get_spo2_data, cal), cal)
    events += shape_respiration(safe(g.get_respiration_data, cal), cal)
    events += shape_hrv(safe(g.get_hrv_data, cal), cal)
    events += shape_bodycomp(safe(g.get_body_composition, cal), cal)
    events += shape_usermetrics(safe(g.get_max_metrics, cal), safe(g.get_fitnessage_data, cal), cal)
    events += shape_activities(safe(g.get_activities_by_date, cal, cal), cal)
    return events


# ---------------------------------------------------------------- HEC
def hec_send(batch):
    if not HEC_URL or not HEC_TOKEN:
        sys.exit("set SPLUNK_HEC_URL and SPLUNK_HEC_TOKEN")
    body = "".join(json.dumps(e) for e in batch)
    r = requests.post(HEC_URL, data=body,
                      headers={"Authorization": f"Splunk {HEC_TOKEN}"},
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
        d += datetime.timedelta(days=1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", metavar="YYYY-MM-DD")
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    st = load_state()

    if args.status:
        print(f"checkpoint: {st.get('checkpoint')}")
        print(f"person_id : {PID}   index: {INDEX}")
        print(f"dedup keys: {len(st.get('dedup', {}))}")
        return

    today = datetime.date.today()
    if args.date:
        dates = [args.date]
    elif args.backfill:
        dates = list(daterange(datetime.date.fromisoformat(args.backfill), today))
    else:
        cp = st.get("checkpoint")
        start = (datetime.date.fromisoformat(cp) - datetime.timedelta(days=OVERLAP_DAYS)
                 if cp else today - datetime.timedelta(days=OVERLAP_DAYS))
        dates = list(daterange(start, today))

    g = Garmin()
    try:
        g.login(TOKENSTORE)
    except Exception as e:
        sys.exit(f"no saved Garmin session ({e}); run tools/garmin_probe.py first")

    dedup, sent_total, skipped = st.setdefault("dedup", {}), 0, 0
    for cal in dates:
        print(f"[{cal}]")
        evs = pull_day(g, cal)
        # dedup per (sourcetype, date) by content hash; HR grouped as one bucket/day
        buckets = {}
        for stype, t, ev in evs:
            buckets.setdefault(stype, []).append((t, ev))
        batch = []
        for stype, items in buckets.items():
            h = _hash(items)
            key = f"{stype}|{cal}"
            if dedup.get(key) == h:
                skipped += len(items); continue
            for t, ev in items:
                batch.append(to_hec(stype, t, ev))
            dedup[key] = h
            print(f"    {stype}: {len(items)} event(s)")
        if batch and not args.dry_run:
            for i in range(0, len(batch), 200):
                hec_send(batch[i:i+200])
        sent_total += len(batch)

    if not args.dry_run:
        st["checkpoint"] = today.isoformat()
        save_state(st)
    print(f"\n{'(dry-run) ' if args.dry_run else ''}sent {sent_total} events, "
          f"skipped {skipped} unchanged. checkpoint={st.get('checkpoint')}")


if __name__ == "__main__":
    main()
