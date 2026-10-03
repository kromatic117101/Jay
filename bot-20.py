# bot.py
import os
import sys
import json
import time
import shutil
import zipfile
import logging
import threading
import subprocess
import asyncio
import re
import shlex
import socket
import select
import struct
import random
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode

# ═══════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8217841082:AAHkPyeG45I0EikkIwp8rB4qNpquPCoK41k")
OWNER_ID = 8502412097
PASSWORD = "ジェイ"
DOWNLOADS_DIR = "downloads"
LOGS_DIR = "logs"
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PYTHON_ENV = os.path.join(PROJECT_ROOT, ".pythonlibs", "bin", "python")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set.")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
# PROXY SYSTEM
# ═══════════════════════════════════════════════════
PROXY_PORT       = 8889          # local router port
PROXY_TEST_URL   = "http://httpbin.org/ip"
PROXY_TIMEOUT    = 8
PROXY_ROTATE_S   = 30            # health check every 30s

PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
]

# Codespaces / restricted env only allows these outbound ports
ALLOWED_PORTS = {80, 443, 8080, 8888, 3128, 1080, 8118, 9090}

# Pre-verified proxy APIs — already checked, no need to test ourselves
PROXY_APIS = [
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=5000&country=all",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4&timeout=5000&country=all",
    "https://proxylist.geonode.com/api/proxy-list?limit=200&page=1&sort_by=lastChecked&sort_type=desc&protocols=http,https,socks4,socks5",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
]

BAN_SIGNALS = [
    "403 forbidden", "429 too many", "407 proxy",
    "access denied", "banned", "blocked", "captcha",
    "rate limit", "too many requests", "cloudflare",
    "ip banned", "your ip", "abuse", "suspicious",
    "connection refused", "ERR_PROXY", "proxyerror",
]

PROXY_RE_PAT = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b')

# Global proxy state
proxy_pool    = []          # list of "host:port" strings
active_proxy  = None        # current active proxy string "host:port"
proxy_lock    = threading.Lock()
proxy_enabled = True
_bot_app_ref  = None        # set in main, used for notifications

def _proxy_log(msg):
    logger.info(f"[PROXY] {msg}")

# ── Fetch one source ──────────────────────────────
def _fetch_url(url: str) -> set:
    import urllib.request, json as _json
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode("utf-8", errors="ignore")

        found = set()

        # geonode returns JSON
        if "geonode" in url:
            try:
                data = _json.loads(raw)
                for item in data.get("data", []):
                    ip   = item.get("ip", "")
                    port = item.get("port", "")
                    if ip and port:
                        found.add(f"{ip}:{port}")
                return found
            except Exception:
                pass

        # plain text ip:port
        for line in raw.splitlines():
            line = line.strip()
            if PROXY_RE_PAT.match(line):
                found.add(line.split()[0])
        return found
    except Exception:
        return set()

# ── Scrape — APIs first, then raw lists ───────────
def scrape_proxy_pool() -> list:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    found = set()
    all_sources = PROXY_APIS + PROXY_SOURCES

    with ThreadPoolExecutor(max_workers=len(all_sources)) as ex:
        futures = {ex.submit(_fetch_url, url): url for url in all_sources}
        for fut in as_completed(futures):
            found |= fut.result()

    # filter to allowed ports only (Codespaces allows 80,443,8080 etc)
    filtered = []
    for p in found:
        try:
            port = int(p.rsplit(":", 1)[-1])
            if port in ALLOWED_PORTS:
                filtered.append(p)
        except Exception:
            pass

    _proxy_log(f"Scraped {len(found)} raw → {len(filtered)} on allowed ports ({ALLOWED_PORTS})")
    return filtered

# ── Check proxy (TCP) ─────────────────────────────
def check_proxy_tcp(proxy_str: str) -> bool:
    try:
        host, port_s = proxy_str.rsplit(":", 1)
        s = socket.create_connection((host, int(port_s)), timeout=3)
        s.close()
        return True
    except Exception:
        return False

# ── Check proxy via HTTPS (for restricted envs) ───
def check_proxy_https(proxy_str: str) -> bool:
    """Works even in restricted envs — routes through port 443/80."""
    import urllib.request
    try:
        host, port_s = proxy_str.rsplit(":", 1)
        handler = urllib.request.ProxyHandler({
            "http":  f"http://{host}:{port_s}",
            "https": f"http://{host}:{port_s}",
        })
        opener = urllib.request.build_opener(handler)
        opener.addheaders = [("User-Agent", "curl/7.88")]
        with opener.open("http://ip-api.com/json", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False

# ── Rotate ────────────────────────────────────────
def rotate_proxy(reason: str = "manual") -> str | None:
    global active_proxy, proxy_pool
    with proxy_lock:
        pool_copy = list(proxy_pool)
        cur       = active_proxy

    random.shuffle(pool_copy)
    for candidate in pool_copy:
        if candidate == cur:
            continue
        # use HTTPS check in restricted env
        if check_proxy_https(candidate):
            with proxy_lock:
                active_proxy = candidate
            _proxy_log(f"Rotated [{reason}]: {cur} → {candidate}")
            return candidate

    _proxy_log("Pool exhausted — refreshing...")
    threading.Thread(target=_refresh_pool_and_pick, daemon=True).start()
    return None

# ── Health loop ───────────────────────────────────
def _proxy_health_loop():
    global active_proxy
    while True:
        time.sleep(PROXY_ROTATE_S)
        if not proxy_enabled:
            continue
        with proxy_lock:
            cur = active_proxy
        if cur and not check_proxy_https(cur):
            _proxy_log(f"Proxy dead: {cur} — rotating...")
            new = rotate_proxy("health-check")
            if _bot_app_ref and new:
                try:
                    asyncio.run_coroutine_threadsafe(
                        _notify_proxy_change(cur, new, "⏱️ Health Check"),
                        _bot_app_ref.loop
                    )
                except Exception:
                    pass
        elif not cur:
            _refresh_pool_and_pick()

# ── Refresh pool ──────────────────────────────────
def _refresh_pool_and_pick():
    global active_proxy, proxy_pool
    _proxy_log("Refreshing proxy pool...")
    fresh = scrape_proxy_pool()
    random.shuffle(fresh)

    # pick first 500 to test, use HTTPS check, concurrent
    from concurrent.futures import ThreadPoolExecutor, as_completed
    candidates = fresh[:500]
    live = []
    live_lock = threading.Lock()

    def _chk(p):
        if check_proxy_https(p):
            with live_lock:
                live.append(p)

    with ThreadPoolExecutor(max_workers=80) as ex:
        futs = [ex.submit(_chk, p) for p in candidates]
        for _ in as_completed(futs):
            with live_lock:
                if len(live) >= 80:
                    break

    live = live[:80]
    with proxy_lock:
        proxy_pool = live
        if live and not active_proxy:
            active_proxy = live[0]
            _proxy_log(f"Pool ready — {len(live)} proxies. Active: {active_proxy}")
        else:
            _proxy_log(f"Pool refreshed — {len(live)} proxies.")

    # notify owner with real stats
    if _bot_app_ref:
        try:
            msg = (
                f"♻️ *Proxy Pool Refreshed*\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📦 Scraped   : `{len(fresh)}`\n"
                f"🔓 Allowed   : `{len(candidates)}`\n"
                f"🟢 Live      : `{len(live)}`\n"
                f"✅ Active    : `{active_proxy or 'None'}`"
            )
            asyncio.run_coroutine_threadsafe(
                _bot_app_ref.bot.send_message(chat_id=OWNER_ID, text=msg, parse_mode="Markdown"),
                _bot_app_ref.loop
            )
        except Exception:
            pass

async def _notify_proxy_change(old, new, reason):
    if not _bot_app_ref:
        return
    try:
        await _bot_app_ref.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🔄 *Proxy Rotated*\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📌 Reason : `{reason}`\n"
                f"❌ Old    : `{old}`\n"
                f"✅ New    : `{new}`"
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ── Local forward proxy server ─────────────────────
import socketserver, http.server

class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP/HTTPS forward proxy that tunnels through active_proxy."""

    def log_message(self, *args): pass   # silence access log

    def _get_upstream(self):
        with proxy_lock:
            return active_proxy

    def do_CONNECT(self):
        """HTTPS CONNECT tunnel."""
        upstream = self._get_upstream()
        host, port_s = self.path.split(":", 1)
        port = int(port_s)
        try:
            if upstream:
                u_host, u_port = upstream.rsplit(":", 1)
                sock = socket.create_connection((u_host, int(u_port)), timeout=PROXY_TIMEOUT)
                # send CONNECT to upstream proxy
                sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
                resp = sock.recv(4096)
                if b"200" not in resp:
                    self.send_error(502, "Upstream proxy failed CONNECT")
                    return
            else:
                sock = socket.create_connection((host, port), timeout=PROXY_TIMEOUT)

            self.send_response(200, "Connection established")
            self.end_headers()
            self._tunnel(self.connection, sock)
        except Exception as e:
            self.send_error(502, str(e))

    def do_GET(self):   self._forward()
    def do_POST(self):  self._forward()
    def do_HEAD(self):  self._forward()
    def do_PUT(self):   self._forward()
    def do_DELETE(self):self._forward()

    def _forward(self):
        upstream = self._get_upstream()
        parsed   = urlparse(self.path)
        host     = parsed.hostname
        port     = parsed.port or 80
        path     = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("proxy-connection", "proxy-authorization")}
        body_len = int(self.headers.get("Content-Length", 0))
        body     = self.rfile.read(body_len) if body_len else b""

        try:
            if upstream:
                u_host, u_port = upstream.rsplit(":", 1)
                sock = socket.create_connection((u_host, int(u_port)), timeout=PROXY_TIMEOUT)
                req  = f"{self.command} {self.path} HTTP/1.1\r\n"
            else:
                sock = socket.create_connection((host, port), timeout=PROXY_TIMEOUT)
                req  = f"{self.command} {path} HTTP/1.1\r\n"

            for k, v in headers.items():
                req += f"{k}: {v}\r\n"
            req += "\r\n"
            sock.sendall(req.encode() + body)

            resp = b""
            sock.settimeout(PROXY_TIMEOUT)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                resp += chunk
            sock.close()

            self.connection.sendall(resp)
        except Exception as e:
            self.send_error(502, str(e))

    @staticmethod
    def _tunnel(client, remote):
        socks = [client, remote]
        try:
            while True:
                r, _, _ = select.select(socks, [], socks, 10)
                if not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    other = remote if s is client else client
                    other.sendall(data)
        except Exception:
            pass
        finally:
            try: remote.close()
            except: pass

class _ThreadedProxy(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def start_proxy_server():
    server = _ThreadedProxy(("127.0.0.1", PROXY_PORT), _ProxyHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    _proxy_log(f"Local proxy router started on 127.0.0.1:{PROXY_PORT}")

def init_proxy_system():
    """Call once at startup."""
    start_proxy_server()
    threading.Thread(target=_refresh_pool_and_pick, daemon=True).start()
    threading.Thread(target=_proxy_health_loop,     daemon=True).start()
    _proxy_log("Proxy system initialised.")

def get_proxy_env():
    """Returns env-var dict to inject into subprocess."""
    proxy_url = f"http://127.0.0.1:{PROXY_PORT}"
    return {
        "HTTP_PROXY":  proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy":  proxy_url,
        "https_proxy": proxy_url,
        "ALL_PROXY":   proxy_url,
    }

# ── Ban detection helper ───────────────────────────
def detect_ban_in_log(log_text: str) -> bool:
    low = log_text.lower()
    return any(sig in low for sig in BAN_SIGNALS)

DATA_FILE = "bot_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                d = json.load(f)
                d.setdefault("approved_users", [])
                d.setdefault("banned_users", [])
                d.setdefault("pending_users", {})   # uid_str -> {name, username, uid, time}
                d.setdefault("user_info", {})        # uid_str -> {name, username, join_time}
                return d
        except:
            pass
    return {"approved_users": [], "banned_users": [], "pending_users": {}, "user_info": {}}

def save_data(data):
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except:
        pass

bot_data = load_data()
active_processes = {}
terminal_active = {}

# ⚡ INPUT WAITER - Terminal mode (echo pipe)
waiting_for_input = {}

# ⚡ STDIN WAITER - Running process stdin forwarding
process_stdin_waiting = {}  # uid_str -> proc_entry dict

def is_owner(uid):
    return uid == OWNER_ID

def is_auth(uid):
    return is_owner(uid) or uid in bot_data.get("approved_users", [])

def is_banned(uid):
    return uid in bot_data.get("banned_users", [])

# ═══════════════════════════════════════════════════
# CHECK IF SCRIPT HAS input()
# ═══════════════════════════════════════════════════
def has_input_function(filepath):
    try:
        with open(filepath, 'r', errors='ignore') as f:
            code = f.read()
        return 'input(' in code
    except:
        return False

# ═══════════════════════════════════════════════════
# TERMINAL
# ═══════════════════════════════════════════════════
def get_session(uid):
    if uid not in terminal_active:
        folder = os.path.join(DOWNLOADS_DIR, str(uid))
        os.makedirs(folder, exist_ok=True)
        terminal_active[uid] = {"cwd": os.path.abspath(folder), "active": False}
    return terminal_active[uid]

def term_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Clear", callback_data="t_clear"),
         InlineKeyboardButton("📁 PWD", callback_data="t_pwd")],
        [InlineKeyboardButton("📋 LS", callback_data="t_ls"),
         InlineKeyboardButton("❌ Exit", callback_data="t_exit")]
    ])

def find_file_in_dir(cwd, name):
    for ext in ['.py', '.js', '.txt', '.json', '.sh']:
        full = os.path.join(cwd, name + ext)
        if os.path.isfile(full):
            return name + ext
    try:
        for f in os.listdir(cwd):
            if f.startswith(name + '.') and os.path.isfile(os.path.join(cwd, f)):
                return f
    except:
        pass
    return None

def is_builtin_cmd(cmd):
    builtins = ['ls', 'cd', 'pwd', 'mkdir', 'rmdir', 'rm', 'cp', 'mv', 'cat', 'head', 'tail',
        'grep', 'find', 'chmod', 'touch', 'echo', 'clear', 'exit', 'quit',
        'whoami', 'id', 'uname', 'date', 'uptime', 'free', 'df', 'du', 'ps', 'kill',
        'wget', 'curl', 'tar', 'zip', 'unzip', 'which', 'ping', 'ifconfig', 'ip',
        'apt', 'apt-get', 'pip', 'pip3', 'npm', 'npx', 'node', 'python', 'python3',
        'git', 'nano', 'vim', 'vi', 'wc', 'sort', 'bash', 'sh', 'sudo']
    return cmd.lower() in builtins

def fix_cmd(cmd, cwd):
    c = cmd.strip()
    if not c: return c
    parts = c.split()
    first = parts[0]
    
    if first.lower() in ('python', 'python3'):
        if len(parts) > 1 and not parts[1].startswith('-'):
            found = find_file_in_dir(cwd, parts[1])
            if found: parts[1] = found
        return f"{shlex.quote(PYTHON_ENV)} {' '.join(parts[1:])}"
    
    if first.lower() in ('pip', 'pip3'):
        return f"uv pip --python {shlex.quote(PYTHON_ENV)} {' '.join(parts[1:])}"
    
    if first.lower() == 'node':
        if len(parts) > 1 and not parts[1].startswith('-'):
            found = find_file_in_dir(cwd, parts[1])
            if found and found.endswith('.js'): parts[1] = found
        return c
    
    if not is_builtin_cmd(first) and '/' not in first and '.' not in first:
        found = find_file_in_dir(cwd, first)
        if found:
            if found.endswith('.py'):
                return f"{shlex.quote(PYTHON_ENV)} {shlex.quote(os.path.join(cwd, found))}"
            elif found.endswith('.js'):
                return f"node {os.path.join(cwd, found)}"
            elif found.endswith('.sh'):
                return f"bash {os.path.join(cwd, found)}"
    return c

async def run_term_cmd(uid, cmd, user_input_val=None):
    session = get_session(uid)
    cwd = session["cwd"]
    fixed = fix_cmd(cmd, cwd)
    
    stripped = cmd.strip()
    if stripped.startswith("cd ") or stripped == "cd":
        parts = stripped.split(None, 1)
        target = parts[1] if len(parts) > 1 else os.path.expanduser("~")
        if target == "..": new = os.path.dirname(cwd)
        elif target == "~": new = os.path.expanduser("~")
        elif os.path.isabs(target): new = target
        else: new = os.path.join(cwd, target)
        new = os.path.normpath(new)
        if os.path.isdir(new):
            session["cwd"] = new
            return f"📁 `{new}`", new
        return f"❌ Not found: `{target}`", cwd
    
    if stripped.lower() in ("exit", "quit"):
        session["active"] = False
        return "👋 Terminal closed.", cwd
    
    if stripped.lower() == "clear":
        return "🧹 Cleared.", cwd
    
    if stripped.lower() == "pwd":
        return f"📁 `{cwd}`", cwd
    
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        
        if user_input_val is not None:
            safe_val = shlex.quote(user_input_val)
            final_cmd = f"echo {safe_val} | {fixed}"
        else:
            final_cmd = fixed
        
        proc = subprocess.run(
            final_cmd, shell=True, cwd=cwd, env=env,
            capture_output=True, text=True, timeout=120
        )
        
        out = ""
        if proc.stdout: out += proc.stdout
        if proc.stderr: out += ("\n" if out else "") + proc.stderr
        if not out: out = "(no output)"
        
        if len(out) > 4000:
            out = "...\n" + out[-4000:]
        
        return out, cwd
    
    except subprocess.TimeoutExpired:
        return "⏱️ Timeout (120s)", cwd
    except Exception as e:
        return f"❌ {e}", cwd

# ═══════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════
def stop_kb(uid):
    procs = active_processes.get(str(uid), [])
    buttons = []
    for i, p in enumerate(procs):
        status = "🟢" if p["proc"].poll() is None else "🔴"
        buttons.append([InlineKeyboardButton(f"🛑 {status} {p['name']}", callback_data=f"stop_{i}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

def logs_kb(uid):
    procs = active_processes.get(str(uid), [])
    buttons = []
    for i, p in enumerate(procs):
        buttons.append([InlineKeyboardButton(f"📝 {p['name']}", callback_data=f"logs_{i}")])
    buttons.append([InlineKeyboardButton("🔙", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

def main_kb(uid=None):
    rows = [
        [KeyboardButton("💻 Terminal"), KeyboardButton("📁 Upload File")],
        [KeyboardButton("🛑 Stop Script"), KeyboardButton("📂 My Scripts")],
        [KeyboardButton("📝 View Logs"), KeyboardButton("🌐 Proxy Status")]
    ]
    if uid and is_owner(uid):
        rows.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 User List", callback_data="adm_users"),
         InlineKeyboardButton("⏳ Pending", callback_data="adm_pending")],
        [InlineKeyboardButton("🖥️ All Scripts", callback_data="adm_scripts"),
         InlineKeyboardButton("📊 Stats", callback_data="adm_stats")],
        [InlineKeyboardButton("🚫 Banned List", callback_data="adm_banned")],
        [InlineKeyboardButton("🔙 Close", callback_data="adm_close")]
    ])

def approval_kb(target_uid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{target_uid}"),
        InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{target_uid}")
    ]])

# ═══════════════════════════════════════════════════
# FLASK
# ═══════════════════════════════════════════════════
flask_app = Flask('')

@flask_app.route('/')
def index():
    return "<h1>Bot Online</h1>"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port, debug=False)

# ═══════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════
async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send approval request to admin when unauthorized user tries to use bot."""
    uid = update.effective_user.id
    user = update.effective_user
    uid_str = str(uid)

    # Already pending?
    if uid_str in bot_data.get("pending_users", {}):
        await update.message.reply_text(
            "⏳ *Tumhara request admin ko bhej diya gaya hai.*\n"
            "Admin approve kare tabtak wait karo.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Save pending
    bot_data.setdefault("pending_users", {})[uid_str] = {
        "uid": uid,
        "name": user.full_name,
        "username": f"@{user.username}" if user.username else "N/A",
        "time": datetime.now().strftime("%d/%m %H:%M")
    }
    save_data(bot_data)

    await update.message.reply_text(
        "📨 *Access Request bhej diya!*\n"
        "Admin approve kare tabtak wait karo ⏳",
        parse_mode=ParseMode.MARKDOWN
    )

    # Notify admin
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🔔 *Naya Access Request!*\n\n"
                f"👤 Name: {user.full_name}\n"
                f"🆔 ID: `{uid}`\n"
                f"📛 Username: @{user.username or 'N/A'}\n"
                f"🕐 Time: {datetime.now().strftime('%d/%m %H:%M')}"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=approval_kb(uid)
        )
    except Exception as e:
        logger.warning(f"Could not notify admin: {e}")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user

    if is_banned(uid):
        await update.message.reply_text("🚫 Banned.")
        return

    if not is_auth(uid):
        await request_access(update, context)
        return

    # Save user info
    bot_data.setdefault("user_info", {})[str(uid)] = {
        "name": user.full_name,
        "username": f"@{user.username}" if user.username else "N/A",
        "join_time": datetime.now().strftime("%d/%m %H:%M")
    }
    # Remove from pending if was there
    bot_data.get("pending_users", {}).pop(str(uid), None)
    save_data(bot_data)

    await update.message.reply_text(
        f"👋 *Welcome {user.first_name}!*\n\n"
        f"⚡ Script me `input()` hoga toh bot khud maang lega!\n\n"
        f"👇 Tap button below",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_kb(uid)
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("🚫 Sirf admin ke liye.")
        return
    pending = len(bot_data.get("pending_users", {}))
    approved = len(bot_data.get("approved_users", []))
    total_scripts = sum(len(v) for v in active_processes.values())
    running = sum(
        1 for procs in active_processes.values()
        for p in procs if p["proc"].poll() is None
    )
    await update.message.reply_text(
        f"👑 *Admin Panel*\n\n"
        f"✅ Approved Users: {approved}\n"
        f"⏳ Pending Requests: {pending}\n"
        f"🖥️ Total Scripts: {total_scripts} ({running} running)\n\n"
        f"👇 Select action:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_kb()
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    uid_str = str(uid)
    data = query.data

    # ── ADMIN-ONLY CALLBACKS (approve/ban/panel) ──────────────────────────
    if data.startswith("approve_") or data.startswith("ban_") or data.startswith("unban_") or data.startswith("remove_") or data.startswith("adm_") or data.startswith("kill_"):
        if not is_owner(uid):
            await query.answer("🚫 Sirf admin!", show_alert=True)
            return

        if data.startswith("approve_"):
            target = int(data.split("_")[1])
            t_str = str(target)
            info = bot_data.get("pending_users", {}).pop(t_str, None)
            if target not in bot_data["approved_users"]:
                bot_data["approved_users"].append(target)
            bot_data.get("banned_users", [])  # ensure exists
            save_data(bot_data)
            name = info["name"] if info else str(target)
            await query.edit_message_text(
                f"✅ *{name}* ko approve kar diya!\n🆔 `{target}`",
                parse_mode=ParseMode.MARKDOWN
            )
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text="✅ *Admin ne tumhe approve kar diya!*\nAbh /start karo.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except: pass

        elif data.startswith("ban_"):
            target = int(data.split("_")[1])
            t_str = str(target)
            bot_data.get("pending_users", {}).pop(t_str, None)
            if target in bot_data.get("approved_users", []):
                bot_data["approved_users"].remove(target)
            if target not in bot_data.get("banned_users", []):
                bot_data.setdefault("banned_users", []).append(target)
            save_data(bot_data)
            await query.edit_message_text(f"🚫 User `{target}` ban kar diya.", parse_mode=ParseMode.MARKDOWN)
            try:
                await context.bot.send_message(chat_id=target, text="🚫 Tumhe admin ne ban kar diya.")
            except: pass

        elif data == "adm_stats":
            pending = len(bot_data.get("pending_users", {}))
            approved = len(bot_data.get("approved_users", []))
            banned = len(bot_data.get("banned_users", []))
            total_scripts = sum(len(v) for v in active_processes.values())
            running = sum(1 for procs in active_processes.values() for p in procs if p["proc"].poll() is None)
            await query.edit_message_text(
                f"📊 *Bot Stats*\n\n"
                f"✅ Approved: {approved}\n"
                f"⏳ Pending: {pending}\n"
                f"🚫 Banned: {banned}\n"
                f"🖥️ Scripts (total/running): {total_scripts}/{running}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_kb()
            )

        elif data == "adm_pending":
            pending = bot_data.get("pending_users", {})
            if not pending:
                await query.edit_message_text("⏳ Koi pending request nahi.", reply_markup=admin_kb())
                return
            msg = "⏳ *Pending Requests:*\n\n"
            buttons = []
            for u_str, info in pending.items():
                msg += f"👤 {info['name']} | {info['username']} | `{info['uid']}` | {info['time']}\n"
                buttons.append([
                    InlineKeyboardButton(f"✅ {info['name']}", callback_data=f"approve_{info['uid']}"),
                    InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{info['uid']}")
                ])
            buttons.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back")])
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "adm_users":
            users = bot_data.get("user_info", {})
            approved = bot_data.get("approved_users", [])
            if not approved:
                await query.edit_message_text("👥 Koi approved user nahi.", reply_markup=admin_kb())
                return
            msg = "👥 *Approved Users:*\n\n"
            buttons = []
            for u_id in approved:
                u_str = str(u_id)
                info = users.get(u_str, {})
                name = info.get("name", u_str)
                uname = info.get("username", "N/A")
                msg += f"• {name} | {uname} | `{u_id}`\n"
                buttons.append([
                    InlineKeyboardButton(f"❌ Remove {name}", callback_data=f"remove_{u_id}"),
                    InlineKeyboardButton(f"🚫 Ban {name}", callback_data=f"ban_{u_id}")
                ])
            buttons.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back")])
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "adm_scripts":
            if not active_processes:
                await query.edit_message_text("🖥️ Koi script nahi chal rahi.", reply_markup=admin_kb())
                return
            msg = "🖥️ *All Running Scripts:*\n\n"
            buttons = []
            for u_str, procs in active_processes.items():
                users = bot_data.get("user_info", {})
                uname = users.get(u_str, {}).get("name", u_str)
                for i, p in enumerate(procs):
                    st = "🟢" if p["proc"].poll() is None else "🔴"
                    up = int(time.time() - p["start_time"])
                    m, s = divmod(up, 60)
                    msg += f"{st} {p['name']} ({uname}) — {m}m{s}s\n"
                    if p["proc"].poll() is None:
                        buttons.append([InlineKeyboardButton(
                            f"🛑 Kill {p['name']} ({uname})",
                            callback_data=f"kill_{u_str}_{i}"
                        )])
            buttons.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back")])
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

        elif data.startswith("kill_"):
            parts = data.split("_", 2)
            t_str, idx = parts[1], int(parts[2])
            procs = active_processes.get(t_str, [])
            if 0 <= idx < len(procs):
                p = procs[idx]
                if p["proc"].poll() is None:
                    p["proc"].terminate()
                    try: p["proc"].wait(timeout=5)
                    except: p["proc"].kill()
                    await query.edit_message_text(f"🛑 Killed `{p['name']}` (user `{t_str}`)", parse_mode=ParseMode.MARKDOWN)
                else:
                    await query.edit_message_text("ℹ️ Script already stopped.")
                procs.pop(idx)
            else:
                await query.edit_message_text("❌ Script nahi mila.")

        elif data == "adm_back":
            pending = len(bot_data.get("pending_users", {}))
            approved = len(bot_data.get("approved_users", []))
            running = sum(1 for procs in active_processes.values() for p in procs if p["proc"].poll() is None)
            await query.edit_message_text(
                f"👑 *Admin Panel*\n\n"
                f"✅ Approved: {approved} | ⏳ Pending: {pending}\n"
                f"🖥️ Running scripts: {running}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_kb()
            )

        elif data == "adm_banned":
            banned = bot_data.get("banned_users", [])
            if not banned:
                await query.edit_message_text("✅ Koi banned user nahi.", reply_markup=admin_kb())
                return
            users = bot_data.get("user_info", {})
            msg = "🚫 *Banned Users:*\n\n"
            buttons = []
            for b_id in banned:
                info = users.get(str(b_id), {})
                name = info.get("name", str(b_id))
                uname = info.get("username", "N/A")
                msg += f"• {name} | {uname} | `{b_id}`\n"
                buttons.append([InlineKeyboardButton(f"✅ Unban {name}", callback_data=f"unban_{b_id}")])
            buttons.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back")])
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

        elif data.startswith("remove_"):
            target = int(data.split("_")[1])
            if target in bot_data.get("approved_users", []):
                bot_data["approved_users"].remove(target)
            save_data(bot_data)
            users = bot_data.get("user_info", {})
            name = users.get(str(target), {}).get("name", str(target))
            await query.edit_message_text(
                f"❌ *{name}* ka access remove kar diya!\n🆔 `{target}`\n\n_Ab woh dobara request kar sakta hai._",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_kb()
            )
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text="❌ *Admin ne tumhara access remove kar diya.*\nDobara access chahiye toh /start karo.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except: pass

        elif data.startswith("unban_"):
            target = int(data.split("_")[1])
            if target in bot_data.get("banned_users", []):
                bot_data["banned_users"].remove(target)
            if target not in bot_data.get("approved_users", []):
                bot_data.setdefault("approved_users", []).append(target)
            save_data(bot_data)
            users = bot_data.get("user_info", {})
            name = users.get(str(target), {}).get("name", str(target))
            await query.edit_message_text(
                f"✅ *{name}* ko unban kar diya!\n🆔 `{target}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_kb()
            )
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text="✅ *Admin ne tumhara ban hata diya!*\nAbh /start karo.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except: pass

        elif data == "adm_close":
            await query.delete_message()
        return

    # ── REGULAR USER CALLBACKS ────────────────────────────────────────────
    if not is_auth(uid):
        await query.answer("🔐 Access nahi hai!", show_alert=True)
        return

    # ── PROXY CALLBACKS ───────────────────────────────────────────────────
    if data == "proxy_rotate":
        if not is_owner(uid):
            await query.answer("🚫 Sirf admin!", show_alert=True)
            return
        await query.answer("🔄 Rotating...")
        with proxy_lock:
            old = active_proxy
        new = rotate_proxy("manual")
        if new:
            await query.edit_message_text(
                f"✅ *Proxy Rotated!*\n❌ Old: `{old}`\n✅ New: `{new}`",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text("❌ Koi live proxy nahi mila. Pool refresh karo.")

    elif data == "proxy_refresh":
        if not is_owner(uid):
            await query.answer("🚫 Sirf admin!", show_alert=True)
            return
        await query.edit_message_text("♻️ Pool refresh ho raha hai...")
        threading.Thread(target=_refresh_pool_and_pick, daemon=True).start()
        await asyncio.sleep(5)
        with proxy_lock:
            cur   = active_proxy
            total = len(proxy_pool)
        await query.edit_message_text(
            f"✅ *Pool Refreshed!*\n📦 Live: `{total}`\n✅ Active: `{cur or 'None'}`",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "proxy_off":
        if not is_owner(uid):
            await query.answer("🚫 Sirf admin!", show_alert=True)
            return
        global proxy_enabled
        proxy_enabled = False
        await query.edit_message_text("⏸️ Proxy system disabled.")

    elif data == "proxy_on":
        if not is_owner(uid):
            await query.answer("🚫 Sirf admin!", show_alert=True)
            return
        proxy_enabled = True
        await query.edit_message_text("▶️ Proxy system enabled.")

    elif data == "t_clear":
        get_session(uid)["active"] = True
        await query.edit_message_text("🧹 Cleared. Type command:", reply_markup=term_kb())
    elif data == "t_pwd":
        s = get_session(uid)
        s["active"] = True
        await query.edit_message_text(f"📁 `{s['cwd']}`", parse_mode=ParseMode.MARKDOWN, reply_markup=term_kb())
    elif data == "t_ls":
        s = get_session(uid)
        s["active"] = True
        out, _ = await run_term_cmd(uid, "ls -la")
        await query.edit_message_text(f"```\n{out}\n```", parse_mode=ParseMode.MARKDOWN, reply_markup=term_kb())
    elif data == "t_exit":
        get_session(uid)["active"] = False
        if uid_str in waiting_for_input:
            del waiting_for_input[uid_str]
        await query.edit_message_text("👋 Terminal closed.")
    elif data == "back_main":
        await query.edit_message_text("👇 Menu:")
    elif data.startswith("stop_"):
        idx = int(data.split("_")[1])
        procs = active_processes.get(uid_str, [])
        if 0 <= idx < len(procs):
            p = procs[idx]
            if p["proc"].poll() is None:
                p["proc"].terminate()
                try: p["proc"].wait(timeout=5)
                except: p["proc"].kill()
                await query.edit_message_text(f"✅ Stopped `{p['name']}`", parse_mode=ParseMode.MARKDOWN)
            else:
                await query.edit_message_text(f"ℹ️ `{p['name']}` already stopped", parse_mode=ParseMode.MARKDOWN)
            procs.pop(idx)
    elif data.startswith("logs_"):
        idx = int(data.split("_")[1])
        procs = active_processes.get(uid_str, [])
        if 0 <= idx < len(procs):
            p = procs[idx]
            if os.path.exists(p["log_path"]):
                with open(p["log_path"], 'r', errors='ignore') as f:
                    log = f.read()[-3000:]
                await query.edit_message_text(f"📝 *{p['name']}*:\n```\n{log}\n```", parse_mode=ParseMode.MARKDOWN, reply_markup=logs_kb(uid))
            else:
                await query.edit_message_text("❌ No log file")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user
    text = update.message.text
    uid_str = str(uid)
    
    if is_banned(uid):
        await update.message.reply_text("🚫 Banned.")
        return
    
    if not is_auth(uid):
        await request_access(update, context)
        return
    
    session = get_session(uid)

    # ⚡ RUNNING PROCESS STDIN FORWARD
    if uid_str in process_stdin_waiting and text and not update.message.document:
        proc_entry = process_stdin_waiting.pop(uid_str)
        proc = proc_entry["proc"]
        if proc.poll() is None:
            try:
                proc.stdin.write(text + "\n")
                proc.stdin.flush()
                await update.message.reply_text(
                    f"📥 Sent: `{text}`",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Stdin error: {e}")
        else:
            await update.message.reply_text("ℹ️ Script already khatam ho gaya.")
        return

    # ⚡ AGAR SCRIPT NE INPUT MAANGA THA (terminal echo-pipe mode)
    if uid_str in waiting_for_input and text and not update.message.document:
        info = waiting_for_input.pop(uid_str)
        cmd = info["cmd"]
        user_val = text.strip()
        
        await update.message.reply_text(f"⏳ Injecting input aur running `{info['name']}`...")
        
        out, cwd = await run_term_cmd(uid, cmd, user_input_val=user_val)
        
        await update.message.reply_text(
            f"💻 `$ {cmd}`\n📥 *Input:* `{user_val}`\n```\n{out}\n```\n📁 `{cwd}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=term_kb()
        )
        return
    
    # TERMINAL MODE
    if session["active"] and text and not update.message.document:
        cmd = text.strip()
        cwd = session["cwd"]
        fixed = fix_cmd(cmd, cwd)
        
        target_file = None
        parts = cmd.split()
        
        if parts[0].lower() in ('python', 'python3') and len(parts) > 1:
            fname = parts[1]
            found = find_file_in_dir(cwd, fname)
            if found and found.endswith('.py'):
                target_file = os.path.join(cwd, found)
        elif not is_builtin_cmd(parts[0]) and '/' not in parts[0] and '.' not in parts[0]:
            found = find_file_in_dir(cwd, parts[0])
            if found and found.endswith('.py'):
                target_file = os.path.join(cwd, found)
        
        if target_file and has_input_function(target_file):
            waiting_for_input[uid_str] = {
                "cmd": cmd,
                "cwd": cwd,
                "name": os.path.basename(target_file)
            }
            await update.message.reply_text(
                f"⚠️ `{os.path.basename(target_file)}` me `input()` mila!\n\n"
                f"👇 Jo maang raha hai wo abhi type karo:\n"
                f"(Bot khud paste karke enter marega)",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=term_kb()
            )
            return
        
        out, new_cwd = await run_term_cmd(uid, cmd)
        
        if not session["active"]:
            await update.message.reply_text(
                f"💻 `$ {cmd}`\n```\n{out}\n```\n👋 Terminal closed.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_kb(uid)
            )
        else:
            await update.message.reply_text(
                f"💻 `$ {cmd}`\n```\n{out}\n```\n📁 `{new_cwd}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=term_kb()
            )
        return
    
    # BUTTONS
    if text == "💻 Terminal":
        session["active"] = True
        await update.message.reply_text(
            f"💻 *Terminal Active*\n"
            f"📁 `{session['cwd']}`\n\n"
            f"⚡ *Auto Input Feature ON!*\n"
            f"Agar script me `input()` hai toh bot khud maang lega\n\n"
            f"• `hi` → Token maangega\n"
            f"• `ls` `cd` `pwd`\n"
            f"• `exit` to close",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=term_kb()
        )
    
    elif text == "📁 Upload File":
        await update.message.reply_text("📤 Send .py / .js / .zip file")
    
    elif text == "🛑 Stop Script":
        procs = active_processes.get(uid_str, [])
        if not procs:
            await update.message.reply_text("❌ No scripts")
        else:
            await update.message.reply_text("🛑 Select:", reply_markup=stop_kb(uid))
    
    elif text == "📂 My Scripts":
        procs = active_processes.get(uid_str, [])
        if not procs:
            await update.message.reply_text("📂 No scripts")
        else:
            msg = "📂 *Scripts:*\n\n"
            for i, p in enumerate(procs):
                st = "🟢" if p["proc"].poll() is None else "🔴"
                up = int(time.time() - p["start_time"])
                m, s = divmod(up, 60)
                msg += f"{i+1}. {st} `{p['name']}` ({m}m{s}s)\n"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif text == "📝 View Logs":
        procs = active_processes.get(uid_str, [])
        if not procs:
            await update.message.reply_text("❌ No scripts")
        else:
            await update.message.reply_text("📝 Select:", reply_markup=logs_kb(uid))

    elif text == "🌐 Proxy Status":
        with proxy_lock:
            cur   = active_proxy
            total = len(proxy_pool)
        status_emoji = "🟢" if cur else "🔴"
        pool_text    = f"{total} proxies in pool" if total else "Pool empty"
        proxy_url    = f"http://127.0.0.1:{PROXY_PORT}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Rotate Now",    callback_data="proxy_rotate")],
            [InlineKeyboardButton("♻️ Refresh Pool",  callback_data="proxy_refresh")],
            [InlineKeyboardButton("⏸️ Disable",       callback_data="proxy_off") if proxy_enabled
             else InlineKeyboardButton("▶️ Enable",   callback_data="proxy_on")],
        ])
        await update.message.reply_text(
            f"🌐 *Proxy System*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{status_emoji} Active : `{cur or 'None'}`\n"
            f"📡 Router: `{proxy_url}`\n"
            f"📦 Pool  : `{pool_text}`\n"
            f"🔁 Check : every {PROXY_ROTATE_S}s",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )

    elif text == "👑 Admin Panel":
        if not is_owner(uid):
            await update.message.reply_text("🚫 Sirf admin ke liye.")
            return
        pending = len(bot_data.get("pending_users", {}))
        approved = len(bot_data.get("approved_users", []))
        running = sum(1 for procs in active_processes.values() for p in procs if p["proc"].poll() is None)
        await update.message.reply_text(
            f"👑 *Admin Panel*\n\n"
            f"✅ Approved: {approved}\n"
            f"⏳ Pending: {pending}\n"
            f"🖥️ Running scripts: {running}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_kb()
        )

    elif update.message.document:
        await handle_upload(update, context)

async def monitor_process_stdin(uid_str, proc_entry, context, chat_id):
    """Monitor running process: live-edit ONE message with latest output, prompt stdin when idle."""
    proc = proc_entry["proc"]
    log_path = proc_entry["log_path"]
    last_size = 0
    last_output_time = time.time()
    stdin_prompted = False
    live_msg = None          # the single message we keep editing
    last_edit_time = 0
    EDIT_INTERVAL = 3        # seconds between edits (Telegram rate-limit friendly)

    def read_tail(path, from_byte, max_chars=3500):
        try:
            with open(path, 'r', errors='ignore') as f:
                f.seek(from_byte)
                raw = f.read()
            clean = re.sub(r'\x1b\[[0-9;]*[mGKHABCDJK]|\x1b\([A-Z]|\x1b=|\r', '', raw)
            return clean
        except:
            return ""

    while proc.poll() is None:
        await asyncio.sleep(1)

        try:
            current_size = os.path.getsize(log_path)
        except:
            current_size = last_size

        if current_size > last_size:
            last_size = current_size
            last_output_time = time.time()
            stdin_prompted = False

            # ── Ban detection ──────────────────────────────
            try:
                with open(log_path, 'r', errors='ignore') as _lf:
                    _lf.seek(max(0, current_size - 2000))
                    _recent = _lf.read()
                if detect_ban_in_log(_recent) and proxy_enabled:
                    with proxy_lock:
                        _old_p = active_proxy
                    _new_p = rotate_proxy("ban-detected")
                    if _new_p:
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"🚫 *IP Ban Detected!*\n"
                                    f"📄 Script: `{proc_entry['name']}`\n"
                                    f"━━━━━━━━━━━━━━━━\n"
                                    f"❌ Old: `{_old_p}`\n"
                                    f"✅ New: `{_new_p}`\n"
                                    f"🔄 Proxy rotated automatically"
                                ),
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Exception:
                            pass
            except Exception:
                pass

            # Edit/create the live message every EDIT_INTERVAL seconds
            now = time.time()
            if now - last_edit_time >= EDIT_INTERVAL:
                last_edit_time = now
                # Read last ~3500 chars of full log for display
                try:
                    with open(log_path, 'r', errors='ignore') as f:
                        raw_all = f.read()
                    clean_all = re.sub(r'\x1b\[[0-9;]*[mGKHABCDJK]|\x1b\([A-Z]|\x1b=|\r', '', raw_all)
                    snippet = clean_all[-3500:].strip()
                except:
                    snippet = ""

                if snippet:
                    msg_text = f"📺 *Live Output:*\n```\n{snippet}\n```"
                    try:
                        if live_msg is None:
                            live_msg = await context.bot.send_message(
                                chat_id=chat_id,
                                text=msg_text,
                                parse_mode=ParseMode.MARKDOWN
                            )
                        else:
                            await live_msg.edit_text(msg_text, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        pass

        elif not stdin_prompted and (time.time() - last_output_time) > 1.5 and last_size > 0:
            # No new output for 1.5s → likely waiting for input
            stdin_prompted = True
            process_stdin_waiting[uid_str] = proc_entry
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⌨️ *Script input maang raha hai* — jo chahiye wo type karo:",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass

    # Process finished — clean up
    if uid_str in process_stdin_waiting and process_stdin_waiting.get(uid_str) is proc_entry:
        del process_stdin_waiting[uid_str]

    # Final edit: show complete last output and mark done
    try:
        with open(log_path, 'r', errors='ignore') as f:
            raw_all = f.read()
        clean_all = re.sub(r'\x1b\[[0-9;]*[mGKHABCDJK]|\x1b\([A-Z]|\x1b=|\r', '', raw_all)
        snippet = clean_all[-3500:].strip()
    except:
        snippet = ""

    done_text = f"🏁 *{proc_entry['name']} khatam*\n```\n{snippet}\n```" if snippet else f"🏁 `{proc_entry['name']}` khatam ho gaya."
    try:
        if live_msg:
            await live_msg.edit_text(done_text, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(chat_id=chat_id, text=done_text, parse_mode=ParseMode.MARKDOWN)
    except:
        pass


async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    fname = doc.file_name
    user_dir = os.path.join(DOWNLOADS_DIR, str(uid))
    os.makedirs(user_dir, exist_ok=True)
    fpath = os.path.join(user_dir, fname)
    
    msg = await update.message.reply_text(f"⏳ Downloading `{fname}`...")
    
    try:
        file = await context.bot.get_file(doc.file_id)
        
        downloaded = False
        try:
            await file.download_to_drive(fpath)
            downloaded = os.path.exists(fpath) and os.path.getsize(fpath) > 0
        except: pass
        if not downloaded:
            try:
                await file.download(custom_path=fpath)
                downloaded = os.path.exists(fpath) and os.path.getsize(fpath) > 0
            except: pass
        if not downloaded:
            try:
                content = await file.download_as_bytearray()
                with open(fpath, 'wb') as f:
                    f.write(content)
                downloaded = os.path.exists(fpath) and os.path.getsize(fpath) > 0
            except: pass
        
        if not downloaded:
            await msg.edit_text("❌ Download failed")
            return
        
        await msg.edit_text(f"✅ Downloaded `{fname}`")
        
        run_cmd = None
        work_dir = os.path.abspath(user_dir)
        
        if fname.endswith('.py'):
            await msg.edit_text("📦 Installing deps...")
            with open(fpath, 'r', errors='ignore') as f:
                content = f.read()
            imports = re.findall(r'^(?:from|import)\s+([a-zA-Z0-9_]+)', content, re.MULTILINE)
            stdlib = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else set()
            external = [i for i in set(imports) if i not in stdlib and i not in ('os','sys','time','json','re','asyncio','logging','threading','subprocess','datetime','pathlib','collections','itertools','functools','math','random','string','io','typing','abc','copy','hashlib','base64','struct','socket','ssl','http','urllib','email','html','xml','csv','sqlite3','argparse','configparser','tempfile','shutil','glob','fnmatch','traceback','warnings','contextlib','enum','numbers','decimal','fractions','statistics','pprint','textwrap','difflib','zipfile','tarfile','gzip','bz2','lzma','zlib','pickle','shelve','dbm','sched','queue','multiprocessing','concurrent','socketserver','ipaddress','uuid','secrets','hmac','dataclasses','operator','ctypes','array','weakref','types','inspect','dis','code','ast','token','tokenize','plistlib','tty','termios','pty','fcntl','grp','pwd','crypt','syslog','os.path','builtins')]
            
            pip_map = {"telegram": "python-telegram-bot", "PIL": "pillow", "cv2": "opencv-python",
                       "bs4": "beautifulsoup4", "sklearn": "scikit-learn", "yaml": "pyyaml",
                       "discord": "discord.py", "google": "google-api-python-client",
                       "_brotli": "Brotli", "brotli": "Brotli"}
            
            for dep in external:
                pkg = pip_map.get(dep, dep)
                await msg.edit_text(f"📦 Installing {pkg}...")
                subprocess.run(
                    ["uv", "pip", "install", "--python", PYTHON_ENV, pkg],
                    capture_output=True
                )
            
            run_cmd = [PYTHON_ENV, os.path.abspath(fpath)]
        
        elif fname.endswith('.js'):
            run_cmd = ["node", os.path.abspath(fpath)]
        
        elif fname.endswith('.zip'):
            await msg.edit_text("📦 Extracting...")
            ext_dir = os.path.join(user_dir, fname.replace('.zip', ''))
            if os.path.exists(ext_dir):
                shutil.rmtree(ext_dir)
            with zipfile.ZipFile(fpath, 'r') as z:
                z.extractall(ext_dir)
            work_dir = ext_dir
            
            main_py = os.path.join(ext_dir, 'main.py')
            index_js = os.path.join(ext_dir, 'index.js')
            
            if os.path.exists(main_py):
                req = os.path.join(ext_dir, 'requirements.txt')
                if os.path.exists(req):
                    await msg.edit_text("📦 pip install -r requirements.txt...")
                    subprocess.run(
                        ["uv", "pip", "install", "--python", PYTHON_ENV, "-r", req],
                        capture_output=True
                    )
                run_cmd = [PYTHON_ENV, main_py]
            elif os.path.exists(index_js):
                if os.path.exists(os.path.join(ext_dir, 'package.json')):
                    await msg.edit_text("📦 npm install...")
                    subprocess.run(["npm", "install"], cwd=ext_dir, capture_output=True)
                run_cmd = ["node", index_js]
            else:
                await msg.edit_text("❌ No main.py or index.js in ZIP")
                return
        
        if run_cmd:
            await msg.edit_text(f"🚀 Running `{fname}`...")
            log_path = os.path.join(LOGS_DIR, f"{uid}_{fname}_{int(time.time())}.log")
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            # ── Inject proxy into every script ──
            if proxy_enabled and active_proxy:
                env.update(get_proxy_env())
                _proxy_log(f"Proxy injected into {fname}: 127.0.0.1:{PROXY_PORT} → {active_proxy}")
            chat_id = update.effective_chat.id

            # Auto-retry loop: if script crashes with ModuleNotFoundError, install and re-run
            pip_map_retry = {"telegram": "python-telegram-bot", "PIL": "pillow", "cv2": "opencv-python",
                             "bs4": "beautifulsoup4", "sklearn": "scikit-learn", "yaml": "pyyaml",
                             "discord": "discord.py", "google": "google-api-python-client",
                             "_brotli": "Brotli", "brotli": "Brotli"}
            max_retries = 5
            proc = None
            for attempt in range(max_retries):
                log_f = open(log_path, "w", encoding="utf-8", errors="ignore")
                proc = subprocess.Popen(
                    run_cmd, stdin=subprocess.PIPE, stdout=log_f, stderr=log_f,
                    cwd=work_dir, env=env, text=True
                )

                await asyncio.sleep(2)

                if proc.poll() is None:
                    # Still running — success
                    break

                # Crashed — read error output
                log_f.close()
                with open(log_path, 'r', errors='ignore') as f:
                    err = f.read()

                missing = re.search(r"ModuleNotFoundError: No module named '([^']+)'", err)
                if missing:
                    mod = missing.group(1).split('.')[0]
                    pkg = pip_map_retry.get(mod, mod)
                    await msg.edit_text(f"📦 Missing `{mod}` — installing `{pkg}`... (attempt {attempt+1})")
                    result = subprocess.run(
                        ["uv", "pip", "install", "--python", PYTHON_ENV, pkg],
                        capture_output=True,
                        text=True
                    )
                    if result.returncode != 0:
                        await msg.edit_text(
                            f"❌ Failed to install `{pkg}`:\n```\n{result.stderr[-500:]}\n```",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        return
                    continue
                else:
                    clean = re.sub(r'\x1b\[[0-9;]*[mGKHABCDJK]|\x1b\([A-Z]|\x1b=|\r', '', err).strip()
                    await msg.edit_text(
                        f"❌ `{fname}` crashed!\n```\n{clean[-1000:]}\n```",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return
            else:
                with open(log_path, 'r', errors='ignore') as f:
                    err = f.read()[-1000:]
                await msg.edit_text(
                    f"❌ `{fname}` crashed after {max_retries} attempts!\n```\n{err}\n```",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

            uid_str = str(uid)
            if uid_str not in active_processes:
                active_processes[uid_str] = []
            proc_entry = {
                "name": fname,
                "proc": proc,
                "log_path": log_path,
                "start_time": time.time()
            }
            active_processes[uid_str].append(proc_entry)

            await msg.edit_text(
                f"✅ `{fname}` running! (PID: {proc.pid})\n\n"
                f"📤 Output automatically forward hoga\n"
                f"⌨️ Input maangega toh bot poochega",
                parse_mode=ParseMode.MARKDOWN
            )

            # Start background monitor for output + stdin
            asyncio.create_task(monitor_process_stdin(uid_str, proc_entry, context, chat_id))
    
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
if __name__ == '__main__':
    # Flask keep-alive
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    # Proxy system — local router + pool scraper + health loop
    init_proxy_system()

    app = ApplicationBuilder().token(TOKEN).build()

    # Store app ref so proxy threads can send Telegram notifications
    _bot_app_ref = app

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)
