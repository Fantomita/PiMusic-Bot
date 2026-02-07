# MusicBot Project Context

## Project Overview
This is a comprehensive Discord Music Bot featuring a local web dashboard, audio caching, and playlist management. It is designed to run on a Linux environment (specifically optimized for Raspberry Pi).

**Key Features:**
*   **Music Playback:** High-quality streaming and downloading via `yt-dlp` and `ffmpeg`.
*   **Web Dashboard:** A Flask-like (Quart) web interface exposed via **Cloudflare Tunnel** for remote control.
*   **Local Caching:** Downloads played songs to `./music_cache` to save bandwidth on replays.
*   **Playlist System:** Supports saving queues as playlists and loading YouTube playlists.
*   **Discord UI:** Uses Buttons, Select Menus, and Slash Commands.

## Key Files
*   `bot.py`: The main entry point. Contains the Discord bot logic, Quart web server, Cloudflare Tunnel manager, and music player core.
*   `.env`: Configuration file for secrets (only `DISCORD_TOKEN` needed).
*   `server_settings.json`: Stores guild-specific configuration (e.g., bound text channels).
*   `playlists.json`: Database of user-saved playlists.
*   `cache_map.json`: Index of locally cached audio files.
*   `music_cache/`: Directory where downloaded audio files (`.webm`) and thumbnails are stored.

## Setup & Configuration

### Prerequisites
*   Python 3.8+
*   FFmpeg (Required for audio processing)
    ```bash
    sudo apt install ffmpeg
    ```

### Dependencies
```bash
pip install discord.py yt-dlp quart requests python-dotenv psutil pynacl
```

### Environment Variables (.env)
Create a `.env` file with:
```ini
DISCORD_TOKEN=your_discord_bot_token
```

## Running the Bot
To start the bot and the web dashboard:
```bash
python bot.py
```

## Architecture Notes
*   **Tunneling:** The bot automatically downloads the `cloudflared` binary for the host architecture (AMD64/ARM64/ARM) and establishes a tunnel to `localhost:5000`.
*   **Async:** Extensively uses `asyncio` for Discord, Quart, and background tasks (like downloading the binary).
*   **Logging:** Logs are written to `bot_logs.txt`.