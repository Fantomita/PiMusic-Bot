import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')

# Handle YouTube Cookies from Environment
cookies = os.getenv('YOUTUBE_COOKIES')
if cookies:
    try:
        import base64
        # Check if it looks like Base64 (no spaces, certain characters)
        if " " not in cookies.strip() and len(cookies) > 100:
            try:
                decoded = base64.b64decode(cookies).decode('utf-8')
                cookies = decoded
            except:
                pass # Fallback to plain text if decoding fails

        with open('cookies.txt', 'w') as f:
            f.write(cookies)
    except Exception:
        pass

# File Paths
CACHE_DIR = './music_cache'
CACHE_MAP_FILE = 'cache_map.json'
PLAYLIST_FILE = 'playlists.json'
SETTINGS_FILE = 'server_settings.json'
MAX_CACHE_SIZE_GB = int(os.getenv('MAX_CACHE_SIZE_GB', 16))

# Audio Settings
COLOR_MAIN = 0xFFD700  # Gold

# FFmpeg Options
FFMPEG_STREAM_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin',
    'options': '-vn -threads 2 -bufsize 16384k'
}
FFMPEG_LOCAL_OPTS = {
    'options': '-vn -threads 2 -bufsize 16384k'
}

# yt-dlp Options
COMMON_YDL_ARGS = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'socket_timeout': 30,
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'referer': 'https://www.youtube.com/',
    'ignoreerrors': True,
    'no_check_certificate': True
}

if os.path.exists('cookies.txt'):
    COMMON_YDL_ARGS['cookiefile'] = 'cookies.txt'

YDL_PLAY_OPTS = {
    'format': 'bestaudio/best',
    **COMMON_YDL_ARGS
}

YDL_SINGLE_OPTS = {
    'extract_flat': 'in_playlist',
    'playlist_items': '1', 
    **COMMON_YDL_ARGS,
    'noplaylist': True 
}

YDL_FLAT_OPTS = {
    'extract_flat': 'in_playlist',
    'playlist_items': '1-100',
    **COMMON_YDL_ARGS,
    'noplaylist': False
}

YDL_SEARCH_OPTS = {
    'extract_flat': False,
    **COMMON_YDL_ARGS
}

YDL_MIX_OPTS = {
    'extract_flat': 'in_playlist',
    'playlist_items': '1-20',
    **COMMON_YDL_ARGS,
    'noplaylist': False
}

YDL_DOWNLOAD_OPTS = {
    'format': 'bestaudio/best',
    'outtmpl': f'{CACHE_DIR}/%(id)s.%(ext)s',
    'writethumbnail': True,
    'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}],
    **COMMON_YDL_ARGS
}

YDL_PLAYLIST_LOAD_OPTS = {
    'extract_flat': 'in_playlist',
    'playlist_items': '1-50',
    **COMMON_YDL_ARGS,
    'noplaylist': False
}
