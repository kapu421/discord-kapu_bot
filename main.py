import os
import logging
import sys
import io
import aiohttp

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from keep_alive import keep_alive

# ログ設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
VOICEVOX_URL = os.getenv("VOICEVOX_URL")  # Renderに設定したngrokのURL

if not BOT_TOKEN:
    logger.error("BOT_TOKEN が .env に設定されていません。")
    sys.exit("BOT_TOKEN is required in .env")

# インテントの設定（VC状態とメッセージコンテンツを取得）
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

# --- プロキシ設定 (Webshare等のHTTP/HTTPSプロキシ用) ---
USE_PROXY = os.getenv("USE_PROXY", "false").lower() in ("true", "1", "yes")
PROXY_URL = os.getenv("PROXY_URL")

proxy_to_use = PROXY_URL if (USE_PROXY and PROXY_URL) else None

if USE_PROXY:
    if PROXY_URL:
        logger.info("HTTP/HTTPSプロキシ経由でDiscordに接続します: %s", PROXY_URL)
    else:
        logger.warning("USE_PROXY=true ですが PROXY_URL が設定されていないため、プロキシなしで起動します。")
else:
    logger.info("プロキシなしでDiscordに接続します。")

class MyBot(commands.Bot):
    pass

bot = MyBot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    proxy=proxy_to_use,  # Webshareプロキシを適用
)


# --- VOICEVOX 音声生成処理 ---

async def generate_voice(text: str) -> io.BytesIO | None:
    if not VOICEVOX_URL:
        logger.error("VOICEVOX_URL が設定されていません。")
        return None

    # ngrokの初回警告ページを回避するヘッダーを設定
    headers = {"ngrok-skip-browser-warning": "true"}

    try:
        async with aiohttp.ClientSession() as session:
            # 1. 音声合成用のクエリを作成 (3は「ずんだもん (ノーマル)」)
            async with session.post(
                f"{VOICEVOX_URL}/audio_query",
                params={"text": text, "speaker": 3},
                headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.error("VOICEVOX Audio Query Failed: %s", resp.status)
                    return None
                query_data = await resp.json()

            # 2. 音声データを生成
            async with session.post(
                f"{VOICEVOX_URL}/synthesis",
                params={"speaker": 3},
                json=query_data,
                headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.error("VOICEVOX Synthesis Failed: %s", resp.status)
                    return None
                voice_bytes = await resp.read()
                return io.BytesIO(voice_bytes)
    except Exception as e:
        logger.exception("VOICEVOX との通信に失敗しました: %s", e)
        return None


@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
        logger.info("App commands synced.")
    except Exception as e:
        logger.exception("Failed to sync app commands: %s", e)


# --- メッセージ受信・自動読み上げ処理（確認用ログ追加版） ---

@bot.event
async def on_message(message: discord.Message):
    # Bot自身の発言やコマンド(!で始まるもの)は読み上げない
    if message.author.bot or message.content.startswith("!"):
        return

    logger.info(f"メッセージを受信: {message.content}")

    # サーバー内のVC状態を確認
    voice_client = message.guild.voice_client if message.guild else None

    # BotがVCに参加しているか確認
    if voice_client and voice_client.is_connected():
        logger.info(f"BotはVCに接続中: {voice_client.channel.name}")

        # メッセージ送信者がBotと同じVCに入っているかチェック
        if message.author.voice and message.author.voice.channel.id == voice_client.channel.id:
            logger.info("ユーザーも同じVCにいるため、音声生成を開始します")
            audio_stream = await generate_voice(message.content)

            if audio_stream:
                logger.info("音声データの生成に成功しました。再生を試みます。")
                try:
                    # FFmpegを使ってDiscordで音声再生
                    source = discord.FFmpegPCMAudio(audio_stream, pipe=True)
                    if not voice_client.is_playing():
                        voice_client.play(source)
                        logger.info("再生を開始しました")
                    else:
                        logger.info("現在別の音声を再生中のためスキップしました")
                except Exception as e:
                    logger.exception("音声再生エラー: %s", e)
            else:
                logger.error("音声データの生成（VOICEVOX通信）に失敗しました")
        else:
            logger.info("ユーザーがBotと同じVCにいないため読み上げません")
    else:
        logger.info("BotがVCに参加していないため読み上げません")

    # コマンドの実行処理を継続
    await bot.process_commands(message)


# --- 自動切断処理（メンバーが全員いなくなったら切断） ---

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # 状態が変わったチャンネルを取得
    voice_state = before if before.channel else after
    if not voice_state or not voice_state.channel:
        return

    channel = voice_state.channel
    guild = channel.guild
    voice_client = guild.voice_client

    # BotがそのVCに参加しているか確認
    if voice_client and voice_client.channel.id == channel.id:
        # Bot以外の人間（BotフラグがFalseのメンバー）をカウント
        human_members = [m for m in channel.members if not m.bot]

        # 人間が0人になったら切断
        if len(human_members) == 0:
            await voice_client.disconnect()
            logger.info(f"チャンネル『{channel.name}』から全員が退出したため、Botが自動切断しました。")


# --- VC参加・切断の共通処理 ---

async def join_vc_logic(member: discord.Member, guild: discord.Guild) -> str:
    if not member.voice or not member.voice.channel:
        return "先にボイスチャンネルに入ってからコマンドを実行してください。"

    target_channel = member.voice.channel
    voice_client = guild.voice_client

    if voice_client:
        if voice_client.channel.id == target_channel.id:
            return f"すでに『{target_channel.name}』に参加しています。"
        else:
            await voice_client.move_to(target_channel)
            return f"『{target_channel.name}』に移動しました！"
    else:
        await target_channel.connect()
        return f"『{target_channel.name}』に参加しました！"


async def leave_vc_logic(guild: discord.Guild) -> str:
    voice_client = guild.voice_client
    if voice_client:
        channel_name = voice_client.channel.name
        await voice_client.disconnect()
        return f"『{channel_name}』から切断しました。"
    else:
        return "ボットはどのボイスチャンネルにも参加していません。"


# --- テキストコマンド (!join / !leave) ---

@bot.command(name="join")
async def join_command(ctx: commands.Context):
    if not ctx.guild or not isinstance(ctx.author, discord.Member):
        return
    res = await join_vc_logic(ctx.author, ctx.guild)
    await ctx.send(res)


@bot.command(name="leave")
async def leave_command(ctx: commands.Context):
    if not ctx.guild:
        return
    res = await leave_vc_logic(ctx.guild)
    await ctx.send(res)


# --- スラッシュコマンド (/join / /leave) ---

@bot.tree.command(name="join", description="実行者が参加しているボイスチャンネルに参加します")
async def join_slash(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
        return
    res = await join_vc_logic(interaction.user, interaction.guild)
    await interaction.response.send_message(res)


@bot.tree.command(name="leave", description="ボイスチャンネルから切断します")
async def leave_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
        return
    res = await leave_vc_logic(interaction.guild)
    await interaction.response.send_message(res)


if __name__ == "__main__":
    try:
        keep_alive()  # Renderの休眠対策（Flaskサーバー起動）
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.exception("Bot の実行に失敗しました: %s", e)
