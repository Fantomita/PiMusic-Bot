import os
import json
import re
import requests
from urllib.parse import quote
from bs4 import BeautifulSoup
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
    
    def clean_string(self, s):
        # Remove common garbage for better search matching
        s = re.sub(r'\(.*?\)|\[.*?\]|official|video|audio|lyrics|feat\.|ft\.', '', s.lower())
        return re.sub(r'[^a-z0-9\s]', '', s).strip()

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
            
            clean_title = self.clean_string(title)
            clean_artist = self.clean_string(artist)
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}

            # 4. Fallback: Genius (Scraping via API search to find URL)
            try:
                # Use public API endpoint to find the song page URL
                search_url = f"https://genius.com/api/search/multi?per_page=1&q={quote(artist + ' ' + title)}"
                res = requests.get(search_url, headers=headers, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    sections = data.get('response', {}).get('sections', [])
                    for sec in sections:
                        if sec.get('type') == 'song' and sec.get('hits'):
                            song_url = sec['hits'][0]['result']['url']
                            
                            # Scrape the page
                            page = requests.get(song_url, headers=headers, timeout=5)
                            if page.status_code == 200:
                                soup = BeautifulSoup(page.text, 'html.parser')
                                # Genius lyrics are often in data-lyrics-container
                                lyrics_divs = soup.find_all('div', attrs={'data-lyrics-container': 'true'})
                                if lyrics_divs:
                                    text = "\n".join([d.get_text(separator="\n") for d in lyrics_divs])
                                    with open(path, 'w', encoding='utf-8') as f:
                                        f.write(text)
                                    log_info(f"📝 Lyrics cached (Genius) for {video_id}")
                                    return text
            except Exception:
                pass

            # 5. Fallback: AZLyrics (Direct URL Construction)
            try:
                # azlyrics format: azlyrics.com/lyrics/artist/title.html
                az_artist = re.sub(r'[^a-z0-9]', '', clean_artist)
                az_title = re.sub(r'[^a-z0-9]', '', clean_title)
                
                if az_artist and az_title:
                    url = f"https://www.azlyrics.com/lyrics/{az_artist}/{az_title}.html"
                    res = requests.get(url, headers=headers, timeout=5)
                    
                    if res.status_code == 200:
                        soup = BeautifulSoup(res.text, 'html.parser')
                        # AZLyrics usually has lyrics in a div with no class, but inside a container
                        # Look for the comment
                        for div in soup.find_all('div'):
                            if not div.attrs and "Usage of azlyrics.com" in str(div):
                                text = div.get_text(separator="\n").strip()
                                # Clean up the license text
                                text = text.split("Usage of azlyrics.com")[0].strip()
                                # Clean up header
                                if "\n" in text:
                                    parts = text.split("\n", 1)
                                    if "lyrics" in parts[0].lower():
                                        text = parts[1]
                                
                                with open(path, 'w', encoding='utf-8') as f:
                                    f.write(text)
                                log_info(f"📝 Lyrics cached (AZLyrics) for {video_id}")
                                return text
            except Exception:
                pass
                
        return None

# Singleton instance
lyrics_manager = LyricsManager()