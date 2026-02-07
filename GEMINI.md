# MusicBot Project Context

## Project Overview
This is a comprehensive Discord Music Bot featuring a local web dashboard, audio caching, and playlist management. It is designed to run on a Linux environment (specifically optimized for Raspberry Pi with GPIO support, though functional elsewhere).

**Key Features:**
*   **Music Playback:** High-quality streaming and downloading via `yt-dlp` and `ffmpeg`.
*   **Web Dashboard:** A Flask-like (Quart) web interface exposed via ngrok for remote control (Queue, Play/Pause, Search).
*   **Local Caching:** Downloads played songs to `./music_cache` to save bandwidth on replays.
*   **Playlist System:** Supports saving queues as playlists and loading YouTube playlists.
*   **Discord UI:** Uses Buttons, Select Menus, and Slash Commands.

## Key Files
*   `bot.py`: The main entry point. Contains the Discord bot logic, Quart web server, and music player core.
*   `.env`: Configuration file for secrets.
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
No `requirements.txt` is present. Based on imports, the following are required:
```bash
pip install discord.py yt-dlp quart pyngrok python-dotenv psutil gpiozero pynacl
```

### Environment Variables (.env)
Create a `.env` file with the following keys:
```ini
DISCORD_TOKEN=your_discord_bot_token
NGROK_AUTH_TOKEN=your_ngrok_auth_token
```

## Running the Bot
To start the bot and the web dashboard:
```bash
python bot.py
```

## Development Conventions
*   **Logging:** Logs are written to `bot_logs.txt` and stdout.
*   **Formatting:** The code seems to follow standard Python practices but merges logic from previous iterations (`bot_working.py`, `bot_pretty.py`).
*   **Async:** Extensively uses `asyncio` for both Discord and Web handling.
