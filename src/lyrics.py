import os
import json
import requests
from urllib.parse import quote
from ytmusicapi import YTMusic
from config import CACHE_DIR
from utils import log_error, log_info

class LyricsManager:
    def __init__(self):
        self.ytmusic = YTMusic()
        if not os.path.exists(CACHE_DIR):
            os.makedirs(CACHE_DIR, exist_ok=True)

    def get_lyrics_path(self, video_id):
        return os.path.join(CACHE_DIR, f"{video_id}.lyrics")

    def get_lyrics(self, video_id, title=None, artist=None):
        """Fetch lyrics for a given YouTube video ID."""
        if not video_id: return None
        
        # 1. Check Cache
        path = self.get_lyrics_path(video_id)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                log_error(f"Error reading cached lyrics for {video_id}: {e}")

        # 2. Fetch from YouTube Music
        try:
            # Get song metadata to find lyrics ID
            song = self.ytmusic.get_song(video_id)
            if song and 'videoDetails' in song:
                browse_id = self.ytmusic.get_watch_playlist(videoId=video_id).get('lyrics')
                if browse_id:
                    lyrics_data = self.ytmusic.get_lyrics(browse_id)
                    if lyrics_data and 'lyrics' in lyrics_data:
                        text = lyrics_data['lyrics']
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write(text)
                        log_info(f"📝 Lyrics cached (YTM) for {video_id}")
                        return text
        except Exception:
            pass
            
        # 3. Fallback: LRCLIB.NET (Community sourced)
        if title and artist:
            try:
                # Clean up title/artist slightly (remove "Official Video", etc if needed)
                # But simple is often okay for LRCLIB
                url = f"https://lrclib.net/api/get?artist_name={quote(artist)}&track_name={quote(title)}"
                res = requests.get(url, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    text = data.get('plainLyrics')
                    if text:
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write(text)
                        log_info(f"📝 Lyrics cached (LRCLIB) for {video_id}")
                        return text
            except Exception:
                pass
                
        return None

# Singleton instance
lyrics_manager = LyricsManager()