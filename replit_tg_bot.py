import subprocess
import sys
import os

# ============================================================
# AUTO INSTALL
# ============================================================
def install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

print("[*] Dependencies install ho rahi hain...")
for pkg in ["playwright", "python-telegram-bot"]:
    install(pkg)

subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
print("[+] Sab install ho gaya!\n")

# ============================================================
# IMPORTS
# ============================================================
import asyncio
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from playwright.async_api import async_playwright, Browser, Page

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN  = "8808870951:AAFyLPLCnHiUtNwY1oWxcZPbHdILjWO4q3Q"
ALLOWED_ID = 8502412097
REPL_URL   = "https://replit.com"          # default, /seturl se badlo

COOKIES = [
    {
        "name": "connect.sid",
        "value": "eyJhbGciOiJSUzI1NiIsImtpZCI6Iktna0hjZyJ9.eyJpc3MiOiJodHRwczovL3Nlc3Npb24uZmlyZWJhc2UuZ29vZ2xlLmNvbS9yZXBsaXQtd2ViIiwibmFtZSI6IkogSm4iLCJwaWN0dXJlIjoiaHR0cHM6Ly9saDMuZ29vZ2xldXNlcmNvbnRlbnQuY29tL2EvQUNnOG9jSVhJaldoWDVZeGUyVmQxdjZySzN1VlNnRHJwV3JGeHp6MzVKdW4xYnBWc0hwZWJRXHUwMDNkczk2LWMiLCJyb2xlcyI6W10sInJlcGxpdF91c2VyX2lkIjo2MjQ3MDkxMiwiYXVkIjoicmVwbGl0LXdlYiIsImF1dGhfdGltZSI6MTc5MTAyNjQ2NiwidXNlcl9pZCI6ImowcVo1b3VkYVhZdEVoV2RLc0diVWFZYXBjNDIiLCJzdWIiOiJqMHFaNW91ZGFYWXRFaFdkS3NHYlVhWWFwYzQyIiwiaWF0IjoxNzkxMDI2NDczLCJleHAiOjE3OTIyMzYwNzMsImVtYWlsIjoiYWxyZWVraGFybGV5QGdtYWlsLmNvbSIsImVtYWlsX3ZlcmlmaWVkIjp0cnVlLCJmaXJlYmFzZSI6eyJpZGVudGl0aWVzIjp7Imdvb2dsZS5jb20iOlsiMTE3NDgxMjQ2ODUwOTg2NTY5NzQ2Il0sImVtYWlsIjpbImFscmVla2hhcmxleUBnbWFpbC5jb20iXX0sInNpZ25faW5fcHJvdmlkZXIiOiJnb29nbGUuY29tIn19.WGLKmGvR0WXCDgfddh4QF7-UZI5ghCtkvwzPMHFw-DTE0f_bHKKTYewDqDgTB1AvuzQzdCqkJ5_ap3K0EMHOln8M_GP-2gKdb0n3dE9L3-aFAKh926H9p8bZMX6NspPAN_8lcP9ajcIBa0YDoaIEhdUZK9G4t5qbyhmTWVZ07EH1vxJAjg-WwwTENEwlvNwNBFBsG05t4Dv-1gnG_H6T8zLFFCByPVNQCKGUOIUl2_D1ZaCrY0Qu9kDLs9oP5FVdNnv0dnmEsDOfojw6fnRjg6vM1qKnydXMlMXBm5RtFQZ0r89_VDIqWkZznDOjYcmCnMUxnxtvG3dprPyHbin5xg",
        "domain": "replit.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax"
    },
    {"name": "replit_authed", "value": "1", "domain": "replit.com", "path": "/", "secure": False, "httpOnly": False, "sameSite": "Lax"},
    {
        "name": "__Host-session-sig",
        "value": "eyJhbGciOiJSUzI1NiIsImtpZCI6ImNmLWp3dC0yMDI2LTA1LTA2LTE4MDMiLCJ0eXAiOiJKV1QifQ.eyJzdWIiOiI2MjQ3MDkxMiIsInRjIjoxNzg0NTQ0MTE3LCJlbnQiOmZhbHNlLCJwYWlkIjpmYWxzZSwiaWF0IjoxNzkxMDI2NDczLCJleHAiOjE3OTEwMjczNzN9.JOJviGhFvB1HtCo_O5pBRtWzMvsNXbSFet1AuJ0-rtOfq5gp6cm2ip0PIjz61VJIpHUvwaYYl_ZZjADFFQ_g3aZiPC3tIz83vDjDNQk5xtOVaQCCwCu-zms_tYbMoPXFJk6jJnj7fgqL6s1dIe40CzcgEpFaFIEInXTYf-rCjybR3d8HoGw1BgjChnb3qsVT5qBgskCondJyCCbs5pyO2C9O1YOBlmqcFLn-keNcGAPMBhZgqF8c5jbNUeFYJsKxcgSaqupWUak5_XPDWZP_qm4jz9xBDEC5b8qWLnBhj-iwsl4f5w6SEffnYLV56KZFfbF21q0_1f4LYohQIK_kSA",
        "domain": "replit.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax"
    },
    {"name": "replit_consent", "value": "0", "domain": "replit.com", "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax"},
]

# ============================================================
# BROWSER STATE
# ============================================================
state: dict = {
    "browser": None,
    "page":    None,
    "pw":      None,
    "url":     REPL_URL,
}

logging.basicConfig(level=logging.WARNING)

# ============================================================
# GUARD
# ============================================================
def guard(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_ID

async def deny(update: Update):
    await update.message.reply_text("❌ Access denied.")

# ============================================================
# BROWSER HELPERS
# ============================================================
async def get_page() -> Page:
    if state["page"] and not state["page"].is_closed():
        return state["page"]
    await launch_browser()
    return state["page"]

async def launch_browser():
    if state["pw"] is None:
        state["pw"] = await async_playwright().start()
    if state["browser"] and state["browser"].is_connected():
        await state["browser"].close()
    state["browser"] = await state["pw"].chromium.launch(headless=True)
    ctx = await state["browser"].new_context()
    await ctx.add_cookies(COOKIES)
    state["page"] = await ctx.new_page()

async def take_ss(page: Page) -> str:
    path = "/tmp/ss.png"
    await page.screenshot(path=path, full_page=False)
    return path

# ============================================================
# COMMANDS
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not guard(update): return await deny(update)
    msg = (
        "🤖 *Replit Bot Ready*\n\n"
        "━━━━━━━━━━━━━━━\n"
        "*/open* — project open karo\n"
        "*/refresh* — page refresh karo\n"
        "*/shell* `command` — shell mein command chalaao\n"
        "*/ss* — screenshot lo\n"
        "*/goto* `url` — koi bhi URL kholo\n"
        "*/seturl* `url` — default repl URL set karo\n"
        "*/status* — browser status dekho\n"
        "*/close* — browser band karo\n"
        "━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not guard(update): return await deny(update)
    await update.message.reply_text(f"🔄 Opening: `{state['url']}`", parse_mode="Markdown")
    try:
        page = await get_page()
        await page.goto(state["url"], wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)
        ss = await take_ss(page)
        await update.message.reply_photo(photo=open(ss, "rb"), caption="✅ Project open ho gaya!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not guard(update): return await deny(update)
    await update.message.reply_text("🔄 Refresh ho raha hai...")
    try:
        page = await get_page()
        await page.reload(wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)
        ss = await take_ss(page)
        await update.message.reply_photo(photo=open(ss, "rb"), caption="✅ Refresh ho gaya!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_shell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not guard(update): return await deny(update)
    args = ctx.args
    if not args:
        return await update.message.reply_text("Usage: /shell `ls -la`", parse_mode="Markdown")
    command = " ".join(args)
    await update.message.reply_text(f"⚡ Command: `{command}`", parse_mode="Markdown")
    try:
        page = await get_page()
        # shell tab click
        try:
            shell = page.locator('text=Shell').first
            await shell.click(timeout=5000)
            await asyncio.sleep(1)
        except:
            pass
        # terminal mein click karke command daalo
        await page.keyboard.type(command)
        await page.keyboard.press("Enter")
        await asyncio.sleep(3)
        ss = await take_ss(page)
        await update.message.reply_photo(photo=open(ss, "rb"), caption=f"✅ `{command}` chala diya", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_ss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not guard(update): return await deny(update)
    try:
        page = await get_page()
        ss = await take_ss(page)
        await update.message.reply_photo(photo=open(ss, "rb"), caption="📸 Screenshot")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_goto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not guard(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /goto https://replit.com/@user/project")
    url = ctx.args[0]
    await update.message.reply_text(f"🔄 Ja raha hoon: `{url}`", parse_mode="Markdown")
    try:
        page = await get_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)
        ss = await take_ss(page)
        await update.message.reply_photo(photo=open(ss, "rb"), caption=f"✅ Open: {url}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_seturl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not guard(update): return await deny(update)
    if not ctx.args:
        return await update.message.reply_text("Usage: /seturl https://replit.com/@user/project")
    state["url"] = ctx.args[0]
    await update.message.reply_text(f"✅ URL set ho gaya:\n`{state['url']}`", parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not guard(update): return await deny(update)
    browser_ok = state["browser"] and state["browser"].is_connected()
    page_ok = state["page"] and not state["page"].is_closed()
    current_url = "—"
    if page_ok:
        try:
            current_url = state["page"].url
        except:
            pass
    msg = (
        f"🟢 Browser: {'Connected' if browser_ok else 'Disconnected'}\n"
        f"🟢 Page: {'Open' if page_ok else 'Closed'}\n"
        f"🌐 URL: `{current_url}`\n"
        f"📌 Default: `{state['url']}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not guard(update): return await deny(update)
    try:
        if state["browser"]:
            await state["browser"].close()
            state["browser"] = None
            state["page"] = None
        await update.message.reply_text("✅ Browser band ho gaya.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ============================================================
# MAIN
# ============================================================
def main():
    print("[*] Bot start ho raha hai...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("open",    cmd_open))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("shell",   cmd_shell))
    app.add_handler(CommandHandler("ss",      cmd_ss))
    app.add_handler(CommandHandler("goto",    cmd_goto))
    app.add_handler(CommandHandler("seturl",  cmd_seturl))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("close",   cmd_close))

    print("[+] Bot ready! Telegram pe /start bhejo.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
