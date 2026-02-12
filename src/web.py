import asyncio
import logging
import os
import random
import re
import shutil
import psutil
from quart import Quart, jsonify, make_response, redirect, render_template, request, send_from_directory
import yt_dlp

from config import (
    CACHE_DIR, PLAYLIST_FILE, YDL_FLAT_OPTS, YDL_PLAYLIST_LOAD_OPTS
)
from utils import (
    log_error, save_json, format_time, get_thumbnail_url, 
    cache_map, saved_playlists
)

app = Quart(__name__, template_folder='templates')
logging.getLogger('quart.serving').setLevel(logging.ERROR)
logging.getLogger('hypercorn.error').setLevel(logging.ERROR)
bot_instance = None 

# --- Auth Helpers ---
def get_bot_cog():
    """Reliably retrieves the MusicBot cog instance."""
    if 'BOT_COG' in app.config:
        return app.config['BOT_COG']
    # Fallback to global search
    if bot_instance:
        return bot_instance.get_cog('MusicBot')
    return None

def get_first_available_guild():
    """Returns the first guild the bot is connected to (for single-server setups)."""
    cog = get_bot_cog()
    if cog and cog.bot:
        if cog.bot.voice_clients:
            return cog.bot.voice_clients[0].guild
        if cog.bot.guilds:
            return cog.bot.guilds[0]
    return None

def get_bot_token():
    """Retrieves the secure web token from the bot instance."""
    cog = get_bot_cog()
    if cog:
        return cog.web_auth_token
    return None

def get_target_guild(guild_id=None):
    """Returns the target guild based on URL param, cookie, or fallback."""
    cog = get_bot_cog()
    if not cog or not cog.bot: return None
    
    # 1. Try URL param (highest priority)
    if guild_id:
        try:
            guild = cog.bot.get_guild(int(guild_id))
            if guild: return guild
        except: pass

    # 2. Try cookie
    g_id = request.cookies.get('pi_music_guild_id')
    if g_id:
        try:
            guild = cog.bot.get_guild(int(g_id))
            if guild: return guild
        except: pass
        
    return get_first_available_guild()

def set_bot_instance(bot):
    global bot_instance
    bot_instance = bot

# --- Routes ---

@app.before_request
def check_auth():
    if request.path.startswith('/auth') or request.path.startswith('/cache') or request.path.startswith('/health'):
        return
    
    user_token = request.cookies.get('pi_music_auth')
    server_token = get_bot_token()
    
    if not server_token or user_token != server_token:
        html = """
            <body style="background:#0f0f0f; color:#eee; font-family:sans-serif; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; margin:0;">
                <h1 style="color:#ff4444; font-size:4rem; margin:0;">NO ACCESS</h1>
                <h2 style="margin-top:10px;">Access Denied</h2>
                <p style="color:#888;">Use <code>/link</code> in Discord to generate a secure key.</p>
            </body>
        """
        return html, 403

@app.route('/auth')
async def auth_route():
    token_from_url = request.args.get('token')
    guild_id = request.args.get('guild')
    server_token = get_bot_token()
    
    if token_from_url == server_token:
        resp = await make_response(redirect(f'/dashboard/{guild_id}' if guild_id else '/'))
        # Using Lax SameSite policy to allow cookie to set during redirect
        resp.set_cookie('pi_music_auth', token_from_url, max_age=86400, samesite='Lax')
        if guild_id:
            resp.set_cookie('pi_music_guild_id', guild_id, max_age=86400, samesite='Lax')
        return resp
    
    log_error(f"Auth Failed. URL Token: {token_from_url}, Server Token: {server_token}")
    return "❌ Invalid Token.", 403

@app.route('/cache/thumb/<path:filename>')
async def serve_thumbnail(filename):
    return await send_from_directory(os.path.abspath(CACHE_DIR), filename)

@app.route('/health')
async def health_check():
    return "OK", 200

@app.route('/')
async def home_redirect():
    g_id = request.cookies.get('pi_music_guild_id')
    if g_id:
        return redirect(f'/dashboard/{g_id}')
    
    guild = get_first_available_guild()
    if guild:
        return redirect(f'/dashboard/{guild.id}')
    
    return "❌ No server found. Use /link in Discord.", 404

@app.route('/dashboard/<int:guild_id>')
async def dashboard(guild_id):
    cog = get_bot_cog()
    if not cog or not cog.bot: return redirect('/')
    
    guild = cog.bot.get_guild(guild_id)
    if not guild: return "❌ Bot is not in this server.", 404
    
    name = cog.bot.user.name if cog.bot.user else "MusicBot"
    return await render_template('dashboard.html', bot_name=name, guild_id=guild_id)

@app.route('/api/sysinfo')
async def api_sysinfo():
    # CPU Usage
    cpu_usage = psutil.cpu_percent()
    
    # RAM Usage
    ram = psutil.virtual_memory()
    ram_usage = ram.percent
    
    # Temperature (Linux/Pi)
    temp = 0
    try:
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                temp = int(f.read()) / 1000.0
    except:
        pass
    
    # Storage (Music Cache specific)
    try:
        if not os.path.exists(CACHE_DIR):
            os.makedirs(CACHE_DIR, exist_ok=True)
        
        # Calculate total size of files in cache folder
        total_used_bytes = 0
        for f in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, f)
            if os.path.isfile(fp):
                total_used_bytes += os.path.getsize(fp)
        
        used_gb = total_used_bytes / (1024**3)
        from config import MAX_CACHE_SIZE_GB
        limit_gb = MAX_CACHE_SIZE_GB
        free_in_cache_gb = max(0, limit_gb - used_gb)
        storage_percent = (used_gb / limit_gb) * 100 if limit_gb > 0 else 0
        
        storage_display_free = free_in_cache_gb
        storage_display_total = limit_gb
    except Exception:
        storage_display_free = 0
        storage_display_total = 0
        storage_percent = 0
    
    return jsonify({
        'cpu': cpu_usage,
        'ram': ram_usage,
        'temp': round(temp, 1),
        'storage_free': round(storage_display_free, 1),
        'storage_total': round(storage_display_total, 1),
        'storage_percent': round(storage_percent, 1)
    })

# --- API Routes ---

@app.route('/api/<int:guild_id>/status')
async def api_status(guild_id):
    guild = get_target_guild(guild_id)
    cog = get_bot_cog()
    
    # Debug info
    if not cog: log_error("API Error: Bot Cog not found in config.")
    elif not guild: log_error(f"API Error: Bot is online but no guild found. Guilds len: {len(cog.bot.guilds) if cog and cog.bot else 'None'}")
    
    if not guild or not cog:
        return jsonify({'current': None, 'queue': [], 'guild': None, 'autoplay': False})
    
    state = cog.get_state(guild.id)
    
    current = None
    if state.current_track:
        current = {
            'title': state.current_track['title'],
            'author': state.current_track['author'],
            'duration': state.current_track['duration'],
            'thumbnail': get_thumbnail_url(state.current_track['id'])
        }
    
    queue_data = []
    for t in state.queue:
        queue_data.append({
            'title': t['title'],
            'author': t.get('author', 'Unknown'),
            'id': t['id'],
            'thumbnail': get_thumbnail_url(t['id']),
            'suggested': t.get('suggested', False)
        })
        
    return jsonify({'current': current, 'queue': queue_data, 'guild': guild.name, 'autoplay': state.autoplay})

@app.route('/api/<int:guild_id>/playlists', methods=['GET'])
async def api_get_playlists(guild_id):
    data = []
    for name, content in saved_playlists.items():
        if isinstance(content, list):
            data.append({'name': name, 'count': len(content), 'type': 'static'})
        elif isinstance(content, dict):
            data.append({'name': name, 'count': 0, 'type': 'live'})
    return jsonify(data)

@app.route('/api/<int:guild_id>/playlists/save', methods=['POST'])
async def api_save_playlist(guild_id):
    data = await request.get_json()
    name = data.get('name', '').lower()
    url = data.get('url', '')
    
    if not name:
        return jsonify({'error': 'No name'}), 400
        
    if url:
        if 'youtube.com' not in url and 'youtu.be' not in url:
             return jsonify({'error': 'Invalid YouTube URL'}), 400
             
        saved_playlists[name] = {'type': 'live', 'url': url}
        save_json(PLAYLIST_FILE, saved_playlists)
        return jsonify({'status': 'ok'})
    
    # Save current queue
    guild = get_target_guild(guild_id)
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
        
    state = cog.get_state(guild.id)
    
    tracks = []
    if state.current_track:
        tracks.append(state.current_track)
    tracks.extend(state.queue)
    
    if not tracks:
        return jsonify({'error': 'Empty'}), 400
    
    clean = [{
        'id': t['id'], 
        'title': t['title'], 
        'author': t['author'], 
        'duration': t['duration'], 
        'duration_seconds': t['duration_seconds'], 
        'webpage': t['webpage']
    } for t in tracks]
    
    saved_playlists[name] = clean
    save_json(PLAYLIST_FILE, saved_playlists)
    return jsonify({'status': 'ok'})

@app.route('/api/<int:guild_id>/playlists/load', methods=['POST'])
async def api_load_playlist(guild_id):
    data = await request.get_json()
    name = data.get('name', '').lower()
    
    if name not in saved_playlists:
        return jsonify({'error': 'Not found'}), 404
        
    guild = get_target_guild(guild_id)
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
        
    state = cog.get_state(guild.id)
    content = saved_playlists[name]
    new_tracks = []
    
    if isinstance(content, list):
        new_tracks = content
    elif isinstance(content, dict):
        try:
            info = await cog.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_PLAYLIST_LOAD_OPTS).extract_info(content['url'], download=False))
            if 'entries' in info:
                for e in info['entries']:
                    if e:
                        new_tracks.append({
                            'id': e['id'], 
                            'title': e['title'], 
                            'author': e['uploader'], 
                            'duration': format_time(e['duration']), 
                            'duration_seconds': e['duration'], 
                            'webpage': f"https://www.youtube.com/watch?v={e['id']}"
                        })
            asyncio.create_task(cog.load_rest_of_playlist(content['url'], guild.id))
        except Exception as e:
            log_error(f"Playlist load error: {e}")
            return jsonify({'error': 'Fetch fail'}), 500
        
    if new_tracks:
        state.queue.extend(new_tracks)
        # Try to connect if not in VC
        if not guild.voice_client:
            for channel in guild.voice_channels:
                if len(channel.members) > 0:
                    await channel.connect()
                    break

        if guild.voice_client and not guild.voice_client.is_playing() and not state.processing_next:
             class DummyCtx:
                 def __init__(self, g, v):
                     self.guild = g
                     self.voice_client = v
                     self.author = "WebUser"
                 async def send(self, *args, **kwargs): pass 
             
             await cog.play_next(DummyCtx(guild, guild.voice_client))
        return jsonify({'status': 'ok'})
        
    return jsonify({'error': 'Empty'}), 400

@app.route('/api/<int:guild_id>/playlists/delete', methods=['POST'])
async def api_del_playlist(guild_id):
    data = await request.get_json()
    if data['name'] in saved_playlists:
        del saved_playlists[data['name']]
        save_json(PLAYLIST_FILE, saved_playlists)
    return jsonify({'status': 'ok'})

@app.route('/api/<int:guild_id>/search', methods=['POST'])
async def api_search(guild_id):
    data = await request.get_json()
    cog = get_bot_cog()
    if not cog: return jsonify([]), 500
    try:
        info = await cog.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(f"ytsearch5:{data['query']}", download=False))
        res = []
        if 'entries' in info:
            for e in info['entries']:
                if e:
                    thumb = e.get('thumbnail')
                    if not thumb or not thumb.startswith('http'):
                        thumb = f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"
                    
                    res.append({
                        'title': e['title'], 
                        'author': e['uploader'], 
                        'duration': format_time(e['duration']), 
                        'url': f"https://www.youtube.com/watch?v={e['id']}", 
                        'thumbnail': thumb
                    })
        return jsonify(res)
    except Exception:
        return jsonify([]), 500

@app.route('/api/<int:guild_id>/control/<action>', methods=['POST'])
async def api_control(guild_id, action):
    guild = get_target_guild(guild_id)
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
        
    vc = guild.voice_client
    state = cog.get_state(guild.id)
    
    if action == 'pause' and vc: 
        if vc.is_playing():
            vc.pause() 
        elif vc.is_paused():
            vc.resume()
    elif action == 'skip' and vc:
        vc.stop()
    elif action == 'clear':
        if state.autoplay:
            state.queue = [t for t in state.queue if t.get('suggested')]
        else:
            state.queue = []
    elif action == 'shuffle':
        user_queue = [t for t in state.queue if not t.get('suggested')]
        suggested = [t for t in state.queue if t.get('suggested')]
        random.shuffle(user_queue)
        state.queue[:] = user_queue + suggested
    elif action == 'autoplay':
        state.autoplay = not state.autoplay
        await cog.ensure_autoplay(guild.id)
        if state.autoplay and state.queue and vc and not vc.is_playing():
             class DummyCtx:
                 def __init__(self, g, v):
                     self.guild = g
                     self.voice_client = v
                     self.author = "WebUser"
                 async def send(self, *args, **kwargs): pass 
             await cog.play_next(DummyCtx(guild, vc))
    elif action == 'regenerate':
        await cog.regenerate_autoplay(guild.id)
        
    return jsonify({'status':'ok'})

@app.route('/api/<int:guild_id>/remove/<int:index>', methods=['POST'])
async def api_remove(guild_id, index):
    guild = get_target_guild(guild_id)
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
    
    state = cog.get_state(guild.id)
    if 0 <= index < len(state.queue):
        if state.queue[index].get('suggested') and state.autoplay:
            return jsonify({'error': 'Cannot remove autoplay suggestion'}), 400
        del state.queue[index]
    return jsonify({'status': 'ok'})

@app.route('/api/<int:guild_id>/add', methods=['POST'])
async def api_add(guild_id):
    data = await request.get_json()
    guild = get_target_guild(guild_id)
    cog = get_bot_cog()
    if not guild or not cog:
        return jsonify({'error': 'No guild'}), 400
    
    state = cog.get_state(guild.id)
    query = data['query']
    if not re.match(r'^https?://', query):
        query = f"ytsearch1:{query}"
    
    if not state.last_text_channel:
        state.last_text_channel = guild.text_channels[0]
    
    # 1. Safer clear: Only remove the suggestion if it's at the end to make room
    if state.queue and state.queue[-1].get('suggested'):
        state.queue.pop()

    try:
        # Try to connect if not in VC
        if not guild.voice_client:
            for channel in guild.voice_channels:
                if len(channel.members) > 0:
                    await channel.connect()
                    break

        # Use Flat Options (verified working)
        info = await cog.bot.loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YDL_FLAT_OPTS).extract_info(query, download=False))
        
        # 2. Safer clear: Re-check after await
        if state.queue and state.queue[-1].get('suggested'):
            state.queue.pop()

        def process(e): 
            url = e.get('webpage_url') or e.get('url') or f"https://www.youtube.com/watch?v={e['id']}"
            return {
                'id': e['id'], 
                'title': e['title'], 
                'author': e.get('uploader', 'Unknown'), 
                'duration': format_time(e.get('duration', 0)), 
                'duration_seconds': e.get('duration', 0), 
                'webpage': url
            }
        
        if 'entries' in info:
            state.queue.extend([process(e) for e in info['entries'] if e])
        else:
            state.queue.append(process(info))
        
        # Ensure autoplay suggestion is at the end
        cog.bot.loop.create_task(cog.ensure_autoplay(guild.id, force=True))

        if guild.voice_client and not guild.voice_client.is_playing() and not state.processing_next:
             class DummyCtx:
                 def __init__(self, g, v):
                     self.guild = g
                     self.voice_client = v
                     self.author = "WebUser"
                 async def send(self, *args, **kwargs): pass 
             
             await cog.play_next(DummyCtx(guild, guild.voice_client))

        return jsonify({'status':'ok'})
    except Exception:
        return jsonify({'error':'fail'}), 500

@app.route('/api/<int:guild_id>/game/status')
async def api_game_status(guild_id):
    guild = get_target_guild(guild_id)
    cog = get_bot_cog()
    if not guild or not cog: return jsonify({'active': False})
    
    state = cog.get_state(guild.id)
    if not state.game or not state.game.active:
        return jsonify({'active': False})
        
    g = state.game
    # Clean scores for JSON
    scores = []
    for uid, s in g.scores.items():
        if isinstance(uid, int):
            user = cog.bot.get_user(uid)
            name = user.display_name if user else f"User {uid}"
        else:
            name = uid.replace('web_', '')
        scores.append({'name': name, 'score': s})
        
    scores.sort(key=lambda x: x['score'], reverse=True)
    
    return jsonify({
        'active': True,
        'mode': g.mode,
        'round_duration': g.play_duration,
        'scores': scores,
        'transitioning': g.transitioning
    })

@app.route('/api/<int:guild_id>/game/guess', methods=['POST'])
async def api_game_guess(guild_id):
    data = await request.get_json()
    guess = data.get('guess', '').strip()
    name = data.get('name', 'WebUser').strip()
    
    if not guess: return jsonify({'error': 'Empty guess'}), 400
    
    guild = get_target_guild(guild_id)
    cog = get_bot_cog()
    if not guild or not cog: return jsonify({'error': 'No guild'}), 400
    
    state = cog.get_state(guild.id)
    if not state.game or not state.game.active:
        return jsonify({'error': 'No active game'}), 400
        
    result = await state.game.process_web_guess(name, guess)
    return jsonify({'correct': result})

@app.route('/api/<int:guild_id>/game/control/<action>', methods=['POST'])
async def api_game_web_control(guild_id, action):
    log_info(f"🕹️ Web Game Control: {action} for guild {guild_id}")
    guild = get_target_guild(guild_id)
    cog = get_bot_cog()
    if not guild or not cog: 
        log_error(f"Web Control Error: Guild {guild_id} or Cog not found")
        return jsonify({'error': 'No guild'}), 400
    
    state = cog.get_state(guild.id)
    if not state.game or not state.game.active:
        log_error(f"Web Control Error: No active game for guild {guild.id}")
        return jsonify({'error': 'No active game'}), 400
    
    g = state.game
    if action == 'more_time':
        if not g.transitioning: await g.play_segment(extra=5)
    elif action == 'rehear':
        if not g.transitioning: await g.play_segment(extra=0)
    elif action == 'skip':
        await g.trigger_transition(reveal=True)
    elif action == 'stop':
        await g.stop()
        
    return jsonify({'status': 'ok'})
