import os
import logging
import sys
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

try:
    from aiohttp_socks import ProxyConnector
except ImportError:  # ローカル開発でプロキシを使わない場合は未インストールでも動くようにする
    ProxyConnector = None

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

# 荒らし対策：匿名メッセージ送信時に送信者情報をDMで通知する開発者のユーザーID
DEVELOPER_USER_ID = 944085652444700702

# DoS/連打対策：ユーザーごとの送信クールダウン（秒）
ANONYMOUS_MSG_COOLDOWN_SECONDS = 30
# key: user_id, value: 最後に送信した時刻（time.monotonic()）
_last_sent_at: dict[int, float] = {}

intents = discord.Intents.default()
intents.message_content = True  # DMで送られてきたメッセージ本文・添付ファイルを匿名転送するために必要

# --- warp-plus 経由のローカルSOCKS5プロキシ設定 ---
# Render等の無権限コンテナ環境では、同一コンテナ内でバックグラウンド起動した
# warp-plus (SOCKS5: 127.0.0.1:8086) を経由してDiscord APIへ接続する。
# USE_PROXY=false にすればプロキシなしの通常接続に戻せる（ローカル開発用）。
USE_PROXY = os.getenv("USE_PROXY", "true").lower() not in ("false", "0", "no")
SOCKS5_PROXY_URL = os.getenv("SOCKS5_PROXY_URL", "socks5://127.0.0.1:8086")

connector = None
if USE_PROXY:
    if ProxyConnector is None:
        logger.error("aiohttp-socks がインストールされていません。requirements.txt を確認してください。")
        sys.exit("aiohttp-socks is required when USE_PROXY=true")
    async def main():
    if SOCKS5_PROXY_URL:
        connector = ProxyConnector.from_url(SOCKS5_PROXY_URL)
    logger.info("SOCKS5プロキシ経由でDiscordに接続します: %s", SOCKS5_PROXY_URL)
else:
    logger.info("プロキシなしでDiscordに接続します。")

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    connector=connector,
)

# 共通ユーティリティ

def contains_ng_word(text: str) -> bool:
    for w in NG_WORDS:
        if w in text:
            return True
    return False


def quoted_block(text: str) -> str:
    # メッセージの各行を引用ブロックとして整形
    lines = text.splitlines() or [text]
    return "\n".join(["> " + line for line in lines])


async def send_anonymous_message(interaction: discord.Interaction, message: str):
    """
    NGワードチェック・長さチェックを行い、CHANNEL_ID の受信用チャンネルへ
    匿名メッセージとして転送する共通処理。
    スラッシュコマンドとモーダルの両方から呼び出される。
    """
    # DoS/連打対策：クールダウンチェック（モーダル・スラッシュコマンド両方に効く）
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
    # 先に記録しておくことで、送信処理中に連打されても弾ける
    _last_sent_at[user_id] = now

    # NGワードチェック
    if contains_ng_word(message):
        await interaction.response.send_message("不適切な言葉が含まれています", ephemeral=True)
        return

    # Discord のメッセージ上限に近い長さを弾く（安全対策）
    if len(message) > 1900:
        await interaction.response.send_message("メッセージが長すぎます（2000文字以内にしてください）", ephemeral=True)
        return

    # 送信先チャンネル取得（キャッシュに無ければ fetch）
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except Exception as e:
            logger.exception("転送先チャンネルの取得に失敗: %s", e)
            await interaction.response.send_message("送信に失敗しました（チャンネルが見つかりません）。管理者に連絡してください。", ephemeral=True)
            return

    # チャンネルがテキスト送信可能か確認
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.PartialMessageable, discord.abc.Messageable)):
        await interaction.response.send_message("送信先チャンネルのタイプが不正です。管理者に連絡してください。", ephemeral=True)
        return

    # 送信するメッセージ整形
    content = "📩 **匿名メッセージが届きました**\n" + quoted_block(message)

    # メッセージ送信
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

    # 荒らし対策ログ：送信者情報をサーバーログ＋開発者DMに記録する
    guild_name = interaction.guild.name if interaction.guild else "DM/不明"
    guild_id = interaction.guild.id if interaction.guild else "不明"
    await log_sender_for_moderation(interaction.user, guild_name, guild_id, message)

    # 成功レスポンス（実行者本人のみ表示）
    await interaction.response.send_message("送信しました！", ephemeral=True)


async def log_sender_for_moderation(user: discord.abc.User, guild_name: str, guild_id, content: str):
    """
    匿名メッセージ機能の悪用（荒らし）対策として、実際の送信者情報を
    ・サーバーログ（logger.info）
    ・開発者への Discord DM
    の両方に記録する。ユーザー向けの表示は匿名のままにしつつ、
    運営側だけが必要な時に送信者を特定できるようにするための仕組み。
    スラッシュコマンド/モーダル経由・Bot DM経由のどちらからも呼べる共通関数。
    """
    # サーバー側ログ（コンソール/ログファイルに残る）
    logger.info(
        "匿名メッセージ送信: user=%s (ID: %s) guild=%s (ID: %s) content=%s",
        user, user.id, guild_name, guild_id, content,
    )

    # 開発者へDM通知
    try:
        developer = bot.get_user(DEVELOPER_USER_ID) or await bot.fetch_user(DEVELOPER_USER_ID)
        dm_content = (
            "secret_mes_log\n"
            f"from: {user} (ID: {user.id})\n"
            f"server: {guild_name} (ID: {guild_id})\n"
            f"info:\n{quoted_block(content) if content else '（本文なし・添付ファイルのみ）'}"
        )
        # Discordのメッセージ上限(2000文字)を超えないように保険で分割送信
        if len(dm_content) <= 2000:
            await developer.send(dm_content)
        else:
            await developer.send(dm_content[:2000])
            await developer.send(dm_content[2000:4000])
    except Exception as e:
        # DM送信に失敗しても匿名メッセージ自体の処理は継続する
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


# ボタン（View）

class AnonymousMessageView(discord.ui.View):
    """
    timeout=None + custom_id 固定 にすることで、
    Bot再起動後もボタンが機能し続ける「永続View」にしています。
    """

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

# 募集メッセージの送信先チャンネル（全体公開）
RECRUIT_TARGET_CHANNEL_ID = 1535644078660780153

# メンションするロールの選択肢
MENTION_ROLE_CHOICES = [
    ("なし", None),
    ("ロールを選択1", 1533825506124763177),
    ("ロールを選択2", 1533824492810272889),
    ("ロールを選択3", 1533824191839735828),
    ("ロールを選択4", 1481108235673927730),
    ("ロールを選択5", 1533823603672613006),
]

# やっている内容の選択肢
CONTENT_CHOICES = ["雑談", "ゲーム", "作業・勉強", "その他"]

# 使用できるVCチャンネルのID一覧
VC_CHANNEL_IDS = [
    1430828208277946432,
    1430828208277946433,
    1529408955594440704,
]

# 現在進行中のVC募集を管理する辞書
# key: VCチャンネルID, value: {"channel_id": 送信先チャンネルID, "message_id": 送信したメッセージID}
active_recruitments: dict[int, dict] = {}


class RoleSelect(discord.ui.Select):
    """メンションするロールを選択するドロップダウン"""

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
    """やっている内容を選択するドロップダウン"""

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
    """使用するVCチャンネルを選択するドロップダウン"""

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
    """「募集を送信する」ボタンを押した後に表示される、一言入力用のモーダル"""

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

        # 送信先チャンネル取得（キャッシュに無ければ fetch）
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

        # VCが空になったことを検知して終了表示するために記録
        # （同じVCで新しい募集が作られた場合は上書きされるはず）
        active_recruitments[view.vc_id] = {
            "channel_id": target_channel.id,
            "message_id": sent_message.id,
        }

        await interaction.response.send_message("募集を送信しました！", ephemeral=True)

        # 元の選択UI（本人にだけ見えているメッセージ）を操作不可にして完了表示にする
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
    """「募集を送信する」ボタン"""

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
    """
    /vc コマンドで表示する、本人にだけ見える（ephemeral）選択UI。
    ロール・内容・VCチャンネルを選択し、送信ボタンで一言モーダルへ進みます。
    """

    def __init__(self, origin_interaction: discord.Interaction):
        super().__init__(timeout=600)  # 10分操作が無ければ自動的に無効化
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
    # Bot自身/他のBotからのメッセージは無視
    if message.author.bot:
        return

    # DM以外（サーバー内の通常メッセージ）は匿名転送の対象外
    if not isinstance(message.channel, discord.DMChannel):
        await bot.process_commands(message)
        return

    await handle_anonymous_dm(message)
    await bot.process_commands(message)


async def handle_anonymous_dm(message: discord.Message):
    """
    Botへ直接送られてきたDM（テキスト・画像・GIFなどの添付ファイル）を、
    既存の匿名メッセージ機能と同じ転送先チャンネル（CHANNEL_ID）へ、
    スラッシュコマンド/モーダル経由と同じ扱いで匿名転送する。
    """
    user = message.author
    content = message.content or ""

    # DoS/連打対策：スラッシュコマンド/モーダルと共通のクールダウンを適用
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

    # 本文も添付ファイルも無いメッセージ（空メッセージ等）は無視
    if not content and not message.attachments:
        return

    # NGワードチェック
    if content and contains_ng_word(content):
        try:
            await message.channel.send("不適切な言葉が含まれています")
        except Exception:
            pass
        return

    # 長さチェック
    if len(content) > 1900:
        try:
            await message.channel.send("メッセージが長すぎます（2000文字以内にしてください）")
        except Exception:
            pass
        return

    # 添付ファイルのサイズチェック（Botのアップロード上限は通常25MB/ファイル）
    MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
    for a in message.attachments:
        if a.size and a.size > MAX_ATTACHMENT_BYTES:
            try:
                await message.channel.send(f"添付ファイル「{a.filename}」がサイズ上限を超えています。")
            except Exception:
                pass
            return

    # 転送先チャンネル取得（キャッシュに無ければ fetch）
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

    # 添付ファイルを実体としてダウンロード→再アップロード（URL直貼りだと期限切れ等の懸念があるため）
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

    # メッセージ送信（テキスト＋画像/GIFなどの添付ファイル）
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

    # 荒らし対策ログ：送信者情報をサーバーログ＋開発者DMに記録する
    await log_sender_for_moderation(user, "DM（サーバー外から送信）", "N/A", content)

    # 成功レスポンス（送信者本人のDMにのみ返信）
    try:
        await message.channel.send("送信しました！")
    except Exception:
        pass


@bot.event
async def on_ready():
    try:
        # 永続Viewを登録（Bot再起動後もボタンを押せるようにする）
        bot.add_view(AnonymousMessageView())

        # アプリコマンドを同期
        await bot.tree.sync()
        logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
        logger.info("App commands synced.")
    except Exception as e:
        logger.exception("Failed to sync app commands: %s", e)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """
    VC募集で指定されたVCから全員がいなくなったタイミングを検知し、
    対応する募集メッセージを編集して「終了済み」がわかるようにする。
    """
    left_channel = before.channel
    if left_channel is None:
        return

    # 同じチャンネル内での状態変化（ミュート/スピーカー切替など）は無視
    if after.channel is not None and after.channel.id == left_channel.id:
        return

    info = active_recruitments.get(left_channel.id)
    if info is None:
        return

    # まだ誰か残っている場合は何もしない
    if len(left_channel.members) > 0:
        return

    # 追跡対象から削除（重複編集防止）
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
    # 実行者本人にだけ見えるレスポンス（ephemeral=True）
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
    """
    このコマンドを「送信用チャンネル」（例: #お便り箱）で実行すると、
    ボタン付きのメッセージがそのチャンネルに設置されます。
    """
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
    """
    ロール（任意）・内容・使用VC・一言（任意）を選択し、
    指定チャンネルへ募集メッセージを送信するUIを表示するコマンド。
    """
    try:
        view = VCRecruitView(interaction)
        await interaction.response.send_message(view.status_text(), view=view, ephemeral=True)
    except Exception as e:
        logger.exception("/vc コマンド実行中にエラーが発生しました: %s", e)
        try:
            await interaction.response.send_message("エラーが発生しました。管理者に連絡してください。", ephemeral=True)
        except Exception:
            pass


def wait_for_proxy(url: str, timeout: float = 30.0) -> None:
    """
    warp-plus のSOCKS5ポートが実際にLISTENするまで待つ。
    keep_alive() のFlaskサーバーは既に起動済みの状態でこの待機に入るため、
    Renderのヘルスチェックはこの待ち時間の影響を受けない。
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 8086

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                logger.info("SOCKS5プロキシの起動を確認しました: %s:%s", host, port)
                return
        except OSError:
            time.sleep(1)
    logger.error("SOCKS5プロキシ(%s:%s)が%s秒以内に起動しませんでした。", host, port, timeout)
    sys.exit(f"warp-plus proxy did not become ready at {url} within {timeout}s")


if __name__ == "__main__":
    try:
        keep_alive()  # UptimeRobotのping用にFlaskサーバーを起動（プロキシ待機より先に開ける）
        if USE_PROXY:
            wait_for_proxy(SOCKS5_PROXY_URL)
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.exception("Bot の実行に失敗しました: %s", e)
