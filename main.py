import asyncio
import discord
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import re
from discord.ext import commands
from collections import deque
import nest_asyncio
import os
import server  # This will start the HTTP server when the bot runs

ffmpeg_path = "./ffmpeg-master-latest-linux64-gpl-shared/bin/ffmpeg"

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

source = discord.FFmpegPCMAudio(data['url'], executable=ffmpeg_path, **ffmpeg_options)


nest_asyncio.apply()

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Spotify Configuration
SPOTIFY_CLIENT_ID = 'e5964b3ed9c140009fb32ec09f433668'
SPOTIFY_CLIENT_SECRET = 'b4d483c5fdfb4c34a0099d24825902cd'

spotify = spotipy.Spotify(
    client_credentials_manager=SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET
    )
)

# --------------------------------------------------------------------
# YTDL Options
# --------------------------------------------------------------------
ytdl_base_opts = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'nooverwrites': True,
    'no_color': True,
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
}

# These are special, so we load only the first track
ytdl_opts_first = {
    'playliststart': 1,
    'playlistend': 1,
}

# Then for everything else (tracks 2..N)
ytdl_opts_rest = {
    'playliststart': 2
}

# --------------------------------------------------------------------
# Helpers to detect Spotify / YouTube
# --------------------------------------------------------------------
def is_spotify_url(url: str) -> bool:
    spotify_patterns = [
        r'https?://open\.spotify\.com/track/\w+',
        r'https?://open\.spotify\.com/playlist/\w+',
        r'https?://open\.spotify\.com/album/\w+',
    ]
    return any(re.match(pattern, url) for pattern in spotify_patterns)

def is_youtube_playlist(url: str) -> bool:
    return ("list=" in url) or ("youtube.com/playlist" in url)

# --------------------------------------------------------------------
# YTDLSource class
# --------------------------------------------------------------------
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title', 'Unknown Title')
        self.url = data.get('webpage_url', '')
        self.duration = data.get('duration', 0)

    @classmethod
    async def from_url(cls, url: str, *, loop=None, stream=True, custom_ytdl_opts=None):
        loop = loop or asyncio.get_event_loop()
        combined_opts = dict(ytdl_base_opts)
        if custom_ytdl_opts:
            combined_opts.update(custom_ytdl_opts)

        ytdl = yt_dlp.YoutubeDL(combined_opts)

        def _extract_info():
            return ytdl.extract_info(url, download=not stream)

        data = await loop.run_in_executor(None, _extract_info)
        if data is None:
            raise ValueError("Could not extract info from URL.")

        # If there's a playlist
        if 'entries' in data:
            sources = []
            for entry in data['entries']:
                if entry:
                    sources.append(await cls.create_source(entry, loop=loop))
            return sources
        else:
            # single track
            return await cls.create_source(data, loop=loop)

    @classmethod
    async def create_source(cls, data, *, loop):
        if 'url' not in data:
            raise ValueError("No URL in extracted data.")

        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'
        }
        source = discord.FFmpegPCMAudio(data['url'], **ffmpeg_options)
        return cls(source, data=data)

# --------------------------------------------------------------------
# MusicQueue
# --------------------------------------------------------------------
class MusicQueue:
    def __init__(self):
        self.queue = deque()
        self.now_playing = None

    def add_song(self, song):
        self.queue.append(song)

    def next_song(self):
        if self.queue:
            self.now_playing = self.queue.popleft()
            return self.now_playing
        return None

    def clear(self):
        self.queue.clear()
        self.now_playing = None

    def __len__(self):
        return len(self.queue)

# --------------------------------------------------------------------
# Global management
# --------------------------------------------------------------------
music_queues = {}
background_tasks = {}

def get_music_queue(guild_id):
    if guild_id not in music_queues:
        music_queues[guild_id] = MusicQueue()
    return music_queues[guild_id]

def cancel_background_task(guild_id):
    task = background_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()

# --------------------------------------------------------------------
# Playback logic
# --------------------------------------------------------------------
import time

async def play_next(ctx):
    guild_id = ctx.guild.id
    music_queue = get_music_queue(guild_id)

    if not ctx.voice_client or not ctx.voice_client.is_connected():
        print("Voice client not connected.")
        return

    next_song = music_queue.next_song()
    if not next_song:
        await ctx.send("No more songs in the queue...")
        return

    print(f"Now playing: {next_song.title} ({next_song.url})")

    def after_playing(error):
        if error:
            print(f"Player error: {error}")
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    retries = 3
    for attempt in range(retries):
        try:
            print(f"Attempt {attempt + 1}: FFmpeg is about to play {next_song.url}")
            ctx.voice_client.play(next_song, after=after_playing)
            await ctx.send(f"🎵 Now playing: **{next_song.title}**")
            break
        except Exception as e:
            print(f"Attempt {attempt + 1} failed: {type(e).__name__}: {e}")
            await ctx.send(f"Attempt {attempt + 1} failed: {type(e).__name__}: {e}")
            if attempt < retries - 1:
                time.sleep(2)  # Wait before retrying
            else:
                print("Max retries reached. Skipping to next track.")
                await play_next(ctx)



# --------------------------------------------------------------------
# YOUTUBE PLAYLIST LOGIC
# --------------------------------------------------------------------
async def handle_youtube_playlist_first_track(ctx, url):
    """
    1) Load only the first track from the YT playlist.
    2) If successful, queue it & play if idle.
    3) Start a background task to load the rest of the playlist from track #2 onwards.
    """
    music_queue = get_music_queue(ctx.guild.id)

    try:
        await ctx.send("🔎 Detected a YouTube playlist. Loading the **first track**...")
        first_tracks = await YTDLSource.from_url(url, loop=bot.loop, stream=True, custom_ytdl_opts=ytdl_opts_first)
        if isinstance(first_tracks, list) and first_tracks:
            for t in first_tracks:
                music_queue.add_song(t)
        elif first_tracks:
            music_queue.add_song(first_tracks)
        else:
            return await ctx.send("❌ Could not load the first track from that playlist.")

        await ctx.send("🎶 Loaded the first track! Starting playback...")
        if not ctx.voice_client.is_playing():
            await play_next(ctx)

        # Now load the rest in the background
        await ctx.send("⏳ Loading the remaining tracks in the background...")
        task = asyncio.create_task(load_youtube_playlist_rest(ctx, url))
        background_tasks[ctx.guild.id] = task

    except Exception as e:
        await ctx.send(f"❌ Error loading YouTube playlist: {e}")

async def load_youtube_playlist_rest(ctx, url):
    """
    Loads track #2..N from the YT playlist one by one.
    If you want a 'fine-grained' approach, we can parse metadata
    and load track-by-track, but here's a simpler approach:
    - Use 'playliststart=2' so YTDL loads everything from 2..N.
    - Add them all at once to the queue.
    - If the bot is idle after each track, it will automatically
      move on to the next in play_next().
    """
    guild_id = ctx.guild.id
    music_queue = get_music_queue(guild_id)
    try:
        rest_tracks = await YTDLSource.from_url(url, loop=bot.loop, stream=True, custom_ytdl_opts=ytdl_opts_rest)
        count = 0
        if rest_tracks:
            if isinstance(rest_tracks, list):
                for track in rest_tracks:
                    # If cancelled mid-process, break
                    if guild_id not in background_tasks:
                        break
                    music_queue.add_song(track)
                    count += 1

                    # If idle, try playing
                    if ctx.voice_client and not ctx.voice_client.is_playing():
                        await play_next(ctx)
            else:
                # single track
                music_queue.add_song(rest_tracks)
                count += 1
                if ctx.voice_client and not ctx.voice_client.is_playing():
                    await play_next(ctx)

        if count > 0:
            await ctx.send(f"➕ Added {count} more track(s) from the YT playlist in the background.")

    except asyncio.CancelledError:
        print(f"YouTube playlist background loading canceled for guild {guild_id}.")
    except Exception as e:
        print(f"Error loading rest of YT playlist: {e}")
        await ctx.send(f"❌ Error loading the remaining YT playlist tracks: {e}")

# --------------------------------------------------------------------
# SPOTIFY HELPERS (unchanged)
# --------------------------------------------------------------------
def get_album_id(url: str) -> str:
    # e.g. "https://open.spotify.com/album/6iAVrBmZ9ZNcdwclpryp89?si=riaFqUsmQ7ea0zMIE5ZP0A"
    parts = url.split('/')
    album_part = parts[-1].split('?')[0]
    return album_part

def get_playlist_id(url: str) -> str:
    parts = url.split('/')
    playlist_part = parts[-1].split('?')[0]
    return playlist_part

async def handle_spotify_track(ctx, track_id):
    track_info = spotify.track(track_id)
    track_name = track_info["name"]
    artist_name = track_info["artists"][0]["name"]
    search_str = f"ytsearch:{artist_name} - {track_name}"
    await add_youtube_search_to_queue(ctx, search_str)

async def handle_spotify_album_first_track(ctx, album_id):
    album_data = spotify.album_tracks(album_id)
    if not album_data or not album_data["items"]:
        await ctx.send("❌ No tracks found in this album.")
        return

    all_tracks = []
    while True:
        if not album_data or not album_data["items"]:
            break
        for item in album_data["items"]:
            all_tracks.append(item)
        if album_data["next"]:
            album_data = spotify.next(album_data)
        else:
            break

    # Load first track
    first_track = all_tracks[0]
    track_name = first_track["name"]
    artist_name = first_track["artists"][0]["name"]
    first_search = f"ytsearch:{artist_name} - {track_name}"
    await add_youtube_search_to_queue(ctx, first_search)

    # BG load the rest
    if len(all_tracks) > 1:
        task = asyncio.create_task(load_spotify_album_rest(ctx, all_tracks[1:]))
        background_tasks[ctx.guild.id] = task
    else:
        await ctx.send("✅ Loaded the only track in that album.")

async def load_spotify_album_rest(ctx, remaining_tracks):
    guild_id = ctx.guild.id
    music_queue = get_music_queue(guild_id)
    count = 0

    try:
        for item in remaining_tracks:
            if guild_id not in background_tasks:
                break

            track_name = item["name"]
            artist_name = item["artists"][0]["name"]
            search_str = f"ytsearch:{artist_name} - {track_name}"

            try:
                result = await YTDLSource.from_url(search_str, loop=bot.loop, stream=True)
                if isinstance(result, list):
                    for r in result:
                        music_queue.add_song(r)
                        count += 1
                else:
                    music_queue.add_song(result)
                    count += 1

                if ctx.voice_client and not ctx.voice_client.is_playing():
                    await play_next(ctx)

            except asyncio.CancelledError:
                print(f"Album loading canceled for guild {guild_id}.")
                return
            except Exception as e:
                print(f"Error loading album track: {e}")

        if count:
            await ctx.send(f"➕ Added {count} more track(s) from Spotify album in the background.")
    except asyncio.CancelledError:
        print("Album background task canceled.")
    except Exception as e:
        print(f"General error in album background loading: {e}")

async def handle_spotify_playlist_first_track(ctx, playlist_id):
    playlist_data = spotify.playlist_items(playlist_id)
    if not playlist_data or not playlist_data["items"]:
        await ctx.send("❌ No tracks found in this playlist.")
        return

    all_items = []
    while True:
        for item in playlist_data["items"]:
            if item["track"] is None:
                continue
            all_items.append(item["track"])
        if playlist_data["next"]:
            playlist_data = spotify.next(playlist_data)
        else:
            break

    # Load first track
    first_track = all_items[0]
    track_name = first_track["name"]
    artist_name = first_track["artists"][0]["name"]
    first_search = f"ytsearch:{artist_name} - {track_name}"
    await add_youtube_search_to_queue(ctx, first_search)

    if len(all_items) > 1:
        task = asyncio.create_task(load_spotify_playlist_rest(ctx, all_items[1:]))
        background_tasks[ctx.guild.id] = task
    else:
        await ctx.send("✅ This playlist has only 1 track!")

async def load_spotify_playlist_rest(ctx, remaining_tracks):
    guild_id = ctx.guild.id
    music_queue = get_music_queue(guild_id)
    count = 0

    try:
        for item in remaining_tracks:
            if guild_id not in background_tasks:
                break

            track_name = item["name"]
            artist_name = item["artists"][0]["name"]
            search_str = f"ytsearch:{artist_name} - {track_name}"

            try:
                result = await YTDLSource.from_url(search_str, loop=bot.loop, stream=True)
                if isinstance(result, list):
                    for r in result:
                        music_queue.add_song(r)
                        count += 1
                else:
                    music_queue.add_song(result)
                    count += 1

                if ctx.voice_client and not ctx.voice_client.is_playing():
                    await play_next(ctx)

            except asyncio.CancelledError:
                print(f"Playlist loading canceled for guild {guild_id}.")
                return
            except Exception as e:
                print(f"Error loading playlist track: {e}")

        if count:
            await ctx.send(f"➕ Added {count} more track(s) from Spotify playlist in background.")
    except asyncio.CancelledError:
        print("Playlist background task canceled.")
    except Exception as e:
        print(f"Error in background playlist loading: {e}")

async def add_youtube_search_to_queue(ctx, search_str: str):
    guild_id = ctx.guild.id
    music_queue = get_music_queue(guild_id)
    try:
        results = await YTDLSource.from_url(search_str, loop=bot.loop, stream=True)
        if isinstance(results, list):
            for track in results:
                music_queue.add_song(track)
        else:
            music_queue.add_song(results)

        if ctx.voice_client and not ctx.voice_client.is_playing():
            await play_next(ctx)

    except Exception as e:
        await ctx.send(f"❌ Error searching YouTube for `{search_str}`: {e}")

# --------------------------------------------------------------------
# Bot Commands
# --------------------------------------------------------------------
@bot.command(name='play')
async def play_command(ctx, *, url):
    if not ctx.author.voice:
        return await ctx.send("❌ You must join a voice channel first!")

    channel = ctx.author.voice.channel
    guild_id = ctx.guild.id
    music_queue = get_music_queue(guild_id)
    cancel_background_task(guild_id)

    try:
        if not ctx.voice_client:
            await channel.connect()
        elif ctx.voice_client.channel != channel:
            await ctx.voice_client.move_to(channel)
    except Exception as e:
        return await ctx.send(f"❌ Error connecting to voice: {e}")

    async with ctx.typing():
        # ----------------------------------------------------------------
        # 1) SPOTIFY
        # ----------------------------------------------------------------
        if is_spotify_url(url):
            if "track" in url:
                await ctx.send("🎧 Detected Spotify **track**. Loading first track immediately...")
                track_id = url.split('/')[-1].split('?')[0]
                await handle_spotify_track(ctx, track_id)

            elif "album" in url:
                album_id = get_album_id(url)
                await ctx.send("🎧 Detected Spotify **album**. Loading first track now...")
                await handle_spotify_album_first_track(ctx, album_id)

            elif "playlist" in url:
                playlist_id = get_playlist_id(url)
                await ctx.send("🎧 Detected Spotify **playlist**. Loading first track now...")
                await handle_spotify_playlist_first_track(ctx, playlist_id)

            return  # done

        # ----------------------------------------------------------------
        # 2) YOUTUBE PLAYLIST
        # ----------------------------------------------------------------
        if is_youtube_playlist(url):
            # Now we actually handle it
            await handle_youtube_playlist_first_track(ctx, url)
            return

        # ----------------------------------------------------------------
        # 3) SINGLE YOUTUBE or OTHER URL
        # ----------------------------------------------------------------
        try:
            player = await YTDLSource.from_url(url, loop=bot.loop, stream=True)
            if isinstance(player, list):
                for p in player:
                    music_queue.add_song(p)
                await ctx.send(f"📑 Added {len(player)} track(s) to the queue.")
            else:
                music_queue.add_song(player)
                await ctx.send(f"➕ Added: **{player.title}**")

            if not ctx.voice_client.is_playing():
                await play_next(ctx)

        except Exception as e:
            await ctx.send(f"❌ Error adding track: {e}")

@bot.command(name='skip')
async def skip_command(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        return await ctx.send("❌ There's nothing playing to skip.")
    ctx.voice_client.stop()
    await ctx.send("⏭️ Skipped.")

@bot.command(name='stop')
async def stop_command(ctx):
    cancel_background_task(ctx.guild.id)
    music_queue = get_music_queue(ctx.guild.id)
    music_queue.clear()

    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send("⏹️ Stopped and disconnected.")
    else:
        await ctx.send("❌ I'm not in a voice channel.")

@bot.command(name='clean')
async def clean_command(ctx):
        guild_id = ctx.guild.id
        cancel_background_task(guild_id)
        music_queue = get_music_queue(guild_id)
        currently_playing = music_queue.now_playing
        music_queue.clear()

        if currently_playing:
            music_queue.now_playing = currently_playing

        await ctx.send(
            "✅ Cleaned: canceled background loading and cleared upcoming queue. Current track is still playing.")

@bot.command(name='queue')
async def queue_command(ctx):
    guild_id = ctx.guild.id
    music_queue = get_music_queue(guild_id)

    if not music_queue.now_playing and not music_queue.queue:
        return await ctx.send("The queue is empty.")

    lines = []
    if music_queue.now_playing:
        lines.append(f"▶️ Now playing: **{music_queue.now_playing.title}**")

    for i, song in enumerate(music_queue.queue, start=1):
        lines.append(f"{i}. {song.title}")

    await ctx.send("\n".join(lines))

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandInvokeError):
        await ctx.send(f"❌ Error: {str(error.original)}")
    else:
        await ctx.send(f"❌ Error: {str(error)}")




bot.run(os.getenv("DISCORD_BOT_TOKEN"))

