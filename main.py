import os
import logging
import sys
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from keep_alive import keep_alive

# 初期設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID_STR = os.getenv("CHANNEL_ID")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN が .env に設定されていません。")
    sys.exit("BOT_TOKEN is required in .env")

if not CHANNEL_ID_STR:
    logger.error("CHANNEL_ID が .env に設定されていません。")
    sys.exit("CHANNEL_ID is required in .env")

try:
    CHANNEL_ID = int(CHANNEL_ID_STR)
except ValueError:
    logger.error("CHANNEL_ID は整数である必要があります。")
    sys.exit("CHANNEL_ID must be an integer in .env")


# NGワード
NG_WORDS = ["死ね"]

DEVELOPER_USER_ID = 944085652444700702

# DoS対策：ユーザーごとの送信クールダウン
ANONYMOUS_MSG_COOLDOWN_SECONDS = 5
_last_sent_at: dict[int, float] = {}

intents = discord.Intents.default()
intents.message_content = True  # DMで送られてきたメッセージ本文、添付ファイルを匿名転送するために必要

#Webshare等のHTTP/HTTPSプロキシ用
USE_PROXY = os.getenv("USE_PROXY", "false").lower() in ("true", "1", "yes")
PROXY_URL = os.getenv("PROXY_URL")


class MyBot(commands.Bot):
    pass


# プロキシを使用する場合は commands.Bot の proxy 引数に
proxy_to_use = PROXY_URL if (USE_PROXY and PROXY_URL) else None

if USE_PROXY:
    if PROXY_URL:
        logger.info("HTTP/HTTPSプロキシ経由でDiscordに接続します: %s", PROXY_URL)
    else:
        logger.warning("USE_PROXY=true ですが PROXY_URL が設定されていないため、プロキシなしで起動します。")
else:
    logger.info("プロキシなしでDiscordに接続します。")

bot = MyBot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    proxy=proxy_to_use,
)

# 共通ユーティリティ

def contains_ng_word(text: str) -> bool:
    for w in NG_WORDS:
        if w in text:
            return True
    return False


def quoted_block(text: str) -> str:
    lines = text.splitlines() or [text]
    return "\n".join(["> " + line for line in lines])


async def send_anonymous_message(interaction: discord.Interaction, message: str):
    user_id = interaction.user.id
    now = time.monotonic()
    last = _last_sent_at.get(user_id)
    if last is not None:
        elapsed = now - last
        if elapsed < ANONYMOUS_MSG_COOLDOWN_SECONDS:
            remaining = int(ANONYMOUS_MSG_COOLDOWN_SECONDS - elapsed) + 1
            await interaction.response.send_message(
                f"送信間隔が短すぎます。あと {remaining} 秒待ってから送信してください。",
                ephemeral=True,
            )
            return
    _last_sent_at[user_id] = now

    if contains_ng_word(message):
        await interaction.response.send_message("不適切な言葉が含まれています", ephemeral=True)
        return

    if len(message) > 1900:
        await interaction.response.send_message("メッセージが長すぎます（2000文字以内にしてください）", ephemeral=True)
        return

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except Exception as e:
            logger.exception("転送先チャンネルの取得に失敗: %s", e)
            await interaction.response.send_message("送信に失敗しました（チャンネルが見つかりません）。管理者に連絡してください。", ephemeral=True)
            return

    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.PartialMessageable, discord.abc.Messageable)):
        await interaction.response.send_message("送信先チャンネルのタイプが不正です。管理者に連絡してください。", ephemeral=True)
        return

    content = "📩 **匿名メッセージが届きました**\n" + quoted_block(message)

    try:
        await channel.send(content)
    except discord.Forbidden:
        logger.exception("Bot に送信権限がありません。")
        await interaction.response.send_message("ボットにチャンネルへの送信権限がありません。管理者に連絡してください。", ephemeral=True)
        return
    except Exception as e:
        logger.exception("メッセージ送信中にエラーが発生しました: %s", e)
        await interaction.response.send_message("送信中にエラーが発生しました。あとでもう一度試してください。", ephemeral=True)
        return

    guild_name = interaction.guild.name if interaction.guild else "DM/不明"
    guild_id = interaction.guild.id if interaction.guild else "不明"
    await log_sender_for_moderation(interaction.user, guild_name, guild_id, message)

    await interaction.response.send_message("送信しました！", ephemeral=True)


async def log_sender_for_moderation(user: discord.abc.User, guild_name: str, guild_id, content: str):
    logger.info(
        "匿名メッセージ送信: user=%s (ID: %s) guild=%s (ID: %s) content=%s",
        user, user.id, guild_name, guild_id, content,
    )

    try:
        developer = bot.get_user(DEVELOPER_USER_ID) or await bot.fetch_user(DEVELOPER_USER_ID)
        dm_content = (
            "secret_mes_log\n"
            f"from: {user} (ID: {user.id})\n"
            f"server: {guild_name} (ID: {guild_id})\n"
            f"info:\n{quoted_block(content) if content else '（本文なし・添付ファイルのみ）'}"
        )
        if len(dm_content) <= 2000:
            await developer.send(dm_content)
        else:
            await developer.send(dm_content[:2000])
            await developer.send(dm_content[2000:4000])
    except Exception as e:
        logger.exception("開発者への送信者ログDM送信に失敗しました: %s", e)


# モーダル

class AnonymousMessageModal(discord.ui.Modal, title="匿名メッセージを送る"):
    message_input = discord.ui.TextInput(
        label="メッセージ内容",
        style=discord.TextStyle.paragraph,
        placeholder="ここに送りたい内容を入力してください（1900文字以内）",
        max_length=1900,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await send_anonymous_message(interaction, str(self.message_input.value))
        except Exception as e:
            logger.exception("モーダル送信処理中にエラーが発生しました: %s", e)
            try:
                await interaction.response.send_message("エラーが発生しました。管理者に連絡してください。", ephemeral=True)
            except Exception:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("モーダルでエラーが発生しました: %s", error)
        try:
            await interaction.response.send_message("エラーが発生しました。管理者に連絡してください。", ephemeral=True)
        except Exception:
            pass


# ボタン

class AnonymousMessageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="匿名メッセージを送る",
        style=discord.ButtonStyle.primary,
        emoji="✉️",
        custom_id="anonymous_message_button",
    )
    async def send_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AnonymousMessageModal())


# VC募集機能

RECRUIT_TARGET_CHANNEL_ID = 1535644078660780153

MENTION_ROLE_CHOICES = [
    ("なし", None),
    ("ロールを選択1", 1533825506124763177),
    ("ロールを選択2", 1533824492810272889),
    ("ロールを選択3", 1533824191839735828),
    ("ロールを選択4", 1481108235673927730),
    ("ロールを選択5", 1533823603672613006),
]

CONTENT_CHOICES = ["雑談", "ゲーム", "作業・勉強", "その他"]

VC_CHANNEL_IDS = [
    1430828208277946432,
    1430828208277946433,
    1529408955594440704,
]

active_recruitments: dict[int, dict] = {}


class RoleSelect(discord.ui.Select):
    def __init__(self, guild: Optional[discord.Guild]):
        options = []
        for label, role_id in MENTION_ROLE_CHOICES:
            if role_id is None:
                options.append(discord.SelectOption(label="なし", value="none"))
                continue
            role = guild.get_role(role_id) if guild else None
            display_label = role.name if role else f"ロール ({role_id})"
            options.append(discord.SelectOption(label=display_label[:100], value=str(role_id)))

        super().__init__(
            placeholder="メンションするロールを選択（任意）",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view: VCRecruitView = self.view  # type: ignore
        value = self.values[0]
        view.role_id = None if value == "none" else int(value)
        await view.update_message(interaction)


class ContentSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=c, value=c) for c in CONTENT_CHOICES]
        super().__init__(
            placeholder="やっている内容を選択",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view: VCRecruitView = self.view  # type: ignore
        view.content = self.values[0]
        await view.update_message(interaction)


class VCSelect(discord.ui.Select):
    def __init__(self, guild: Optional[discord.Guild]):
        options = []
        for ch_id in VC_CHANNEL_IDS:
            channel = guild.get_channel(ch_id) if guild else None
            label = channel.name if channel else f"VCチャンネル ({ch_id})"
            options.append(discord.SelectOption(label=label[:100], value=str(ch_id)))

        super().__init__(
            placeholder="使用するVCチャンネルを選択",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view: VCRecruitView = self.view  # type: ignore
        view.vc_id = int(self.values[0])
        await view.update_message(interaction)


class VCExtraModal(discord.ui.Modal, title="募集の一言（任意）"):
    comment = discord.ui.TextInput(
        label="一言（任意）",
        style=discord.TextStyle.short,
        placeholder="例: 途中参加OK！",
        required=False,
        max_length=200,
    )

    def __init__(self, recruit_view: "VCRecruitView"):
        super().__init__()
        self.recruit_view = recruit_view

    async def on_submit(self, interaction: discord.Interaction):
        view = self.recruit_view
        comment_text = str(self.comment.value or "").strip()

        role_mention = f"<@&{view.role_id}>" if view.role_id else None
        vc_mention = f"<#{view.vc_id}>"

        embed = discord.Embed(
            title="📢 VC募集",
            color=discord.Color.green(),
        )
        embed.add_field(name="内容", value=view.content, inline=False)
        embed.add_field(name="場所", value=vc_mention, inline=False)
        if comment_text:
            embed.add_field(name="一言", value=comment_text, inline=False)
        embed.set_footer(text=f"募集者: {interaction.user.display_name}")

        target_channel = bot.get_channel(RECRUIT_TARGET_CHANNEL_ID)
        if target_channel is None:
            try:
                target_channel = await bot.fetch_channel(RECRUIT_TARGET_CHANNEL_ID)
            except Exception as e:
                logger.exception("募集送信先チャンネルの取得に失敗: %s", e)
                await interaction.response.send_message(
                    "送信先チャンネルが見つかりませんでした。管理者に連絡してください。", ephemeral=True
                )
                return

        try:
            sent_message = await target_channel.send(
                content=role_mention if role_mention else None,
                embed=embed,
            )
        except discord.Forbidden:
            logger.exception("募集メッセージ送信権限がありません。")
            await interaction.response.send_message(
                "募集メッセージを送信する権限がありません。管理者に連絡してください。", ephemeral=True
            )
            return
        except Exception as e:
            logger.exception("募集メッセージ送信中にエラーが発生しました: %s", e)
            await interaction.response.send_message("送信中にエラーが発生しました。", ephemeral=True)
            return

        active_recruitments[view.vc_id] = {
            "channel_id": target_channel.id,
            "message_id": sent_message.id,
        }

        await interaction.response.send_message("募集を送信しました！", ephemeral=True)

        for item in view.children:
            item.disabled = True
        try:
            await view.origin_interaction.edit_original_response(
                content="✅ 募集を送信しました。", view=view
            )
        except Exception:
            logger.exception("元メッセージの更新に失敗しました。")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("VC募集モーダルでエラーが発生しました: %s", error)
        try:
            await interaction.response.send_message("エラーが発生しました。管理者に連絡してください。", ephemeral=True)
        except Exception:
            pass


class VCSendButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="募集を送信する", style=discord.ButtonStyle.success, emoji="📢")

    async def callback(self, interaction: discord.Interaction):
        view: VCRecruitView = self.view  # type: ignore
        if not view.content or not view.vc_id:
            await interaction.response.send_message(
                "「内容」と「VCチャンネル」は必須項目です。選択してから送信してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(VCExtraModal(view))


class VCRecruitView(discord.ui.View):
    def __init__(self, origin_interaction: discord.Interaction):
        super().__init__(timeout=600)
        self.origin_interaction = origin_interaction
        self.role_id: Optional[int] = None
        self.content: Optional[str] = None
        self.vc_id: Optional[int] = None

        guild = origin_interaction.guild

        self.add_item(RoleSelect(guild))
        self.add_item(ContentSelect())
        self.add_item(VCSelect(guild))
        self.add_item(VCSendButton())

    def status_text(self) -> str:
        role_text = f"<@&{self.role_id}>" if self.role_id else "なし"
        content_text = self.content or "未選択"
        vc_text = f"<#{self.vc_id}>" if self.vc_id else "未選択"
        return (
            "**VC募集の内容を選択してください**\n"
            f"・メンションロール: {role_text}\n"
            f"・内容: {content_text}\n"
            f"・VCチャンネル: {vc_text}\n\n"
            "選択が終わったら「募集を送信する」を押してください。\n"
            "（一言メッセージは送信ボタンを押した後に入力できます）"
        )

    async def update_message(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.status_text(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.origin_interaction.edit_original_response(
                content="⌛ 時間切れのため募集UIを終了しました。もう一度 /vc を実行してください。",
                view=self,
            )
        except Exception:
            pass


# イベント

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # DMの場合は匿名送信処理
    if isinstance(message.channel, discord.DMChannel):
        await handle_anonymous_dm(message)
        return

    # 通常のテキストチャンネルの場合はコマンド処理
    await bot.process_commands(message)


async def handle_anonymous_dm(message: discord.Message):
    user = message.author
    content = message.content or ""

    now = time.monotonic()
    last = _last_sent_at.get(user.id)
    if last is not None:
        elapsed = now - last
        if elapsed < ANONYMOUS_MSG_COOLDOWN_SECONDS:
            remaining = int(ANONYMOUS_MSG_COOLDOWN_SECONDS - elapsed) + 1
            try:
                await message.channel.send(f"送信間隔が短すぎます。あと {remaining} 秒待ってから送信してください。")
            except Exception:
                pass
            return
    _last_sent_at[user.id] = now

    if not content and not message.attachments:
        return

    if content and contains_ng_word(content):
        try:
            await message.channel.send("不適切な言葉が含まれています")
        except Exception:
            pass
        return

    if len(content) > 1900:
        try:
            await message.channel.send("メッセージが長すぎます（2000文字以内にしてください）")
        except Exception:
            pass
        return

    MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
    for a in message.attachments:
        if a.size and a.size > MAX_ATTACHMENT_BYTES:
            try:
                await message.channel.send(f"添付ファイル「{a.filename}」がサイズ上限を超えています。")
            except Exception:
                pass
            return

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except Exception as e:
            logger.exception("転送先チャンネルの取得に失敗: %s", e)
            try:
                await message.channel.send("送信に失敗しました。管理者に連絡してください。")
            except Exception:
                pass
            return

    files = []
    try:
        for a in message.attachments:
            files.append(await a.to_file())
    except Exception as e:
        logger.exception("添付ファイルの取得に失敗しました: %s", e)
        try:
            await message.channel.send("添付ファイルの転送に失敗しました。もう一度試してください。")
        except Exception:
            pass
        return

    if content:
        text_content = "📩 **匿名メッセージが届きました（DM経由）**\n" + quoted_block(content)
    else:
        text_content = "📩 **匿名メッセージが届きました（DM経由・添付ファイルのみ）**"

    try:
        await channel.send(content=text_content, files=files if files else None)
    except discord.Forbidden:
        logger.exception("Bot に送信権限がありません。")
        try:
            await message.channel.send("ボットにチャンネルへの送信権限がありません。管理者に連絡してください。")
        except Exception:
            pass
        return
    except Exception as e:
        logger.exception("DMメッセージ転送中にエラーが発生しました: %s", e)
        try:
            await message.channel.send("送信中にエラーが発生しました。あとでもう一度試してください。")
        except Exception:
            pass
        return

    await log_sender_for_moderation(user, "DM（サーバー外から送信）", "N/A", content)

    try:
        await message.channel.send("送信しました！")
    except Exception:
        pass


@bot.event
async def on_ready():
    try:
        bot.add_view(AnonymousMessageView())
        logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    except Exception as e:
        logger.exception("Failed in on_ready: %s", e)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    left_channel = before.channel
    if left_channel is None:
        return

    if after.channel is not None and after.channel.id == left_channel.id:
        return

    info = active_recruitments.get(left_channel.id)
    if info is None:
        return

    if len(left_channel.members) > 0:
        return

    active_recruitments.pop(left_channel.id, None)

    try:
        msg_channel = bot.get_channel(info["channel_id"])
        if msg_channel is None:
            msg_channel = await bot.fetch_channel(info["channel_id"])

        message = await msg_channel.fetch_message(info["message_id"])

        if message.embeds:
            embed = message.embeds[0]
            embed.title = "🔴【募集終了】VC募集"
            embed.color = discord.Color.dark_gray()
            embed.add_field(name="状態", value="VCが空になったため、この募集は終了しました。", inline=False)
            await message.edit(embed=embed)
    except Exception as e:
        logger.exception("募集終了メッセージの更新に失敗しました: %s", e)


# スラッシュコマンド

@bot.tree.command(name="secret-msg", description="匿名で管理者チャンネルにメッセージを送信します")
@app_commands.describe(message="送信したいメッセージ")
async def secret_msg(interaction: discord.Interaction, message: str):
    try:
        await send_anonymous_message(interaction, message)
    except Exception as e:
        logger.exception("予期しないエラー: %s", e)
        try:
            await interaction.response.send_message("エラーが発生しました。管理者に連絡してください。", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="setup-anonymous", description="【管理者用】このチャンネルに匿名メッセージ送信ボタンを設置します")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_anonymous(interaction: discord.Interaction):
    try:
        embed = discord.Embed(
            title="📮 匿名メッセージ受付",
            description=(
                "下のボタンを押すと入力フォームが開きます。\n"
                "画像やGIFを送りたい場合は、このBotに直接DMを送ってください（そのまま匿名で転送されます）。\n"
                "送信者情報は一切記録・表示されません。安心してご利用ください。"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.channel.send(embed=embed, view=AnonymousMessageView())
        await interaction.response.send_message("このチャンネルにボタンを設置しました。", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("ボットにこのチャンネルへの送信権限がありません。", ephemeral=True)
    except Exception as e:
        logger.exception("setup-anonymous 実行中にエラーが発生しました: %s", e)
        await interaction.response.send_message("エラーが発生しました。", ephemeral=True)


@setup_anonymous.error
async def setup_anonymous_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("このコマンドはサーバー管理権限を持つ人のみ実行できます。", ephemeral=True)
    else:
        logger.exception("setup-anonymous コマンドエラー: %s", error)
        try:
            await interaction.response.send_message("エラーが発生しました。", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="vc", description="VC募集を作成します（ロール・内容・VC・一言を選んで送信）")
async def vc_recruit(interaction: discord.Interaction):
    try:
        view = VCRecruitView(interaction)
        await interaction.response.send_message(view.status_text(), view=view, ephemeral=True)
    except Exception as e:
        logger.exception("/vc コマンド実行中にエラーが発生しました: %s", e)
        try:
            await interaction.response.send_message("エラーが発生しました。管理者に連絡してください。", ephemeral=True)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        keep_alive()
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.exception("Bot の実行に失敗しました: %s", e)
