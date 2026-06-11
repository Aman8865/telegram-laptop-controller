#!/usr/bin/env python3
"""
sysbot.py — Telegram se apna Linux laptop/server securely control karne ke liye bot.
            (LLM agent isi file me integrated hai — natural language bhi chalega.)

Ye file 2 hisson me bani hai:
  HISSA 1 (LLM AGENT)  -> niche, "agent_respond" tak. LLM sochta hai + tools chalata hai.
  HISSA 2 (TELEGRAM)   -> uske baad. Messages sunta hai + reply bhejta hai.

Slash commands:
  /status        -> uptime, load, RAM, disk ek saath
  /uptime        -> kitni der se up hai
  /disk          -> df -h
  /mem           -> free -h
  /services      -> running systemd services (top 20)
  /svc <name>    -> ek service ka status
  /restart <svc> -> service restart
  /stop <svc>    -> service stop
  /startsvc <svc>-> service start
  /docker        -> docker ps
  /logs <svc> [n]-> service ke last n log lines (default 30)
  /run <cmd>     -> arbitrary shell command (OWNER ONLY, ALLOW_SHELL=1 hona chahiye)
  /whoami        -> aapka telegram user id (auth setup ke liye useful)

Natural language (koi bhi normal text, jo /command nahi hai):
  -> niche wala LLM agent samajh kar safe tools call karta hai.
     Jaise "youtube kholo" -> laptop pe YouTube khulega.

Security:
  - Har command sirf OWNER_IDS me listed users hi chala sakte hain.
  - Arbitrary shell (/run) default OFF. ALLOW_SHELL=1 par hi chalega.
  - Har command pe timeout aur output truncation hai (Telegram 4096 char limit).
"""

import os
import json
import shlex
import asyncio
import logging
from functools import wraps

from openai import AsyncOpenAI

from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ===========================================================================
#  CONFIG (environment variables se — .env file me set hote hain)
# ===========================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_IDS = {
    int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x
}
ALLOW_SHELL = os.environ.get("ALLOW_SHELL", "0").strip() == "1"
CMD_TIMEOUT = int(os.environ.get("CMD_TIMEOUT", "30"))
MAX_OUTPUT = 3500

# LLM config
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")  # Ollama
LLM_API_KEY = os.environ.get("LLM_API_KEY", "ollama")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.1")
MAX_STEPS = int(os.environ.get("LLM_MAX_STEPS", "6"))
LLM_MAX_OUTPUT = 3000
LLM_ALLOW_WRITE = os.environ.get("LLM_ALLOW_WRITE", "0").strip() == "1"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("sysbot")

# Shor kam karo — har getUpdates/sendMessage ka HTTP log mat dikhao.
# Sirf WARNING+ dikhega (error aaye toh tab bhi dikhega).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


# ===========================================================================
#  HISSA 1 — LLM AGENT (dimaag: sochna + tools chalana)
# ===========================================================================
SYSTEM_PROMPT = (
    "Tum ek Linux server administrator assistant ho jo Telegram ke through ek server "
    "manage kar raha hai. User Hinglish ya English me baat karega. Available tools use "
    "karke server ki info nikaalo ya actions chalao. Hamesha tools ka actual output dekh "
    "kar hi conclusion do — apne aap se mat maan lo. Jawab short aur clear rakho. "
    "Destructive action (restart/stop) sirf tab karo jab user ne clearly maanga ho. "
    "Agar user kahe koi app ya website kholo (jaise 'youtube kholo', 'gmail open karo'), "
    "toh open_app tool use karo; kisi specific website ke liye open_url use karo. "
    "Gaana/video chalane ko kahe (jaise 'arijit ka gana chala') toh youtube_play use karo. "
    "Volume ke liye set_volume, brightness ke liye set_brightness, "
    "screen lock ke liye lock_screen, aur band/restart ke liye shutdown_pc/restart_pc use karo. "
    "Internet pe kuch dhoondhne ko kahe toh web_search."
)


async def _shell(cmd: str) -> str:
    """Shell command run karo, stdout+stderr return karo (truncated)."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=CMD_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Timeout {CMD_TIMEOUT}s — killed."
        text = (out or b"").decode("utf-8", errors="replace").strip()
        text = text or f"(no output, exit {proc.returncode})"
        return text[:LLM_MAX_OUTPUT]
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


def _safe_name(name: str) -> bool:
    return bool(name) and all(c.isalnum() or c in ".-_@:" for c in name)


# ---- Tool implementations -------------------------------------------------
async def t_status(**_):
    return await _shell("uptime; echo; free -h; echo; df -h | head -12")


async def t_disk(**_):
    return await _shell("df -h")


async def t_mem(**_):
    return await _shell("free -h")


async def t_list_services(**_):
    return await _shell(
        "systemctl list-units --type=service --state=running --no-pager --no-legend | head -25"
    )


async def t_service_status(name: str = "", **_):
    if not _safe_name(name):
        return "Invalid service name."
    return await _shell(f"systemctl status {shlex.quote(name)} --no-pager -n 8")


async def t_docker_ps(**_):
    return await _shell('docker ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}"')


async def t_logs(name: str = "", lines: int = 30, **_):
    if not _safe_name(name):
        return "Invalid service name."
    n = min(int(lines or 30), 100)
    return await _shell(f"journalctl -u {shlex.quote(name)} -n {n} --no-pager")


async def t_restart_service(name: str = "", **_):
    if not LLM_ALLOW_WRITE:
        return "DENIED: write actions OFF hain (LLM_ALLOW_WRITE=1 set karo)."
    if not _safe_name(name):
        return "Invalid service name."
    return await _shell(f"sudo systemctl restart {shlex.quote(name)} && echo restarted {name}")


async def t_stop_service(name: str = "", **_):
    if not LLM_ALLOW_WRITE:
        return "DENIED: write actions OFF hain (LLM_ALLOW_WRITE=1 set karo)."
    if not _safe_name(name):
        return "Invalid service name."
    return await _shell(f"sudo systemctl stop {shlex.quote(name)} && echo stopped {name}")


# ---- Desktop (GUI) tools — laptop pe app/website kholne ke liye -----------
# Inhe chalane ke liye bot us machine pe chalna chahiye jiska desktop khulna hai.
GUI_ENV = "DISPLAY=${DISPLAY:-:0} "

# Aam apps ke liye shortcut naam -> launch command.
KNOWN_APPS = {
    # --- Google / basics ---
    "youtube": "xdg-open https://www.youtube.com",
    "google": "xdg-open https://www.google.com",
    "gmail": "xdg-open https://mail.google.com",
    "drive": "xdg-open https://drive.google.com",
    "maps": "xdg-open https://maps.google.com",
    "translate": "xdg-open https://translate.google.com",
    "whatsapp": "xdg-open https://web.whatsapp.com",
    # --- AI tools ---
    "chatgpt": "xdg-open https://chat.openai.com",
    "gpt": "xdg-open https://chat.openai.com",
    "gemini": "xdg-open https://gemini.google.com",
    "claude": "xdg-open https://claude.ai",
    "perplexity": "xdg-open https://www.perplexity.ai",
    # --- Social media ---
    "instagram": "xdg-open https://www.instagram.com",
    "insta": "xdg-open https://www.instagram.com",
    "facebook": "xdg-open https://www.facebook.com",
    "twitter": "xdg-open https://twitter.com",
    "x": "xdg-open https://twitter.com",
    "reddit": "xdg-open https://www.reddit.com",
    "linkedin": "xdg-open https://www.linkedin.com",
    "telegram": "xdg-open https://web.telegram.org",
    # --- Entertainment ---
    "netflix": "xdg-open https://www.netflix.com",
    "spotify": "xdg-open https://open.spotify.com",
    "prime": "xdg-open https://www.primevideo.com",
    "hotstar": "xdg-open https://www.hotstar.com",
    # --- Work / dev ---
    "github": "xdg-open https://github.com",
    # --- Local apps (browser nahi) ---
    "chrome": "google-chrome",
    "browser": "xdg-open https://www.google.com",
    "firefox": "firefox",
    "files": "xdg-open .",
    "terminal": "x-terminal-emulator",
    "calculator": "gnome-calculator",
    "settings": "gnome-control-center",
}


async def _launch_gui(cmd: str):
    """GUI app/URL launch karo. (ok: bool, error_text: str) return karta hai.

    App ko detach karke chalata hai, phir thoda ruk kar dekhta hai ki woh turant
    crash to nahi hua. Agar error/crash hua toh ok=False + asli error message."""
    # error capture karne ke liye output /dev/null nahi bhejte.
    full = f"{GUI_ENV} setsid {cmd}"
    try:
        proc = await asyncio.create_subprocess_shell(
            full, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    # 2 second tak dekho — agar itni jaldi exit hua matlab kuch gadbad.
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except asyncio.TimeoutError:
        # abhi tak chal raha = sahi se khul gaya. Background me chhod do.
        return True, ""
    # process khatam ho gaya — exit code aur output dekho.
    err = (out or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode == 0:
        return True, ""  # turant exit par bhi 0 = theek (jaise xdg-open).
    return False, err or f"exit code {proc.returncode}"


async def t_open_app(name: str = "", **_):
    """Laptop pe ek app ya known website kholo (e.g. 'youtube', 'chrome')."""
    key = (name or "").strip().lower()
    if not key:
        return "Konsa app kholu? naam batao (e.g. youtube, chrome)."
    cmd = KNOWN_APPS.get(key)
    if not cmd:
        return (
            f"❌ Sorry, '{name}' ko main nahi pehchanta.\n"
            f"Maloom apps: {', '.join(sorted(KNOWN_APPS))}.\n"
            f"Kisi bhi website ke liye: /web <url>"
        )
    ok, err = await _launch_gui(cmd)
    if ok:
        return f"Khol diya: {name}"
    return f"❌ Sorry, '{name}' nahi khul paaya.\nError: {err[:300]}"


async def t_open_url(url: str = "", **_):
    """Browser me koi bhi website kholo. URL http(s) hona chahiye."""
    u = (url or "").strip()
    if not u:
        return "URL khaali hai — kya kholu?"
    if not (u.startswith("http://") or u.startswith("https://")):
        u = "https://" + u
    ok, err = await _launch_gui(f"xdg-open {shlex.quote(u)}")
    if ok:
        return f"Browser me khol diya: {u}"
    return f"❌ Sorry, '{u}' nahi khul paaya.\nError: {err[:300]}"


# ---- Laptop control tools (gaana, volume, brightness, lock, power) --------
def _yt_find(query: str):
    """yt-dlp se pehla matching YouTube video ka (title, url) laao."""
    import yt_dlp  # lazy import — sirf zaroorat par
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    entries = info.get("entries") or []
    if not entries:
        return None, None
    v = entries[0]
    url = v.get("url") or ("https://www.youtube.com/watch?v=" + v.get("id", ""))
    return v.get("title", query), url


async def t_youtube_play(query: str = "", **_):
    """YouTube pe gaana/video search karke pehla result browser me play karo."""
    q = (query or "").strip()
    if not q:
        return "Konsa gaana/video chalu? naam batao."
    try:
        # yt-dlp network call blocking hai — thread me chalao taaki bot na atke.
        title, url = await asyncio.to_thread(_yt_find, q)
    except Exception as e:  # noqa: BLE001
        log.warning("yt-dlp fail: %s", e)
        title, url = None, None
    if not url:
        # fallback: seedha YouTube search page khol do.
        sq = q.replace(" ", "+")
        url = f"https://www.youtube.com/results?search_query={sq}"
        await _shell(f"{GUI_ENV} setsid xdg-open {shlex.quote(url)} >/dev/null 2>&1 &")
        return f"Direct play nahi mila, search khol diya: {q}"
    await _shell(f"{GUI_ENV} setsid xdg-open {shlex.quote(url)} >/dev/null 2>&1 &")
    return f"Playing: {title}"


async def t_web_search(query: str = "", **_):
    """Google pe search karke browser me kholo."""
    q = (query or "").strip()
    if not q:
        return "Kya search karu? batao."
    sq = q.replace(" ", "+")
    url = f"https://www.google.com/search?q={sq}"
    await _shell(f"{GUI_ENV} setsid xdg-open {shlex.quote(url)} >/dev/null 2>&1 &")
    return f"Google pe search kar diya: {q}"


async def t_volume(level: int = 50, **_):
    """System volume set karo (0-100). PipeWire wpctl use karta hai."""
    try:
        lvl = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return "Volume 0 se 100 ke beech number do."
    # unmute + volume set (mute hona aam dikkat hai)
    await _shell("wpctl set-mute @DEFAULT_AUDIO_SINK@ 0")
    out = await _shell(f"wpctl set-volume @DEFAULT_AUDIO_SINK@ {lvl}%")
    if out and "rror" in out.lower():
        return f"Volume set nahi hua: {out}"
    return f"Volume {lvl}% kar diya"


async def t_brightness(level: int = 50, **_):
    """Screen brightness set karo (0-100). GNOME gdbus use karta hai (bina install)."""
    try:
        lvl = max(5, min(100, int(level)))  # 5% se kam nahi, taaki screen kaali na ho
    except (TypeError, ValueError):
        return "Brightness 0 se 100 ke beech number do."
    cmd = (
        f"{GUI_ENV} gdbus call --session "
        "--dest org.gnome.SettingsDaemon.Power "
        "--object-path /org/gnome/SettingsDaemon/Power "
        "--method org.freedesktop.DBus.Properties.Set "
        "org.gnome.SettingsDaemon.Power.Screen Brightness "
        f'"<int32 {lvl}>"'
    )
    out = await _shell(cmd)
    if out and "rror" in out.lower():
        return f"Brightness set nahi hua (GNOME pe hi chalta hai): {out[:200]}"
    return f"Brightness {lvl}% kar diya"


async def t_lock(**_):
    """Laptop screen lock karo."""
    await _shell(f"{GUI_ENV} loginctl lock-session")
    return "Laptop lock kar diya"


async def t_shutdown(**_):
    if not LLM_ALLOW_WRITE:
        return "DENIED: shutdown band hai (LLM_ALLOW_WRITE=1 set karo)."
    # systemctl poweroff bina sudo chalta hai (polkit desktop user ko allow karta hai).
    await _shell("systemctl poweroff 2>&1 || shutdown -h now")
    return "Shutting down..."


async def t_restart_pc(**_):
    if not LLM_ALLOW_WRITE:
        return "DENIED: restart band hai (LLM_ALLOW_WRITE=1 set karo)."
    await _shell("systemctl reboot 2>&1 || shutdown -r now")
    return "Restarting..."


# ---- Screenshot / Webcam (file path return karte hain) -------------------
SHOT_PATH = "/tmp/sysbot_screenshot.png"
CAM_PATH = "/tmp/sysbot_webcam.jpg"


async def t_screenshot(**_):
    """Screen ka photo le kar file-path return karo (scrot se)."""
    await _shell(f"rm -f {SHOT_PATH}")
    await _shell(f"{GUI_ENV} scrot -o {SHOT_PATH}")
    import os
    if os.path.exists(SHOT_PATH) and os.path.getsize(SHOT_PATH) > 0:
        return SHOT_PATH
    return "❌ Screenshot fail — scrot nahi chala (GUI session chahiye)."


async def t_webcam(**_):
    """Webcam se ek photo le kar file-path return karo (ffmpeg se)."""
    await _shell(f"rm -f {CAM_PATH}")
    # ffmpeg se 1 frame capture (thoda warmup -ss 0.5)
    await _shell(
        f"ffmpeg -y -f v4l2 -i /dev/video0 -ss 0.5 -frames:v 1 {CAM_PATH} "
        f"-loglevel error"
    )
    import os
    if os.path.exists(CAM_PATH) and os.path.getsize(CAM_PATH) > 0:
        return CAM_PATH
    return "❌ Webcam fail — camera busy ya permission nahi."


# ---- Keyboard / Mouse control (xdotool — X11) ----------------------------
async def t_type_text(text: str = "", **_):
    """Jo bhi app focus me hai, usme text type karo (xdotool)."""
    t = text or ""
    if not t:
        return "Kya type karu? text do."
    out = await _shell(f"{GUI_ENV} xdotool type --clearmodifiers -- {shlex.quote(t)}")
    if "not found" in out.lower() or "xdotool" in out.lower() and "rror" in out.lower():
        return "❌ xdotool install nahi hai. Terminal me: sudo apt install xdotool"
    return f"Type kar diya: {t[:50]}"


async def t_press_key(key: str = "", **_):
    """Ek key dabao, jaise Return, Enter, space, ctrl+a (xdotool)."""
    k = (key or "").strip()
    if not k:
        return "Konsi key? batao (e.g. Return)."
    out = await _shell(f"{GUI_ENV} xdotool key --clearmodifiers {shlex.quote(k)}")
    if "not found" in out.lower():
        return "❌ xdotool install nahi hai."
    return f"Key dabaya: {k}"


async def t_click(button: str = "left", **_):
    """Mouse click karo (left/right/middle) — current position pe."""
    b = {"left": "1", "right": "3", "middle": "2"}.get((button or "left").lower(), "1")
    out = await _shell(f"{GUI_ENV} xdotool click {b}")
    if "not found" in out.lower():
        return "❌ xdotool install nahi hai."
    return f"Click kar diya: {button}"


# ---- WhatsApp message bhejo (ek command me) -------------------------------
async def _wa_window_id():
    """WhatsApp wali window ka id return karo (string), warna ''."""
    for q in ("--name 'WhatsApp - Google Chrome'", "--name WhatsApp"):
        out = (await _shell(f"{GUI_ENV} xdotool search {q}")).strip()
        for line in out.split("\n"):
            if line.strip().isdigit():
                return line.strip()
    return ""


async def _wa_focus():
    """WhatsApp window ko saamne laao (focus). True/False."""
    wid = await _wa_window_id()
    if wid:
        await _shell(f"{GUI_ENV} xdotool windowactivate {wid}")
        await _shell(f"{GUI_ENV} xdotool windowraise {wid}")
        return True
    return False


async def t_whatsapp_send(name: str = "", message: str = "", **_):
    """WhatsApp Web pe contact ka chat khol kar message type+send karo.

    'name' agar number hai (91...) toh number se kholta hai (zyada reliable).
    warna naam se search karta hai. xdotool + WhatsApp Web login zaroori."""
    nm = (name or "").strip()
    msg = (message or "").strip()
    if not nm or not msg:
        return "Usage: naam/number aur message dono do."
    chk = await _shell("command -v xdotool || echo NO")
    if "NO" in chk:
        return "❌ xdotool install nahi hai. Terminal me: sudo apt install xdotool"

    from urllib.parse import quote
    digits = "".join(c for c in nm if c.isdigit())

    if len(digits) >= 10:
        # NUMBER se: wa.me URL message pre-fill kar deta hai.
        url = f"https://web.whatsapp.com/send?phone={digits}&text={quote(msg)}"
        log.info("WA: opening url for %s", digits)
        await _shell(f"{GUI_ENV} setsid xdg-open {shlex.quote(url)} >/dev/null 2>&1 &")
        # WhatsApp slow load hota hai ("Starting chat") — accha time do.
        log.info("WA: waiting 20s for load")
        await asyncio.sleep(20)
        # WhatsApp window ko saamne laao aur uska window-id lo.
        wid = await _wa_window_id()
        log.info("WA: window id = %s", wid)
        if not wid:
            return "❌ WhatsApp window nahi mili. Pehle WhatsApp Web khol kar login karo."
        # window ko activate karo, aur mouse ko us window me le jaa kar text box pe le jao.
        await _shell(f"{GUI_ENV} xdotool windowactivate --sync {wid}")
        await asyncio.sleep(1.0)
        # text box WhatsApp khulte hi focus me hota hai + pre-filled text.
        # SEEDHE Return is window ko bhejo (mouse-click bilkul nahi — wahi gadbad karta tha).
        log.info("WA: sending Return to window %s", wid)
        await _shell(f"{GUI_ENV} xdotool key --window {wid} Return")
        await asyncio.sleep(0.8)
        log.info("WA: done for %s", digits)
        return f"WhatsApp pe {digits} ko bhej diya: {msg[:60]}"

    # NAAM se: WhatsApp Web khol kar search box me naam, phir chat, phir message.
    await _shell(f"{GUI_ENV} setsid xdg-open 'https://web.whatsapp.com' >/dev/null 2>&1 &")
    await asyncio.sleep(15)
    await _wa_focus()  # window saamne laao
    await asyncio.sleep(1.5)
    await _shell(f"{GUI_ENV} xdotool key ctrl+alt+slash")  # search focus
    await asyncio.sleep(1)
    await _shell(f"{GUI_ENV} xdotool type --clearmodifiers -- {shlex.quote(nm)}")
    await asyncio.sleep(2.5)
    await _shell(f"{GUI_ENV} xdotool key Down")
    await asyncio.sleep(0.5)
    await _shell(f"{GUI_ENV} xdotool key Return")
    await asyncio.sleep(2)
    # message box pe click karke focus pakka karo, phir type + send.
    geo = await _shell("xdotool getdisplaygeometry")
    try:
        sw, sh = (int(x) for x in geo.split()[:2])
    except Exception:  # noqa: BLE001
        sw, sh = 1366, 768
    await _shell(f"{GUI_ENV} xdotool mousemove {sw // 2} {int(sh * 0.95)} click 1")
    await asyncio.sleep(0.6)
    await _shell(f"{GUI_ENV} xdotool type --clearmodifiers -- {shlex.quote(msg)}")
    await asyncio.sleep(1.0)  # type pura hone do
    await _shell(f"{GUI_ENV} xdotool key Return")
    return f"WhatsApp pe '{nm}' ko bhej diya: {msg[:60]}"


async def t_run_shell(command: str = "", **_):
    if not ALLOW_SHELL:
        return "DENIED: arbitrary shell OFF hai (ALLOW_SHELL=1 set karo)."
    if not command:
        return "Empty command."
    log.warning("LLM ran shell: %s", command)
    return await _shell(command)


# name -> (callable, openai-tool-schema)
TOOLS = {
    "get_status": (t_status, "Server health: uptime, RAM, disk — sab ek saath.", {}),
    "get_disk": (t_disk, "Disk usage (df -h).", {}),
    "get_mem": (t_mem, "Memory usage (free -h).", {}),
    "list_services": (t_list_services, "Running systemd services list.", {}),
    "service_status": (
        t_service_status,
        "Ek specific service ka status.",
        {"name": {"type": "string", "description": "service name e.g. nginx"}},
    ),
    "docker_ps": (t_docker_ps, "Running docker containers.", {}),
    "get_logs": (
        t_logs,
        "Ek service ke recent journald logs.",
        {
            "name": {"type": "string", "description": "service name"},
            "lines": {"type": "integer", "description": "kitni lines (default 30, max 100)"},
        },
    ),
    "restart_service": (
        t_restart_service,
        "Ek service restart karo (DESTRUCTIVE — sirf clear request par).",
        {"name": {"type": "string", "description": "service name"}},
    ),
    "stop_service": (
        t_stop_service,
        "Ek service stop karo (DESTRUCTIVE).",
        {"name": {"type": "string", "description": "service name"}},
    ),
    "open_app": (
        t_open_app,
        "Laptop pe ek app ya known website kholo (jaise youtube, chrome, gmail, whatsapp, files).",
        {"name": {"type": "string", "description": "app/website naam, e.g. youtube"}},
    ),
    "open_url": (
        t_open_url,
        "Browser me koi bhi website URL kholo, jaise https://example.com.",
        {"url": {"type": "string", "description": "poora website URL"}},
    ),
    "youtube_play": (
        t_youtube_play,
        "YouTube pe gaana ya video search karke seedha play karo. User 'arijit ka gana chala' kahe toh ye use karo.",
        {"query": {"type": "string", "description": "gaane/video ka naam, e.g. arijit singh tum hi ho"}},
    ),
    "web_search": (
        t_web_search,
        "Google pe kuch search karke browser me kholo.",
        {"query": {"type": "string", "description": "search text"}},
    ),
    "set_volume": (
        t_volume,
        "System ka volume set karo (0-100).",
        {"level": {"type": "integer", "description": "0 se 100 ke beech, e.g. 50"}},
    ),
    "set_brightness": (
        t_brightness,
        "Screen brightness set karo (0-100).",
        {"level": {"type": "integer", "description": "0 se 100 ke beech, e.g. 70"}},
    ),
    "lock_screen": (t_lock, "Laptop screen lock karo.", {}),
    "shutdown_pc": (
        t_shutdown,
        "Laptop band (shutdown) karo (DESTRUCTIVE — sirf LLM_ALLOW_WRITE=1 par).",
        {},
    ),
    "restart_pc": (
        t_restart_pc,
        "Laptop restart karo (DESTRUCTIVE — sirf LLM_ALLOW_WRITE=1 par).",
        {},
    ),
    "type_text": (
        t_type_text,
        "Jo app abhi khula/focus me hai usme text type karo.",
        {"text": {"type": "string", "description": "jo type karna hai"}},
    ),
    "press_key": (
        t_press_key,
        "Ek keyboard key dabao, jaise Return, space, ctrl+a.",
        {"key": {"type": "string", "description": "key naam, e.g. Return"}},
    ),
    "whatsapp_send": (
        t_whatsapp_send,
        "WhatsApp Web pe kisi contact ko message bhejo. 'X ko ye bhejo' kahe toh ye use karo.",
        {
            "name": {"type": "string", "description": "contact ka naam"},
            "message": {"type": "string", "description": "jo message bhejna hai"},
        },
    ),
    "run_shell": (
        t_run_shell,
        "Arbitrary shell command (sirf ALLOW_SHELL=1 par; aakhri sahara).",
        {"command": {"type": "string", "description": "poora shell command"}},
    ),
}


def _openai_tools():
    out = []
    for name, (_, desc, props) in TOOLS.items():
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": list(props.keys()),
                    },
                },
            }
        )
    return out


async def _execute(name: str, args: dict) -> str:
    fn = TOOLS.get(name, (None,))[0]
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return await fn(**args)
    except Exception as e:  # noqa: BLE001
        return f"Tool error: {e}"


async def agent_respond(user_text: str):
    """Return (final_text, trace_list). trace = ['get_status', 'restart_service(nginx)']."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    tools = _openai_tools()
    trace = []

    for _ in range(MAX_STEPS):
        resp = await client.chat.completions.create(
            model=LLM_MODEL, messages=messages, tools=tools, tool_choice="auto"
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return (msg.content or "(koi jawab nahi mila)", trace)

        # assistant turn (tool calls ke saath) ko history me daalo
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            arg_str = ",".join(str(v) for v in args.values())
            trace.append(f"{name}({arg_str})" if arg_str else name)
            log.info("LLM tool call: %s args=%s", name, args)
            result = await _execute(name, args)
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result}
            )

    return ("Max steps reached — task complete nahi hua.", trace)


# ===========================================================================
#  HISSA 2 — TELEGRAM (darwaaza: messages sunna + reply bhejna)
# ===========================================================================
def restricted(func):
    """Sirf OWNER_IDS ke users hi command chala sakein."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **k):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in OWNER_IDS:
            log.warning("Unauthorized access attempt by user id=%s", uid)
            if update.message:
                await update.message.reply_text(
                    f"⛔ Not authorized. Aapki user id: `{uid}`\n"
                    f"Owner ko ye id OWNER_IDS me add karni hogi.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            return
        return await func(update, context, *a, **k)
    return wrapper


async def reply_code(update: Update, text: str):
    await update.message.reply_text(
        f"```\n{text}\n```", parse_mode=ParseMode.MARKDOWN
    )


# ---- Command handlers -----------------------------------------------------
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"User id: `{u.id}`\nUsername: @{u.username}", parse_mode=ParseMode.MARKDOWN
    )


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ SysBot ready. /help se commands dekho.\n"
        f"Shell mode: {'ON' if ALLOW_SHELL else 'OFF'}"
    )


@restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*SysBot commands*\n"
        "/status — uptime + load + RAM + disk\n"
        "/uptime — uptime\n"
        "/disk — df -h\n"
        "/mem — free -h\n"
        "/services — running services\n"
        "/svc <name> — ek service status\n"
        "/restart <svc> — restart\n"
        "/stop <svc> — stop\n"
        "/startsvc <svc> — start\n"
        "/docker — docker ps\n"
        "/logs <svc> [n] — last n log lines\n"
        "/run <cmd> — shell (sirf agar ALLOW_SHELL=1)\n"
        "/whoami — aapki telegram id\n\n"
        "*Laptop control (fast):*\n"
        "/youtube — YouTube kholo\n"
        "/open <app> — koi app (gmail, whatsapp, chrome…)\n"
        "/web <url> — website (e.g. /web github.com)\n"
        "/play <gaana> — YouTube pe gaana play (e.g. /play tum hi ho)\n"
        "/vol <0-100> — volume set\n"
        "/bright <0-100> — brightness set\n"
        "/lock — laptop lock\n\n"
        "_Normal text bhejo toh LLM agent samajh ke kaam karega._"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = (
        "echo '== UPTIME =='; uptime; "
        "echo; echo '== MEMORY =='; free -h; "
        "echo; echo '== DISK =='; df -h --output=source,size,used,avail,pcent,target -x tmpfs -x devtmpfs 2>/dev/null | head -15"
    )
    await reply_code(update, await _shell(cmd))


@restricted
async def uptime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_code(update, await _shell("uptime"))


@restricted
async def disk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_code(update, await _shell("df -h"))


@restricted
async def mem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_code(update, await _shell("free -h"))


@restricted
async def services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = "systemctl list-units --type=service --state=running --no-pager --no-legend | head -20"
    await reply_code(update, await _shell(cmd))


def _safe_arg(context) -> str | None:
    if not context.args:
        return None
    name = context.args[0]
    if all(c.isalnum() or c in ".-_@:" for c in name):
        return name
    return None


@restricted
async def svc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _safe_arg(context)
    if not name:
        await update.message.reply_text("Usage: /svc <service-name>")
        return
    out = await _shell(f"systemctl status {shlex.quote(name)} --no-pager -n 10")
    await reply_code(update, out)


@restricted
async def restart_svc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _safe_arg(context)
    if not name:
        await update.message.reply_text("Usage: /restart <service-name>")
        return
    out = await _shell(f"sudo systemctl restart {shlex.quote(name)} && echo 'restarted: {name}'")
    await reply_code(update, out)


@restricted
async def stop_svc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _safe_arg(context)
    if not name:
        await update.message.reply_text("Usage: /stop <service-name>")
        return
    out = await _shell(f"sudo systemctl stop {shlex.quote(name)} && echo 'stopped: {name}'")
    await reply_code(update, out)


@restricted
async def start_svc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _safe_arg(context)
    if not name:
        await update.message.reply_text("Usage: /startsvc <service-name>")
        return
    out = await _shell(f"sudo systemctl start {shlex.quote(name)} && echo 'started: {name}'")
    await reply_code(update, out)


@restricted
async def docker_ps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = 'docker ps --format "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}"'
    await reply_code(update, await _shell(cmd))


@restricted
async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _safe_arg(context)
    if not name:
        await update.message.reply_text("Usage: /logs <service-name> [lines]")
        return
    n = 30
    if len(context.args) > 1 and context.args[1].isdigit():
        n = min(int(context.args[1]), 100)
    out = await _shell(f"journalctl -u {shlex.quote(name)} -n {n} --no-pager")
    await reply_code(update, out)


@restricted
async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ALLOW_SHELL:
        await update.message.reply_text(
            "\U0001f512 Shell mode OFF hai. Enable karne ke liye ALLOW_SHELL=1 set karo "
            "(samajh-soch ke — ye full shell access deta hai)."
        )
        return
    if not context.args:
        await update.message.reply_text("Usage: /run <command>")
        return
    cmd = update.message.text.partition(" ")[2]
    log.info("Owner %s ran shell: %s", update.effective_user.id, cmd)
    await reply_code(update, await _shell(cmd))


# ---- Direct app/website kholne wale commands (FAST — bina LLM ke) ---------
@restricted
async def open_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out = await t_open_app(name="youtube")
    await update.message.reply_text("▶️ " + out)


@restricted
async def open_app_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = _safe_arg(context)
    if not name:
        await update.message.reply_text(
            "Usage: /open <app>\nMaloom apps: " + ", ".join(sorted(KNOWN_APPS))
        )
        return
    out = await t_open_app(name=name)
    await update.message.reply_text("🚀 " + out)


@restricted
async def open_web_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /web <url>\nMisaal: /web github.com")
        return
    url = context.args[0]
    out = await t_open_url(url=url)
    await update.message.reply_text("🌐 " + out)


@restricted
async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /play <gaana>\nMisaal: /play tum hi ho arijit")
        return
    query = " ".join(context.args)
    await update.message.reply_text("🔎 Dhoond raha hoon...")
    out = await t_youtube_play(query=query)
    await update.message.reply_text("▶️ " + out)


@restricted
async def vol_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /vol <0-100>\nMisaal: /vol 40")
        return
    out = await t_volume(level=int(context.args[0]))
    await update.message.reply_text("🔊 " + out)


@restricted
async def bright_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /bright <0-100>\nMisaal: /bright 70")
        return
    out = await t_brightness(level=int(context.args[0]))
    await update.message.reply_text("💡 " + out)


@restricted
async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out = await t_lock()
    await update.message.reply_text("🔒 " + out)


# ---- Screenshot / Webcam / Input / WhatsApp / Power commands -------------
import os as _os  # photo file check ke liye


async def _send_photo_or_err(update: Update, result: str, caption: str):
    """result agar file-path hai toh photo bhejo, warna error text bhejo."""
    if result and _os.path.exists(result):
        with open(result, "rb") as f:
            await update.message.reply_photo(photo=f, caption=caption)
    else:
        await update.message.reply_text(result or "❌ Kuch nahi mila.")


@restricted
async def screenshot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Screenshot le raha hoon...")
    out = await t_screenshot()
    await _send_photo_or_err(update, out, "🖥️ Aapki screen")


@restricted
async def webcam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📷 Webcam photo le raha hoon...")
    out = await t_webcam()
    await _send_photo_or_err(update, out, "📷 Webcam")


@restricted
async def type_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /type <text>\nMisaal: /type hello world")
        return
    text = update.message.text.partition(" ")[2]
    out = await t_type_text(text=text)
    await update.message.reply_text("⌨️ " + out)


@restricted
async def key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /key <key>\nMisaal: /key Return  ya  /key ctrl+a")
        return
    out = await t_press_key(key=context.args[0])
    await update.message.reply_text("⌨️ " + out)


@restricted
async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enter dabao (message/form bhejne ke liye). /send ya 'send kar'."""
    await t_press_key(key="Return")
    await update.message.reply_text("📨 Bhej diya (Enter dabaya)")


@restricted
async def click_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btn = context.args[0] if context.args else "left"
    out = await t_click(button=btn)
    await update.message.reply_text("🖱️ " + out)


@restricted
async def wamsg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /wamsg <naam> <message...> — pehla shabd naam, baaki message
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /wamsg <naam> <message>\nMisaal: /wamsg Mummy main aa raha hoon"
        )
        return
    name = context.args[0]
    message = " ".join(context.args[1:])
    await update.message.reply_text(f"📲 WhatsApp khol kar '{name}' ko bhej raha hoon... (thoda ruko)")
    out = await t_whatsapp_send(name=name, message=message)
    await update.message.reply_text("✅ " + out)


@restricted
async def shutdown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out = await t_shutdown()
    await update.message.reply_text("⏻ " + out)


@restricted
async def restart_pc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out = await t_restart_pc()
    await update.message.reply_text("🔄 " + out)


# "kholo" type words — inme se koi bhi message me ho toh ye app-open request hai.
_OPEN_WORDS = ("open", "khol", "kholo", "kholdo", "chalu", "chala", "launch", "start kar")

# common typo / likhne ke alag tareeke -> sahi app naam.
_APP_ALIASES = {
    "youtube": "youtube", "you tube": "youtube", "u tube": "youtube",
    "yt": "youtube", "your": "youtube",  # "open your" jaisa typo
    "whats app": "whatsapp", "wa": "whatsapp",
    "g mail": "gmail", "mail": "gmail",
}


def _word_in(word: str, low: str) -> bool:
    """'x' ko 'netflix' ke andar match na kare — poora shabd hi match ho."""
    import re
    return re.search(r"\b" + re.escape(word) + r"\b", low) is not None


def _detect_open_request(text: str):
    """Agar message 'youtube kholo' jaisa hai toh app-naam return karo, warna None.

    LLM ko poochne se pehle ye fast shortcut chalta hai (slash command jaisa pakka)."""
    low = text.lower()
    if not any(w in low for w in _OPEN_WORDS):
        return None
    # pehle known apps me se exact naam dekho (poora shabd match)
    for app_name in KNOWN_APPS:
        if _word_in(app_name, low):
            return app_name
    # phir typo/alias dekho (poora shabd match)
    for alias, real in _APP_ALIASES.items():
        if _word_in(alias, low):
            return real
    return None


# "gaana chalao" type words — inke baad/aas-paas jo aaye woh gaane ka naam.
_PLAY_WORDS = ("play", "gaana", "gana", "song", "bajao", "baja", "sunao", "suna")
# ye shabd gaane ke naam ka hissa nahi — inhe naam se hata do.
_PLAY_STOP = {
    "play", "song", "gaana", "gana", "bajao", "baja", "sunao", "suna",
    "youtube", "yt", "pe", "par", "pr", "on", "kar", "karo", "do", "de",
    "open", "and", "aur", "chala", "chalao", "chalu", "mera", "ek", "koi",
    "the", "a", "me", "muje", "mujhe",
}


def _detect_play_request(text: str):
    """Agar message 'X gaana chala' / 'play song X' jaisa hai toh gaane ka naam laao."""
    low = text.lower()
    if not any(w in low for w in _PLAY_WORDS):
        return None
    # bekaar shabd hata kar jo bachra, wahi gaane ka naam.
    import re
    words = re.findall(r"[a-z0-9ऀ-ॿ]+", low)  # english + hindi
    name = " ".join(w for w in words if w not in _PLAY_STOP).strip()
    return name or None


# search request ke shabd
_SEARCH_WORDS = ("search", "google", "dhoondh", "dhundo", "khojo", "find", "poocho", "pucho")
# search query se hatane wale shabd
_SEARCH_STOP = {
    "search", "google", "dhoondh", "dhundo", "khojo", "find", "poocho", "pucho",
    "kar", "karo", "kardo", "do", "de", "pe", "par", "pr", "on", "me", "in",
    "open", "khol", "kholo", "and", "aur", "ye", "yeh", "this", "for", "ek", "koi",
    "chatgpt", "gpt", "youtube", "yt", "browser",
}


def _detect_search_request(text: str):
    """'google pe X search karo' / 'chatgpt pe X poocho' detect karo.

    Return: (where, query) — where = 'youtube'|'chatgpt'|'google', warna None."""
    low = text.lower()
    if not any(w in low for w in _SEARCH_WORDS):
        return None
    import re
    # kahan search karna hai?
    if _word_in("youtube", low) or _word_in("yt", low):
        where = "youtube"
    elif _word_in("chatgpt", low) or _word_in("gpt", low) or _word_in("poocho", low) or _word_in("pucho", low):
        where = "chatgpt"
    else:
        where = "google"  # default
    words = re.findall(r"[a-z0-9ऀ-ॿ]+", low)
    query = " ".join(w for w in words if w not in _SEARCH_STOP).strip()
    if not query:
        return None
    return where, query


async def t_chatgpt_ask(query: str = "", **_):
    """ChatGPT khol kar sawaal pre-fill karo (best-effort)."""
    q = (query or "").strip()
    if not q:
        return "Kya poochu? sawaal batao."
    from urllib.parse import quote
    url = "https://chat.openai.com/?q=" + quote(q)
    ok, err = await _launch_gui(f"xdg-open {shlex.quote(url)}")
    if ok:
        return f"ChatGPT khol diya, sawaal: {q}\n(login chahiye to ho sakta hai)"
    return f"❌ Sorry, ChatGPT nahi khula.\nError: {err[:200]}"


# WhatsApp message bhejne ke shabd.
_WA_WORDS = ("whatsapp", "whats app", "wa ", "whatsap")


# "type kar X" / "ab likh X" ke shuruaati shabd.
_TYPE_PREFIXES = (
    "ab type kar ", "type kar ", "type karo ", "type kro ", "type ",
    "ab likh ", "likh do ", "likho ", "likh ", "ab likho ",
)


def _detect_type_request(text: str):
    """'ab type kar hello' jaisa message? to typing wala text return karo, warna None.

    Sirf tab match jab message in prefixes se SHURU ho — taaki normal baat na pakde."""
    low = text.lower()
    for p in _TYPE_PREFIXES:
        if low.startswith(p):
            # original text se utna hi hissa kaato (case bachane ke liye).
            return text[len(p):].strip() or None
    return None


# "send/bhej/enter" jaise koi bhi shabd ho toh ye send-request hai.
_SEND_HINTS = ("send", "bhej", "enter", "press", "dabao", "dab do", "bhejdo")
# ye shabd ho to ye send NAHI hai (kuch aur kaam hai) — skip karo.
_SEND_BLOCK = ("whatsapp", "message", "msg", "type", "likh", "open", "khol",
               "youtube", "play", "search", "gaana", "screenshot")


def _detect_send_request(text: str):
    """'send kro', 'ab bhej de', 'bhejo yaar', 'enter dabao' — kuch bhi.

    Smart hai: chhota message ho + 'send/bhej/enter' ho + koi aur kaam ke
    shabd na ho + number na ho. Tab Enter dabana hai."""
    low = text.lower().strip()
    words = low.split()
    if len(words) > 4:           # lamba message = ye send nahi, kuch aur baat hai
        return False
    if any(c.isdigit() for c in low):  # number hai = shायad whatsapp/kuch aur
        return False
    if any(b in low for b in _SEND_BLOCK):  # 'type'/'whatsapp' etc. = ye send nahi
        return False
    return any(h in low for h in _SEND_HINTS)


def _detect_whatsapp_request(text: str):
    """'whatsapp pe 91xxx ko hello bhejo' detect karo.

    Return: (target, message) — target = number ya naam, warna None."""
    low = text.lower()
    if "whatsapp" not in low and "whats app" not in low and "whatsap" not in low:
        return None
    if not any(w in low for w in ("bhej", "send", "message", "msg", "likh", "bol")):
        return None
    import re
    # number dhoondo (10+ digit)
    m = re.search(r"(\+?\d[\d\s-]{8,}\d)", text)
    target = None
    if m:
        target = "".join(c for c in m.group(1) if c.isdigit())
    # message nikalo: "message"/"bhejo"/"send" ke aas-paas ke shabd hata kar
    # sabse aasaan: 'message' ya 'msg' ke baad jo aaye, number/keywords hata kar.
    cleaned = text
    cleaned = re.sub(r"(\+?\d[\d\s-]{8,}\d)", " ", cleaned)  # number hata do
    words = cleaned.split()
    stop = {
        "open", "whatsapp", "whats", "app", "send", "message", "msg", "bhejo",
        "bhej", "bhejdo", "karo", "kar", "kro", "ko", "pe", "par", "pr", "do",
        "de", "likh", "likho", "bol", "and", "aur", "the", "a", "to",
    }
    msg = " ".join(w for w in words if w.lower() not in stop).strip()
    # agar target na mila, toh msg ke pehle shabd ko naam maan lo
    if not target:
        parts = msg.split()
        if len(parts) >= 2:
            target = parts[0]
            msg = " ".join(parts[1:])
        else:
            return None
    if not target or not msg:
        return None
    return target, msg


# ---- Har incoming message terminal me dikhao (commands + text dono) -------
async def log_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group -1 me chalta hai — sabse pehle. Sirf print karta hai, kuch rokta nahi."""
    msg = update.message
    if not msg:
        return
    u = update.effective_user
    who = f"@{u.username}" if u and u.username else "?"
    uid = u.id if u else "?"
    text = msg.text if msg.text is not None else f"<{msg.effective_attachment.__class__.__name__ if msg.effective_attachment else 'non-text'}>"
    print(f"\n📩 INPUT  from {who} (id={uid}): {text!r}", flush=True)


# power action ke shabd (reboot / shutdown).
_REBOOT_HINTS = ("restart", "reboot", "reastart")
_SHUTDOWN_HINTS = ("shutdown", "shut down", "band kar", "band kr", "power off", "poweroff", "switch off")
# pending power-action: 'reboot' ya 'shutdown' ya None. Confirm ke liye.
_pending_power = {"action": None}
_YES_WORDS = ("haan", "ha", "yes", "yep", "y", "kar do", "kardo", "ok", "okay", "confirm", "pakka")


def _is_power_request(low: str):
    """'system restart/band karo' detect karo. Return 'reboot'/'shutdown'/None."""
    # 'system' ya 'laptop' ya 'pc' ho toh hi power-action (warna service restart confuse ho)
    if not any(w in low for w in ("system", "laptop", "pc", "computer", "machine")):
        return None
    if any(h in low for h in _REBOOT_HINTS):
        return "reboot"
    if any(h in low for h in _SHUTDOWN_HINTS):
        return "shutdown"
    return None


# ---- Natural language -> LLM agent ---------------------------------------
@restricted
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    low = text.lower()

    # FAST PATH -2: power (reboot/shutdown) with CONFIRM.
    # pehle: agar pichli baar power-action pending tha aur ab 'haan' aaya -> karo.
    if _pending_power["action"] and low.strip() in _YES_WORDS:
        act = _pending_power["action"]
        _pending_power["action"] = None
        if act == "reboot":
            await update.message.reply_text("🔄 Restart kar raha hoon... bye!")
            await t_restart_pc()
        else:
            await update.message.reply_text("⏻ Shutdown kar raha hoon... bye!")
            await t_shutdown()
        return
    # naya power request? -> confirm maango.
    pw = _is_power_request(low)
    if pw:
        _pending_power["action"] = pw
        word = "RESTART" if pw == "reboot" else "BAND (shutdown)"
        await update.message.reply_text(
            f"⚠️ Pakka laptop {word} karna hai?\n"
            f"Haan karne ke liye *haan* ya *yes* bhejo.\n"
            f"(Cancel karne ke liye kuch aur bhejo.)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    # koi aur message aaya -> pending power cancel ho jaye.
    if _pending_power["action"]:
        _pending_power["action"] = None

    # FAST PATH -1: simple laptop actions (screenshot, webcam, lock).
    # Ye photo/action turant karte hain — LLM ke bina.
    if any(w in low for w in ("screenshot", "screen shot", "ss lo", "screen ka photo")):
        await update.message.reply_text("📸 Screenshot le raha hoon...")
        out = await t_screenshot()
        await _send_photo_or_err(update, out, "🖥️ Aapki screen")
        return
    if any(w in low for w in ("webcam", "web cam", "camera", "selfie", "mera photo")):
        await update.message.reply_text("📷 Webcam photo le raha hoon...")
        out = await t_webcam()
        await _send_photo_or_err(update, out, "📷 Webcam")
        return
    if any(w in low for w in ("lock kar", "lock kr", "laptop lock", "screen lock")):
        out = await t_lock()
        await update.message.reply_text("🔒 " + out)
        return

    # FAST PATH -0b: "send kro" / "ab bhej de" / "bhejo yaar" — Enter dabao.
    # (ye whatsapp-detect se PEHLE — warna 'send' usme chala jaye.)
    if _detect_send_request(text):
        await t_press_key(key="Return")
        await update.message.reply_text("📨 Bhej diya (Enter dabaya)")
        return

    # FAST PATH -0c: "type kar X" / "ab likh X" — jaha cursor hai waha type karo.
    typ = _detect_type_request(text)
    if typ is not None:
        out = await t_type_text(text=typ)
        await update.message.reply_text("⌨️ " + out)
        return

    # FAST PATH 0a: "whatsapp pe 91xxx ko hello bhejo"?
    wa = _detect_whatsapp_request(text)
    if wa:
        target, message = wa
        await update.message.reply_text(f"📲 WhatsApp khol kar '{target}' ko bhej raha hoon... (~12 sec ruko)")
        try:
            out = await t_whatsapp_send(name=target, message=message)
        except Exception as e:  # noqa: BLE001
            log.warning("whatsapp fail: %s", e)
            await update.message.reply_text(f"❌ Sorry, WhatsApp message nahi gaya.\nError: {e}")
            return
        await update.message.reply_text("✅ " + out)
        return

    # FAST PATH 0: "google pe X search karo" / "chatgpt pe X poocho"?
    sr = _detect_search_request(text)
    if sr:
        where, query = sr
        try:
            if where == "youtube":
                await update.message.reply_text("🔎 YouTube search...")
                out = await t_youtube_play(query=query)
            elif where == "chatgpt":
                out = await t_chatgpt_ask(query=query)
            else:
                out = await t_web_search(query=query)
        except Exception as e:  # noqa: BLE001
            log.warning("search fail: %s", e)
            await update.message.reply_text(f"❌ Sorry, search nahi hua.\nError: {e}")
            return
        await update.message.reply_text("🔍 " + out)
        return

    # FAST PATH 1: "X gaana chala" / "play song X"? Seedha gaana play karo.
    # (youtube-open se pehle check karo — "play" zyada specific hai.)
    song = _detect_play_request(text)
    if song:
        await update.message.reply_text("🔎 Dhoond raha hoon...")
        try:
            out = await t_youtube_play(query=song)
        except Exception as e:  # noqa: BLE001
            log.warning("play fail: %s", e)
            await update.message.reply_text(f"❌ Sorry, gaana play nahi hua.\nError: {e}")
            return
        await update.message.reply_text("▶️ " + out)
        return

    # FAST PATH 2: "youtube kholo" jaisa message? Seedha kholo, LLM skip karo.
    app_name = _detect_open_request(text)
    if app_name:
        try:
            out = await t_open_app(name=app_name)
        except Exception as e:  # noqa: BLE001
            log.warning("open fail: %s", e)
            await update.message.reply_text(f"❌ Sorry, '{app_name}' nahi khula.\nError: {e}")
            return
        await update.message.reply_text("🚀 " + out)
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        reply, trace = await agent_respond(text)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"⚠️ LLM error: {e}")
        return
    if trace:
        reply = "\U0001f527 " + " → ".join(trace) + "\n\n" + reply
    if len(reply) > 4000:
        reply = reply[:4000] + "\n... (truncated)"
    await update.message.reply_text(reply)


# ===========================================================================
#  MAIN
# ===========================================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var set karo (.env me).")
    if not OWNER_IDS:
        log.warning("OWNER_IDS khaali hai — koi command nahi chalega. /whoami se id lo.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # har incoming message terminal me print karo (sabse pehle, block kiye bina)
    app.add_handler(MessageHandler(filters.ALL, log_incoming), group=-1)

    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("uptime", uptime))
    app.add_handler(CommandHandler("disk", disk))
    app.add_handler(CommandHandler("mem", mem))
    app.add_handler(CommandHandler("services", services))
    app.add_handler(CommandHandler("svc", svc))
    app.add_handler(CommandHandler("restart", restart_svc))
    app.add_handler(CommandHandler("stop", stop_svc))
    app.add_handler(CommandHandler("startsvc", start_svc))
    app.add_handler(CommandHandler("docker", docker_ps))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("run", run_cmd))

    # app/website kholne wale fast commands (bina LLM)
    app.add_handler(CommandHandler("youtube", open_youtube))
    app.add_handler(CommandHandler("open", open_app_cmd))
    app.add_handler(CommandHandler("web", open_web_cmd))

    # laptop control fast commands (bina LLM)
    app.add_handler(CommandHandler("play", play_cmd))
    app.add_handler(CommandHandler("vol", vol_cmd))
    app.add_handler(CommandHandler("bright", bright_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))

    # screenshot / webcam / input / whatsapp / power
    app.add_handler(CommandHandler("screenshot", screenshot_cmd))
    app.add_handler(CommandHandler("ss", screenshot_cmd))
    app.add_handler(CommandHandler("webcam", webcam_cmd))
    app.add_handler(CommandHandler("type", type_cmd))
    app.add_handler(CommandHandler("key", key_cmd))
    app.add_handler(CommandHandler("send", send_cmd))
    app.add_handler(CommandHandler("click", click_cmd))
    app.add_handler(CommandHandler("wamsg", wamsg_cmd))
    app.add_handler(CommandHandler("shutdown", shutdown_cmd))
    app.add_handler(CommandHandler("reboot", restart_pc_cmd))

    # natural language handler (commands ke baad register hota hai).
    # block=False -> LLM background me chalta hai, baaki messages ko nahi rokta.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text, block=False))

    log.info("SysBot starting... (shell mode: %s)", "ON" if ALLOW_SHELL else "OFF")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
