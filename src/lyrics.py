import os
import re
import requests
from urllib.parse import quote
from bs4 import BeautifulSoup
from ytmusicapi import YTMusic
from googlesearch import search
from config import CACHE_DIR
from utils import log_error, log_info

class LyricsManager:
    def __init__(self):
        self.ytmusic = YTMusic()
        if not os.path.exists(CACHE_DIR):
            os.makedirs(CACHE_DIR, exist_ok=True)
        self.headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}

    def get_lyrics_path(self, video_id):
        return os.path.join(CACHE_DIR, f"{video_id}.lyrics")
    
    def clean_string(self, s):
        # Remove common garbage for better search matching
        s = re.sub(r'\(.*?\)|\[.*?\]|official|video|audio|lyrics|feat\.|ft\.', '', s.lower())
        return re.sub(r'[^a-z0-9\s]', '', s).strip()

    def extract_genius(self, soup):
        # Genius: <div data-lyrics-container="true">
        lyrics_divs = soup.find_all('div', attrs={'data-lyrics-container': 'true'})
        if lyrics_divs:
            return "\n".join([d.get_text(separator="\n") for d in lyrics_divs])
        # Old Genius format
        lyrics_div = soup.find('div', class_='lyrics')
        if lyrics_div: return lyrics_div.get_text(separator="\n")
        return None

    def extract_azlyrics(self, soup):
        # AZLyrics: Div with no class, near comments
        for div in soup.find_all('div'):
            if not div.attrs and "Usage of azlyrics.com" in str(div):
                text = div.get_text(separator="\n").strip()
                return text.split("Usage of azlyrics.com")[0].strip()
        return None

    def extract_musixmatch(self, soup):
        # Musixmatch: .lyrics__content__ok and .lyrics__content__warning
        lyrics_spans = soup.find_all('span', class_='lyrics__content__ok')
        if lyrics_spans:
            return "\n".join([s.get_text(separator="\n") for s in lyrics_spans])
        return None

    def get_lyrics(self, video_id, title=None, artist=None):
        """Fetch lyrics with robust fallbacks."""
        if not video_id: return None
        
        # 1. Check Cache
        path = self.get_lyrics_path(video_id)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read()
            except: pass

        # 2. YouTube Music (Official)
        try:
            song = self.ytmusic.get_song(video_id)
            if song and 'videoDetails' in song:
                browse_id = self.ytmusic.get_watch_playlist(videoId=video_id).get('lyrics')
                if browse_id:
                    data = self.ytmusic.get_lyrics(browse_id)
                    if data and 'lyrics' in data:
                        text = data['lyrics']
                        self.save_cache(path, text, "YTM")
                        return text
        except: pass
            
        # 3. LRCLIB (Community API)
        if title and artist:
            try:
                url = f"https://lrclib.net/api/get?artist_name={quote(artist)}&track_name={quote(title)}"
                res = requests.get(url, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    text = data.get('plainLyrics')
                    if text:
                        self.save_cache(path, text, "LRCLIB")
                        return text
            except: pass
            
            # 4. Web Search Scraper (The "Generic" Fallback)
            try:
                query = f"{artist} {title} lyrics"
                log_info(f"🔎 Searching web for lyrics: {query}")
                
                # Search Google (top 5 results)
                results = search(query, num_results=5, advanced=True)
                
                for res in results:
                    url = res.url
                    domain = url.split('/')[2]
                    
                    try:
                        page = requests.get(url, headers=self.headers, timeout=5)
                        if page.status_code != 200: continue
                        
                        soup = BeautifulSoup(page.text, 'html.parser')
                        text = None
                        
                        if 'genius.com' in domain:
                            text = self.extract_genius(soup)
                        elif 'azlyrics.com' in domain:
                            text = self.extract_azlyrics(soup)
                        elif 'musixmatch.com' in domain:
                            text = self.extract_musixmatch(soup)
                            
                        if text and len(text) > 50:
                            self.save_cache(path, text, f"WebSearch ({domain})")
                            return text
                            
                    except Exception as e:
                        continue
                        
            except Exception as e:
                log_error(f"Web search lyrics failed: {e}")

        return None

    def save_cache(self, path, text, source):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            log_info(f"📝 Lyrics cached via {source}")
        except: pass

# Singleton instance
lyrics_manager = LyricsManager()