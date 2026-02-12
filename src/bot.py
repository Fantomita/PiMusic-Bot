"""
PiMusic Bot - Discord Music Bot with Web Dashboard
Optimized for Raspberry Pi / Linux Environments.
"""

import asyncio
import datetime
import difflib
import logging
import os
import unicodedata
import platform
import random
import re
import shutil
import stat
import subprocess
import sys
import time
from uuid import uuid4

import discord
import psutil
import requests
import yt_dlp
from discord import app_commands, ui
from discord.ext import commands, tasks

from config import (
    CACHE_DIR, CACHE_MAP_FILE, COLOR_MAIN, FFMPEG_LOCAL_OPTS, FFMPEG_STREAM_OPTS,
    MAX_CACHE_SIZE_GB, PLAYLIST_FILE, SETTINGS_FILE, TOKEN, YDL_DOWNLOAD_OPTS,
    YDL_FLAT_OPTS, YDL_MIX_OPTS, YDL_PLAY_OPTS, YDL_PLAYLIST_LOAD_OPTS,
    YDL_SEARCH_OPTS, COMMON_YDL_ARGS
)
from utils import (
    log_error, log_info, load_json, save_json, format_time, 
    enforce_cache_limit, get_thumbnail_url, cache_map, saved_playlists, server_settings
)

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================

# --- System Optimization ---
sys.dont_write_bytecode = True
try:
    os.nice(-15)  # Higher priority for audio process
except Exception:
    pass

if not TOKEN:
    log_error("❌ ERROR: DISCORD_TOKEN missing.")
    sys.exit(1)

if not os.path.exists(CACHE_DIR): 
    os.makedirs(CACHE_DIR)

from web import app, set_bot_instance

# ==========================================
# 5. DISCORD UI CLASSES
# ==========================================

class ServerState:
    """Stores the music state for a single guild."""
    def __init__(self):
        self.queue = []
        self.current_track = None
        self.last_interaction = datetime.datetime.now()
        self.processing_next = False 
        self.history = []
        self.autoplay = False
        self.fetching_autoplay = False
        self.stopping = False
        self.last_text_channel = None 
        self.game = None

class SelectionMenu(ui.Select):
    """Dropdown menu for search results."""
    def __init__(self, entries, cog, ctx):
        options = []
        for entry in entries[:10]:
            title = (entry.get('title', 'Unknown')[:90] + '..') if len(entry.get('title', '')) > 90 else entry.get('title', 'Unknown')
            val = f"https://www.youtube.com/watch?v={entry['id']}" if entry.get('id') else entry.get('url')
            if val: options.append(discord.SelectOption(label=title, value=val))
        super().__init__(placeholder="Select a song...", options=options)
        self.cog, self.ctx = cog, ctx

    async def callback(self, interaction):
        if interaction.user != self.ctx.author: return
        await interaction.response.edit_message(content="✅ **Confirmed.**", view=None)
        await self.cog.prepare_song(self.ctx, self.values[0])

class SelectionView(ui.View):
    """View container for the selection menu."""
    def __init__(self, entries, cog, ctx):
        super().__init__(timeout=30)
        self.add_item(SelectionMenu(entries, cog, ctx))
        self.message = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if self.message:
                await self.message.edit(content="⌛ **Search expired.**", view=self)
        except: pass

class MusicControlView(ui.View):
    """Persistent buttons for the 'Now Playing' message."""
    def __init__(self, cog, guild_id):
        super().__init__(timeout=None)
        self.cog, self.guild_id = cog, guild_id
    @ui.button(emoji="⏯️", style=discord.ButtonStyle.blurple)
    async def play_pause(self, interaction, button):
        vc = interaction.guild.voice_client
        if vc: 
            if vc.is_paused(): vc.resume()
            elif vc.is_playing(): vc.pause()
        await interaction.response.defer()
    @ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction, button):
        if interaction.guild.voice_client: interaction.guild.voice_client.stop()
        await interaction.response.defer()
    @ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction, button):
        state = self.cog.get_state(self.guild_id)
        user_queue = [t for t in state.queue if not t.get('suggested')]
        suggested = [t for t in state.queue if t.get('suggested')]
        random.shuffle(user_queue)
        state.queue[:] = user_queue + suggested
        await interaction.response.send_message("🔀 Shuffled queue!", ephemeral=True, silent=True)
    @ui.button(emoji="📋", style=discord.ButtonStyle.gray)
    async def q_btn(self, interaction, button):
        state = self.cog.get_state(self.guild_id)
        if not state.current_track and not state.queue:
            return await interaction.response.send_message("Queue empty!", ephemeral=True, silent=True)
        view = ListPaginator(state.queue, title="Server Queue", is_queue=True, current=state.current_track)
        await interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True, silent=True)
    @ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction, button):
        await self.cog.stop_logic(self.guild_id)
        await interaction.response.send_message("👋 Stopping & Saving...", ephemeral=True, silent=True)

class GuessModeSelectView(ui.View):
    """View to select the game mode for the Guess game."""
    def __init__(self, cog, ctx, seed_song):
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.seed_song = seed_song

    @ui.button(label="Guess the Title", emoji="🎵", style=discord.ButtonStyle.primary)
    async def guess_title(self, interaction, button):
        await self.start_game(interaction, "title")

    @ui.button(label="Guess the Author", emoji="🎤", style=discord.ButtonStyle.primary)
    async def guess_author(self, interaction, button):
        await self.start_game(interaction, "author")

    @ui.button(label="Guess Both", emoji="📀", style=discord.ButtonStyle.primary)
    async def guess_both(self, interaction, button):
        await self.start_game(interaction, "both")

    async def start_game(self, interaction, mode):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("❌ This menu is not for you!", ephemeral=True)
            
        state = self.cog.get_state(self.ctx.guild.id)
        if state.game:
            await interaction.response.edit_message(content="❌ A game is already in progress!", view=None)
            return
        
        await interaction.response.edit_message(content=f"🎮 Starting **Guess the {mode.capitalize()}** game...", view=None)
        state.game = GuessGame(self.cog, self.ctx, seed_song=self.seed_song, mode=mode)
        await state.game.start()

class GuessGameView(ui.View):
    """Buttons for the Guess the Song game."""
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    @ui.button(label="+5s", emoji="➕", style=discord.ButtonStyle.blurple)
    async def more_time(self, interaction, button):
        if not self.game.active or self.game.transitioning: return
        await interaction.response.defer()
        await self.game.play_segment(extra=5)

    @ui.button(label="Rehear", emoji="👂", style=discord.ButtonStyle.secondary)
    async def rehear(self, interaction, button):
        if not self.game.active or self.game.transitioning: return
        await interaction.response.defer()
        await self.game.play_segment(extra=0)

    @ui.button(label="Skip / Reveal", emoji="⏭️", style=discord.ButtonStyle.gray)
    async def skip_song(self, interaction, button):
        if not self.game.active or self.game.transitioning: return
        # Immediate stop and transition
        await self.game.trigger_transition(reveal=True)
        await interaction.response.defer()

    @ui.button(label="End Game", emoji="🛑", style=discord.ButtonStyle.danger)
    async def end_game(self, interaction, button):
        await self.game.stop()
        await interaction.response.send_message("🛑 Game ended.")

class GuessGame:
    """Logic for Guess the Song game."""
    def __init__(self, cog, ctx, seed_song=None, mode="title"):
        self.cog = cog
        self.ctx = ctx
        self.seed_song = seed_song
        self.mode = mode # "title", "author", or "both"
        self.songs_pool = []
        self.current_song = None
        self.play_duration = 5
        self.active = True
        self.skips = set()
        self.message = None
        self.scores = {} # user_id -> points
        self.processing_guess = False
        self.transitioning = False
        self.lock = asyncio.Lock()
        self.played_ids = set()

    def remove_diacritics(self, text):
        """Removes Romanian diacritics and other accents."""
        return "".join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')

    async def fetch_more_songs(self, seed_id=None):
        """Uses autoplay logic to find related songs for the game."""
        # Use provided seed_id, or fall back to original seed_song
        sid = seed_id or self.seed_song['id']
        try:
            url = f"https://www.youtube.com/watch?v={sid}&list=RD{sid}"
            info = await self.cog.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_MIX_OPTS).extract_info(url, download=False))
            if 'entries' in info:
                # Strictly filter out already played IDs and already pooled IDs
                pooled_ids = {s['id'] for s in self.songs_pool}
                new_entries = [e for e in info['entries'] if e and e['id'] not in self.played_ids and e['id'] not in pooled_ids]
                
                added_count = 0
                for e in new_entries:
                    track = {
                        'id': e['id'], 
                        'title': e['title'], 
                        'author': e.get('uploader', 'Unknown'),
                        'url': e.get('url') or f"https://www.youtube.com/watch?v={e['id']}"
                    }
                    self.songs_pool.append(track)
                    added_count += 1
                
                random.shuffle(self.songs_pool)
                log_info(f"🎮 GuessGame: Added {added_count} new songs to pool using seed {sid}")
        except Exception as e:
            log_error(f"Guess Game pool fetch failed: {e}")

    async def start(self):
        # Join VC
        if not self.ctx.voice_client:
            if self.ctx.author.voice:
                try: await self.ctx.author.voice.channel.connect()
                except: 
                    self.active = False
                    return await self.ctx.send("❌ Could not join VC.")
            else:
                self.active = False
                return await self.ctx.send("❌ You must be in a VC!")
        
        if self.ctx.voice_client.is_playing():
            self.ctx.voice_client.stop()
        
        await self.ctx.send(f"🎮 Starting **Guess the {self.mode.capitalize()}** game based on: **{self.seed_song['title']}**")
        await self.next_song()

    async def trigger_transition(self, reveal=False, winner=None):
        """Centralized transition logic to prevent races and stop music."""
        async with self.lock:
            if self.transitioning or not self.active: return
            self.transitioning = True
            self.processing_guess = True # Block guesses immediately
            
            # Stop music IMMEDIATELY
            if self.ctx.voice_client and self.ctx.voice_client.is_playing():
                self.ctx.voice_client.stop()

            if reveal or winner:
                if winner:
                    self.scores[winner.id] = self.scores.get(winner.id, 0) + 1
                    embed = discord.Embed(title="🎉 Correct!", description=f"**{winner.display_name}** got it!\n\nIt was: **{self.current_song['title']}**\nBy: **{self.current_song['author']}**", color=discord.Color.green())
                else:
                    embed = discord.Embed(title="⏭️ Skipped", description=f"It was: **{self.current_song['title']}**\nBy: **{self.current_song['author']}**", color=discord.Color.orange())
                
                embed.set_thumbnail(url=f"https://i.ytimg.com/vi/{self.current_song['id']}/mqdefault.jpg")
                await self.ctx.send(embed=embed)
                await asyncio.sleep(2.5) # Time to see the reveal

            # Cleanup message if it exists
            if self.message:
                try: await self.message.delete()
                except: pass
                self.message = None

            self.transitioning = False # Reset for next song
            await self.next_song()

    async def next_song(self):
        if not self.active: return
        
        # Refill pool if empty or low
        if len(self.songs_pool) < 2:
            # Use the last played song as a new seed to get fresh variety
            new_seed = self.current_song['id'] if self.current_song else None
            await self.ctx.send("🔄 Refreshing song pool...")
            await self.fetch_more_songs(seed_id=new_seed)
        
        # Final safety filter
        self.songs_pool = [s for s in self.songs_pool if s['id'] not in self.played_ids]
        
        if not self.songs_pool:
            await self.ctx.send("❌ Could not find enough unique songs. Ending game.")
            return await self.stop()

        self.current_song = self.songs_pool.pop(0)
        self.played_ids.add(self.current_song['id'])
        self.play_duration = 5
        self.processing_guess = False
        self.transitioning = False
        
        if self.mode == "title": target_type = "Title"
        elif self.mode == "author": target_type = "Artist/Author"
        else: target_type = "Artist & Title"

        embed = discord.Embed(title=f"🎮 Guess the {target_type}!", description=f"Listen and type the **{target_type.lower()}**!\n\n*Romanian diacritics and capitals are ignored.*", color=COLOR_MAIN)
        if self.mode == "both":
            embed.set_footer(text="Format: Artist - Title (or just Title - Artist)")

        embed.add_field(name="Difficulty", value=f"Playing {self.play_duration}s")
        
        self.message = await self.ctx.send(embed=embed, view=GuessGameView(self))
        await self.play_segment()

    async def play_segment(self, extra=0):
        if not self.active or not self.ctx.voice_client or self.transitioning: return
        self.play_duration += extra
        
        if self.ctx.voice_client.is_playing() and extra == 0:
            return

        if self.ctx.voice_client.is_playing():
            self.ctx.voice_client.stop()
            await asyncio.sleep(0.3)

        try:
            info = await self.cog.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAY_OPTS).extract_info(self.current_song['id'], download=False))
            opts = FFMPEG_STREAM_OPTS.copy()
            opts['options'] = f"-vn -threads 2 -bufsize 8192k -t {self.play_duration}"
            
            # Double check we haven't transitioned while fetching info
            if self.transitioning or not self.active: return

            source = await discord.FFmpegOpusAudio.from_probe(info['url'], **opts)
            self.ctx.voice_client.play(source)
            
            if extra > 0:
                target_type = "Title" if self.mode == "title" else "Artist"
                embed = discord.Embed(title=f"🎮 Guess the {target_type}!", description="Playing a longer segment...", color=COLOR_MAIN)
                embed.add_field(name="Difficulty", value=f"Playing {self.play_duration}s")
                await self.message.edit(embed=embed)
        except Exception as e:
            log_error(f"Guess Game Play Error: {e}")
            if self.active and not self.transitioning:
                await self.next_song()

    def clean_text(self, text):
        """Standardizes text for comparison."""
        text = text.lower()
        text = self.remove_diacritics(text)
        # Remove common suffixes/prefixes for music
        text = re.sub(r'\(.*?\)|\[.*?\]|official|video|audio|lyrics|feat\.|ft\.| - topic|remix|hd|4k', '', text).strip()
        # Keep only alphanumeric and a few important symbols for 'both' mode
        text = re.sub(r'[^a-z0-9\s\-]', '', text)
        # Normalize spaces
        text = " ".join(text.split())
        return text

    async def validate_guess(self, raw_guess):
        """Core logic to check if a guess is correct."""
        if not self.active or self.processing_guess or self.transitioning: return False

        clean_guess = self.clean_text(raw_guess)
        if len(clean_guess) < 2: return False
        
        if self.mode == "both":
            target_author = self.clean_text(self.current_song['author'])
            target_title = self.clean_text(self.current_song['title'])
            
            author_match = difflib.SequenceMatcher(None, clean_guess, target_author).ratio() > 0.6 or target_author in clean_guess
            title_match = difflib.SequenceMatcher(None, clean_guess, target_title).ratio() > 0.6 or target_title in clean_guess
            
            return author_match and title_match

        target_text = self.current_song['title'] if self.mode == "title" else self.current_song['author']
        clean_target = self.clean_text(target_text)
        
        # Anti-author check for title mode (prevent guessing artist when title is needed)
        if self.mode == "title":
            clean_author = self.clean_text(self.current_song['author'])
            author_ratio = difflib.SequenceMatcher(None, clean_guess, clean_author).ratio()
            if author_ratio > 0.85:
                # If guess matches author too well, check if it ALSO matches title
                title_ratio = difflib.SequenceMatcher(None, clean_guess, clean_target).ratio()
                if title_ratio < 0.7:
                    return False

        ratio = difflib.SequenceMatcher(None, clean_guess, clean_target).ratio()
        is_correct = ratio > 0.8
        if not is_correct and len(clean_guess) > 3:
            if clean_guess in clean_target or clean_target in clean_guess:
                is_correct = True
        
        return is_correct

    async def check_guess(self, message):
        """Discord message handler for guesses."""
        if not self.active or message.channel.id != self.ctx.channel.id: return
        
        if await self.validate_guess(message.content):
            await message.add_reaction("✅")
            await self.trigger_transition(winner=message.author)

    async def process_web_guess(self, user_name, guess_text):
        """Web handler for guesses."""
        if await self.validate_guess(guess_text):
            # Create a dummy user object for the winner
            class WebUser:
                def __init__(self, name):
                    self.id = f"web_{name}"
                    self.display_name = f"{name} (Web)"
            
            await self.trigger_transition(winner=WebUser(user_name))
            return True
        return False

    async def stop(self):
        self.active = False
        state = self.cog.get_state(self.ctx.guild.id)
        state.game = None
        if self.ctx.voice_client and self.ctx.voice_client.is_playing():
            self.ctx.voice_client.stop()
        
        # Show scoreboard
        if self.scores:
            sorted_scores = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
            board = "\n".join([f"<@{uid}>: {pts} pts" for uid, pts in sorted_scores])
            embed = discord.Embed(title="🏆 Final Scores", description=board, color=COLOR_MAIN)
            await self.ctx.send(embed=embed)

class ListPaginator(ui.View):
    """Pagination for queue, history, and cache lists."""
    def __init__(self, data_list, title="List", is_queue=True, current=None):
        super().__init__(timeout=60)
        self.data_list = data_list
        self.title = title
        self.is_queue = is_queue
        self.current = current
        self.page = 0
        self.items_per_page = 10
        self.max_pages = max(0, (len(data_list) - 1) // self.items_per_page)

    def get_embed(self):
        embed = discord.Embed(title=f"📜 {self.title}", color=COLOR_MAIN)
        if self.is_queue and self.current:
            source = "💾 Local" if os.path.exists(f"{CACHE_DIR}/{self.current['id']}.webm") else "☁️ Stream"
            embed.add_field(name=f"▶️ Now Playing ({source})", value=f"**{self.current['title']}**", inline=False)
        start = self.page * self.items_per_page
        end = start + self.items_per_page
        if not self.data_list: desc = "Empty."
        else:
            desc_lines = []
            for i, s in enumerate(self.data_list[start:end]):
                if isinstance(s, dict): 
                    prefix = "✨ " if s.get('suggested') else ""
                    line = f"`{start+i+1}.` {prefix}**{s['title']}** by {s.get('author', 'Unknown')} ({s.get('duration', '?:??')})"
                else: line = f"`{start+i+1}.` {s}"
                desc_lines.append(line)
            desc = "\n".join(desc_lines)
        embed.description = desc
        embed.set_footer(text=f"Page {self.page+1}/{self.max_pages+1} • Total: {len(self.data_list)}")
        return embed

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.gray)
    async def prev(self, interaction, button):
        if self.page > 0: self.page -= 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)
    @ui.button(emoji="➡️", style=discord.ButtonStyle.gray)
    async def next(self, interaction, button):
        if self.page < self.max_pages: self.page += 1
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

# ==========================================
# 6. MAIN MUSIC BOT CLASS
# ==========================================

class MusicBot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.states = {}
        self.cleanup_loop.start()
        self.tunnel_monitor.start()
        self.public_url = None
        self.web_auth_token = str(uuid4())
        self.tunnel_proc = None
        self.drain_task = None
        
        # Store direct reference for reliable access in Quart
        app.config['BOT_COG'] = self
        set_bot_instance(bot)
        
        global bot_instance
        bot_instance = bot 
        self.web_task = self.bot.loop.create_task(app.run_task(host='0.0.0.0', port=5000))
        
        # Pre-start Cloudflared
        self.bot.loop.create_task(self.start_cloudflared())

    async def cog_unload(self):
        self.cleanup_loop.stop()
        self.tunnel_monitor.stop()
        if self.drain_task: self.drain_task.cancel()
        if self.tunnel_proc:
            try: 
                self.tunnel_proc.terminate()
                await self.tunnel_proc.wait()
            except: pass
        if self.web_task: self.web_task.cancel()

    # --- Cloudflare Tunnel Logic ---
    def ensure_cloudflared(self):
        """Downloads the correct cloudflared binary for the system."""
        if os.path.exists("./cloudflared"): return True
        
        arch = platform.machine().lower()
        if arch in ['x86_64', 'amd64']: c_arch = 'amd64'
        elif arch in ['aarch64', 'arm64']: c_arch = 'arm64'
        else: c_arch = 'arm' # Pi Zero / Pi 2 / Pi 3 32-bit
        
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{c_arch}"
        log_info(f"⬇️ Downloading Cloudflared ({c_arch})...")
        
        try:
            r = requests.get(url, stream=True)
            with open("./cloudflared", 'wb') as f:
                shutil.copyfileobj(r.raw, f)
            st = os.stat("./cloudflared")
            os.chmod("./cloudflared", st.st_mode | stat.S_IEXEC)
            log_info("✅ Cloudflared installed.")
            return True
        except Exception as e:
            log_error(f"❌ Failed to download cloudflared: {e}")
            return False

    async def drain_stderr(self, proc):
        """Continuously reads stderr from the tunnel process to prevent buffer fill-up."""
        while True:
            line = await proc.stderr.readline()
            if not line: break
            
            try:
                line = line.decode('utf-8')
                # Still look for the URL if not found yet (backup)
                if not self.public_url and "trycloudflare.com" in line:
                    match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare.com', line)
                    if match:
                        self.public_url = match.group(0)
                        log_info(f"🌍 Tunnel Active (found in drain): {self.public_url}")
                
                if "error" in line.lower():
                    log_error(f"☁️ Cloudflared: {line.strip()}")
            except: pass

    @tasks.loop(seconds=30)
    async def tunnel_monitor(self):
        """Monitors the health of the tunnel and web server."""
        if self.public_url:
            # Check process
            if self.tunnel_proc and self.tunnel_proc.returncode is not None:
                log_error("⚠️ Cloudflared process died! Resetting public URL.")
                self.public_url = None
                if self.drain_task: self.drain_task.cancel()
                return

            # Check local responsiveness
            try:
                def check(): return requests.get("http://127.0.0.1:5000/health", timeout=5)
                r = await self.bot.loop.run_in_executor(None, check)
                if r.status_code != 200:
                    log_error(f"⚠️ Local Web Server health check failed: {r.status_code}")
            except Exception as e:
                log_error(f"⚠️ Local Web Server unreachable: {e}")

    async def start_cloudflared(self):
        """Starts the tunnel and retrieves the URL."""
        # 1. If already running and active, return URL
        if self.public_url and self.tunnel_proc and self.tunnel_proc.returncode is None:
            return self.public_url
        
        # 2. If process is running but no URL yet, wait for it (join existing startup)
        if self.tunnel_proc and self.tunnel_proc.returncode is None:
            log_info("⏳ Waiting for existing tunnel startup...")
            start_time = time.time()
            while time.time() - start_time < 20:
                if self.public_url: return self.public_url
                if self.tunnel_proc.returncode is not None: break # Process died
                await asyncio.sleep(0.5)
        
        # 3. Start fresh
        self.public_url = None
        if self.drain_task: self.drain_task.cancel()
        
        # Download in background thread to avoid blocking heartbeat
        if not await self.bot.loop.run_in_executor(None, self.ensure_cloudflared):
            return None
        
        # Kill existing
        try: subprocess.run(["pkill", "-f", "cloudflared tunnel"], capture_output=True)
        except: pass

        log_info("☁️ Starting Cloudflared Tunnel...")
        # Use 127.0.0.1 to avoid IPv6/localhost resolution issues
        self.tunnel_proc = await asyncio.create_subprocess_exec(
            "./cloudflared", "tunnel", "--url", "http://127.0.0.1:5000",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )

        # Start draining in background
        self.drain_task = self.bot.loop.create_task(self.drain_stderr(self.tunnel_proc))

        # Wait for URL
        start_time = time.time()
        while time.time() - start_time < 20:
            if self.tunnel_proc.returncode is not None:
                log_error("❌ Cloudflared failed to start.")
                return None
            
            if self.public_url:
                log_info(f"🌍 Tunnel Active: {self.public_url}")
                return self.public_url
                
            await asyncio.sleep(0.5)
            
        log_error("⏳ Cloudflared timed out waiting for URL.")
        return None

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if hasattr(ctx.command, 'on_error'):
            return
            
        if isinstance(error, commands.CommandNotFound):
            return
            
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Permission denied", silent=True)
        else:
            try:
                await ctx.send(f"❌ Error: {str(error)[:100]}", silent=True)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild: return
        state = self.get_state(message.guild.id)
        if state.game and state.game.active:
            await state.game.check_guess(message)

    def get_state(self, guild_id):
        if guild_id not in self.states:
            self.states[guild_id] = ServerState()
        return self.states[guild_id]

    # --- Playback Logic ---

    async def background_download(self, track):
        """Proactively download a song in the background with low priority."""
        # Check if already cached or being downloaded
        if os.path.exists(f"{CACHE_DIR}/{track['id']}.webm"):
            return
            
        await enforce_cache_limit(self.bot.loop)
        
        def do_download():
            try:
                # Lower process priority even more for the download thread if possible
                if platform.system() != "Windows":
                    try: os.nice(19) 
                    except: pass
                
                # Use a specific YDL instance for background tasks
                with yt_dlp.YoutubeDL(YDL_DOWNLOAD_OPTS) as ydl:
                    ydl.download([f'https://www.youtube.com/watch?v={track["id"]}'])
                
                cache_map[track['id']] = track['title']
                save_json(CACHE_MAP_FILE, cache_map)
                log_info(f"✅ Background Cached: {track['title']}")
            except Exception as e:
                log_error(f"Background DL Fail for {track['id']}: {e}")

        await self.bot.loop.run_in_executor(None, do_download)

    async def stop_logic(self, guild_id):
        """Clean disconnect logic."""
        if guild_id not in self.states:
            return
            
        guild = self.bot.get_guild(guild_id)
        state = self.states[guild_id]
        state.stopping = True
        
        if guild and guild.voice_client:
            await guild.voice_client.disconnect()
            
        del self.states[guild_id]

    @tasks.loop(minutes=2)
    async def cleanup_loop(self):
        """Auto-disconnect if alone or idle."""
        now = datetime.datetime.now()
        for gid in list(self.states.keys()):
            guild = self.bot.get_guild(gid)
            if not guild:
                del self.states[gid]
                continue
                
            state = self.states[gid]
            if guild.voice_client:
                # FIX: Reset the timer while music is playing
                if guild.voice_client.is_playing():
                    state.last_interaction = now

                is_alone = len(guild.voice_client.channel.members) == 1
                is_idle = not guild.voice_client.is_playing() and (now - state.last_interaction).total_seconds() > 300
                
                if is_alone or is_idle:
                    await self.stop_logic(gid)

    def get_notification_channel(self, guild):
        if str(guild.id) in server_settings:
            ch_id = server_settings[str(guild.id)]
            ch = guild.get_channel(ch_id)
            if ch and ch.permissions_for(guild.me).send_messages:
                return ch
                
        state = self.get_state(guild.id)
        if state.last_text_channel:
            return state.last_text_channel
            
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                if any(x in ch.name.lower() for x in ['music', 'muzica', 'bot', 'general']):
                    return ch
                    
        return guild.text_channels[0] if guild.text_channels else None

    async def load_rest_of_playlist(self, url, guild_id):
        """Background task to load large playlists."""
        REST_OPTS = {'extract_flat': 'in_playlist', 'playlist_items': '51-', **COMMON_YDL_ARGS, 'noplaylist': False}
        try:
            info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(REST_OPTS).extract_info(url, download=False))
            if 'entries' in info:
                state = self.get_state(guild_id)
                count = 0
                for e in info['entries']:
                    if e: 
                        state.queue.append({
                            'id': e['id'], 
                            'title': e['title'], 
                            'author': e['uploader'], 
                            'duration': format_time(e['duration']), 
                            'duration_seconds': e['duration'], 
                            'webpage': f"https://www.youtube.com/watch?v={e['id']}"
                        })
                        count += 1
                
                guild = self.bot.get_guild(guild_id)
                ch = self.get_notification_channel(guild)
                # if ch:
                #    await ch.send(f"✅ Loaded {count} more tracks in background.", silent=True)
        except Exception:
            pass

    async def ensure_autoplay(self, guild_id, avoid_ids=None, force=False):
        """Logic for buffering exactly one suggested song at the end of the queue."""
        state = self.get_state(guild_id)
        if avoid_ids is None: avoid_ids = []
        
        # 1. If Autoplay is OFF, remove any suggested tracks
        if not state.autoplay:
            state.queue = [t for t in state.queue if not (isinstance(t, dict) and t.get('suggested'))]
            return

        # Prevent concurrent fetches (unless forced, but even then we should be careful)
        if state.fetching_autoplay and not force:
            return

        # 2. Always maintain exactly one suggestion at the end. 
        # If forced, we clear and re-fetch. Otherwise, we only clear if it's not at the end.
        suggestions = [t for t in state.queue if isinstance(t, dict) and t.get('suggested')]
        if suggestions:
            if force or not state.queue[-1].get('suggested') or len(suggestions) > 1:
                state.queue = [t for t in state.queue if not (isinstance(t, dict) and t.get('suggested'))]
            else:
                # Already have exactly one at the end and not forced
                return

        # 3. Find a seed track (last user track in queue, or current)
        seed = None
        for t in reversed(state.queue):
            if isinstance(t, dict) and not t.get('suggested'): 
                seed = t
                break
        
        if not seed: seed = state.current_track
        if not seed and state.history: seed = state.history[-1]
        
        if not seed: return

        # 4. Fetch recommendation
        state.fetching_autoplay = True
        try:
            # Run in executor to avoid blocking
            info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_MIX_OPTS).extract_info(f"https://www.youtube.com/watch?v={seed['id']}&list=RD{seed['id']}", download=False))
            if 'entries' in info:
                # History check (last 20)
                recent_ids = [h['id'] for h in state.history[-20:]]
                
                # Filter candidates
                candidates = []
                for e in info['entries']:
                    if not e: continue
                    eid = e['id']
                    if eid == seed['id']: continue
                    if eid in avoid_ids: continue
                    if eid in recent_ids: continue
                    
                    # Also avoid tracks already in queue
                    if any(isinstance(t, dict) and t['id'] == eid for t in state.queue): continue
                    
                    candidates.append(e)
                    if len(candidates) >= 5: break
                
                if candidates:
                    e = random.choice(candidates)
                    track = {'id':e['id'], 'title':e['title'], 'author':e['uploader'], 'duration':format_time(e['duration']), 'duration_seconds':e['duration'], 'webpage':e['url'], 'suggested': True}
                    
                    # Double check no suggestions were added
                    state.queue = [t for t in state.queue if not (isinstance(t, dict) and t.get('suggested'))]
                    state.queue.append(track)
                    
        except Exception as e:
            log_error(f"Autoplay fetch failed: {e}")
        finally:
            state.fetching_autoplay = False

    async def regenerate_autoplay(self, guild_id):
        """Regenerates the current autoplay suggestion."""
        state = self.get_state(guild_id)
        if not state.autoplay: return False
        
        # Find current suggestion
        if state.queue and state.queue[-1].get('suggested'):
            old_suggestion = state.queue.pop() # Remove it
            # Avoid this one, and also ensure we don't pick it again immediately
            await self.ensure_autoplay(guild_id, avoid_ids=[old_suggestion['id']], force=True)
            return True
        else:
            # No suggestion present, just ensure one
            await self.ensure_autoplay(guild_id, force=True)
            return True

    async def prepare_song(self, ctx, query):
        """Main entry point for adding a song to the queue."""
        state = self.get_state(ctx.guild.id)
        state.last_interaction = datetime.datetime.now()
        state.stopping = False
        if hasattr(ctx, 'channel'): state.last_text_channel = ctx.channel
        
        # 1. Aggressive clear (before potential awaits)
        state.queue = [t for t in state.queue if not (isinstance(t, dict) and t.get('suggested'))]
        
        # VC Join Logic
        if not ctx.voice_client:
            if ctx.author.voice: 
                try: await ctx.author.voice.channel.connect()
                except Exception as e: return await ctx.send(embed=discord.Embed(description=f"❌ Error joining VC: {e}", color=discord.Color.red()), silent=True)
            else: return await ctx.send(embed=discord.Embed(description="❌ You must be in a Voice Channel!", color=discord.Color.red()), silent=True)

        if ctx.interaction and not ctx.interaction.response.is_done(): 
            await ctx.interaction.response.defer()
        
        # Use Flat Options (verified working)
        info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(query, download=False))
        
        # 2. Aggressive clear (after awaits, ensures we clear any suggestion added during info extraction)
        state.queue = [t for t in state.queue if not (isinstance(t, dict) and t.get('suggested'))]

        def proc(e): 
            url = e.get('webpage_url') or e.get('url') or f"https://www.youtube.com/watch?v={e['id']}"
            return {
                'id': e['id'], 
                'title': e['title'], 
                'author': e.get('uploader', 'Unknown'), 
                'duration': format_time(e.get('duration', 0)), 
                'duration_seconds': e.get('duration', 0), 
                'webpage': url
            }
        
        async def send_res(msg):
            if ctx.interaction: await ctx.interaction.followup.send(embed=discord.Embed(description=msg, color=COLOR_MAIN), silent=True)
            else: await ctx.send(embed=discord.Embed(description=msg, color=COLOR_MAIN), silent=True)

        if 'entries' in info: 
            tracks = [proc(e) for e in info['entries'] if e]
            state.queue.extend(tracks)
            await send_res(f"✅ Added **{len(info['entries'])}** tracks.")
            
            # Start pre-downloading first 3 tracks of a playlist
            for t in tracks[:3]:
                self.bot.loop.create_task(self.background_download(t))
        else: 
            track = proc(info)
            state.queue.append(track)
            if ctx.voice_client.is_playing(): await send_res(f"✅ Queued: **{info['title']}**")
            # Start pre-downloading immediately
            self.bot.loop.create_task(self.background_download(track))
            
        # Re-verify autoplay (moves suggestion to end)
        self.bot.loop.create_task(self.ensure_autoplay(ctx.guild.id, force=True))

        if not ctx.voice_client.is_playing(): await self.play_next(ctx)

    async def play_next(self, ctx):
        """Recursive function to play the next song in the queue."""
        state = self.get_state(ctx.guild.id)
        
        if state.stopping or not ctx.guild.voice_client or not ctx.guild.voice_client.is_connected():
             return
        if state.processing_next: return
        
        if state.queue:
            state.processing_next = True 
            next_song = state.queue.pop(0)
            state.current_track = next_song
            state.history.append(next_song)
            if len(state.history) > 20: state.history.pop(0)

            try:
                local = os.path.abspath(f"{CACHE_DIR}/{next_song['id']}.webm")
                play_local = os.path.exists(local) and os.path.getsize(local) > 1024
                
                # Thumbnail Check
                thumb_local = f"{CACHE_DIR}/{next_song['id']}.jpg"
                if play_local and not os.path.exists(thumb_local):
                    try: await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL({'writethumbnail':True, 'skip_download':True, 'outtmpl': f'{CACHE_DIR}/%(id)s.%(ext)s', 'quiet':True}).download([f"https://www.youtube.com/watch?v={next_song['id']}"])) # noqa
                    except: pass

                if play_local:
                    os.utime(local, None)
                    source = await discord.FFmpegOpusAudio.from_probe(local, **FFMPEG_LOCAL_OPTS)
                else:
                    # If not local, stream it, but also trigger a download for future use
                    self.bot.loop.create_task(self.background_download(next_song))
                    
                    info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAY_OPTS).extract_info(next_song['id'], download=False))
                    
                    opts = FFMPEG_STREAM_OPTS.copy()
                    if 'http_headers' in info:
                        header_args = ""
                        for key, value in info['http_headers'].items():
                            header_args += f"{key}: {value}\r\n"
                        opts['before_options'] = f'-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -headers "{header_args}" -nostdin'
                    
                    source = await discord.FFmpegOpusAudio.from_probe(info['url'], **opts)

                ctx.voice_client.play(source, after=lambda e: self.bot.loop.create_task(self.play_next(ctx)))
                state.processing_next = False 
                
                # Proactively pre-download the NEW first song in the queue
                if state.queue:
                    self.bot.loop.create_task(self.background_download(state.queue[0]))
                
                # Trigger autoplay prefetch for the NEXT song
                self.bot.loop.create_task(self.ensure_autoplay(ctx.guild.id))
                
                embed = discord.Embed(title="🎶 Now Playing", description=f"**[{next_song['title']}]({next_song['webpage']})**", color=COLOR_MAIN)
                embed.set_thumbnail(url=f"https://i.ytimg.com/vi/{next_song['id']}/mqdefault.jpg")
                embed.add_field(name="Author", value=next_song['author'])
                embed.add_field(name="Duration", value=next_song['duration'])
                if next_song.get('suggested'): embed.set_footer(text="✨ Autoplay Suggestion")
                
                ch = self.get_notification_channel(ctx.guild)
                if ch: await ch.send(embed=embed, view=MusicControlView(self, ctx.guild.id), silent=True)
            
            except Exception as e: 
                log_error(f"Playback error: {e}")
                state.processing_next = False
                await asyncio.sleep(2) 
                self.bot.loop.create_task(self.play_next(ctx))
        else:
            state.current_track = None
            state.processing_next = False

    # --- COMMANDS ---
    @commands.hybrid_command(name="help", description="Show all commands")
    async def help(self, ctx):
        embed = discord.Embed(title="🎵 PiMusic Bot Commands", description="Control your music with these commands:", color=COLOR_MAIN)
        embed.add_field(name="🎵 Music", value="`/play [song]` - Play music\n`/pause` / `/resume`\n`/skip`\n`/stop`\n`/autoplay`\n`/new` - Regen recommendation", inline=False)
        embed.add_field(name="🎛️ Dashboard", value="`/link` - Get Web Panel\n`/setchannel` - Set output channel", inline=False)
        embed.add_field(name="📂 Playlists", value="`/saveplaylist`\n`/loadplaylist`\n`/listplaylists`\n`/delplaylist`", inline=False)
        embed.add_field(name="📜 Queue", value="`/queue`\n`/history`\n`/shuffle`\n`/clear`", inline=False)
        embed.add_field(name="🎮 Games", value="`/guess [search]` - Start song quiz", inline=False)
        embed.add_field(name="⚙️ Utils", value="`/search`\n`/cache`\n`/dash`", inline=False)
        await ctx.send(embed=embed, silent=True)

    @commands.command()
    async def sync(self, ctx):
        await ctx.bot.tree.sync()
        await ctx.send("✅ Synced! Commands will appear shortly.", silent=True)

    @commands.hybrid_command(name="setchannel")
    async def set_channel(self, ctx):
        server_settings[str(ctx.guild.id)] = ctx.channel.id
        save_json(SETTINGS_FILE, server_settings)
        embed = discord.Embed(description=f"✅ Bound to {ctx.channel.mention}", color=COLOR_MAIN)
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name="link")
    async def link(self, ctx):
        await ctx.defer()
        
        # Auto-join VC
        if not ctx.voice_client and ctx.author.voice:
            try: await ctx.author.voice.channel.connect()
            except: pass

        if not self.public_url:
             self.public_url = await self.start_cloudflared()
        
        if self.public_url:
            secure_link = f"{self.public_url}/auth?token={self.web_auth_token}&guild={ctx.guild.id}"
            embed = discord.Embed(title="🎛️ Web Dashboard", description="Click below to open the control panel.", color=COLOR_MAIN)
            embed.set_footer(text="Powered by Cloudflare Tunnel ☁️")
            view = ui.View()
            view.add_item(ui.Button(label="Open Dashboard", url=secure_link))
            await ctx.send(embed=embed, view=view, silent=True)
        else:
            await ctx.send("❌ Could not start Cloudflare Tunnel. Check logs.", silent=True)

    @commands.hybrid_command(name="play", aliases=["p"])
    async def play(self, ctx, *, search: str):
        q = search if re.match(r'^https?://', search) else f"ytsearch1:{search}"
        await self.prepare_song(ctx, q)

    @commands.hybrid_command(name="stop", aliases=["dc", "leave"])
    async def stop(self, ctx): 
        await self.stop_logic(ctx.guild.id)
        embed = discord.Embed(description="👋 Stopped.", color=COLOR_MAIN)
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name="skip", aliases=["s", "next"])
    async def skip(self, ctx): 
        ctx.voice_client.stop()
        embed = discord.Embed(description="⏭️ Skipped.", color=COLOR_MAIN)
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name="clear", description="Clear the music queue")
    async def clear(self, ctx):
        state = self.get_state(ctx.guild.id)
        if state.autoplay:
            state.queue = [t for t in state.queue if t.get('suggested')]
        else:
            state.queue = []
        embed = discord.Embed(description="🗑️ Queue cleared.", color=COLOR_MAIN)
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name="queue", aliases=["q"])
    async def queue(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.current_track and not state.queue:
            return await ctx.send(embed=discord.Embed(description="Queue empty.", color=COLOR_MAIN), silent=True)
        view = ListPaginator(state.queue, title="Server Queue", is_queue=True, current=state.current_track)
        await ctx.send(embed=view.get_embed(), view=view, silent=True)

    @commands.hybrid_command(name="pause")
    async def pause(self, ctx):
        if ctx.voice_client.is_playing(): 
            ctx.voice_client.pause()
            await ctx.send(embed=discord.Embed(description="⏸️ Paused.", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="resume")
    async def resume(self, ctx):
        if ctx.voice_client.is_paused(): 
            ctx.voice_client.resume()
            await ctx.send(embed=discord.Embed(description="▶️ Resumed.", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="shuffle")
    async def shuffle(self, ctx):
        state = self.get_state(ctx.guild.id)
        user_queue = [t for t in state.queue if not t.get('suggested')]
        suggested = [t for t in state.queue if t.get('suggested')]
        random.shuffle(user_queue)
        state.queue[:] = user_queue + suggested
        await ctx.send(embed=discord.Embed(description="🔀 Shuffled.", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="saveplaylist")
    async def saveplaylist(self, ctx, name: str, url: str = None):
        if url: 
            # Added validation from Working
            if 'youtube.com' not in url and 'youtu.be' not in url:
                return await ctx.send(embed=discord.Embed(description="❌ Invalid YouTube URL.", color=discord.Color.red()), silent=True)
            saved_playlists[name] = {'type': 'live', 'url': url}
        else:
            state = self.get_state(ctx.guild.id)
            tracks = []
            if state.current_track: tracks.append(state.current_track)
            tracks.extend(state.queue)
            if not tracks: return await ctx.send(embed=discord.Embed(description="Queue empty.", color=discord.Color.red()), silent=True)
            clean = [{'id':t['id'], 'title':t['title'], 'author':t['author'], 'duration':t['duration'], 'duration_seconds':t['duration_seconds'], 'webpage':t['webpage']} for t in tracks]
            saved_playlists[name] = clean
        save_json(PLAYLIST_FILE, saved_playlists)
        await ctx.send(embed=discord.Embed(description=f"💾 Saved **{name}**.", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="loadplaylist")
    async def loadplaylist(self, ctx, name: str):
        if name not in saved_playlists: return await ctx.send(embed=discord.Embed(description="❌ Not found.", color=discord.Color.red()), silent=True)
        content = saved_playlists[name]
        state = self.get_state(ctx.guild.id)
        
        if isinstance(content, list):
            state.queue.extend(content)
            await ctx.send(embed=discord.Embed(description=f"📂 Loaded **{len(content)}** songs.", color=COLOR_MAIN), silent=True)
        elif isinstance(content, dict):
            await ctx.send(embed=discord.Embed(description="🔄 Loading live playlist (First 50)...", color=COLOR_MAIN), silent=True)
            try:
                info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAYLIST_LOAD_OPTS).extract_info(content['url'], download=False))
                tracks = [{'id':e['id'], 'title':e['title'], 'author':e['uploader'], 'duration':format_time(e['duration']), 'duration_seconds':e['duration'], 'webpage':f"https://www.youtube.com/watch?v={e['id']}"} for e in info['entries'] if e]
                state.queue.extend(tracks)
                await ctx.send(embed=discord.Embed(description=f"✅ Loaded **{len(tracks)}**. Rest loading in BG...", color=COLOR_MAIN), silent=True)
                asyncio.create_task(self.load_rest_of_playlist(content['url'], ctx.guild.id))
            except: await ctx.send(embed=discord.Embed(description="❌ Error loading.", color=discord.Color.red()), silent=True)

        if not ctx.voice_client:
            if ctx.author.voice: await ctx.author.voice.channel.connect()
        if not ctx.voice_client.is_playing(): await self.play_next(ctx)

    @commands.hybrid_command(name="listplaylists")
    async def listplaylists(self, ctx):
        msg = "\n".join([f"{k}" for k in saved_playlists.keys()])
        await ctx.send(embed=discord.Embed(title="📂 Saved Playlists", description=msg if msg else "None", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="delplaylist")
    async def delplaylist(self, ctx, name: str):
        if name in saved_playlists: 
            del saved_playlists[name]
            save_json(PLAYLIST_FILE, saved_playlists)
            await ctx.send(embed=discord.Embed(description=f"🗑️ Deleted **{name}**.", color=COLOR_MAIN), silent=True)
        else: await ctx.send(embed=discord.Embed(description="❌ Not found.", color=discord.Color.red()), silent=True)

    @commands.hybrid_command(name="cache")
    async def cache_list(self, ctx):
        valid = [f for f in os.listdir(CACHE_DIR) if f.endswith('.webm')]
        data = [{'title': cache_map.get(f.replace('.webm',''), f), 'duration': 'Cached'} for f in valid]
        if not data: return await ctx.send(embed=discord.Embed(description="Cache empty.", color=COLOR_MAIN), silent=True)
        data.sort(key=lambda x: x['title'])
        view = ListPaginator(data, title="Local Cache", is_queue=False)
        await ctx.send(embed=view.get_embed(), view=view, silent=True)

    @commands.hybrid_command(name="dash")
    async def dash(self, ctx):
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        try: temp = os.popen("vcgencmd measure_temp").readline().replace("temp=","").strip()
        except: temp = "N/A"
        count = len([n for n in os.listdir(CACHE_DIR) if n.endswith('.webm')])
        size = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in os.listdir(CACHE_DIR) if f.endswith('.webm')) / (1024**3)
        embed = discord.Embed(title="🚀 Pi Stats", color=COLOR_MAIN)
        embed.add_field(name="System", value=f"CPU: `{cpu}%` | RAM: `{ram}%` | {temp}")
        embed.add_field(name="Storage", value=f"`{count}` songs | `{size:.2f} GB` / {MAX_CACHE_SIZE_GB} GB")
        await ctx.send(embed=embed, silent=True)

    @commands.hybrid_command(name="search")
    async def search(self, ctx, *, query: str):
        await ctx.defer()
        info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(f"ytsearch5:{query}", download=False))
        if not info.get('entries'): return await ctx.send("❌ No results.", silent=True)
        view = SelectionView(info['entries'], self, ctx)
        view.message = await ctx.send("🔎 **Results:**", view=view, silent=True)

    @commands.hybrid_command(name="history")
    async def history(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.history: return await ctx.send(embed=discord.Embed(description="History empty.", color=COLOR_MAIN), silent=True)
        view = ListPaginator(list(reversed(state.history)), title="History", is_queue=False)
        await ctx.send(embed=view.get_embed(), view=view, silent=True)

    @commands.hybrid_command(name="guess", description="Start a 'Guess the Song' quiz. Usage: /guess [search]")
    @app_commands.describe(search="A song to base the quiz on (uses Autoplay)")
    async def guess_command(self, ctx, *, search: str = None):
        state = self.get_state(ctx.guild.id)
        if state.game:
            return await ctx.send("❌ A game is already in progress!", ephemeral=True)
        
        await ctx.defer()
        
        seed_song = None
        if search:
            # Search for the seed song
            try:
                q = search if re.match(r'^https?://', search) else f"ytsearch1:{search}"
                info = await self.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(q, download=False))
                if 'entries' in info:
                    e = info['entries'][0]
                else:
                    e = info
                
                seed_song = {
                    'id': e['id'], 
                    'title': e['title'], 
                    'author': e.get('uploader', 'Unknown'),
                    'url': f"https://www.youtube.com/watch?v={e['id']}"
                }
            except Exception as e:
                return await ctx.send(f"❌ Could not find seed song: {e}")
        else:
            # Fallback to current track or a random cached song
            if state.current_track:
                seed_song = state.current_track
            else:
                valid_cached = [f.replace('.webm', '') for f in os.listdir(CACHE_DIR) if f.endswith('.webm')]
                if valid_cached:
                    vid_id = random.choice(valid_cached)
                    seed_song = {'id': vid_id, 'title': cache_map.get(vid_id, 'Unknown'), 'author': 'Unknown'}
                else:
                    return await ctx.send("❌ Please provide a song name to start the quiz (e.g. `/guess manele`).")

        embed = discord.Embed(title="🎮 Guess Game", description="Choose the type of the game you want to play:", color=COLOR_MAIN)
        if seed_song:
            embed.set_footer(text=f"Based on: {seed_song['title']}")
        
        await ctx.send(embed=embed, view=GuessModeSelectView(self, ctx, seed_song))

    @commands.hybrid_command(name="autoplay")
    async def autoplay(self, ctx):
        state = self.get_state(ctx.guild.id)
        state.autoplay = not state.autoplay
        await self.ensure_autoplay(ctx.guild.id)
        
        if state.autoplay and state.queue and ctx.guild.voice_client and not ctx.guild.voice_client.is_playing():
             await self.play_next(ctx)

        await ctx.send(embed=discord.Embed(description=f"📻 Auto-Play: **{'ON' if state.autoplay else 'OFF'}**", color=COLOR_MAIN), silent=True)

    @commands.hybrid_command(name="new", aliases=["regen", "mix"], description="Regenerate the autoplay suggestion")
    async def new_suggestion(self, ctx):
        state = self.get_state(ctx.guild.id)
        if not state.autoplay:
            return await ctx.send(embed=discord.Embed(description="❌ Auto-Play is OFF.", color=discord.Color.red()), silent=True)
        
        await ctx.defer()
        if await self.regenerate_autoplay(ctx.guild.id):
            await ctx.send(embed=discord.Embed(description="🎲 **Regenerated suggestion!**", color=COLOR_MAIN), silent=True)
        else:
            await ctx.send(embed=discord.Embed(description="❌ Could not regenerate.", color=discord.Color.red()), silent=True)

# ==========================================
# 7. STARTUP
# ==========================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True  # CRITICAL FIX for API visibility

bot = commands.Bot(command_prefix='$', intents=intents)
bot.remove_command('help') 

@bot.event
async def on_ready():
    log_info(f'✅ Logged in as {bot.user}')
    global bot_instance
    bot_instance = bot 
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="$help"))
    try:
        synced = await bot.tree.sync()
        log_info(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        log_error(f"Sync error: {e}")

async def main():
    try:
        async with bot:
            await bot.add_cog(MusicBot(bot))
            await bot.start(TOKEN)
    except KeyboardInterrupt: pass
    finally:
        if not bot.is_closed(): await bot.close()
        log_info("👋 Bot Shutdown.")

if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
        log_info("🚀 UVLoop enabled.")
    except ImportError:
        pass
    asyncio.run(main())