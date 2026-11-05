#!/usr/bin/env python3
"""
garmin_personal_probe.py — exercise Garmin's official Personal API with YOUR key and report
whether it can replace the unofficial python-garminconnect login in garmin_to_hec.py.

What it answers (the open questions from the 2026-11 research):
  1. Which endpoints answer for your key (200 vs 401/403/404)?  Includes UNWRAPPED
     candidate paths (respiration, devices, VO2max, training readiness ...) that Garmin's
     own client has no wrapper for.
  2. Are the response field names the same ones our shapers already read?  (Per endpoint:
     which expected keys are found anywhere in the JSON, which are missing.)
  3. Is the heart-rate payload per-sample (heartRateValues) or a daily summary?
  4. How far back does history go?   (--history 30,365,730,1500)

Auth: the key comes ONLY from the environment (or a gitignored .env next to this script, or
~/.config/garmin/connect-personal-api-key) — never an argument, never printed:
    export GARMIN_PERSONAL_API_KEY='...'
Create the key yourself at connect.garmin.com -> Settings -> Personal API (shown once).
The key is exchanged for a 24-hour bearer token on every run (no refresh tokens exist).

Output: a one-line status per endpoint on stdout (key NAMES only, no health values) and the
full raw JSON per endpoint in --out (default tools/garmin_personal_samples/, gitignored —
it is personal health data; never commit it).

Stdlib only (urllib) — runs on any Python 3.9+, no pip installs.

Usage:
    python3 garmin_personal_probe.py [--date YYYY-MM-DD] [--history 30,365,730,1500]
                                     [--out DIR] [--only NAME[,NAME]] [--list]
"""
import argparse, datetime, json, os, ssl, sys, urllib.error, urllib.parse, urllib.request

BASE = os.getenv("GARMIN_PERSONAL_API_BASE_URL", "https://personal.connectapi.garmin.com")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
KEY_FILE = os.path.expanduser("~/.config/garmin/connect-personal-api-key")
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "garmin_personal_samples")
TIMEOUT = 30


def load_dotenv():
    """Same tiny .env loader as the other TA fetchers: existing env wins; .env is gitignored."""
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
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except IOError:
        pass


def get_api_key(key_file=None):
    # --key-file wins (lets you test a second person's key without touching tools/.env and
    # without putting the key on a command line / in shell history).
    key = "" if key_file else os.getenv("GARMIN_PERSONAL_API_KEY", "").strip()
    if not key:
        try:
            with open(os.path.expanduser(key_file or KEY_FILE)) as f:
                key = f.read().strip()
        except IOError:
            pass
    if not key:
        sys.exit("No key found. Set GARMIN_PERSONAL_API_KEY (env or tools/.env), or put it in\n"
                 f"  {KEY_FILE}\n"
                 "Create one at connect.garmin.com -> Settings -> Personal API.")
    return key


def _ssl_context():
    """TLS verification stays ON. On macOS behind a TLS-intercepting proxy (Cisco Umbrella/
    Squid) the proxy root CA lives only in the system keychain, not in Python's CA store ->
    CERTIFICATE_VERIFY_FAILED. So (unless SSL_CERT_FILE/--cacert says otherwise) also trust
    the keychain certs — the same trust set curl uses. In-memory only; nothing written."""
    cafile = os.getenv("SSL_CERT_FILE") or None
    ctx = ssl.create_default_context(cafile=cafile)
    if cafile is None and sys.platform == "darwin":
        import subprocess
        for kc in ("/System/Library/Keychains/SystemRootCertificates.keychain",
                   "/Library/Keychains/System.keychain"):
            try:
                pem = subprocess.run(["security", "find-certificate", "-a", "-p", kc],
                                     capture_output=True, text=True, timeout=30).stdout
                if pem.strip():
                    ctx.load_verify_locations(cadata=pem)
            except Exception:
                pass
    return ctx


_OPENER = None


def _open(req):
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()))
    return _OPENER.open(req, timeout=TIMEOUT)


def mint_token(key):
    """POST the ApiKey -> raw JWT string (the body is NOT json). 24h lifetime."""
    req = urllib.request.Request(BASE + TOKEN_PATH, data=b"", method="POST",
                                 headers={"Authorization": "ApiKey " + key, "Accept": "*/*"})
    try:
        with _open(req) as r:
            tok = r.read().decode().strip()
    except urllib.error.HTTPError as e:
        sys.exit(f"token exchange failed: HTTP {e.code} (key wrong, expired or deleted?)")
    except Exception as e:
        sys.exit(f"token exchange failed: {e.__class__.__name__}: {e}")
    if tok.count(".") != 2:
        sys.exit("token exchange returned something that is not a JWT — API shape changed?")
    return tok


def token_info(token):
    """Decode the JWT payload (NO signature check — we only read timing) to learn whether the
    token reveals when the KEY expires. Garmin says a token lives 24h 'or until the key
    expires, whichever first', so exp-iat < 24h means the key dies within that window.
    Prints claim NAMES plus iat/exp/nbf timing; other claim values (user ids) are not shown."""
    import base64
    try:
        seg = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode())
    except Exception as e:
        print("[token] could not decode JWT payload (%s)" % e.__class__.__name__)
        return
    print("[token] claims: %s" % ", ".join(sorted(claims)))
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    iat, exp = claims.get("iat"), claims.get("exp")
    if isinstance(exp, (int, float)):
        print("[token] expires in %.1f h (exp=%s UTC)" % (
            (exp - now) / 3600.0, datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
            .strftime("%Y-%m-%d %H:%M")))
        if isinstance(iat, (int, float)):
            print("[token] lifetime iat->exp = %.1f h  (24.0 = normal; less => key expires sooner)"
                  % ((exp - iat) / 3600.0))
    for k, v in sorted(claims.items()):
        if isinstance(v, (int, float)) and k not in ("iat", "exp", "nbf") and (
                "exp" in k.lower() or "key" in k.lower() or "valid" in k.lower()):
            print("[token] numeric claim %s=%s  <-- possible key-expiry signal" % (k, v))
    print()


def call(token, path, params=None):
    url = BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                               "Accept": "application/json"})
    try:
        with _open(req) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode() or "null"), len(raw)
            except ValueError:
                return r.status, None, len(raw)
    except urllib.error.HTTPError as e:
        return e.code, None, 0
    except Exception as e:
        return 0, "%s: %s" % (e.__class__.__name__, e), 0


def all_keys(obj, acc=None):
    """Every dict key at any depth (names only) — robust to wrapper-shape differences."""
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k)
            all_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj[:50]:
            all_keys(v, acc)
    return acc


def has_data(obj):
    if obj is None:
        return False
    if isinstance(obj, (list, dict)):
        return len(obj) > 0
    return True


# (name, path, params, expected key names our shapers/props read, wrapped-by-Garmin-client?)
# {d} = --date.  Paths marked cand are NOT in Garmin's client; they mirror the internal
# Connect paths python-garminconnect uses and may 403/404 for a personal key.
def endpoints(d):
    P = {"date": d}
    R = {"startDate": d, "endDate": d}
    return [
        ("sleep", "/sleep-service/sleep/dailySleep", P,
         ["dailySleepDTO", "sleepTimeSeconds", "deepSleepSeconds", "lightSleepSeconds",
          "remSleepSeconds", "awakeSleepSeconds", "sleepScores", "sleepStartTimestampGMT",
          "sleepEndTimestampGMT"], True),
        ("sleep_summaries", "/sleep-service/sleep/dailySleepSummariesByDate", R, [], True),
        ("heart_rate", "/wellness-service/wellness/dailyHeartRate", P,
         ["heartRateValues", "restingHeartRate"], True),
        ("hrv", "/hrv-service/hrv/%s" % d, None,
         ["hrvSummary", "lastNightAvg", "lastNight5MinHigh", "weeklyAvg", "status"], True),
        ("daily_summaries", "/usersummary-service/v2/dailysummaries", R,
         ["totalSteps", "totalDistanceMeters", "activeKilocalories", "bmrKilocalories",
          "moderateIntensityMinutes", "vigorousIntensityMinutes", "restingHeartRate",
          "averageStressLevel", "bodyBatteryMostRecentValue", "dailyStepGoal"], True),
        ("stress_range", "/usersummary-service/stats/stress/daily/%s/%s" % (d, d), None,
         ["avgStressLevel", "maxStressLevel"], True),
        ("body_battery", "/wellness-service/wellness/bodyBattery/reports/daily", R,
         ["bodyBatteryValuesArray"], True),
        ("spo2", "/wellness-service/wellness/daily/spo2acclimation/%s" % d, None,
         ["averageSpO2", "lowestSpO2", "latestSpO2"], True),
        ("weight", "/weight-service/weight/range/%s/%s" % (d, d), {"includeAll": "true"},
         ["weight", "bmi", "bodyFat", "bodyWater", "muscleMass", "boneMass"], True),
        ("fitness_age", "/fitnessage-service/fitnessage/v2/%s" % d, None,
         ["chronologicalAge", "fitnessAge", "achievableFitnessAge"], True),
        ("activities", "/activitylist-service/activities/search/activities",
         {"start": 0, "limit": 5, "startDate": d, "endDate": d},
         ["activityId", "activityName", "activityType", "startTimeGMT", "duration",
          "distance", "calories", "averageHR", "maxHR"], True),
        # ---- candidates: no wrapper in Garmin's client ----
        ("respiration (cand)", "/wellness-service/wellness/daily/respiration/%s" % d, None,
         ["avgSleepRespirationValue", "avgWakingRespirationValue", "lowestRespirationValue",
          "highestRespirationValue"], False),
        ("stress_day (cand)", "/wellness-service/wellness/dailyStress/%s" % d, None,
         ["avgStressLevel", "maxStressLevel", "stressValuesArray"], False),
        ("spo2_day (cand)", "/wellness-service/wellness/daily/spo2/%s" % d, None,
         ["averageSpO2", "lowestSpO2", "latestSpO2"], False),
        ("usersummary_day (cand)", "/usersummary-service/usersummary/daily", {"calendarDate": d},
         ["totalSteps", "activeKilocalories", "restingHeartRate"], False),
        ("devices (cand)", "/device-service/deviceregistration/devices", None,
         ["deviceId", "productDisplayName", "currentFirmwareVersion", "partNumber"], False),
        ("max_metrics (cand)", "/metrics-service/metrics/maxmet/daily/%s/%s" % (d, d), None,
         ["vo2MaxValue", "generic"], False),
        ("training_readiness (cand)", "/metrics-service/metrics/trainingreadiness/%s" % d, None,
         ["score", "level"], False),
        ("intensity_minutes (cand)", "/wellness-service/wellness/daily/im/%s" % d, None, [], False),
        ("bodycomp_range (cand)", "/weight-service/weight/dateRange", R,
         ["totalAverage", "weight", "bodyFat"], False),
    ]


def report(name, status, body, size, expected, wrapped):
    tag = "" if wrapped else "  [unwrapped]"
    if status != 200:
        why = {401: "unauthorized", 403: "forbidden (scope?)", 404: "not found",
               429: "rate limited"}.get(status, "")
        extra = body if isinstance(body, str) else ""
        print("[%3s] %-26s %s %s%s" % (status or "ERR", name, why, extra, tag))
        return
    if not has_data(body):
        print("[200] %-26s EMPTY (%dB) — answered but no data for this date%s" % (name, size, tag))
        return
    keys = all_keys(body)
    top = sorted(body.keys())[:25] if isinstance(body, dict) else ["<list of %d>" % len(body)]
    found = [k for k in expected if k in keys]
    miss = [k for k in expected if k not in keys]
    line = "[200] %-26s %dB  top-level: %s" % (name, size, ", ".join(top))
    if expected:
        line += "\n        shaper keys found %d/%d" % (len(found), len(expected))
        if miss:
            line += "  MISSING: " + ", ".join(miss)
    print(line + tag)
    if name == "heart_rate" and isinstance(body, dict):
        hv = body.get("heartRateValues")
        print("        heartRateValues: %s" % (
            "%d samples (per-sample HR available)" % len(hv) if isinstance(hv, list) and hv
            else "absent/empty — NO per-sample HR for this key"))


def history_probe(token, offsets):
    print("\n== History depth (one call per offset; EMPTY = answered but nothing there) ==")
    today = datetime.date.today()
    for n in offsets:
        day = (today - datetime.timedelta(days=n)).isoformat()
        out = []
        for label, path, params in (
            ("sleep", "/sleep-service/sleep/dailySleep", {"date": day}),
            ("dailies", "/usersummary-service/v2/dailysummaries", {"startDate": day, "endDate": day}),
            ("hr", "/wellness-service/wellness/dailyHeartRate", {"date": day}),
        ):
            st, body, size = call(token, path, params)
            if st != 200:
                out.append("%s=HTTP%s" % (label, st or "ERR"))
            elif label == "sleep":
                dto = body.get("dailySleepDTO") if isinstance(body, dict) else None
                out.append("sleep=%s" % ("DATA" if dto and dto.get("sleepTimeSeconds") else "empty"))
            elif label == "hr":
                hv = body.get("heartRateValues") if isinstance(body, dict) else None
                out.append("hr=%s" % ("DATA(%d)" % len(hv) if hv else "empty"))
            else:
                out.append("dailies=%s" % ("DATA" if has_data(body) and "totalSteps" in all_keys(body)
                                           else "empty"))
        print("  %5d days back (%s):  %s" % (n, day, "  ".join(out)))


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--date", default=str(datetime.date.today() - datetime.timedelta(days=1)),
                    help="calendar date YYYY-MM-DD (default: yesterday — pick a day the watch was worn)")
    ap.add_argument("--history", default="", help="comma-separated days-back list, e.g. 30,365,730,1500")
    ap.add_argument("--out", default=DEFAULT_OUT, help="dir for raw JSON dumps (gitignored)")
    ap.add_argument("--only", default="", help="comma-separated endpoint names (substring match)")
    ap.add_argument("--key-file", metavar="PATH",
                    help="read the key from this file (one line) instead of GARMIN_PERSONAL_API_KEY — "
                         "e.g. a second person's key; chmod 600 it, keep it outside the repo")
    ap.add_argument("--token-only", action="store_true",
                    help="mint a token, print its timing/claim names (key-expiry signals), and exit")
    ap.add_argument("--list", action="store_true", help="list endpoint names and exit (no key needed)")
    a = ap.parse_args()

    eps = endpoints(a.date)
    if a.list:
        for name, path, _, _, wrapped in eps:
            print("%-26s %s%s" % (name, path, "" if wrapped else "   [unwrapped candidate]"))
        return
    if a.only:
        wanted = [w.strip() for w in a.only.split(",") if w.strip()]
        eps = [e for e in eps if any(w in e[0] for w in wanted)]

    token = mint_token(get_api_key(a.key_file))
    print("[auth] key accepted, bearer token minted (24h). date=%s" % a.date)
    token_info(token)
    if a.token_only:
        return
    os.makedirs(a.out, exist_ok=True)
    try:
        os.chmod(a.out, 0o700)
    except OSError:
        pass

    ok = 0
    for name, path, params, expected, wrapped in eps:
        status, body, size = call(token, path, params)
        report(name, status, body, size, expected, wrapped)
        if status == 200 and has_data(body):
            ok += 1
            fn = os.path.join(a.out, name.replace(" ", "").replace("(", "_").replace(")", "") + ".json")
            with open(fn, "w") as f:
                json.dump(body, f, indent=2)
            os.chmod(fn, 0o600)
    print("\n%d/%d endpoints returned data.  Raw JSON in %s (personal data — do not commit)."
          % (ok, len(eps), a.out))

    if a.history:
        history_probe(token, [int(x) for x in a.history.split(",") if x.strip()])


if __name__ == "__main__":
    main()
