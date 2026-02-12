import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')

# File Paths
CACHE_DIR = './music_cache'
CACHE_MAP_FILE = 'cache_map.json'
PLAYLIST_FILE = 'playlists.json'
SETTINGS_FILE = 'server_settings.json'
MAX_CACHE_SIZE_GB = 16

# Audio Settings
COLOR_MAIN = 0xFFD700  # Gold

# FFmpeg Options
FFMPEG_STREAM_OPTS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin',
    'options': '-vn -threads 2 -bufsize 2048k'
}
FFMPEG_LOCAL_OPTS = {
    'options': '-vn -threads 2 -bufsize 2048k'
}

# yt-dlp Options
COMMON_YDL_ARGS = {
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'socket_timeout': 30
}

YDL_PLAY_OPTS = {
    'format': 'bestaudio[ext=webm]/bestaudio/best',
    **COMMON_YDL_ARGS
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
    'format': 'bestaudio[ext=webm]/bestaudio/best',
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
