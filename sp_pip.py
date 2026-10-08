# ================= AUTO DEPENDENCY INSTALLER =================
def _ensure_dependencies():
    import importlib.util
    import subprocess
    import sys
    import os
    import shutil

    FRESH = "--fresh" in sys.argv        # python sp_auto_pip.py --fresh

    packages = {
        "playwright": "playwright",
    }

    # ---- 1. fresh mode: uninstall pehle ----
    if FRESH:
        print("[SETUP] --fresh: removing existing packages...")
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", *packages.values()],
                       check=False)
        # playwright cache bhi saaf
        for cache_dir in [
            os.path.expanduser("~/.cache/ms-playwright"),
            os.path.expanduser("~/.cache/ms-playwright-go"),
        ]:
            if os.path.exists(cache_dir):
                print(f"[SETUP] removing cache: {cache_dir}")
                shutil.rmtree(cache_dir, ignore_errors=True)

    # ---- 2. install python package ----
    missing = [pkg for mod, pkg in packages.items()
               if importlib.util.find_spec(mod) is None]
    if missing or FRESH:
        print("[SETUP] Installing:", ", ".join(packages.values()))
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--upgrade", *packages.values()])

    # ---- 3. system libs (libnspr4 etc) ----
    if not _system_deps_present():
        print("[SETUP] installing system libraries for chromium...")
        # try playwright install-deps first
        rc = subprocess.run([sys.executable, "-m", "playwright", "install-deps", "chromium"],
                            check=False).returncode
        if rc != 0:
            # fallback: manual apt
            pkgs = ("libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 "
                    "libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 "
                    "libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2")
            for cmd in (["sudo", "apt-get"], ["apt-get"]):
                try:
                    subprocess.check_call(cmd + ["update"], timeout=120)
                    subprocess.check_call(cmd + ["install", "-y"] + pkgs.split(), timeout=300)
                    break
                except Exception as e:
                    print(f"[SETUP] {cmd[0]} failed: {e}")
                    continue

    # ---- 4. playwright browser binary ----
    if not _chromium_present() or FRESH:
        print("[SETUP] installing playwright chromium browser...")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"],
                              timeout=600)


def _system_deps_present():
    """check libnspr4 milta hai ya nahi (linux only)."""
    import ctypes, sys, platform
    if platform.system() != "Linux":
        return True
    try:
        ctypes.CDLL("libnspr4.so")
        return True
    except OSError:
        return False


def _chromium_present():
    """check playwright chromium binary cache mein hai."""
    import os
    cache = os.path.expanduser("~/.cache/ms-playwright")
    if not os.path.isdir(cache):
        return False
    for entry in os.listdir(cache):
        if entry.startswith("chromium") and "headless_shell" not in entry:
            return True
    return False


_ensure_dependencies()
# ==============================================================