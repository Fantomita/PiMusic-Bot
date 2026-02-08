import json
import logging
import os
import sys
import asyncio
from config import CACHE_DIR, CACHE_MAP_FILE, MAX_CACHE_SIZE_GB, PLAYLIST_FILE, SETTINGS_FILE

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bot_logs.txt", mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def log_error(msg): logging.error(msg)
def log_info(msg): logging.info(msg)

def load_json(filename):
    """Safely loads a JSON file."""
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def save_json(filename, data):
    """Safely saves data to a JSON file."""
    try:
        with open(filename, 'w') as f:
            json.dump(data, f, indent=4)
    except OSError as e:
        log_error(f"Failed to save JSON to {filename}: {e}")

# Load Initial State
cache_map = load_json(CACHE_MAP_FILE)
saved_playlists = load_json(PLAYLIST_FILE)
server_settings = load_json(SETTINGS_FILE)

def format_time(seconds):
    """Formats seconds into MM:SS or HH:MM:SS."""
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"

def _enforce_cache_limit_sync():
    """Deletes old cached files if the directory exceeds the size limit (Synchronous)."""
    max_bytes = MAX_CACHE_SIZE_GB * 1024 * 1024 * 1024
    files = []
    total_size = 0
    
    # Scan directory for usage
    with os.scandir(CACHE_DIR) as it:
        for entry in it:
            if entry.is_file():
                total_size += entry.stat().st_size
                if entry.name.endswith('.webm'):
                    files.append(entry)
    
    if total_size > max_bytes:
        # Sort by modification time (oldest first)
        files.sort(key=lambda x: x.stat().st_mtime)
        
        for entry in files:
            try:
                os.remove(entry.path)
                
                # Try to remove associated thumbnail
                thumb_path = entry.path.replace('.webm', '.jpg')
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
                
                total_size -= entry.stat().st_size
                vid_id = entry.name.replace('.webm', '')
                
                if vid_id in cache_map:
                    del cache_map[vid_id]
                
                # Stop if we are safely under the limit (buffer of 100MB)
                if total_size <= (max_bytes - 100 * 1024 * 1024):
                    break
            except OSError as e:
                log_error(f"Error cleaning cache file {entry.name}: {e}")
                
        save_json(CACHE_MAP_FILE, cache_map)

async def enforce_cache_limit(loop):
    """Async wrapper for cache limit enforcement."""
    await loop.run_in_executor(None, _enforce_cache_limit_sync)

def get_thumbnail_url(vid_id):
    """Returns local thumbnail path if cached, else remote URL."""
    if os.path.exists(f"{CACHE_DIR}/{vid_id}.jpg"):
        return f"/cache/thumb/{vid_id}.jpg"
    return f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"
