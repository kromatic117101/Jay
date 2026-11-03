#!/usr/bin/env python3
"""
HI2 Email Checker — Telegram Notify Edition
Usage:
    python hi2check.py                    # interactive
    python hi2check.py john@hi2.in        # single inline
    python hi2check.py emails.txt         # bulk file
"""

import re
import string
import sys
import time
import urllib.parse
from threading import Lock

import requests

# ── Colour layer ──────────────────────────────────────────────────────────
try:
    from colorama import Fore, Style, init as _cinit
    _cinit(autoreset=True)
    G  = Fore.GREEN   + Style.BRIGHT   # available
    R  = Fore.RED     + Style.BRIGHT   # taken
    Y  = Fore.YELLOW  + Style.BRIGHT   # warn / rate-limited
    C  = Fore.CYAN    + Style.BRIGHT   # headers / email labels
    M  = Fore.MAGENTA + Style.BRIGHT   # accent / invalid
    W  = Fore.WHITE   + Style.BRIGHT   # normal bright text
    D  = Style.DIM                     # dim text
    Z  = Style.RESET_ALL               # reset
except ImportError:
    class _Stub:
        def __getattr__(self, _): return ""
    Fore = Style = _Stub()
    G = R = Y = C = M = W = D = Z = ""

# ── Telegram ───────────────────────────────────────────────────────────────
_TG: dict[str, str] = {"token": "", "chat_id": ""}


def tg_send(text: str) -> None:
    if not _TG["token"] or not _TG["chat_id"]:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG['token']}/sendMessage",
            json={"chat_id": _TG["chat_id"], "text": text},
            timeout=8,
        )
    except Exception:
        pass


def setup_telegram() -> None:
    print(f"\n{C}  ╔─ Telegram Notify ────────────────────────╗{Z}")
    tok = input(f"{C}  ║{Z}  Bot Token  : {W}").strip()
    cid = input(f"{C}  ║{Z}  Chat ID    : {W}").strip()
    print(f"{C}  ╚──────────────────────────────────────────╝{Z}")
    if tok and cid:
        _TG["token"]   = tok
        _TG["chat_id"] = cid
        print(f"  {G}✔ Telegram configured — available hits will be forwarded.{Z}")
        tg_send("🟢 HI2 Checker connected.\nAvailable hits will land here.")
    else:
        print(f"  {D}Telegram skipped (left blank).{Z}")


# ── reCAPTCHA machinery ────────────────────────────────────────────────────
ALLOWED_DOMAINS = ("hi2.in", "telegmail.com")

_SITEKEY = "6LfEUPkgAAAAAKTgbMoewQkWBEQhO2VPL4QviKct"
_CO      = "aHR0cHM6Ly9oaTIuaW46NDQz"
_UA      = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_tok_lock  = Lock()
_tok_cache: dict = {"token": None, "ts": 0.0}
_ver_cache: dict = {"version": None, "ts": 0.0}


def _get_version() -> str:
    if _ver_cache["version"] and time.time() - _ver_cache["ts"] < 3600:
        return _ver_cache["version"]
    try:
        r = requests.get(
            "https://www.google.com/recaptcha/api.js",
            headers={"User-Agent": _UA}, timeout=8,
        )
        m = re.search(r"/releases/([\w-]+)/", r.text)
        if m:
            _ver_cache["version"] = m.group(1)
            _ver_cache["ts"] = time.time()
            return m.group(1)
    except Exception as e:
        print(f"  {Y}[warn] version fetch failed: {e}{Z}")
    return "rAqPVhe2JMK6mSJKi8r_vw"


def _get_token() -> str | None:
    with _tok_lock:
        if _tok_cache["token"] and time.time() - _tok_cache["ts"] < 90:
            return _tok_cache["token"]

    version = _get_version()
    cb = "".join(__import__("random").choices(string.ascii_lowercase + string.digits, k=10))

    sess = requests.Session()
    sess.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})

    anchor_url = (
        f"https://www.google.com/recaptcha/api2/anchor"
        f"?ar=1&k={_SITEKEY}&co={_CO}&hl=en&v={version}&size=invisible&cb={cb}"
    )
    try:
        r1 = sess.get(anchor_url, timeout=10)
    except Exception as e:
        print(f"  {Y}[warn] anchor fetch failed: {e}{Z}")
        return None

    challenge = None
    for pat in (r'"rresp","([^"]+)"', r'id="recaptcha-token" value="([^"]+)"'):
        m = re.search(pat, r1.text)
        if m:
            challenge = m.group(1)
            break
    if not challenge:
        print(f"  {Y}[warn] challenge not found (status={r1.status_code}){Z}")
        return None

    reload_url = f"https://www.google.com/recaptcha/api2/reload?k={_SITEKEY}"
    payload = (
        f"v={version}&reason=q&c={urllib.parse.quote(challenge)}"
        f"&k={_SITEKEY}&co={_CO}&hl=en&size=invisible"
        f"&chr=%5B89%2C64%2C27%5D&vh=13599012192&bg=!GgA4oQEAABkEBQAAAA"
    )
    try:
        r2 = sess.post(
            reload_url, data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": anchor_url,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"  {Y}[warn] reload fetch failed: {e}{Z}")
        return None

    for pat in (r'"rresp","([^"]+)"', r'\["rresp","([^"]+)"'):
        m = re.search(pat, r2.text)
        if m:
            tok = m.group(1)
            with _tok_lock:
                _tok_cache["token"] = tok
                _tok_cache["ts"] = time.time()
            return tok

    print(f"  {Y}[warn] token not found in reload response (status={r2.status_code}){Z}")
    return None


def _invalidate_token() -> None:
    with _tok_lock:
        _tok_cache["token"] = None
        _tok_cache["ts"]    = 0.0


def hi2_check(username: str, domain: str) -> str:
    token = _get_token()
    if not token:
        return "error"
    try:
        r = requests.post(
            "https://hi2.in/api/custom",
            data={"domain": domain, "prefix": username, "recaptcha": token},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin":       "https://hi2.in",
                "Referer":      "https://hi2.in/",
                "User-Agent":   _UA,
            },
            timeout=12,
        )
        if r.status_code == 429:
            _invalidate_token()
            return "rate_limited"
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                print(f"  {Y}[warn] 200 but invalid JSON: {r.text[:100]}{Z}")
                return "error"
            if data.get("email", "").lower() == f"{username}@{domain}".lower():
                return "available"
            return "taken"
        if r.status_code == 400:
            try:
                err = r.json().get("error", "").lower()
                if "already taken" in err:
                    return "taken"
                if "recaptcha" in err:
                    _invalidate_token()
                    return "error"
            except Exception:
                pass
            return "taken"
        print(f"  {Y}[warn] unexpected status {r.status_code}: {r.text[:100]}{Z}")
        return "error"
    except Exception as e:
        print(f"  {Y}[warn] request exception: {e}{Z}")
        return "error"


# ── Parsing ────────────────────────────────────────────────────────────────
_EMAIL_RE  = re.compile(r"^[^@\s]+@([^@\s]+)$")
_PREFIX_RE = re.compile(r"^[a-zA-Z0-9._+\-]+$")

STATUS = {
    "available":    f"{G}✅ AVAILABLE{Z}",
    "taken":        f"{R}❌ TAKEN{Z}",
    "error":        f"{Y}⚠️  ERROR{Z}",
    "rate_limited": f"{Y}🕐 RATE LIMITED{Z}",
    "invalid":      f"{M}🚫 INVALID{Z}",
}


def parse_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "@" not in line:
        if _PREFIX_RE.match(line):
            return line.lower(), "hi2.in"
        return None
    m = _EMAIL_RE.match(line)
    if not m:
        return None
    domain = m.group(1).lower()
    if domain not in ALLOWED_DOMAINS:
        return None
    return line.split("@", 1)[0].lower(), domain


# ── Job runner ─────────────────────────────────────────────────────────────
def run_jobs(jobs: list) -> list:
    available = []
    total = len(jobs)
    for i, (username, domain) in enumerate(jobs, 1):
        email  = f"{username}@{domain}"
        prefix = f"{D}[{i}/{total}]{Z} " if total > 1 else "  "
        print(f"{prefix}{C}{email}{Z} ...", end=" ", flush=True)
        result = hi2_check(username, domain)
        print(STATUS.get(result, result))
        if result == "available":
            available.append(email)
            tg_send(f"✅ Available: {email}")
        if result == "rate_limited":
            print(f"  {Y}Rate limited — waiting 5 s...{Z}")
            _invalidate_token()
            time.sleep(5)
    return available


# ── Banner ─────────────────────────────────────────────────────────────────
def banner() -> None:
    print(f"\n{C}  ╔══════════════════════════════════════════╗")
    print(f"  ║  {M}HI2 EMAIL CHECKER{C}  ·  {W}Telegram Edition{C}  ║")
    print(f"  ╚══════════════════════════════════════════╝{Z}")


# ── Modes ──────────────────────────────────────────────────────────────────
def interactive_mode() -> None:
    banner()
    setup_telegram()
    print(f"\n{C}  ╔─ Usage ───────────────────────────────────╗{Z}")
    print(f"{C}  ║{Z}  john  /  ravi@hi2.in  /  t@telegmail.com  {C}║{Z}")
    print(f"{C}  ║{Z}  multiple: separate with comma or space     {C}║{Z}")
    print(f"{C}  ║{Z}  'q' or Ctrl+C to quit                      {C}║{Z}")
    print(f"{C}  ╚═══════════════════════════════════════════╝{Z}")

    session_available: list[str] = []

    while True:
        try:
            raw = input(f"\n{M}›{Z} ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue
        if raw.lower() in ("q", "quit", "exit"):
            break

        parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
        jobs: list = []
        seen: set  = set()
        for part in parts:
            parsed = parse_line(part)
            if not parsed:
                print(f"  {STATUS['invalid']}  {part}")
                continue
            key = f"{parsed[0]}@{parsed[1]}"
            if key in seen:
                continue
            seen.add(key)
            jobs.append(parsed)

        if jobs:
            found = run_jobs(jobs)
            session_available.extend(found)

    if session_available:
        print(f"\n{G}  ╔══ Available this session ({len(session_available)}) ══════════╗{Z}")
        for e in session_available:
            print(f"{G}  ║  {e}{Z}")
        print(f"{G}  ╚══════════════════════════════════════════╝{Z}")
        out = "available.txt"
        open(out, "w").write("\n".join(session_available))
        print(f"  {D}Saved → {out}{Z}")

    print(f"\n{D}Bye.{Z}\n")


def file_mode(path: str) -> None:
    banner()
    setup_telegram()
    try:
        lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
    except FileNotFoundError:
        print(f"\n  {R}File not found: {path}{Z}")
        sys.exit(1)

    jobs: list = []
    seen: set  = set()
    for line in lines:
        parsed = parse_line(line)
        if not parsed:
            continue
        key = f"{parsed[0]}@{parsed[1]}"
        if key in seen:
            continue
        seen.add(key)
        jobs.append(parsed)

    print(f"\n  {D}Loaded {len(jobs)} unique addresses from {path}{Z}\n")
    available = run_jobs(jobs)

    print(f"\n{C}  ─────────────────────────────────────────{Z}")
    print(f"  {G}Available : {len(available)}{Z}  /  {len(jobs)} checked")
    if available:
        print()
        for e in available:
            print(f"  {G}✅ {e}{Z}")
        out = "available.txt"
        open(out, "w").write("\n".join(available))
        print(f"\n  {D}Saved → {out}{Z}")
    print()


def inline_mode(args: list) -> None:
    banner()
    setup_telegram()
    print()
    jobs: list = []
    seen: set  = set()
    for arg in args:
        parsed = parse_line(arg)
        if not parsed:
            print(f"  {STATUS['invalid']}  {arg}")
            continue
        key = f"{parsed[0]}@{parsed[1]}"
        if key not in seen:
            seen.add(key)
            jobs.append(parsed)
    if jobs:
        run_jobs(jobs)
    print()


# ── Entry ──────────────────────────────────────────────────────────────────
def main() -> None:
    args = sys.argv[1:]
    if not args:
        interactive_mode()
    elif len(args) == 1 and args[0].endswith(".txt"):
        file_mode(args[0])
    else:
        inline_mode(args)


if __name__ == "__main__":
    main()
