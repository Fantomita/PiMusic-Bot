# PiMusic Bot 🎵

> **High-performance Discord Music Bot with a real-time Web Dashboard and Local Caching.**

A feature-rich Discord Music Bot featuring a built-in Web Dashboard, local caching for performance, and seamless Cloudflare Tunneling. Optimized for Raspberry Pi and low-resource environments.

## 🚀 Features

- **High-Quality Audio:** Powered by `yt-dlp` and `ffmpeg` for smooth playback.
- **Responsive Web Dashboard:** Control playback, search songs, and manage the queue from any browser.
  - *Responsive Design:* Optimized interface for both Desktop ("Command Center" view) and Mobile devices.
  - *Tunneling:* Automatically uses **Cloudflare Tunnel** to expose the dashboard securely. Starts automatically on boot, so your link is ready when you need it.
- **Local Caching:** Automatically saves played tracks to disk (`./music_cache`) to save bandwidth and prevent re-downloading.
  - *Non-Blocking I/O:* Cache cleanup and file operations run in background threads to prevent audio stutters on slow storage (SD Cards).
- **Smart Playlists:** Save your current queue as a playlist or load existing YouTube playlists.
- **Auto-Play:** Keeps the music going with related track suggestions when the queue ends.
- **System Stats:** Monitor CPU, RAM, and Temperature (optimized for Raspberry Pi).

## 🛠️ Installation

### 1. Prerequisites

- **Python 3.8+**
- **FFmpeg**: Required for audio transcoding.
  ```bash
  sudo apt update && sudo apt install ffmpeg -y
  ```

### 2. Clone the Repository

```bash
git clone https://github.com/yourusername/PiMusic-Bot.git
cd PiMusic-Bot
```

### 3. Install Dependencies

It is recommended to use a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### ⚡ Performance Boost (Highly Recommended for RPi)

For the best performance on Raspberry Pi Zero 2W or other Linux devices, **install `uvloop`**. The bot detects it automatically and uses it to replace the standard Python event loop, significantly increasing speed.

```bash
pip install uvloop
```

### 4. Configuration

Create a `.env` file in the root directory and add your bot token:

```ini
DISCORD_TOKEN=your_discord_bot_token_here
```

## 🎮 Usage

Run the bot using:

```bash
python bot.py
```

### Discord Commands

- `/play [search or url]` - Play a song or playlist.
- `/autoplay` - Toggle Auto-Play mode (suggests related songs).
- `/new` - Regenerate the current Auto-Play suggestion.
- `/link` - Generate a secure link to the Web Dashboard (now instant!).
- `/setchannel` - Bind the bot to the current text channel for notifications.
- `/queue` - View the current music queue.
- `/history` - View recently played tracks.
- `/search [query]` - Search for songs with interactive results.
- `/stop` - Stop the music, save session cache, and disconnect.
- `/dash` - Monitor system performance and storage stats.
- `/help` - View all available commands.

### Web Dashboard

Use the `/link` command in Discord to generate a unique, authenticated URL (e.g., `https://example-tunnel.trycloudflare.com`).

- **Auto-Install:** On the first run, the bot will automatically download the `cloudflared` binary for your system architecture (AMD64, ARM64, or ARM).
- **Auto-Start:** The tunnel starts when the bot launches. The `/link` command simply retrieves the active URL.
- **Secure:** Each link is protected by a unique session token generated at runtime.

## 📜 License

[MIT](LICENSE)