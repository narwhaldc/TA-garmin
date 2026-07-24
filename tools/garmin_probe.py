#!/usr/bin/env python3
"""
garmin_probe.py — one-time login + sample dumper for the TA-garmin build.

Path B (unofficial): logs into Garmin Connect via python-garminconnect (>=0.3.x,
which does its own auth via curl_cffi — no garth needed) and dumps one recent day
of each data type as raw JSON so we can write the canonical field mappings from the
ACTUAL Connect-API keys.

  * Credentials come ONLY from the environment — never hardcoded, never committed:
        export GARMIN_EMAIL='you@example.com'
        export GARMIN_PASSWORD='...'
    (unset them from your shell history afterward.)
  * MFA: you'll be prompted to type the one-time code interactively.
  * A session token is saved to ~/.garminconnect so later runs resume without
    re-login / MFA.
  * Samples are written OUTSIDE the repo (to --out, default the Claude scratchpad)
    because they contain your personal health data + device ids. Do NOT commit them.

Usage:
    python3 garmin_probe.py [--date YYYY-MM-DD] [--out DIR]
"""
import argparse, datetime, getpass, inspect, json, os, sys

try:
    from garminconnect import Garmin
except ImportError:
    sys.exit("python-garminconnect not installed: pip install garminconnect curl_cffi")

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

TOKENSTORE = os.path.expanduser(os.getenv(
    "GARMIN_TOKENSTORE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".garminconnect")))
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "garmin_samples")


def _lock_down_tokens():
    """Best-effort: restrict the persisted token files to owner-only (0600)."""
    for fn in ("oauth1_token.json", "oauth2_token.json"):
        try:
            os.chmod(os.path.join(TOKENSTORE, fn), 0o600)
        except OSError:
            pass


def connect():
    """Resume the saved token if present (no creds needed); otherwise do a fresh
    credential login and persist the token to TOKENSTORE for next time.

    Hardened: the password is read with getpass — no terminal echo and never in
    shell history. GARMIN_EMAIL / GARMIN_PASSWORD are still honored for unattended
    (cron) first-logins, but interactive use needs no env creds at all."""
    os.makedirs(TOKENSTORE, exist_ok=True)
    os.chmod(TOKENSTORE, 0o700)

    # 1) Token-only resume — needs no credentials if a valid token exists.
    try:
        g = Garmin()
        g.login(TOKENSTORE)
        _lock_down_tokens()
        print(f"[auth] resumed saved session (token at {TOKENSTORE})")
        return g
    except Exception:
        pass  # no/expired token -> fall through to a credential login

    # 2) Fresh login. Prefer env creds (unattended); else prompt (password hidden).
    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    pw = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    if not email or not pw:
        sys.exit("Garmin email + password required for the first login.")
    try:
        # Pass prompt_mfa only if this garminconnect version supports it — older
        # 0.2.x releases (all that runs on Python 3.9) don't accept the kwarg.
        gc_kwargs = {"email": email, "password": pw}
        try:
            if "prompt_mfa" in inspect.signature(Garmin.__init__).parameters:
                gc_kwargs["prompt_mfa"] = lambda: input("Garmin MFA code: ").strip()
        except (TypeError, ValueError):
            pass  # can't introspect -> construct without prompt_mfa
        g = Garmin(**gc_kwargs)
        # Fresh credential login, then persist. Newer garminconnect can do both
        # via login(tokenstore) (resume-or-login + auto-save), but older 0.2.x
        # login(tokenstore) is resume-ONLY (FileNotFoundError on an empty store).
        # So log in WITHOUT a tokenstore (fresh) and save via garth.dump — both
        # work on every version.
        g.login()
        try:
            g.garth.dump(TOKENSTORE)
        except Exception:
            pass  # newer versions may already have persisted the tokens
    finally:
        del pw  # drop the plaintext reference as soon as possible
    _lock_down_tokens()
    print(f"[auth] session ready (token at {TOKENSTORE})")
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(datetime.date.today() - datetime.timedelta(days=1)),
                    help="calendar date YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="dir for sample dumps (default: garmin_samples next to this script)")
    ap.add_argument("--login-only", action="store_true",
                    help="only create/verify the saved session token, then exit (no sample dump)")
    args = ap.parse_args()
    if args.login_only:
        connect()
        return
    d = args.date
    os.makedirs(args.out, exist_ok=True)
    g = connect()

    # (label, callable) — each maps toward a garmin:<type> sourcetype later.
    probes = [
        ("sleep",             lambda: g.get_sleep_data(d)),
        ("heart_rate",        lambda: g.get_heart_rates(d)),
        ("user_summary",      lambda: g.get_user_summary(d)),
        ("stats",             lambda: g.get_stats(d)),
        ("stats_and_body",    lambda: g.get_stats_and_body(d)),
        ("rhr_day",           lambda: g.get_rhr_day(d)),
        ("stress",            lambda: g.get_stress_data(d)),
        ("body_battery",      lambda: g.get_body_battery(d, d)),
        ("spo2",              lambda: g.get_spo2_data(d)),
        ("respiration",       lambda: g.get_respiration_data(d)),
        ("hrv",               lambda: g.get_hrv_data(d)),
        ("body_composition",  lambda: g.get_body_composition(d)),
        ("max_metrics",       lambda: g.get_max_metrics(d)),
        ("fitnessage",        lambda: g.get_fitnessage_data(d)),
        ("training_readiness",lambda: g.get_training_readiness(d)),
        ("intensity_minutes", lambda: g.get_intensity_minutes_data(d)),
        ("activities_by_date",lambda: g.get_activities_by_date(d, d)),
        ("devices",           lambda: g.get_devices()),
        ("device_last_used",  lambda: g.get_device_last_used()),
    ]
    ok, fail = [], []
    for name, fn in probes:
        try:
            data = fn()
            path = os.path.join(args.out, f"{name}.json")
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
            n = len(data) if isinstance(data, (list, dict)) else 1
            ok.append(name); print(f"[ok]  {name:20s} -> {path}  ({n} keys/items)")
        except Exception as e:
            fail.append(name); print(f"[skip] {name:20s} : {e.__class__.__name__}: {e}")
    print(f"\nDone. {len(ok)} dumped, {len(fail)} skipped.  Samples in: {args.out}")
    if fail:
        print("skipped:", ", ".join(fail))


if __name__ == "__main__":
    main()
