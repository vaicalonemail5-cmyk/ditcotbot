"""
Bot Discord: đọc webhook "Rewards Logger" theo thời gian thực,
tự động cập nhật dữ liệu theo từng user, và trả về báo cáo mới nhất bằng /report <user>.

Cài đặt:  pip install -U discord.py
Chạy:     DISCORD_TOKEN=xxx WEBHOOK_CHANNEL_ID=123 python bot.py
"""

import asyncio
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

def load_env() -> str | None:
    """Tìm và đọc file .env (hoặc .env.txt). Trả về đường dẫn file đã đọc."""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    for folder in (base, os.getcwd()):
        for name in (".env", ".env.txt"):
            candidates.append(os.path.join(folder, name))

    for path in candidates:
        if not os.path.isfile(path):
            continue
        # utf-8-sig để bỏ ký tự BOM mà Notepad hay thêm vào đầu file
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[7:]
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if value and not os.environ.get(key):
                    os.environ[key] = value
        return path
    return None


ENV_PATH = load_env()

# ===================== CẤU HÌNH =====================
TOKEN = os.getenv("DISCORD_TOKEN", "")
# ID kênh nhận webhook. Để 0 nếu muốn bot lắng nghe webhook ở mọi kênh nó thấy.
CHANNEL_ID = int(os.getenv("WEBHOOK_CHANNEL_ID", "0"))
# ID server của bạn. Điền vào để lệnh / xuất hiện NGAY (không phải chờ). Để 0 = sync toàn cầu.
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
# Chỉ nhận embed có tiêu đề chứa chuỗi này (không phân biệt hoa thường). Để "" để nhận tất cả.
TITLE_KEYWORD = "rewards logger"
# Đang chạy trên Railway? (Railway tự cung cấp các biến RAILWAY_* cho mọi deployment)
IS_CLOUD = any(
    os.getenv(k)
    for k in (
        "RAILWAY_PROJECT_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_ENVIRONMENT_NAME",
        "RAILWAY_VOLUME_MOUNT_PATH",
    )
)
VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")


def default_data_file() -> str:
    """Trên Railway: lưu vào Volume (không mất khi redeploy). Trên Windows: nằm ngoài OneDrive/Desktop."""
    if VOLUME_PATH:
        folder = VOLUME_PATH
    else:
        root = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        folder = os.path.join(root, "RewardsLoggerBot")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        folder = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(folder, "reports.json")


DATA_FILE = os.getenv("DATA_FILE") or default_data_file()
try:  # đảm bảo thư mục chứa file dữ liệu tồn tại (kể cả khi tự đặt DATA_FILE)
    os.makedirs(os.path.dirname(os.path.abspath(DATA_FILE)), exist_ok=True)
except OSError:
    pass
# Quá số giây này không có logger mới thì coi user là offline (600 = 10 phút)
OFFLINE_AFTER = 10 * 60
# True = kết quả /report chỉ người gõ lệnh nhìn thấy; False = hiện công khai cho cả kênh
PRIVATE_REPLIES = True
# Số lần gửi gần nhất được nhớ để tính tần suất (trung bình)
HISTORY_SIZE = 30
# Cảnh báo @everyone: cứ mỗi ALERT_INTERVAL_MIN phút kiểm tra 1 lần, nếu TẤT CẢ acc offline thì ping
ALERT_INTERVAL_MIN = 30
# ====================================================

# Thứ tự hiển thị các mục trong báo cáo: (khoá trong webhook, tên hiển thị, inline)
SECTIONS = [
    ("player stats", "Player Stats", True),
    ("mission stats", "Mission Stats", True),
    ("drops", "Drops", False),
    ("safety status", "Safety Status", False),
]

# reports: { "tên_user_viết_thường": {...dữ liệu mới nhất...} }
reports: dict[str, dict] = {}


# ---------- Lưu / tải dữ liệu ----------
def load_data() -> None:
    global reports
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            reports = json.load(f)
    except (OSError, json.JSONDecodeError):
        reports = {}


def save_data() -> None:
    """Lưu dữ liệu. Nếu lỗi ghi file thì chỉ cảnh báo, bot vẫn chạy bình thường."""
    payload = json.dumps(reports, ensure_ascii=False, indent=2)
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, DATA_FILE)
        return
    except OSError:
        pass
    # Dự phòng: ghi thẳng vào file chính
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            f.write(payload)
    except OSError as e:
        print(f"[CẢNH BÁO] Không lưu được {DATA_FILE}: {e} (dữ liệu vẫn còn trong bộ nhớ)")


# ---------- Phân tích webhook ----------
def clean(value: str) -> str:
    """Bỏ ``` (code block) và khoảng trắng thừa."""
    return value.replace("```", "").strip()


def parse_embed(embed: discord.Embed) -> dict | None:
    """Chuyển embed webhook thành dict. Trả về None nếu không phải Rewards Logger."""
    if TITLE_KEYWORD and TITLE_KEYWORD not in (embed.title or "").lower():
        return None

    fields = {
        f.name.strip().rstrip(":").strip().lower(): clean(f.value)
        for f in embed.fields
    }
    user = fields.get("user")
    if not user:
        return None

    return {
        "user": user,
        "sections": {key: fields.get(key, "N/A") for key, _, _ in SECTIONS},
    }


def embed_user(embed: discord.Embed) -> str | None:
    """Chỉ đọc tên user để skip log trùng trước khi phân tích stats."""
    if TITLE_KEYWORD and TITLE_KEYWORD not in (embed.title or "").lower():
        return None
    user = None
    for field in embed.fields:
        if field.name.strip().rstrip(":").strip().lower() == "user":
            user = clean(field.value)
    return user or None


def report_message_id(data: dict) -> int:
    """Hỗ trợ cả reports.json cũ chỉ lưu message_url."""
    raw = data.get("message_id")
    if raw is None:
        raw = (data.get("message_url") or "").rstrip("/").rsplit("/", 1)[-1]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def is_newer_report(data: dict, existing: dict | None) -> bool:
    """Ưu tiên tin gửi mới nhất; chỉ xét bản sửa khi cùng message ID."""
    if existing is None:
        return True
    new_id, old_id = report_message_id(data), report_message_id(existing)
    if new_id and old_id:
        if new_id != old_id:
            return new_id > old_id
        # Bản cũ chưa lưu revision: làm mới một lần bằng dữ liệu từ Discord.
        if "source_timestamp" not in existing:
            return True
        return data["source_timestamp"] > existing["source_timestamp"]
    return data["source_timestamp"] > existing.get("updated_at", 0)


def message_metadata(message: discord.Message) -> dict:
    timestamp = (message.edited_at or message.created_at).timestamp()
    return {
        "message_id": message.id,
        "message_url": message.jump_url,
        "source_timestamp": timestamp,
        "updated_at": int(timestamp),
    }


def store_latest(key: str, data: dict) -> bool:
    """Dùng chung cho scan và realtime để log cũ không ghi đè log mới."""
    existing = reports.get(key)
    if not is_newer_report(data, existing):
        return False
    history = list(
        existing.get("history") or [existing["updated_at"]]
    ) if existing else []
    if existing and report_message_id(existing) == report_message_id(data) and history:
        history[-1] = data["updated_at"]
    else:
        history.append(data["updated_at"])
    reports[key] = {**data, "history": sorted(history)[-HISTORY_SIZE:]}
    return True


# ---------- Bot ----------
SETTINGS_FILE = os.path.join(os.path.dirname(DATA_FILE), "settings.json")
settings: dict = {"alert_enabled": False, "alert_channel_id": 0}


def load_settings() -> None:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            settings.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass


def save_settings() -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[CẢNH BÁO] Không lưu được {SETTINGS_FILE}: {e}")


def all_offline() -> bool:
    """True nếu có ít nhất 1 acc và TẤT CẢ đều quá OFFLINE_AFTER giây không có logger."""
    return bool(reports) and all(not is_online(d) for d in reports.values())


def restart_process() -> None:
    """Khởi động lại chính script này."""
    args = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
    if os.name == "nt":
        # Windows: mở cửa sổ console mới chạy bot rồi thoát tiến trình cũ
        subprocess.Popen(args, creationflags=subprocess.CREATE_NEW_CONSOLE)
        os._exit(0)
    os.execv(sys.executable, args)


class ReportBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # BẮT BUỘC để đọc embed của webhook
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._skip_first_alert = True  # bỏ qua lần kiểm tra ngay lúc khởi động

    async def setup_hook(self) -> None:
        load_data()
        load_settings()
        self.alert_loop.start()
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[SYNC] Đã đăng ký {len(synced)} lệnh cho server {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            print(f"[SYNC] Đã đăng ký {len(synced)} lệnh toàn cầu (có thể chờ vài phút mới hiện)")

    async def on_ready(self) -> None:
        print(f"Đã đăng nhập: {self.user} | Đang theo dõi {len(reports)} user")
        print(f"File dữ liệu: {DATA_FILE}")
        if IS_CLOUD and not VOLUME_PATH and not os.getenv("DATA_FILE"):
            print(
                "[CẢNH BÁO] Chưa gắn Volume trên Railway: dữ liệu sẽ MẤT mỗi lần redeploy/restart. "
                "Hãy tạo Volume (Mount path /data) cho service này."
            )
        servers = ", ".join(f"{g.name} ({g.id})" for g in self.guilds) or "KHÔNG CÓ SERVER NÀO"
        print(f"Bot đang ở server: {servers}")

    @tasks.loop(minutes=ALERT_INTERVAL_MIN)
    async def alert_loop(self) -> None:
        # Lần chạy đầu (ngay khi bot bật) bỏ qua, để các acc kịp gửi logger sau khi khởi động
        if self._skip_first_alert:
            self._skip_first_alert = False
            return
        try:
            await self.check_and_alert()
        except Exception as e:  # không để vòng lặp chết vì 1 lỗi
            print(f"[ALERT] Lỗi: {e!r}")

    @alert_loop.before_loop
    async def before_alert_loop(self) -> None:
        await self.wait_until_ready()

    async def check_and_alert(self) -> None:
        if not settings["alert_enabled"] or not settings["alert_channel_id"]:
            return
        if not all_offline():
            return
        channel_id = settings["alert_channel_id"]
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        last = max(d["updated_at"] for d in reports.values())
        await channel.send(
            f"@everyone ⚠️ **Tất cả {len(reports)} acc đều OFFLINE** "
            f"(quá {OFFLINE_AFTER // 60} phút không có logger). Log cuối: <t:{last}:R>",
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        print("[ALERT] Đã ping @everyone")

    def ingest(self, message: discord.Message, is_edit: bool = False) -> None:
        """Đọc 1 tin nhắn webhook và cập nhật dữ liệu user."""
        if message.webhook_id is None:
            return
        if CHANNEL_ID and message.channel.id != CHANNEL_ID:
            return

        for embed in message.embeds:
            data = parse_embed(embed)
            if not data:
                continue
            key = data["user"].lower()
            data.update(message_metadata(message))
            if store_latest(key, data):
                save_data()
                print(f"[UPDATE] {data['user']}")

    async def on_message(self, message: discord.Message) -> None:
        self.ingest(message)

    async def on_message_edit(self, _before: discord.Message, after: discord.Message) -> None:
        # Webhook có thể sửa lại tin nhắn -> cập nhật lại luôn
        self.ingest(after, is_edit=True)


bot = ReportBot()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    print(f"[LỖI LỆNH] {error!r}")
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Bạn cần quyền **Manage Server** để dùng lệnh này."
    else:
        msg = "Có lỗi khi chạy lệnh, xem cửa sổ terminal của bot để biết chi tiết."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


def find_user(name: str) -> dict | None:
    key = name.strip().lower()
    if key in reports:
        return reports[key]
    # Cho phép gõ một phần tên nếu chỉ có đúng 1 kết quả khớp
    matches = [v for k, v in reports.items() if key in k]
    return matches[0] if len(matches) == 1 else None


def is_online(data: dict) -> bool:
    now = int(datetime.now(timezone.utc).timestamp())
    return now - data["updated_at"] <= OFFLINE_AFTER


def count_users() -> tuple[int, int, int]:
    """Trả về (tổng, online, offline)."""
    total = len(reports)
    online = sum(1 for d in reports.values() if is_online(d))
    return total, online, total - online


def fmt_duration(seconds: float) -> str:
    """Đổi số giây thành chữ dễ đọc: 45 giây / 3 phút 12 giây / 1 giờ 5 phút."""
    s = int(round(seconds))
    if s < 60:
        return f"{s} giây"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m} phút {sec} giây" if sec else f"{m} phút"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h} giờ {m} phút" if m else f"{h} giờ"


def frequency_text(data: dict) -> str:
    """Khoảng cách giữa 2 lần gửi gần nhất và trung bình."""
    history = data.get("history") or []
    if len(history) < 2:
        return "Chưa đủ dữ liệu (cần ít nhất 2 lần gửi)"
    gaps = [b - a for a, b in zip(history, history[1:])]
    last_gap = gaps[-1]
    avg = sum(gaps) / len(gaps)
    return (
        f"2 lần gần nhất: cách nhau {fmt_duration(last_gap)}\n"
        f"Trung bình:     {fmt_duration(avg)} ({len(gaps)} khoảng)"
    )


def build_embed(data: dict) -> discord.Embed:
    online = is_online(data)
    embed = discord.Embed(
        title="Tekkit | Rewards Logger",
        color=0x57F287 if online else 0xED4245,
    )
    embed.add_field(name="User:", value=f"```{data['user']}```", inline=False)
    for key, label, inline in SECTIONS:
        embed.add_field(
            name=f"{label}:",
            value=f"```{data['sections'].get(key, 'N/A')}```",
            inline=inline,
        )

    embed.add_field(
        name="Tần suất gửi log:",
        value=f"```{frequency_text(data)}```",
        inline=False,
    )

    status = "🟢 Online" if online else "🔴 Offline"
    embed.add_field(name="Trạng thái:", value=status, inline=True)
    embed.add_field(
        name="Logger lần cuối:",
        value=f"<t:{data['updated_at']}:F>\n(<t:{data['updated_at']}:R>)",
        inline=True,
    )

    total, on, off = count_users()
    embed.add_field(
        name="Tổng quan:",
        value=f"```Tổng user: {total}\nOnline:    {on}\nOffline:   {off}```",
        inline=False,
    )
    embed.set_footer(text=f"Quá {OFFLINE_AFTER // 60} phút không có logger = Offline")
    return embed


def fmt_short(seconds: float) -> str:
    """Định dạng ngắn cho danh sách: 45s / 3m12s / 1h05m."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m{sec:02d}s" if sec else f"{m}m"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if m else f"{h}h"


def gap_stats(data: dict) -> tuple[float, float] | None:
    """(khoảng cách 2 lần gửi gần nhất, khoảng cách trung bình) hoặc None nếu chưa đủ dữ liệu."""
    history = data.get("history") or []
    if len(history) < 2:
        return None
    gaps = [b - a for a, b in zip(history, history[1:])]
    return gaps[-1], sum(gaps) / len(gaps)


def player_stats(data: dict) -> dict[str, str]:
    """Tách mục Player Stats thành dict: {'prestige': '0', 'level': '96', 'gems': '13,310', ...}."""
    text = data.get("sections", {}).get("player stats", "")
    stats = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            stats[key.strip().lower()] = value.strip()
    return stats


def to_int(value: str | None) -> int:
    digits = re.sub(r"\D", "", value or "")
    return int(digits) if digits else 0


EMBED_TOTAL_MAX = 6000
EMBED_BATCH_MAX = 10


def build_list_embeds(title: str, lines: list[str]) -> list[discord.Embed]:
    """Ghép các dòng thành embed, tự chia nhỏ nếu quá dài (mỗi embed tối đa 4096 ký tự)."""
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)

    embeds = []
    for i, text in enumerate(chunks):
        embed = discord.Embed(description=text, color=0x2B2D31)
        if i == 0:
            embed.title = title
        embeds.append(embed)
    return embeds


def embed_char_count(embed: discord.Embed) -> int:
    return len(embed.title or "") + len(embed.description or "")


def split_embed_batches(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    """Chia danh sách embed thành từng gói <= 10 embed và <= 6000 ký tự cho mỗi tin nhắn Discord."""
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    current_len = 0
    for embed in embeds:
        elen = embed_char_count(embed)
        if current and (len(current) >= EMBED_BATCH_MAX or current_len + elen > EMBED_TOTAL_MAX):
            batches.append(current)
            current = []
            current_len = 0
        current.append(embed)
        current_len += elen
    if current:
        batches.append(current)
    return batches


async def send_embeds(
    interaction: discord.Interaction,
    embeds: list[discord.Embed],
    *,
    ephemeral: bool = PRIVATE_REPLIES,
) -> None:
    """Gửi danh sách embed; tự chia thành nhiều tin nhắn followup nếu tổng ký tự vượt 6000."""
    batches = split_embed_batches(embeds)
    if not batches:
        return
    await interaction.response.send_message(embeds=batches[0], ephemeral=ephemeral)
    for batch in batches[1:]:
        await interaction.followup.send(embeds=batch, ephemeral=ephemeral)


def build_time_embeds() -> list[discord.Embed]:
    """/report all: trạng thái + thời gian log cuối + tần suất gửi của mọi user."""
    rows = sorted(
        reports.values(),
        key=lambda d: (not is_online(d), -d["updated_at"]),  # online trước, mới nhất trước
    )
    lines = []
    for d in rows:
        online = is_online(d)
        gs = gap_stats(d)
        freq = (
            f"Cách nhau: {fmt_short(gs[0])} · Trung bình: {fmt_short(gs[1])}"
            if gs
            else "Cách nhau: — · Trung bình: —"
        )
        lines.append(
            f"{'🟢' if online else '🔴'} **{d['user']}** — "
            f"{'Online' if online else 'Offline'} — <t:{d['updated_at']}:R>\n└ {freq}"
        )
    total, on, off = count_users()
    return build_list_embeds(f"Thời gian & trạng thái ({total}) — 🟢 {on} | 🔴 {off}", lines)


def build_stats_embeds() -> list[discord.Embed]:
    """/report stats: Gold, Gems, Prestige, Level của mọi user (không có Rewards/Drops)."""
    def sort_key(d: dict) -> tuple[int, int]:
        s = player_stats(d)
        return to_int(s.get("prestige")), to_int(s.get("level"))

    rows = sorted(reports.values(), key=sort_key, reverse=True)  # prestige, rồi level cao nhất trước
    lines = []
    for d in rows:
        s = player_stats(d)
        lines.append(
            f"**{d['user']}**\n└ "
            f"Gold: {s.get('gold', 'N/A')} · Gems: {s.get('gems', 'N/A')} · "
            f"Prestige: {s.get('prestige', 'N/A')} · Level: {s.get('level', 'N/A')}"
        )
    return build_list_embeds(f"Stats tất cả user ({len(rows)})", lines)


@bot.tree.command(name="report", description="Xem báo cáo mới nhất của một user, hoặc all / stats")
@app_commands.describe(
    user="Tên user; 'all' = thời gian & trạng thái tất cả user; 'stats' = stats tất cả user"
)
async def report(interaction: discord.Interaction, user: str) -> None:
    key = user.strip().lower()
    if key in ("all", "stats"):
        if not reports:
            await interaction.response.send_message(
                "Bot chưa nhận được webhook nào.", ephemeral=True
            )
            return
        embeds = build_time_embeds() if key == "all" else build_stats_embeds()
        await send_embeds(interaction, embeds, ephemeral=PRIVATE_REPLIES)
        return

    data = find_user(user)
    if not data:
        await interaction.response.send_message(
            f"Không tìm thấy user `{user}`. Bot chưa nhận webhook nào của user này.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        embed=build_embed(data), ephemeral=PRIVATE_REPLIES
    )


@report.autocomplete("user")
async def report_autocomplete(
    _interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    choices = []
    special = [
        ("all", "all (thời gian & trạng thái tất cả user)"),
        ("stats", "stats (Gold, Gems, Prestige, Level tất cả user)"),
    ]
    for value, label in special:
        if current in value:
            choices.append(app_commands.Choice(name=label, value=value))
    names = [v["user"] for k, v in reports.items() if current in k]
    choices += [app_commands.Choice(name=n, value=n) for n in names]
    return choices[:25]


def clear_reports(target: str) -> int:
    """Xóa report theo target: 'all', 'offline' hoặc tên 1 user. Trả về số user đã xóa."""
    if target == "all":
        removed = len(reports)
        reports.clear()
    elif target == "offline":
        keys = [k for k, d in reports.items() if not is_online(d)]
        for k in keys:
            del reports[k]
        removed = len(keys)
    else:
        removed = 1 if reports.pop(target, None) is not None else 0
    if removed:
        save_data()
    return removed


class ConfirmClearView(discord.ui.View):
    def __init__(self, author_id: int, target: str) -> None:
        super().__init__(timeout=30)
        self.author_id = author_id
        self.target = target

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Chỉ người gõ lệnh mới bấm được nút này.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Xác nhận xóa", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        removed = clear_reports(self.target)
        self.stop()
        await interaction.response.edit_message(
            content=f"🗑️ Đã xóa report của **{removed}** user.", view=None
        )

    @discord.ui.button(label="Hủy", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Đã hủy.", view=None)


@bot.tree.command(name="clear", description="Xóa log report cũ")
@app_commands.describe(
    target="'all' = xóa hết, 'offline' = xóa các user đang offline, hoặc tên 1 user"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def clear(interaction: discord.Interaction, target: str) -> None:
    t = target.strip().lower()

    if t in ("all", "offline"):
        count = len(reports) if t == "all" else sum(
            1 for d in reports.values() if not is_online(d)
        )
        if count == 0:
            await interaction.response.send_message(
                "Không có report nào để xóa.", ephemeral=True
            )
            return
        what = "TOÀN BỘ" if t == "all" else "các user đang OFFLINE:"
        await interaction.response.send_message(
            f"⚠️ Bạn sắp xóa {what} **{count}** report. Không thể hoàn tác.",
            view=ConfirmClearView(interaction.user.id, t),
            ephemeral=True,
        )
        return

    if t not in reports:
        await interaction.response.send_message(
            f"Không tìm thấy user `{target}`.", ephemeral=True
        )
        return
    name = reports[t]["user"]
    clear_reports(t)
    await interaction.response.send_message(
        f"🗑️ Đã xóa report của **{name}**.", ephemeral=True
    )


@clear.autocomplete("target")
async def clear_autocomplete(
    _interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    choices = [
        app_commands.Choice(name="all (xóa tất cả)", value="all"),
        app_commands.Choice(name="offline (xóa user đang offline)", value="offline"),
    ]
    choices = [c for c in choices if current in c.value]
    choices += [
        app_commands.Choice(name=d["user"], value=d["user"])
        for k, d in reports.items()
        if current in k
    ]
    return choices[:25]


@bot.tree.command(name="alert", description="Bật/tắt cảnh báo @everyone khi tất cả acc offline")
@app_commands.describe(mode="on = bật (gửi vào kênh này), off = tắt, status = xem trạng thái")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="on (bật, gửi cảnh báo vào kênh này)", value="on"),
        app_commands.Choice(name="off (tắt)", value="off"),
        app_commands.Choice(name="status (xem trạng thái)", value="status"),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def alert(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
    if mode.value == "on":
        settings["alert_enabled"] = True
        settings["alert_channel_id"] = interaction.channel_id
        save_settings()
        msg = (
            f"🔔 Đã **BẬT** cảnh báo. Cứ mỗi {ALERT_INTERVAL_MIN} phút, nếu tất cả acc đều "
            f"offline (quá {OFFLINE_AFTER // 60} phút không có logger), bot sẽ @everyone tại kênh này."
        )
        perms = interaction.channel.permissions_for(interaction.guild.me)
        if not perms.send_messages:
            msg += "\n⚠️ Bot **chưa có quyền gửi tin nhắn** trong kênh này."
        elif not perms.mention_everyone:
            msg += "\n⚠️ Bot **chưa có quyền Mention @everyone** trong kênh này, hãy cấp quyền đó nếu không sẽ không ping được."
    elif mode.value == "off":
        settings["alert_enabled"] = False
        save_settings()
        msg = "🔕 Đã **TẮT** cảnh báo @everyone."
    else:
        total, on, off = count_users()
        state = "🔔 BẬT" if settings["alert_enabled"] else "🔕 TẮT"
        channel = f"<#{settings['alert_channel_id']}>" if settings["alert_channel_id"] else "chưa đặt"
        now = "TẤT CẢ đang offline" if all_offline() else f"còn {on}/{total} acc online"
        msg = (
            f"Cảnh báo: **{state}**\n"
            f"Kênh cảnh báo: {channel}\n"
            f"Kiểm tra mỗi: {ALERT_INTERVAL_MIN} phút\n"
            f"Hiện tại: {now}"
        )
    await interaction.response.send_message(msg, ephemeral=True)


_owner_ids: set[int] | None = None


async def is_bot_owner(user_id: int) -> bool:
    global _owner_ids
    if _owner_ids is None:
        app = await bot.application_info()
        _owner_ids = {m.id for m in app.team.members} if app.team else {app.owner.id}
    return user_id in _owner_ids


@bot.tree.command(name="restart", description="Khởi động lại bot (chỉ chủ bot dùng được)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def restart(interaction: discord.Interaction) -> None:
    if not await is_bot_owner(interaction.user.id):
        await interaction.response.send_message(
            "Chỉ chủ bot mới dùng được lệnh này.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "🔄 Đang khởi động lại bot, vui lòng đợi vài giây...", ephemeral=True
    )
    print("[RESTART] Có yêu cầu khởi động lại từ /restart")
    save_data()
    save_settings()
    await bot.close()
    restart_process()


# ---------- Quét log cũ & xếp hạng acc có stats cao nhất ----------
SCAN_DEFAULT = 1000
SCAN_MAX = 500000
_scan_lock = asyncio.Lock()


async def scan_channel(channel, limit: int) -> tuple[int, int, dict[str, dict]]:
    """Đọc mới -> cũ; mỗi user chỉ xét webhook hợp lệ đầu tiên.
    Trả về số tin đã đọc, số log hợp lệ và snapshot mới nhất theo user.
    Snapshot None nghĩa là reports đã có bản bằng hoặc mới hơn.
    Không gom lịch sử từ các webhook cũ bị skip.
    """
    scanned: dict[str, dict] = {}
    messages = logs = 0
    async for message in channel.history(limit=limit, oldest_first=False):
        messages += 1
        if messages % 10000 == 0:
            print(f"[SCAN] Đang quét... {messages}/{limit} tin nhắn ({logs} log, {len(scanned)} user)")
        if message.webhook_id is None:
            continue
        for embed in message.embeds:
            user = embed_user(embed)
            if not user:
                continue
            logs += 1
            key = user.lower()
            if key in scanned:
                continue
            metadata = message_metadata(message)
            scanned[key] = {"latest": None}
            if not is_newer_report(metadata, reports.get(key)):
                continue
            data = parse_embed(embed)
            if data is not None:
                data.update(metadata)
                scanned[key]["latest"] = data
    return messages, logs, scanned


def merge_scanned(scanned: dict[str, dict]) -> tuple[int, int]:
    """Kiểm tra lại lúc gộp: realtime có thể đã nhận webhook mới trong khi scan."""
    new_users = updated = 0
    for key, info in scanned.items():
        data = info["latest"]
        if data is None:
            continue
        is_new = key not in reports
        if store_latest(key, data):
            if is_new:
                new_users += 1
            else:
                updated += 1
    return new_users, updated


@bot.tree.command(name="scan", description="Lấy webhook mới nhất của mỗi user, bỏ qua log cũ hoặc đã có")
@app_commands.describe(
    limit=f"Số tin nhắn gần nhất cần quét (mặc định {SCAN_DEFAULT}, tối đa {SCAN_MAX})"
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def scan(
    interaction: discord.Interaction, limit: app_commands.Range[int, 1, SCAN_MAX] = SCAN_DEFAULT
) -> None:
    channel = interaction.channel
    if CHANNEL_ID:
        try:
            channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
        except discord.HTTPException:
            await interaction.response.send_message(
                f"❌ Không truy cập được kênh webhook `{CHANNEL_ID}` (WEBHOOK_CHANNEL_ID).",
                ephemeral=True,
            )
            return

    if _scan_lock.locked():
        await interaction.response.send_message(
            "Đang có một lần quét chạy rồi, đợi nó xong nhé.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=PRIVATE_REPLIES, thinking=True)
    async with _scan_lock:
        try:
            messages, logs, scanned = await scan_channel(channel, limit)
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Bot không có quyền **View Channel** và **Read Message History** ở <#{channel.id}>.",
                ephemeral=PRIVATE_REPLIES,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Lỗi khi đọc lịch sử: {e}", ephemeral=PRIVATE_REPLIES)
            return
        new_users, updated = merge_scanned(scanned)
        if new_users or updated:
            save_data()

    print(f"[SCAN] {messages} tin nhắn, {logs} log, {len(scanned)} user ({new_users} mới)")
    summary = (
        f"✅ Đã quét **{messages}** tin nhắn trong <#{channel.id}>\n"
        f"• Log hợp lệ: **{logs}** · Số acc tìm thấy: **{len(scanned)}**\n"
        f"• Acc mới thêm: **{new_users}** · Acc được cập nhật: **{updated}**\n"
        f"• Log bỏ qua (trùng/cũ/đã có): **{logs - new_users - updated}**\n"
        f"Dùng `/top` để xem acc có stats cao nhất."
    )
    try:
        await interaction.followup.send(summary, ephemeral=PRIVATE_REPLIES)
    except discord.HTTPException:
        target_ch = getattr(interaction, "channel", None) or channel
        if hasattr(target_ch, "send"):
            try:
                await target_ch.send(summary)
            except discord.HTTPException:
                pass


TOP_DEFAULT = 10
TOP_MAX = 200

TOP_SORTS = {
    "prestige": ("Prestige", lambda s: (to_int(s.get("prestige")), to_int(s.get("level")), to_int(s.get("gold")))),
    "level": ("Level", lambda s: (to_int(s.get("level")), to_int(s.get("prestige")), to_int(s.get("gold")))),
    "gold": ("Gold", lambda s: (to_int(s.get("gold")),)),
    "gems": ("Gems", lambda s: (to_int(s.get("gems")),)),
}


@bot.tree.command(name="top", description="Xếp hạng các acc có stats cao nhất")
@app_commands.describe(
    by="Xếp hạng theo",
    count=f"Số acc hiển thị (mặc định {TOP_DEFAULT}, tối đa {TOP_MAX})",
)
@app_commands.choices(
    by=[
        app_commands.Choice(name="Prestige (rồi đến Level)", value="prestige"),
        app_commands.Choice(name="Level", value="level"),
        app_commands.Choice(name="Gold", value="gold"),
        app_commands.Choice(name="Gems", value="gems"),
    ]
)
async def top(
    interaction: discord.Interaction,
    by: str = "prestige",
    count: app_commands.Range[int, 1, TOP_MAX] = TOP_DEFAULT,
) -> None:
    if not reports:
        await interaction.response.send_message(
            "Chưa có dữ liệu acc nào. Hãy dùng `/scan` để đọc log cũ trong kênh webhook.",
            ephemeral=True,
        )
        return

    label, key_fn = TOP_SORTS.get(by, TOP_SORTS["prestige"])
    rows = sorted(reports.values(), key=lambda d: key_fn(player_stats(d)), reverse=True)[:count]

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, d in enumerate(rows):
        s = player_stats(d)
        rank = medals[i] if i < 3 else f"`{i + 1}.`"
        dot = "🟢" if is_online(d) else "🔴"
        lines.append(
            f"{rank} {dot} **{d['user']}**\n└ "
            f"Prestige: {s.get('prestige', 'N/A')} · Level: {s.get('level', 'N/A')} · "
            f"Gold: {s.get('gold', 'N/A')} · Gems: {s.get('gems', 'N/A')}"
        )
    lines.append("*Dựa trên log gần nhất của mỗi acc. Dùng /scan để nạp thêm log cũ.*")
    embeds = build_list_embeds(f"🏆 Top {len(rows)} theo {label} (trong {len(reports)} acc)", lines)
    await send_embeds(interaction, embeds, ephemeral=PRIVATE_REPLIES)


# ---------- /search: tìm stats theo danh sách username trong file ----------
SEARCH_DEFAULT = 5000        # số tin nhắn gần nhất sẽ tìm (dừng sớm nếu đã thấy đủ username)
SEARCH_MAX_NAMES = 500       # tối đa số username trong 1 file
SEARCH_MAX_BYTES = 256 * 1024
SEARCH_SHOW = 300            # số acc hiện trong embed; đầy đủ nằm trong file CSV đính kèm


def parse_usernames(raw: bytes) -> list[str] | None:
    """Đọc username từ file: mỗi dòng một tên (hoặc cách nhau bằng , ; khoảng trắng).
    Bỏ tên trùng (không phân biệt hoa thường). Trả về None nếu không phải file văn bản."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 (Notepad/PowerShell đời cũ hay lưu kiểu này)
        text = raw.decode("utf-16", errors="replace")
    elif b"\x00" in raw:
        return None
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    names, seen = [], set()
    for token in re.split(r"[\s,;]+", text):
        name = token.strip("\"'`")
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    return names


async def search_history(
    channel, wanted: dict[str, str], limit: int
) -> tuple[int, dict[str, dict], bool]:
    """Đọc mới -> cũ, lấy log MỚI NHẤT của mỗi username trong `wanted` ({tên_thường: tên_gốc}).
    Dừng sớm khi đã thấy đủ. Chỉ đọc, không ghi vào reports.
    Trả về (số tin đã đọc, kết quả theo user, có dừng sớm không)."""
    found: dict[str, dict] = {}
    messages = 0
    stopped_early = False
    async for message in channel.history(limit=limit, oldest_first=False):
        messages += 1
        if message.webhook_id is None:
            continue
        for embed in message.embeds:
            user = embed_user(embed)
            if not user:
                continue
            key = user.lower()
            if key not in wanted or key in found:
                continue
            data = parse_embed(embed)
            if data is None:
                continue
            found[key] = {
                "data": data,
                "ts": int((message.edited_at or message.created_at).timestamp()),
                "url": message.jump_url,
            }
        if len(found) == len(wanted):
            stopped_early = True
            break
    return messages, found, stopped_early


def csv_safe(value: str) -> str:
    """Chặn công thức Excel (=, +, -, @ ở đầu ô)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def build_search_csv(wanted: dict[str, str], found: dict[str, dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["username", "found", "gems", "gold", "prestige", "log_time_utc"])
    for key, name in wanted.items():
        info = found.get(key)
        if not info:
            writer.writerow([csv_safe(name), "no", "", "", "", ""])
            continue
        s = player_stats(info["data"])
        when = datetime.fromtimestamp(info["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([
            csv_safe(info["data"]["user"]), "yes",
            to_int(s.get("gems")), to_int(s.get("gold")), to_int(s.get("prestige")), when,
        ])
    return buf.getvalue().encode("utf-8-sig")  # BOM để Excel mở đúng tiếng Việt


def build_search_embeds(wanted: dict[str, str], found: dict[str, dict]) -> list[discord.Embed]:
    lines = []
    for key in wanted:  # giữ nguyên thứ tự trong file
        info = found.get(key)
        if not info:
            continue
        s = player_stats(info["data"])
        lines.append(
            f"**{info['data']['user']}**\n└ "
            f"Gems: {s.get('gems', 'N/A')} · Gold: {s.get('gold', 'N/A')} · "
            f"Prestige: {s.get('prestige', 'N/A')} · <t:{info['ts']}:R>"
        )
    if len(lines) > SEARCH_SHOW:
        extra = len(lines) - SEARCH_SHOW
        lines = lines[:SEARCH_SHOW] + [f"*… và {extra} acc nữa, xem đầy đủ trong file CSV đính kèm.*"]

    missing = [name for key, name in wanted.items() if key not in found]
    if missing:
        text = ", ".join(missing)
        if len(text) > 1500:
            text = text[:1500].rsplit(", ", 1)[0] + ", … (xem đầy đủ trong file CSV)"
        lines.append(f"\n❌ **Không tìm thấy ({len(missing)}):** {text}")
    return build_list_embeds(f"🔎 Kết quả tìm kiếm ({len(found)}/{len(wanted)})", lines)


@bot.tree.command(name="search", description="Nhập file username, tìm stats (Gems, Gold, Prestige) trong chat webhook")
@app_commands.describe(
    file="File .txt/.csv chứa username (mỗi dòng một tên)",
    limit=f"Số tin nhắn gần nhất cần tìm (mặc định {SEARCH_DEFAULT}, tối đa {SCAN_MAX})",
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def search(
    interaction: discord.Interaction,
    file: discord.Attachment,
    limit: app_commands.Range[int, 1, SCAN_MAX] = SEARCH_DEFAULT,
) -> None:
    if file.size > SEARCH_MAX_BYTES:
        await interaction.response.send_message(
            f"❌ File quá lớn ({file.size // 1024} KB, tối đa {SEARCH_MAX_BYTES // 1024} KB).",
            ephemeral=True,
        )
        return

    channel = interaction.channel
    if CHANNEL_ID:
        try:
            channel = bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)
        except discord.HTTPException:
            await interaction.response.send_message(
                f"❌ Không truy cập được kênh webhook `{CHANNEL_ID}` (WEBHOOK_CHANNEL_ID).",
                ephemeral=True,
            )
            return

    if _scan_lock.locked():
        await interaction.response.send_message(
            "Đang có một lần quét/tìm kiếm chạy rồi, đợi nó xong nhé.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=PRIVATE_REPLIES, thinking=True)

    async def fail(text: str) -> None:
        await interaction.followup.send(text, ephemeral=PRIVATE_REPLIES)

    try:
        raw = await file.read()
    except discord.HTTPException as e:
        await fail(f"❌ Không tải được file: {e}")
        return
    names = parse_usernames(raw)
    if names is None:
        await fail("❌ File này không phải file văn bản. Hãy dùng .txt hoặc .csv, mỗi dòng một username.")
        return
    if not names:
        await fail("❌ Không đọc được username nào trong file.")
        return
    if len(names) > SEARCH_MAX_NAMES:
        await fail(f"❌ File có {len(names)} username, tối đa {SEARCH_MAX_NAMES}. Hãy chia nhỏ file.")
        return
    wanted = {n.lower(): n for n in names}

    async with _scan_lock:
        try:
            messages, found, stopped_early = await search_history(channel, wanted, limit)
        except discord.Forbidden:
            await fail(
                f"❌ Bot không có quyền **View Channel** và **Read Message History** ở <#{channel.id}>."
            )
            return
        except discord.HTTPException as e:
            await fail(f"❌ Lỗi khi đọc lịch sử: {e}")
            return

    missing = len(wanted) - len(found)
    print(f"[SEARCH] {len(wanted)} username, đọc {messages} tin, thấy {len(found)}, thiếu {missing}")
    summary = (
        f"🔎 Đã đọc **{messages}** tin nhắn trong <#{channel.id}> — "
        f"tìm thấy **{len(found)}/{len(wanted)}** username."
    )
    if stopped_early:
        summary += " (đã thấy đủ nên dừng sớm)"
    elif missing and messages >= limit:
        summary += f"\n⚠️ Đã quét hết {limit} tin gần nhất mà vẫn thiếu {missing} tên. Thử tăng `limit` nếu cần."
    elif missing:
        summary += "\nĐã quét hết lịch sử kênh."

    csv_file = discord.File(io.BytesIO(build_search_csv(wanted, found)), filename="search_result.csv")
    embed_batches = split_embed_batches(build_search_embeds(wanted, found))
    await interaction.followup.send(
        content=summary,
        embeds=embed_batches[0] if embed_batches else [],
        file=csv_file,
        ephemeral=PRIVATE_REPLIES,
    )
    for batch in embed_batches[1:]:
        await interaction.followup.send(embeds=batch, ephemeral=PRIVATE_REPLIES)


# ---------- Điều khiển app trong VPS (chỉ app có trong apps.json) ----------
APPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps.json")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def read_apps() -> tuple[dict[str, dict], str | None]:
    """Đọc apps.json (đọc lại mỗi lần gọi, sửa file không cần restart).
    Trả về (danh sách app, thông báo lỗi hoặc None)."""
    try:
        with open(APPS_FILE, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return {}, f"Không thấy file apps.json. Hãy đặt cạnh bot.py: {APPS_FILE}"
    except OSError as e:
        return {}, f"Không đọc được apps.json: {e}"

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as first:
        # Lỗi hay gặp nhất: gõ đường dẫn Windows với 1 dấu \ (JSON bắt buộc \\ hoặc /).
        # Thử tự sửa bằng cách đổi \ thành /, Windows vẫn hiểu đường dẫn kiểu này.
        try:
            raw = json.loads(text.replace("\\", "/"))
            print("[APP] apps.json dùng dấu \\ sai cú pháp, bot đã tự sửa tạm. "
                  "Nên đổi sang dấu / (C:/Duong/Dan/App.exe) hoặc \\\\.")
        except json.JSONDecodeError:
            return {}, (
                f"apps.json sai cú pháp ở dòng {first.lineno}, cột {first.colno} ({first.msg}). "
                "Đường dẫn Windows hãy viết dạng C:/Duong/Dan/App.exe"
            )
    if not isinstance(raw, dict):
        return {}, "apps.json phải bắt đầu bằng { và kết thúc bằng }"
    apps = {
        k.lower(): v for k, v in raw.items() if isinstance(v, dict) and not k.startswith("_")
    }
    return apps, None


def load_apps() -> dict[str, dict]:
    return read_apps()[0]


def process_running(name: str) -> bool:
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True, text=True, creationflags=NO_WINDOW, timeout=15,
            ).stdout
            return name.lower() in out.lower()
        return subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def app_action(action: str, name: str, cfg: dict) -> str:
    path = str(cfg.get("path", ""))
    proc = str(cfg.get("process") or os.path.basename(path))
    running = process_running(proc) if proc else False

    if action == "status":
        return f"{'🟢 Đang chạy' if running else '🔴 Không chạy'}: **{name}**"

    if action == "start":
        if running:
            return f"⚠️ **{name}** đang chạy rồi."
        if os.path.isdir(path):
            hint = os.path.join(path, proc) if proc else path
            return (
                f"❌ `{path}` là một **thư mục**, không phải file chạy. "
                f"Hãy ghi thẳng tới file .exe trong `apps.json`, ví dụ: `{hint}`"
            )
        exe = path if os.path.isfile(path) else shutil.which(path)
        if not exe:
            return f"❌ Không tìm thấy file `{path}`. Kiểm tra lại đường dẫn trong `apps.json`."
        args = [str(a) for a in cfg.get("args", [])]
        cwd = cfg.get("cwd") or os.path.dirname(os.path.abspath(exe))
        quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        try:
            if os.name == "nt":
                subprocess.Popen(
                    [exe, *args], cwd=cwd, **quiet,
                    creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                subprocess.Popen([exe, *args], cwd=cwd, **quiet, start_new_session=True)
        except OSError as e:
            return f"❌ Không mở được **{name}**: {e}"
        return f"✅ Đã gửi lệnh mở **{name}**."

    if action == "stop":
        if not running:
            return f"ℹ️ **{name}** hiện không chạy."
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/IM", proc, "/F"], capture_output=True,
                               creationflags=NO_WINDOW, timeout=30)
            else:
                subprocess.run(["pkill", "-x", proc], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            return f"❌ Không tắt được **{name}**: {e}"
        return f"🛑 Đã gửi lệnh tắt **{name}**."

    return "Lệnh không hợp lệ."


@bot.tree.command(name="app", description="Mở / tắt / xem trạng thái app trong VPS (chỉ chủ bot)")
@app_commands.describe(action="start = mở, stop = tắt, status = xem trạng thái", name="Tên app trong apps.json")
@app_commands.choices(
    action=[
        app_commands.Choice(name="start (mở app)", value="start"),
        app_commands.Choice(name="stop (tắt app)", value="stop"),
        app_commands.Choice(name="status (xem trạng thái)", value="status"),
    ]
)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def app_cmd(interaction: discord.Interaction, action: app_commands.Choice[str], name: str) -> None:
    if not await is_bot_owner(interaction.user.id):
        await interaction.response.send_message("Chỉ chủ bot mới dùng được lệnh này.", ephemeral=True)
        return

    apps, err = read_apps()
    if err:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return
    key = name.strip().lower()
    cfg = apps.get(key)
    if cfg is None:
        listing = ", ".join(f"`{k}`" for k in apps) or "(apps.json chưa có app nào)"
        await interaction.response.send_message(
            f"Không có app `{name}` trong danh sách cho phép.\nApp hiện có: {listing}", ephemeral=True
        )
        return

    print(f"[APP] {interaction.user} ({interaction.user.id}) -> {action.value} {key}")
    await interaction.response.defer(ephemeral=True)
    result = await asyncio.to_thread(app_action, action.value, key, cfg)
    await interaction.followup.send(result, ephemeral=True)


@app_cmd.autocomplete("name")
async def app_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if not await is_bot_owner(interaction.user.id):
        return []
    apps, err = read_apps()
    if err:
        return [app_commands.Choice(name=("⚠️ " + err)[:100], value="loi")]
    current = current.lower()
    return [app_commands.Choice(name=k, value=k) for k in apps if current in k][:25]


if IS_CLOUD:
    # Lệnh /app điều khiển app trên VPS Windows, không có ý nghĩa trong container -> không đăng ký
    bot.tree.remove_command("app")


if __name__ == "__main__":
    if not TOKEN or TOKEN.startswith("dán_"):
        base = os.path.dirname(os.path.abspath(__file__))
        print("=" * 60)
        print("KHÔNG TÌM THẤY DISCORD_TOKEN")
        print(f"Thư mục chứa bot.py : {base}")
        if ENV_PATH:
            print(f"Đã đọc file         : {ENV_PATH}")
            print("-> File có tồn tại nhưng dòng DISCORD_TOKEN đang trống hoặc sai.")
            print("   Dòng đúng:  DISCORD_TOKEN=token_của_bạn  (không cách, không ngoặc)")
        else:
            print("Không thấy file .env / .env.txt trong thư mục trên.")
            print("-> Hãy đặt file .env cùng thư mục với bot.py.")
        print("Các file hiện có    :", ", ".join(sorted(os.listdir(base))))
        print("=" * 60)
        raise SystemExit(1)
    bot.run(TOKEN)
