import discord
import os
import asyncio
import aiohttp
import time
import json
import wavelink
from discord.ext import commands, tasks
from datetime import datetime
import logging

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)

# ==================== CONFIGURATION ====================
TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
API_ENDPOINT = os.environ.get('API_ENDPOINT', 'https://captkira.vercel.app/api/presence')
API_SECRET = os.environ.get('API_SECRET', 'Bisaya-Presence-2024-SecretKey!')

# Lavalink
LAVALINK_HOST = os.environ.get('LAVALINK_HOST', '')
LAVALINK_PORT = os.environ.get('LAVALINK_PORT', '443')
LAVALINK_PASSWORD = os.environ.get('LAVALINK_PASSWORD', '')
LAVALINK_SSL = os.environ.get('LAVALINK_SSL', 'true').lower() in ('1', 'true', 'yes')

# TikTok / Kick live notifications
DATA_DIR = os.environ.get('DATA_DIR', '.')
LIVE_LINKS_FILE = os.path.join(DATA_DIR, 'live_links.json')
LIVE_CHECK_INTERVAL_SECONDS = int(os.environ.get('LIVE_CHECK_INTERVAL_SECONDS', '60'))

try:
    from TikTokLive import TikTokLiveClient
    TIKTOK_AVAILABLE = True
except ImportError:
    TIKTOK_AVAILABLE = False
    logger.warning("⚠️ TikTokLive package not installed — TikTok live checks disabled. Add 'TikTokLive' to requirements.txt.")

# ==================== BOT SETUP ====================
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


class Player(wavelink.Player):
    """wavelink Player subclass so we can remember which text channel to post updates in."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.home: discord.abc.Messageable | None = None


# ==================== AVATAR DECORATION FETCH ====================
# Cache stores (png_url_or_None, fetched_at)
_decoration_cache: dict[int, tuple[str | None, float]] = {}
_DECORATION_TTL = 60 * 60 * 12  # 12 hours


def _build_decoration_url(asset: str) -> str:
    """Build the FULL Discord CDN URL for an avatar decoration asset.

    Discord's decoration asset hashes do NOT use the `a_` animated prefix
    convention (that's for avatars/banners). We always serve the `.png`
    variant of the decoration, regardless of whether it is animated.
    """
    asset = str(asset).strip()
    # Strip any extension the caller may have accidentally included.
    for ext in (".png", ".gif", ".webp"):
        if asset.lower().endswith(ext):
            asset = asset[: -len(ext)]
            break
    return f"https://cdn.discordapp.com/avatar-decoration-presets/{asset}.png?size=240"


async def get_avatar_decoration(user_id: int) -> str | None:
    """Return the FULL CDN URL (.png) for a user's avatar decoration, or None.

    Returns None if the user has no decoration, if the fetch fails, or if the
    API returns a non-200 status. Cached for 12h.
    """
    now = time.time()
    cached = _decoration_cache.get(user_id)
    if cached and (now - cached[1]) < _DECORATION_TTL:
        return cached[0]

    headers = {"Authorization": f"Bot {TOKEN}"}
    url = f"https://discord.com/api/v10/users/{user_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"⚠️ Decoration fetch got {resp.status} for {user_id}")
                    _decoration_cache[user_id] = (None, now)
                    return None
                data = await resp.json()
                deco = None
                deco_data = data.get("avatar_decoration_data")
                if deco_data and deco_data.get("asset"):
                    deco = _build_decoration_url(deco_data["asset"])
                    logger.info(f"✨ Decoration URL for {user_id}: {deco}")
                else:
                    logger.info(f"ℹ️ No decoration for {user_id}")
                _decoration_cache[user_id] = (deco, now)
                return deco
    except Exception as e:
        logger.error(f"❌ Decoration fetch error for {user_id}: {e}")
        _decoration_cache[user_id] = (None, now)
        return None


def clear_decoration_cache():
    _decoration_cache.clear()


# ==================== LAVALINK CONNECTION ====================

async def connect_lavalink(max_attempts: int = 5, base_delay: float = 3.0):
    if not LAVALINK_HOST or not LAVALINK_PASSWORD:
        logger.error("❌ LAVALINK_HOST or LAVALINK_PASSWORD not set — skipping Lavalink connection.")
        return
    scheme = "https" if LAVALINK_SSL else "http"
    uri = f"{scheme}://{LAVALINK_HOST}:{LAVALINK_PORT}"

    for attempt in range(1, max_attempts + 1):
        node = wavelink.Node(uri=uri, password=LAVALINK_PASSWORD)
        try:
            await wavelink.Pool.connect(nodes=[node], client=bot)
            logger.info(f"✅ Connected to Lavalink node at {uri} (attempt {attempt})")
            return
        except Exception as e:
            logger.error(f"❌ Lavalink connect attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                delay = base_delay * attempt
                logger.info(f"⏳ Retrying Lavalink connection in {delay:.0f}s...")
                await asyncio.sleep(delay)

    logger.error("❌ Exhausted all Lavalink connection attempts.")


@bot.command(name="reconnectlavalink")
@commands.has_permissions(administrator=True)
async def reconnect_lavalink(ctx):
    """Manually retry the Lavalink node connection (Admin only)"""
    await ctx.send("🔄 Retrying Lavalink connection...")
    await connect_lavalink()
    if wavelink.Pool.nodes:
        await ctx.send("✅ Lavalink connected!")
    else:
        await ctx.send("❌ Still couldn't connect — check the Lavalink service.")


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    logger.info(f"✅ Wavelink node ready: {payload.node.uri} (resumed={payload.resumed})")


@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    player: Player = payload.player  # type: ignore
    track = payload.track
    if not player or not player.home:
        return

    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**{track.title}**",
        color=discord.Color.blue()
    )
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    if track.length:
        minutes, seconds = divmod(track.length // 1000, 60)
        embed.add_field(name="Duration", value=f"{minutes}:{seconds:02d}", inline=True)

    requester = getattr(track.extras, "requester", None) if track.extras else None
    if requester:
        embed.add_field(name="Requested By", value=requester, inline=True)

    if getattr(track.extras, "is_fallback", False) if track.extras else False:
        embed.set_footer(text="⚠️ Played via SoundCloud fallback")

    try:
        await player.home.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send now-playing embed: {e}")


@bot.event
async def on_wavelink_inactive_player(player: Player):
    if player.home:
        try:
            await player.home.send("📭 Queue is empty — leaving the voice channel due to inactivity.")
        except Exception as e:
            logger.error(f"Failed to send inactive message: {e}")
    await player.disconnect()


# ==================== TRACK LOAD FAILURE FALLBACK ====================

FALLBACK_SOURCE = wavelink.TrackSource.SoundCloud
YOUTUBE_HOSTS = ("youtube.com", "youtu.be")


def _is_youtube_track(track: wavelink.Playable) -> bool:
    source = (getattr(track, "source", "") or "").lower()
    uri = (getattr(track, "uri", "") or "").lower()
    return source == "youtube" or any(host in uri for host in YOUTUBE_HOSTS)


async def _search_fallback(track: wavelink.Playable):
    query_parts = [p for p in (track.title, track.author) if p]
    query = " ".join(query_parts).strip() or track.title
    try:
        result: wavelink.Search = await wavelink.Playable.search(query, source=FALLBACK_SOURCE)
    except Exception as e:
        logger.error(f"Fallback SoundCloud search failed for '{query}': {e}")
        return None
    if not result:
        return None
    return result.tracks[0] if isinstance(result, wavelink.Playlist) else result[0]


def _safe_send_text(reason: str, limit: int = 1800) -> str:
    """Trim a potentially huge error string so it fits Discord's 2000-char limit."""
    if not reason:
        return "Unknown error"
    reason = str(reason)
    if len(reason) <= limit:
        return reason
    return reason[:limit] + "\n…(truncated)"


@bot.event
async def on_wavelink_track_exception(payload: wavelink.TrackExceptionEventPayload):
    player: Player = payload.player  # type: ignore
    track = payload.track
    if not player:
        return

    already_fallback = getattr(track.extras, "is_fallback", False) if track.extras else False

    if _is_youtube_track(track) and not already_fallback:
        logger.warning(f"YouTube playback failed for '{track.title}' — trying SoundCloud fallback.")
        fallback_track = await _search_fallback(track)

        if fallback_track:
            old_extras = track.extras.__dict__ if track.extras else {}
            fallback_track.extras = {**old_extras, "is_fallback": True}
            if player.home:
                try:
                    await player.home.send(
                        f"⚠️ YouTube blocked **{track.title}** — found it on SoundCloud instead."
                    )
                except Exception as e:
                    logger.error(f"Failed to send fallback notice: {e}")
            try:
                await player.play(fallback_track)
            except Exception as e:
                logger.error(f"Failed to start SoundCloud fallback for '{track.title}': {e}")
            return
        else:
            if player.home:
                try:
                    await player.home.send(
                        f"❌ Couldn't play **{track.title}** — no SoundCloud match found."
                    )
                except Exception as e:
                    logger.error(f"Failed to send failure notice: {e}")
    else:
        reason_raw = payload.exception.get("message", "Unknown error") if payload.exception else "Unknown error"
        logger.error(f"Playback failed for '{track.title}': {reason_raw}")

        if player.home:
            try:
                await player.home.send(
                    f"❌ Playback failed for **{track.title}**:\n"
                    f"```{_safe_send_text(reason_raw)}```"
                )
            except Exception as e:
                logger.error(f"Failed to send exception message: {e}")

    if not player.playing and not player.queue.is_empty:
        try:
            await player.play(player.queue.get())
        except Exception as e:
            logger.error(f"Failed to play next track after exception: {e}")


# ==================== VOICE CONNECTION ====================

async def connect_voice(ctx) -> tuple[Player | None, str | None]:
    if not ctx.author.voice:
        return None, "❌ You need to be in a voice channel!"

    voice_channel = ctx.author.voice.channel
    player: Player = ctx.voice_client  # type: ignore
    if player:
        if player.channel == voice_channel:
            player.home = ctx.channel
            return player, None
        await player.disconnect()
        await asyncio.sleep(1)

    try:
        player = await voice_channel.connect(cls=Player, timeout=30.0)
        player.home = ctx.channel
        player.autoplay = wavelink.AutoPlayMode.partial
        logger.info(f"✅ Connected to {voice_channel.name}")
        return player, None
    except Exception as e:
        logger.error(f"Voice connect failed: {e}")
        return None, f"❌ Failed to connect: {str(e)}"


# ==================== MUSIC COMMANDS ====================

DEFAULT_SEARCH_SOURCE = os.environ.get('DEFAULT_SEARCH_SOURCE', 'soundcloud')

_SOURCE_MAP = {
    'soundcloud': wavelink.TrackSource.SoundCloud,
    'youtube_music': wavelink.TrackSource.YouTubeMusic,
    'youtube': wavelink.TrackSource.YouTube,
}
_DEFAULT_SOURCE = _SOURCE_MAP.get(DEFAULT_SEARCH_SOURCE, wavelink.TrackSource.SoundCloud)


def is_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")


async def search_playable(text: str) -> wavelink.Search:
    """Search Lavalink. URLs (including Spotify, YouTube, SoundCloud) go straight
    through to Lavalink so LavaSrc can resolve Spotify natively."""
    if is_url(text):
        return await wavelink.Playable.search(text)
    return await wavelink.Playable.search(text, source=_DEFAULT_SOURCE)


@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query):
    """Play a song. Accepts plain text, YouTube, SoundCloud, Spotify URLs, etc."""
    player, error = await connect_voice(ctx)
    if error:
        await ctx.send(error)
        return

    await ctx.send(f"🔍 Searching for: {query}...")

    try:
        result: wavelink.Search = await search_playable(query)
    except Exception as e:
        logger.error(f"Lavalink search error for '{query}': {e}")
        await ctx.send("❌ Search failed — the Lavalink node may not have a working source plugin.")
        return

    if not result:
        if "spotify.com" in query:
            await ctx.send(
                "❌ Couldn't resolve that Spotify link. Make sure `SPOTIFY_CLIENT_ID` and "
                "`SPOTIFY_CLIENT_SECRET` are set on the **Lavalink** service and LavaSrc is loaded."
            )
        else:
            await ctx.send("❌ No results found!")
        return

    if isinstance(result, wavelink.Playlist):
        for track in result.tracks:
            track.extras = {"requester": ctx.author.mention, "is_fallback": False}
        await player.queue.put_wait(result)
        await ctx.send(f"✅ Added playlist **{result.name}** ({len(result.tracks)} tracks) to queue")
    else:
        track = result[0]
        track.extras = {"requester": ctx.author.mention, "is_fallback": False}
        await player.queue.put_wait(track)
        await ctx.send(f"✅ Added to queue: **{track.title}**")

    if not player.playing:
        await player.play(player.queue.get())


@bot.command(name="skip")
async def skip(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if not player or not player.playing:
        await ctx.send("❌ Nothing is playing!")
        return
    await player.skip(force=True)
    await ctx.send("⏭️ Skipped!")


@bot.command(name="stop")
async def stop(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if not player:
        await ctx.send("❌ I'm not in a voice channel!")
        return
    player.queue.clear()
    await player.stop()
    await player.disconnect()
    await ctx.send("⏹️ Stopped!")


@bot.command(name="pause")
async def pause(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if player and player.playing and not player.paused:
        await player.pause(True)
        await ctx.send("⏸️ Paused!")
    else:
        await ctx.send("❌ Nothing is playing!")


@bot.command(name="resume")
async def resume(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if player and player.paused:
        await player.pause(False)
        await ctx.send("▶️ Resumed!")
    else:
        await ctx.send("❌ Nothing is paused!")


@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if not player or (not player.current and player.queue.is_empty):
        await ctx.send("📭 Queue is empty!")
        return
    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blue())
    if player.current:
        embed.add_field(name="🎶 Currently Playing", value=f"**{player.current.title}**", inline=False)
    if not player.queue.is_empty:
        queue_text = ""
        for i, track in enumerate(list(player.queue)[:10], 1):
            queue_text += f"`{i}.` {track.title}\n"
        embed.add_field(name=f"⏭️ Up Next ({len(player.queue)} tracks)", value=queue_text[:1024], inline=False)
    embed.set_footer(text=f"Queue size: {len(player.queue)}")
    await ctx.send(embed=embed)


@bot.command(name="loop")
async def loop(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if not player:
        await ctx.send("❌ Nothing is playing!")
        return
    if player.queue.mode == wavelink.QueueMode.loop:
        player.queue.mode = wavelink.QueueMode.normal
        await ctx.send("🔁 Loop disabled!")
    else:
        player.queue.mode = wavelink.QueueMode.loop
        await ctx.send("🔁 Loop enabled!")


@bot.command(name="nowplaying", aliases=["np"])
async def now_playing(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if not player or not player.current:
        await ctx.send("❌ Nothing is playing!")
        return
    track = player.current
    embed = discord.Embed(title="🎵 Now Playing", description=f"**{track.title}**", color=discord.Color.blue())
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    if track.length:
        minutes, seconds = divmod(track.length // 1000, 60)
        embed.add_field(name="⏱️ Duration", value=f"{minutes}:{seconds:02d}", inline=True)
    if track.author:
        embed.add_field(name="👤 Uploader", value=track.author, inline=True)
    requester = getattr(track.extras, "requester", None) if track.extras else None
    if requester:
        embed.add_field(name="📝 Requested By", value=requester, inline=True)
    if player.queue.mode == wavelink.QueueMode.loop:
        embed.add_field(name="🔁 Loop", value="Enabled", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="clearqueue", aliases=["cq"])
async def clear_queue(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if player and not player.queue.is_empty:
        player.queue.clear()
        await ctx.send("🗑️ Queue cleared!")
    else:
        await ctx.send("📭 Queue is already empty!")


@bot.command(name="remove")
async def remove_from_queue(ctx, position: int):
    player: Player = ctx.voice_client  # type: ignore
    if not player or player.queue.is_empty:
        await ctx.send("📭 Queue is empty!")
        return
    try:
        removed = player.queue.delete(position - 1)
        await ctx.send(f"✅ Removed: **{removed.title}**")
    except Exception:
        await ctx.send(f"❌ No track at position {position}")


@bot.command(name="shuffle")
async def shuffle_queue(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if not player or len(player.queue) < 2:
        await ctx.send("❌ Need at least 2 songs!")
        return
    player.queue.shuffle()
    await ctx.send("🔀 Queue shuffled!")


@bot.command(name="leave")
async def leave(ctx):
    player: Player = ctx.voice_client  # type: ignore
    if player:
        player.queue.clear()
        await player.disconnect()
        await ctx.send("👋 Left!")
    else:
        await ctx.send("❌ I'm not in a voice channel!")


# ==================== PER-GUILD CONFIG ====================
GUILD_CONFIG_FILE = os.path.join(DATA_DIR, 'guild_config.json')


def load_guild_config():
    if os.path.exists(GUILD_CONFIG_FILE):
        try:
            with open(GUILD_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load guild_config.json: {e}")
    return {}


def save_guild_config():
    try:
        os.makedirs(os.path.dirname(GUILD_CONFIG_FILE) or '.', exist_ok=True)
        with open(GUILD_CONFIG_FILE, 'w') as f:
            json.dump(guild_config, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save guild_config.json: {e}")


guild_config = load_guild_config()


# ==================== TIKTOK / KICK LIVE NOTIFICATIONS ====================

def load_live_data():
    if os.path.exists(LIVE_LINKS_FILE):
        try:
            with open(LIVE_LINKS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load live_links.json: {e}")
    return {"guild_channels": {}, "guild_roles": {}, "users": {}}


def save_live_data():
    try:
        os.makedirs(os.path.dirname(LIVE_LINKS_FILE) or '.', exist_ok=True)
        with open(LIVE_LINKS_FILE, 'w') as f:
            json.dump(live_data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save live_links.json: {e}")


live_data = load_live_data()
live_status_cache: dict[str, dict] = {}


def get_all_live_users():
    """Return a list of all users currently live, with links."""
    out = []
    for user_id, status in live_status_cache.items():
        if not (status.get("tiktok_live") or status.get("kick_live")):
            continue
        links = live_data["users"].get(user_id, {})
        out.append({
            "discord_id": user_id,
            "tiktok_username": links.get("tiktok"),
            "tiktok_url": status.get("tiktok_url") if status.get("tiktok_live") else None,
            "kick_username": links.get("kick"),
            "kick_url": status.get("kick_url") if status.get("kick_live") else None,
            "kick_title": status.get("kick_title")
        })
    return out


async def check_tiktok_live(username: str) -> tuple[bool, str | None]:
    """Best-effort TikTok live check via TikTokLive."""
    if not TIKTOK_AVAILABLE:
        return False, None
    try:
        client = TikTokLiveClient(unique_id=f"@{username}")
        is_live = await client.is_live()
        return is_live, (f"https://www.tiktok.com/@{username}/live" if is_live else None)
    except Exception as e:
        logger.warning(f"TikTok live check failed for @{username}: {e}")
        return False, None


async def check_kick_live(username: str) -> tuple[bool, str | None, str | None]:
    """Best-effort Kick live check via Kick's undocumented public channel API."""
    url = f"https://kick.com/api/v2/channels/{username}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MelophilesBot/1.0)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return False, None, None
                data = await resp.json()
                stream = data.get("livestream")
                if stream:
                    return True, f"https://kick.com/{username}", stream.get("session_title")
                return False, None, None
    except Exception as e:
        logger.warning(f"Kick live check failed for {username}: {e}")
        return False, None, None


async def announce_live(guild: discord.Guild, member: discord.Member, platform: str,
                         username: str, url: str | None, title: str | None = None):
    channel_id = live_data["guild_channels"].get(str(guild.id))
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        return

    color = discord.Color.from_rgb(254, 44, 85) if platform == "TikTok" else discord.Color.from_rgb(83, 252, 24)
    embed = discord.Embed(
        title=f"🔴 {member.display_name} is now LIVE on {platform}!",
        description=title or f"Come watch {member.display_name} on {platform}!",
        url=url,
        color=color
    )
    embed.add_field(name="Channel", value=f"[{username}]({url})" if url else username, inline=False)
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    role_id = live_data["guild_roles"].get(str(guild.id))
    mention = f"<@&{role_id}>" if role_id else "@here"

    try:
        await channel.send(content=f"{mention} {member.mention} just went live on {platform}!", embed=embed)
    except Exception as e:
        logger.error(f"Failed to send live announcement: {e}")


@tasks.loop(seconds=LIVE_CHECK_INTERVAL_SECONDS)
async def check_live_streams():
    for user_id, links in list(live_data["users"].items()):
        tiktok_username = links.get("tiktok")
        kick_username = links.get("kick")
        if not tiktok_username and not kick_username:
            continue

        member_guilds = [g for g in bot.guilds if g.get_member(int(user_id))]
        if not member_guilds:
            continue

        prev = live_status_cache.get(user_id, {
            "tiktok_live": False, "tiktok_url": None,
            "kick_live": False, "kick_url": None, "kick_title": None
        })
        new_status = dict(prev)

        if tiktok_username:
            is_live, url = await check_tiktok_live(tiktok_username)
            new_status["tiktok_live"] = is_live
            new_status["tiktok_url"] = url
            if is_live and not prev.get("tiktok_live"):
                for g in member_guilds:
                    await announce_live(g, g.get_member(int(user_id)), "TikTok", tiktok_username, url)

        if kick_username:
            is_live, url, title = await check_kick_live(kick_username)
            new_status["kick_live"] = is_live
            new_status["kick_url"] = url
            new_status["kick_title"] = title
            if is_live and not prev.get("kick_live"):
                for g in member_guilds:
                    await announce_live(g, g.get_member(int(user_id)), "Kick", kick_username, url, title)

        live_status_cache[user_id] = new_status
        await asyncio.sleep(1)


@bot.command(name="linktiktok")
async def link_tiktok(ctx, username: str):
    """Link your TikTok username so the bot can announce when you go live."""
    if not TIKTOK_AVAILABLE:
        await ctx.send("❌ TikTok tracking isn't set up on this bot yet (missing dependency).")
        return
    username = username.lstrip('@')
    user_id = str(ctx.author.id)
    live_data["users"].setdefault(user_id, {"tiktok": None, "kick": None})
    live_data["users"][user_id]["tiktok"] = username
    save_live_data()
    await ctx.send(f"✅ Linked TikTok **@{username}**. You'll be announced here when you go live.")


@bot.command(name="unlinktiktok")
async def unlink_tiktok(ctx):
    user_id = str(ctx.author.id)
    if user_id in live_data["users"]:
        live_data["users"][user_id]["tiktok"] = None
        save_live_data()
    live_status_cache.pop(user_id, None)
    await ctx.send("✅ TikTok unlinked.")


@bot.command(name="linkkick")
async def link_kick(ctx, username: str):
    """Link your Kick username so the bot can announce when you go live."""
    user_id = str(ctx.author.id)
    live_data["users"].setdefault(user_id, {"tiktok": None, "kick": None})
    live_data["users"][user_id]["kick"] = username
    save_live_data()
    await ctx.send(f"✅ Linked Kick **{username}**. You'll be announced here when you go live.")


@bot.command(name="unlinkkick")
async def unlink_kick(ctx):
    user_id = str(ctx.author.id)
    if user_id in live_data["users"]:
        live_data["users"][user_id]["kick"] = None
        save_live_data()
    await ctx.send("✅ Kick unlinked.")


@bot.command(name="mylive")
async def my_live(ctx):
    """Show your linked accounts and current live status."""
    user_id = str(ctx.author.id)
    links = live_data["users"].get(user_id, {"tiktok": None, "kick": None})
    status = live_status_cache.get(user_id, {})

    embed = discord.Embed(title="🔴 Your Live Links", color=discord.Color.red())
    embed.add_field(
        name="TikTok",
        value=(f"@{links['tiktok']} {'🔴 LIVE' if status.get('tiktok_live') else '⚫ offline'}"
               if links.get('tiktok') else "Not linked — `!linktiktok <username>`"),
        inline=False
    )
    embed.add_field(
        name="Kick",
        value=(f"{links['kick']} {'🔴 LIVE' if status.get('kick_live') else '⚫ offline'}"
               if links.get('kick') else "Not linked — `!linkkick <username>`"),
        inline=False
    )
    await ctx.send(embed=embed)


@bot.command(name="live", aliases=["livenow", "whoslive"])
async def live_now(ctx):
    """Show all users currently live on TikTok or Kick."""
    if not live_status_cache:
        await ctx.send("📭 No live data yet — the bot is still checking. Try again in a minute.")
        return

    entries = []
    for user_id, status in live_status_cache.items():
        if not (status.get("tiktok_live") or status.get("kick_live")):
            continue
        member = ctx.guild.get_member(int(user_id)) or bot.get_user(int(user_id))
        if not member:
            continue
        entries.append((member, status))

    if not entries:
        await ctx.send("😴 Nobody is live right now.")
        return

    embed = discord.Embed(
        title=f"🔴 Live Now ({len(entries)})",
        color=discord.Color.red(),
        timestamp=datetime.now()
    )
    for member, status in entries[:25]:
        lines = []
        if status.get("tiktok_live"):
            url = status.get("tiktok_url") or "https://www.tiktok.com"
            lines.append(f"📱 [TikTok]({url})")
        if status.get("kick_live"):
            url = status.get("kick_url") or "https://kick.com"
            title = status.get("kick_title")
            lines.append(f"🎮 [Kick]({url})" + (f" — *{title}*" if title else ""))
        embed.add_field(
            name=member.display_name,
            value="\n".join(lines) or "Live",
            inline=False
        )
    embed.set_footer(text="Use !linktiktok / !linkkick to be listed here")
    await ctx.send(embed=embed)


@bot.command(name="livelinks")
async def live_links(ctx, member: discord.Member = None):
    """Show a user's linked TikTok/Kick accounts and live status."""
    member = member or ctx.author
    user_id = str(member.id)
    links = live_data["users"].get(user_id, {"tiktok": None, "kick": None})
    status = live_status_cache.get(user_id, {})

    embed = discord.Embed(
        title=f"🔗 {member.display_name}'s Live Links",
        color=discord.Color.purple()
    )
    if links.get("tiktok"):
        state = "🔴 LIVE" if status.get("tiktok_live") else "⚫ offline"
        url = status.get("tiktok_url") or f"https://www.tiktok.com/@{links['tiktok']}"
        embed.add_field(
            name="TikTok",
            value=f"[@{links['tiktok']}]({url}) — {state}",
            inline=False
        )
    else:
        embed.add_field(name="TikTok", value="Not linked", inline=False)

    if links.get("kick"):
        state = "🔴 LIVE" if status.get("kick_live") else "⚫ offline"
        url = status.get("kick_url") or f"https://kick.com/{links['kick']}"
        embed.add_field(
            name="Kick",
            value=f"[{links['kick']}]({url}) — {state}",
            inline=False
        )
    else:
        embed.add_field(name="Kick", value="Not linked", inline=False)

    await ctx.send(embed=embed)


@bot.command(name="setlivechannel")
@commands.has_permissions(administrator=True)
async def set_live_channel(ctx, channel: discord.TextChannel):
    """Admin: set the text channel where live announcements are posted."""
    live_data["guild_channels"][str(ctx.guild.id)] = channel.id
    save_live_data()
    await ctx.send(f"✅ Live notifications will be posted in {channel.mention}")


@bot.command(name="setliverole")
@commands.has_permissions(administrator=True)
async def set_live_role(ctx, role: discord.Role = None):
    """Admin: set a role to ping on live announcements."""
    if role:
        live_data["guild_roles"][str(ctx.guild.id)] = role.id
        save_live_data()
        await ctx.send(f"✅ Live announcements will ping {role.mention}")
    else:
        live_data["guild_roles"].pop(str(ctx.guild.id), None)
        save_live_data()
        await ctx.send("✅ Live announcements will ping @here instead of a role.")


@bot.command(name="livechannel")
async def live_channel_info(ctx):
    channel_id = live_data["guild_channels"].get(str(ctx.guild.id))
    if channel_id:
        channel = ctx.guild.get_channel(channel_id)
        await ctx.send(f"📢 Live notifications are posted in {channel.mention if channel else '`unknown channel`'}")
    else:
        await ctx.send("❌ No live notification channel set. Admins: use `!setlivechannel #channel`")


@bot.command(name="checklive")
@commands.has_permissions(administrator=True)
async def force_check_live(ctx):
    """Admin: force an immediate live-status check."""
    await ctx.send("🔄 Checking live status for all linked accounts...")
    await check_live_streams()
    await ctx.send("✅ Done!")


# ==================== PRESENCE FUNCTIONS ====================

async def update_member_presence(member):
    try:
        status_map = {
            discord.Status.online: "online",
            discord.Status.idle: "idle",
            discord.Status.dnd: "dnd",
            discord.Status.offline: "offline"
        }
        status = status_map.get(member.status, "offline")

        activities = []
        custom_status = None

        for activity in member.activities:
            if activity.type == discord.ActivityType.custom:
                custom_status = {
                    "state": activity.state,
                    "emoji": str(activity.emoji) if activity.emoji else None
                }
            elif activity.type == discord.ActivityType.playing:
                activities.append({
                    "type": "game",
                    "name": activity.name,
                    "details": getattr(activity, "details", None),
                    "state": getattr(activity, "state", None)
                })
            elif activity.type == discord.ActivityType.listening:
                if activity.name == "Spotify":
                    activities.append({
                        "type": "spotify",
                        "song": getattr(activity, "title", "Unknown"),
                        "artist": getattr(activity, "artist", "Unknown"),
                        "album": getattr(activity, "album", "Unknown")
                    })
                else:
                    activities.append({"type": "listening", "name": activity.name})
            elif activity.type == discord.ActivityType.watching:
                activities.append({"type": "watching", "name": activity.name})
            elif activity.type == discord.ActivityType.streaming:
                activities.append({
                    "type": "streaming",
                    "name": activity.name,
                    "url": getattr(activity, "url", None)
                })

        decoration = await get_avatar_decoration(member.id)
        live_status = live_status_cache.get(str(member.id), {})

        payload = {
            "discord_id": str(member.id),
            "username": member.name,
            "global_name": member.global_name,
            "avatar": str(member.avatar.url) if member.avatar else None,
            "avatar_decoration": decoration,  # full CDN URL (.png) or None
            "status": status,
            "custom_status": custom_status,
            "activities": activities,
            "tiktok_live": live_status.get("tiktok_live", False),
            "tiktok_live_url": live_status.get("tiktok_url"),
            "kick_live": live_status.get("kick_live", False),
            "kick_live_url": live_status.get("kick_url"),
            "kick_live_title": live_status.get("kick_title"),
            "live_now": get_all_live_users(),
            "guilds": [g.id for g in bot.guilds if g.get_member(member.id)],
            "last_updated": datetime.now().isoformat()
        }

        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": API_SECRET, "Content-Type": "application/json"}
            async with session.post(API_ENDPOINT, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    logger.info(f"✅ Updated {member.name}: {status} (deco={'yes' if decoration else 'no'})")
                else:
                    logger.warning(f"⚠️ API returned {resp.status} for {member.name}")
    except Exception as e:
        logger.error(f"❌ Error updating {member.name}: {e}")


# ==================== CONTINUOUS MEMBER SYNC TASK ====================

@tasks.loop(minutes=5)
async def sync_members():
    """Continuously sync all members' presence across every guild."""
    total = 0
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            try:
                await update_member_presence(member)
                total += 1
            except Exception as e:
                logger.error(f"❌ Error syncing {member.name}: {e}")
            await asyncio.sleep(0.1)
    logger.info(f"✅ Sync complete! Synced {total} members across {len(bot.guilds)} guild(s)")


# ==================== BOT EVENTS ====================

@bot.event
async def on_ready():
    logger.info(f"✅ {bot.user} is online! (Bot ID: {bot.user.id})")
    logger.info(f"📋 Serving {len(bot.guilds)} guild(s)")

    await connect_lavalink()

    logger.info("🔄 Running initial member sync...")
    for guild in bot.guilds:
        for member in guild.members:
            if not member.bot:
                await update_member_presence(member)
                await asyncio.sleep(0.1)
    logger.info("✅ Initial sync complete!")

    if not sync_members.is_running():
        sync_members.start()
        logger.info("✅ Continuous member sync started (every 5 minutes)")

    if not check_live_streams.is_running():
        check_live_streams.start()
        logger.info(f"✅ TikTok/Kick live check started (every {LIVE_CHECK_INTERVAL_SECONDS}s)")


@bot.event
async def on_presence_update(before, after):
    if not after.bot:
        logger.info(f"🔄 Real-time presence update for {after.name}")
        await update_member_presence(after)


@bot.event
async def on_member_update(before, after):
    if not after.bot:
        if before.status != after.status or before.activities != after.activities:
            logger.info(f"🔄 Member update for {after.name}")
            await update_member_presence(after)


# ==================== BASIC COMMANDS ====================

@bot.command(name="ping")
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latency: {latency}ms")


@bot.command(name="stats")
async def stats(ctx):
    guild = ctx.guild
    total_members = len([m for m in guild.members if not m.bot])
    online = len([m for m in guild.members if m.status != discord.Status.offline and not m.bot])
    voice_members = len([m for m in guild.members if m.voice and m.voice.channel])
    live_count = sum(1 for s in live_status_cache.values()
                     if s.get("tiktok_live") or s.get("kick_live"))

    embed = discord.Embed(title="📊 Bot Statistics", color=discord.Color.blue())
    embed.add_field(name="👥 Tracked Members", value=str(total_members), inline=True)
    embed.add_field(name="🟢 Online Now", value=str(online), inline=True)
    embed.add_field(name="🎙️ In Voice", value=str(voice_members), inline=True)
    embed.add_field(name="🌐 Servers", value=str(len(bot.guilds)), inline=True)
    total_queued = sum(len(p.queue) for p in wavelink.Pool.get_node().players.values()) if wavelink.Pool.nodes else 0
    embed.add_field(name="🎵 Total Queued", value=str(total_queued), inline=True)
    embed.add_field(name="🔴 Currently Live", value=str(live_count), inline=True)
    embed.set_footer(text="Made with ❤️")
    await ctx.send(embed=embed)


@bot.command(name="syncnow")
@commands.has_permissions(administrator=True)
async def sync_now(ctx):
    """Force a manual member sync (Admin only)"""
    await ctx.send("🔄 Forcing manual sync...")
    await sync_members()
    await ctx.send("✅ Manual sync complete!")


@bot.command(name="refreshdecorations")
@commands.has_permissions(administrator=True)
async def refresh_decorations(ctx):
    """Clear decoration cache and re-fetch for all members (Admin only)."""
    await ctx.send("🔄 Clearing decoration cache and re-fetching...")
    clear_decoration_cache()

    count = 0
    for member in ctx.guild.members:
        if not member.bot:
            deco = await get_avatar_decoration(member.id)
            if deco:
                count += 1
            await asyncio.sleep(0.05)

    await ctx.send(f"✅ Refreshed decorations for {count} member(s). Running full sync...")
    await sync_members()
    await ctx.send("✅ Done!")


@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="🎵 Music Bot Commands",
        description="Here are all available commands:",
        color=discord.Color.blue()
    )
    commands_list = {
        "!play / !p": "Play a song (YouTube, Spotify, SoundCloud, or search)",
        "!skip": "Skip the current song",
        "!stop": "Stop playback and clear queue",
        "!pause": "Pause the current song",
        "!resume": "Resume the paused song",
        "!queue / !q": "Show the music queue",
        "!loop": "Toggle loop for current song",
        "!nowplaying / !np": "Show currently playing song",
        "!clearqueue / !cq": "Clear the music queue",
        "!remove": "Remove a song from queue by position",
        "!shuffle": "Shuffle the music queue",
        "!leave": "Bot leaves the voice channel",
        "!ping": "Check bot latency",
        "!stats": "Show bot statistics",
        "!syncnow": "Force manual member sync (Admin only)",
        "!refreshdecorations": "Refresh avatar decoration cache (Admin only)",
        "!help": "Show this help message"
    }
    live_commands_list = {
        "!linktiktok <username>": "Link your TikTok — get announced when you go live",
        "!linkkick <username>": "Link your Kick — get announced when you go live",
        "!unlinktiktok / !unlinkkick": "Remove a linked account",
        "!mylive": "Show your linked accounts and live status",
        "!live / !livenow": "Show everyone currently live 🔴",
        "!livelinks [@user]": "Show a user's linked accounts",
        "!setlivechannel #channel": "Admin: set where live announcements post",
        "!setliverole @role": "Admin: role to ping on live announcements",
        "!livechannel": "Show the current live announcement channel",
        "!checklive": "Admin: force an immediate live check"
    }
    text = ""
    for cmd, desc in commands_list.items():
        text += f"**{cmd}** - {desc}\n"
    embed.add_field(name="📋 Commands", value=text, inline=False)

    live_text = ""
    for cmd, desc in live_commands_list.items():
        live_text += f"**{cmd}** - {desc}\n"
    embed.add_field(name="🔴 TikTok / Kick Live", value=live_text, inline=False)

    embed.set_footer(text="🎶 Enjoy the music! | Auto-sync every 5 minutes")
    await ctx.send(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(f"❌ You don't have permission to use this command!")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument: {error}")
    else:
        logger.error(f"Command error: {error}")
        try:
            await ctx.send(f"❌ An error occurred: {str(error)[:1500]}")
        except Exception:
            pass


# ==================== RUN THE BOT ====================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN not set!")
        exit(1)
    if not LAVALINK_HOST or not LAVALINK_PASSWORD:
        print("⚠️ WARNING: LAVALINK_HOST / LAVALINK_PASSWORD not set — music commands will fail!")

    print("=" * 50)
    print("🚀 Starting bot (Lavalink mode)...")
    print("📡 Member tracking: Enabled (auto-sync every 5 minutes, all guilds)")
    print("✨ Avatar decorations: ENABLED (always .png, 12h cache)")
    print(f"🔴 TikTok/Kick live tracking: Enabled (every {LIVE_CHECK_INTERVAL_SECONDS}s)")
    print("🎵 Spotify: handled natively by LavaSrc via Lavalink")
    print("=" * 50)
    bot.run(TOKEN, reconnect=True)
