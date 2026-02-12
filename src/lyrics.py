import os
import json
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

    def get_lyrics(self, video_id):
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
            if not song or 'videoDetails' not in song:
                return None
                
            browse_id = self.ytmusic.get_watch_playlist(videoId=video_id).get('lyrics')
            
            if not browse_id:
                # Fallback: Try search if direct ID fails (unlikely for YT Music matches but possible)
                return None

            lyrics_data = self.ytmusic.get_lyrics(browse_id)
            if lyrics_data and 'lyrics' in lyrics_data:
                text = lyrics_data['lyrics']
                
                # Cache it
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(text)
                
                log_info(f"📝 Lyrics cached for {video_id}")
                return text
                
        except Exception as e:
            # Often fails if song has no lyrics or isn't on YT Music
            # log_error(f"Lyrics fetch failed for {video_id}: {e}")
            pass
            
        return None

# Singleton instance
lyrics_manager = LyricsManager()