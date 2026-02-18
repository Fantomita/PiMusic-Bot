# PiMusic Bot 🎵

> **High-performance Discord Music Bot with a real-time Web Dashboard and Local Caching.**

A feature-rich Discord Music Bot featuring a built-in Web Dashboard, local caching for performance, and seamless Cloudflare Tunneling. Optimized for both **Raspberry Pi** and **Cloud Environments (Koyeb)**.

## 🚀 Features

- **High-Quality Audio:** Powered by `yt-dlp` and `ffmpeg` with optimized buffering.
- **Responsive Web Dashboard:** Control playback and manage the queue from any browser.
- **Smart Tunneling:** Automatically uses Cloudflare Tunnel (on RPi) or direct public URLs (on Cloud) for dashboard access.
- **Local Caching:** Automatically saves played tracks to disk to save bandwidth.
- **YouTube Bypass:** Built-in support for YouTube cookies to bypass bot detection in data centers.
- **Interactive Games:** Includes a "Guess the Song" quiz game.

---

## 🛠️ Deployment Methods

Choose the method that fits your needs.

### Option A: Raspberry Pi (Local)
*Best for: 24/7 home use, zero cost, and easier YouTube access.*

1. **Install FFmpeg:**
   ```bash
   sudo apt update && sudo apt install ffmpeg -y
   ```
2. **Clone & Install:**
   ```bash
   git clone https://github.com/Fantomita/PiMusic-Bot.git
   cd PiMusic-Bot
   pip install -r requirements.txt
   pip install uvloop  # Highly recommended for RPi Zero 2W
   ```
3. **Configure:**
   Create a `.env` file:
   ```ini
   DISCORD_TOKEN=your_token
   ```
4. **Run:**
   ```bash
   python src/bot.py
   ```
   *Note: On first run, the bot will automatically download the `cloudflared` binary for your Pi's architecture.*

### Option B: Koyeb / Docker (Cloud)
*Best for: High uptime, no hardware needed, and remote management.*

1. **Koyeb Setup:** Create a new **Web Service** from your GitHub repository.
2. **Environment Variables:** Set the following in the Koyeb Dashboard:
   - `DISCORD_TOKEN`: Your bot token (as a Secret).
   - `MAX_CACHE_SIZE_GB`: Set to `0` (Koyeb storage is ephemeral).
   - `PUBLIC_URL`: Your app's Koyeb URL (e.g., `https://app-name.koyeb.app`).
   - `YOUTUBE_COOKIES`: (Required for Cloud) Your Base64-encoded `cookies.txt` (see below).
3. **Instance Size:** Works on the **Free (Nano)** tier.

---

## 🍪 YouTube Cookies (Bypass Bot Detection)

Cloud providers (like Koyeb) are often blocked by YouTube. To fix this:
1. Use a browser extension (like "Get cookies.txt LOCALLY") to export your cookies from YouTube.
2. **On RPi:** Place the `cookies.txt` file in the root folder.
3. **On Cloud:** Encode your `cookies.txt` to Base64:
   - Linux/Mac: `base64 -w 0 cookies.txt`
   - Windows: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))`
4. Set the resulting string as the `YOUTUBE_COOKIES` environment variable.

---

## 🌍 Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DISCORD_TOKEN` | Discord Bot Token | None |
| `PORT` | Port for the Web Dashboard | `5000` |
| `PUBLIC_URL` | Pre-defined URL (Disables Cloudflare Tunnel) | `None` |
| `MAX_CACHE_SIZE_GB` | Max size of the music cache in GB | `16` |
| `YOUTUBE_COOKIES` | Base64 encoded `cookies.txt` content | `None` |

## 🎮 Commands

- `/play [query]` - Play a song or playlist.
- `/link` - Get the link to your Web Dashboard.
- `/guess` - Start a song quiz game.
- `/dash` - Monitor system stats (CPU, RAM, Cache).
- `/help` - View all commands.

## 📜 License
[MIT](LICENSE)
