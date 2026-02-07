# PiMusic Bot 🎵

A feature-rich Discord Music Bot with a built-in Web Dashboard, local caching for performance, and Raspberry Pi GPIO support.

## 🚀 Features

- **High-Quality Audio:** Powered by `yt-dlp` and `ffmpeg`.
- **Web Dashboard:** Control playback, search songs, and manage the queue from any browser (tunnelled via **Cloudflare Tunnel** - No timeouts, no warnings!).
- **Local Caching:** Automatically saves played tracks to disk to save bandwidth and reduce loading times.
- **Smart Playlists:** Save your current queue or load existing YouTube playlists.
- **Auto-Play:** Keep the music going with related track suggestions.
- **System Stats:** Monitor CPU, RAM, and Temperature (optimized for Raspberry Pi).

## 🛠️ Installation

### 1. Prerequisites
- **Python 3.8+**
- **FFmpeg**: Required for audio streaming.
  ```bash
  sudo apt update && sudo apt install ffmpeg -y
  ```

### 2. Clone the Repository
```bash
git clone https://github.com/Fantomita/PiMusic-Bot.git
cd PiMusic-Bot
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configuration
Create a `.env` file in the root directory and add your bot token:
```ini
DISCORD_TOKEN=your_discord_bot_token_here
```
*(No ngrok token required! The bot automatically downloads and configures Cloudflare Tunnel.)*

## 🎮 Usage

Run the bot using:
```bash
python bot.py
```

### Discord Commands
- `/play [search or url]` - Play a song or playlist.
- `/link` - Generate a secure link to the Web Dashboard (Bot auto-joins your VC!).
- `/queue` - View the current music queue.
- `/stop` - Stop the music and disconnect.
- `/help` - View all available commands.

### Web Dashboard
Use the `/link` command in Discord to generate a unique, authenticated URL (`...trycloudflare.com`) for the control panel.
- **Auto-Install:** On the first run, the bot will automatically download the `cloudflared` binary for your system (AMD64, ARM64, or ARM).
- **Secure:** Each link is protected by a unique session token.

## 📜 License
[MIT](LICENSE)