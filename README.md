# Telegram Laptop Controller 🤖

Telegram se apne Linux laptop ko control karo — apps kholo, gaana chalao, screenshot lo,
volume/brightness set karo, WhatsApp message bhejo, aur system control karo.
Natural language (Hinglish) bhi samajhta hai (local LLM se).

## Features

- 🌐 **Apps/websites kholo** — `chatgpt kholo`, `youtube kholo`, `/web github.com`
- 🎵 **YouTube gaana play** — `/play tum hi ho` ya `boom shaka bajao`
- 🔍 **Search** — `google pe delhi weather search karo`
- 📸 **Screenshot / Webcam** — `/ss`, `/webcam` (photo Telegram pe aati hai)
- 🔊 **Volume / Brightness** — `/vol 40`, `/bright 70`
- ⌨️ **Type / Send** — `type kar hello`, `send kar` (jaha cursor ho)
- 📲 **WhatsApp message** — `/wamsg 919999999999 hello`
- 🔒 **Lock / Shutdown / Restart** — `/lock`, `system restart karo` (confirm ke saath)
- 🖥️ **Server info** — `/status`, `/disk`, `/logs`, `/docker`

---

## Doosre system par setup (step-by-step)

### 1. System tools install karo (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip \
  scrot ffmpeg xdotool brightnessctl
```

| Tool | Kis liye |
|------|----------|
| `scrot` | screenshot |
| `ffmpeg` | webcam photo |
| `xdotool` | type / click / WhatsApp (sirf X11 par chalta hai) |
| `brightnessctl` | brightness (ya GNOME gdbus se) |
| `wpctl` | volume (PipeWire ke saath aata hai) |

> Note: ye bot **X11** session par best chalta hai (Wayland par xdotool nahi chalta).
> Check: `echo $XDG_SESSION_TYPE` — `x11` aana chahiye.

### 2. Ollama (local LLM) install karo — natural language ke liye

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2:3b
```

(Sirf slash commands chahiye? toh Ollama optional hai — par `chatgpt kholo`
jaisa natural text LLM ke bina bhi chalta hai, sirf `/play X` etc. ko LLM nahi chahiye.)

### 3. Project clone + Python setup

```bash
git clone https://github.com/<your-username>/telegram-laptop-controller.git
cd telegram-laptop-controller
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 4. Apna config banao (.env)

```bash
cp .env.example .env
nano .env     # apna BOT_TOKEN aur OWNER_IDS daalo
```

- **BOT_TOKEN** — [@BotFather](https://t.me/BotFather) se naya bot bana kar token lo
- **OWNER_IDS** — [@userinfobot](https://t.me/userinfobot) se apni Telegram id lo

### 5. Bot chalao

```bash
set -a && . ./.env && set +a
venv/bin/python sysbot.py
```

`SysBot starting...` dikhe toh bot live hai. Telegram pe `/start` bhejo.

---

## Auto-start (laptop on hote hi bot chalu) — optional

`~/.config/systemd/user/sysbot.service` banao:

```ini
[Unit]
Description=Telegram Laptop Controller
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=/home/<USER>/telegram-laptop-controller
EnvironmentFile=/home/<USER>/telegram-laptop-controller/.env
Environment=DISPLAY=:0
ExecStart=/home/<USER>/telegram-laptop-controller/venv/bin/python sysbot.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Phir:

```bash
systemctl --user daemon-reload
systemctl --user enable --now sysbot
sudo loginctl enable-linger $USER    # logout par bhi chale
```

Manage karne ke commands:

```bash
systemctl --user restart sysbot     # code badle to
systemctl --user stop sysbot        # band
journalctl --user -u sysbot -f      # live logs
```

---

## Security

- Sirf `OWNER_IDS` me listed Telegram users hi bot chala sakte hain.
- `ALLOW_SHELL=0` aur `LLM_ALLOW_WRITE=0` default — destructive actions OFF.
- `.env` (token) kabhi commit mat karo — `.gitignore` me hai.
- GUI actions (app/WhatsApp/type) ke liye laptop **on + unlocked** hona chahiye.
