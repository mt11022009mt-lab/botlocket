import os
import sys
import time
import uuid
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Locket Bot is running 24/7")

    def log_message(self, format, *args):
        pass  # Suppress console log spam

def start_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"Health check HTTP server started on port {port}")
    except Exception as e:
        print(f"Health check server notice: {e}")

from locketlam import LocketClient, AuthenticationError, LocketAPIError

# Nạp file .env
load_dotenv()

# Cấu hình file log
LOG_FILE = Path(__file__).parent / "bot.log"
log_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

# Cấu hình UTF-8 cho console Windows nếu có
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Cấu hình logging
logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=log_handlers,
)
logger = logging.getLogger("LocketBot")

# Lấy cấu hình từ biến môi trường
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
LOCKET_EMAIL = os.getenv("LOCKET_EMAIL", "").strip()
LOCKET_PASSWORD = os.getenv("LOCKET_PASSWORD", "").strip()

# Danh sách user Telegram được phép dùng bot
raw_allowed_ids = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = (
    [int(x.strip()) for x in raw_allowed_ids.split(",") if x.strip().isdigit()]
    if raw_allowed_ids
    else []
)

# Đường dẫn lưu session đa người dùng
SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)
TEMP_DIR = Path("temp_media")
TEMP_DIR.mkdir(exist_ok=True)

# Bộ nhớ đệm Locket Client cho từng User ID Telegram
_user_clients = {}

def get_client(user_id: int) -> LocketClient:
    """Lấy hoặc khởi tạo LocketClient riêng biệt cho từng người dùng Telegram."""
    if user_id not in _user_clients:
        session_file = SESSIONS_DIR / f"locket_session_{user_id}.json"
        _user_clients[user_id] = LocketClient(session_path=str(session_file))
    return _user_clients[user_id]

def delete_user_session(user_id: int):
    """Xóa phiên đăng nhập của người dùng."""
    session_file = SESSIONS_DIR / f"locket_session_{user_id}.json"
    if session_file.exists():
        try:
            session_file.unlink()
        except Exception as e:
            logger.error(f"Lỗi khi xóa session của user {user_id}: {e}")
    # Reset client
    _user_clients[user_id] = LocketClient(session_path=str(session_file))

# Các trạng thái của luồng đăng nhập tách riêng Email & Mật khẩu
WAIT_EMAIL, WAIT_PASSWORD = range(2)


def is_authorized(user_id: int) -> bool:
    """Kiểm tra xem người dùng Telegram này có quyền sử dụng bot hay không."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Bàn phím menu dưới màn hình riêng theo trạng thái của từng người dùng."""
    client = get_client(user_id)
    if client.is_authenticated():
        keyboard = [
            ["👤 Thông tin tài khoản", "👥 Danh sách bạn bè"],
            ["📸 Hướng dẫn đăng bài", "🚪 Đăng xuất"],
        ]
    else:
        keyboard = [
            ["🔑 Đăng nhập Locket"],
            ["📸 Hướng dẫn đăng bài"],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def safe_edit_or_send(msg, text, context, chat_id, **kwargs):
    """Cập nhật nội dung tin nhắn an toàn, tự động gửi mới nếu không thể edit."""
    if msg:
        try:
            return await msg.edit_text(text, **kwargs)
        except Exception as e:
            err_msg = str(e).lower()
            if "can't parse entities" in err_msg:
                clean_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                try:
                    return await msg.edit_text(text, **clean_kwargs)
                except Exception:
                    pass
            logger.warning(f"Không thể edit tin nhắn ({e}), gửi tin nhắn mới...")
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception:
        clean_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
        return await context.bot.send_message(chat_id=chat_id, text=text, **clean_kwargs)


async def unauthorized_reply(update: Update):
    """Phản hồi khi người dùng không có quyền."""
    await update.message.reply_text(
        "⛔ **Bạn không có quyền sử dụng bot này.**\n"
        f"ID Telegram của bạn là: `{update.effective_user.id}`\n\n"
        "Hãy thêm ID này vào biến `ALLOWED_USER_IDS` trong file `.env` của máy chủ.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def post_init_callback(application):
    """Đăng ký danh sách lệnh hiển thị sẵn trong nút Menu của Telegram."""
    commands = [
        BotCommand("start", "🏠 Mở Menu chính & Trạng thái"),
        BotCommand("login", "🔑 Đăng nhập tài khoản Locket"),
        BotCommand("me", "👤 Xem thông tin tài khoản Locket"),
        BotCommand("friends", "👥 Xem danh sách bạn bè"),
        BotCommand("logout", "🚪 Đăng xuất tài khoản Locket"),
        BotCommand("help", "📖 Hướng dẫn chi tiết"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Đã đăng ký danh sách lệnh hiển thị sẵn trên menu Telegram.")


# -------------------------------------------------------------
# COMMAND HANDLERS
# -------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /start: Giới thiệu và hiển thị menu nút bấm."""
    user = update.effective_user
    if not is_authorized(user.id):
        return await unauthorized_reply(update)

    client = get_client(user.id)
    status_str = "🟢 Đã đăng nhập" if client.is_authenticated() else "🔴 Chưa đăng nhập"
    user_info = ""

    if client.is_authenticated():
        try:
            profile = await asyncio.to_thread(client.user.get_profile)
            name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
            username = profile.get("username", "")
            user_info = f"\n👤 Tài khoản hiện tại: **{name}** (@{username})"
        except Exception:
            pass

    msg = (
        f"👋 Xin chào **{user.first_name}**!\n\n"
        f"🤖 Chào mừng bạn đến với **Locket Camera Bot**.\n"
        f"Trạng thái: **{status_str}**{user_info}\n\n"
        "✨ **Cách đăng bài lên Locket:**\n"
        "• Chỉ cần **gửi trực tiếp Ảnh hoặc Video** vào chat này.\n"
        "• Nhập lời nhắn (caption) tùy ý kèm ảnh/video.\n"
        "• Nếu caption bắt đầu bằng icon (vd: `🔥 Cháy phố`), bot sẽ lấy làm icon cảm xúc!\n\n"
        "👇 _Chọn nhanh các tính năng bên dưới bàn phím hoặc nhấn nút **Menu** góc trái:_ "
    )
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_keyboard(user.id),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /help: Hướng dẫn chi tiết."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return await unauthorized_reply(update)

    msg = (
        "📖 **HƯỚNG DẪN SỬ DỤNG LOCKET BOT**\n\n"
        "📸 **1. Đăng Ảnh:**\n"
        "• Gửi ảnh dạng bình thường hoặc gửi dạng File (Document).\n"
        "• Kèm theo caption bạn muốn hiển thị.\n\n"
        "🎬 **2. Đăng Video:**\n"
        "• Gửi video quay ngắn (< 20MB) vào khung chat.\n"
        "• Bot sẽ nén chuẩn định dạng và đăng lên Locket.\n\n"
        "🔘 **3. Thao tác tiện lợi:**\n"
        "• Bạn chỉ cần ấn vào các nút bấm hiển thị sẵn bên dưới màn hình, không cần phải gõ lại lệnh!"
    )
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_keyboard(user_id),
    )


async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /me hoặc nút '👤 Thông tin tài khoản': Kiểm tra tài khoản."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return await unauthorized_reply(update)

    client = get_client(user_id)
    if not client.is_authenticated():
        await update.message.reply_text(
            "⚠️ Bạn chưa đăng nhập tài khoản Locket.\nHãy bấm nút **[🔑 Đăng nhập Locket]** bên dưới để đăng nhập.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(user_id),
        )
        return

    status_msg = await update.message.reply_text("⏳ Đang tải thông tin tài khoản Locket...")
    try:
        profile = await asyncio.to_thread(client.user.get_profile)
        friends = await asyncio.to_thread(client.friends.get_all_friends)

        name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        username = profile.get("username", "Chưa đặt")
        email = profile.get("email", "Ẩn")
        friend_count = len(friends) if isinstance(friends, list) else 0

        info_text = (
            "📋 **THÔNG TIN TÀI KHOẢN LOCKET**\n"
            "────────────────────\n"
            f"• **Họ & Tên:** {name}\n"
            f"• **Username:** @{username}\n"
            f"• **Email:** `{email}`\n"
            f"• **Tổng bạn bè:** {friend_count} người\n"
            f"• **Trạng thái phiên:** 🟢 Đang hoạt động (Tự refresh ngầm)\n"
        )
        await safe_edit_or_send(
            status_msg,
            info_text,
            context,
            update.effective_chat.id,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await safe_edit_or_send(
            status_msg,
            f"❌ Lỗi khi lấy thông tin: `{e}`",
            context,
            update.effective_chat.id,
            parse_mode=ParseMode.MARKDOWN,
        )


async def friends_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /friends hoặc nút '👥 Danh sách bạn bè': Xem bạn bè."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return await unauthorized_reply(update)

    client = get_client(user_id)
    if not client.is_authenticated():
        await update.message.reply_text(
            "⚠️ Bạn chưa đăng nhập tài khoản Locket.\nHãy bấm nút **[🔑 Đăng nhập Locket]** bên dưới.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(user_id),
        )
        return

    status_msg = await update.message.reply_text("⏳ Đang tải danh sách bạn bè...")
    try:
        friends = await asyncio.to_thread(client.friends.get_all_friends)
        if not friends:
            await safe_edit_or_send(
                status_msg,
                "👥 Bạn chưa có người bạn nào trên Locket.",
                context,
                update.effective_chat.id,
            )
            return

        lines = [f"👥 **DANH SÁCH BẠN BÈ ({len(friends)}):**\n"]
        for idx, f in enumerate(friends[:20], 1):
            fname = f"{f.get('first_name', '')} {f.get('last_name', '')}".strip() or "Ẩn danh"
            fuser = f.get("username", "")
            lines.append(f"{idx}. {fname} (@{fuser})")

        if len(friends) > 20:
            lines.append(f"\n_...và còn {len(friends) - 20} người bạn khác._")

        await safe_edit_or_send(
            status_msg,
            "\n".join(lines),
            context,
            update.effective_chat.id,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await safe_edit_or_send(
            status_msg,
            f"❌ Lỗi khi lấy danh sách bạn bè: `{e}`",
            context,
            update.effective_chat.id,
            parse_mode=ParseMode.MARKDOWN,
        )


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /logout hoặc nút '🚪 Đăng xuất'."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return await unauthorized_reply(update)

    delete_user_session(user_id)

    await update.message.reply_text(
        "👋 **Đã đăng xuất thành công!** Phiên lưu trữ của bạn đã được xóa an toàn.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_keyboard(user_id),
    )


# -------------------------------------------------------------
# FLOW ĐĂNG NHẬP RIÊNG BIỆT (TỪNG BƯỚC: EMAIL -> MẬT KHẨU)
# -------------------------------------------------------------

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Bước 1: Bắt đầu đăng nhập, yêu cầu nhập email."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await unauthorized_reply(update)
        return ConversationHandler.END

    client = get_client(user_id)
    cancel_markup = ReplyKeyboardMarkup([["❌ Hủy thao tác"]], resize_keyboard=True)

    if client.is_authenticated():
        msg = (
            "ℹ️ Bạn hiện đã đăng nhập một tài khoản Locket.\n"
            "Nếu muốn đổi sang tài khoản khác, vui lòng gửi địa chỉ **Email mới** vào đây:\n"
            "*(Hoặc bấm nút '❌ Hủy thao tác' bên dưới để hủy)*"
        )
    else:
        msg = (
            "📧 **BƯỚC 1/2: NHẬP EMAIL LOCKET**\n\n"
            "Vui lòng gửi địa chỉ **Email** tài khoản Locket của bạn vào đây:\n"
            "*(Hoặc bấm nút '❌ Hủy thao tác' bên dưới để hủy)*"
        )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_markup,
    )
    return WAIT_EMAIL


async def login_receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Bước 2: Đã nhận email, yêu cầu nhập mật khẩu."""
    email = update.message.text.strip()
    if not email or "@" not in email:
        await update.message.reply_text(
            "⚠️ Email không hợp lệ. Vui lòng nhập lại địa chỉ email (ví dụ: `yourname@gmail.com`):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WAIT_EMAIL

    context.user_data["login_email"] = email
    cancel_markup = ReplyKeyboardMarkup([["❌ Hủy thao tác"]], resize_keyboard=True)

    await update.message.reply_text(
        f"✅ Đã ghi nhận Email: `{email}`\n\n"
        "🔒 **BƯỚC 2/2: NHẬP MẬT KHẨU**\n\n"
        "Vui lòng gửi **Mật khẩu** tài khoản Locket của bạn:\n"
        "🛡️ _Tin nhắn chứa mật khẩu sẽ được bot **tự động xóa ngay lập tức** để bảo đảm an toàn!_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_markup,
    )
    return WAIT_PASSWORD


async def login_receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Bước 3: Đã nhận mật khẩu, xóa tin nhắn ngay lập tức và tiến hành login."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    password = update.message.text.strip()
    email = context.user_data.get("login_email", "")
    client = get_client(user_id)

    # 1. Tự động xóa tin nhắn chứa mật khẩu của người dùng ngay lập tức để bảo mật
    try:
        await update.message.delete()
    except Exception:
        pass

    # 2. Gửi thông báo chờ xử lý (ẩn bàn phím tạm thời)
    status_msg = None
    try:
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Đang kết nối Firebase và đăng nhập vào Locket Camera...",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        pass

    try:
        await asyncio.to_thread(client.login, email, password)
        profile = await asyncio.to_thread(client.user.get_profile)
        name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        username = profile.get("username", "")

        success_text = (
            f"🎉 **Đăng nhập Locket thành công!**\n\n"
            f"👤 Tên hiển thị: **{name}**\n"
            f"🏷️ Username: `@{username}`\n"
            f"🔑 UID: `{client.session.uid}`\n\n"
            f"Bây giờ bạn chỉ cần gửi **Ảnh** hoặc **Video** vào đây là có thể đăng bài ngay lập tức!"
        )
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=success_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(user_id),
        )
    except AuthenticationError as e:
        err_text = (
            f"❌ **Đăng nhập thất bại:** Sai email hoặc mật khẩu.\nChi tiết: `{e}`\n\n"
            f"Hãy bấm lại nút **[🔑 Đăng nhập Locket]** bên dưới để thử lại."
        )
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=err_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(user_id),
        )
    except Exception as e:
        logger.error(f"Lỗi đăng nhập: {e}", exc_info=True)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ **Lỗi xảy ra:** `{e}`\n\nVui lòng thử lại sau.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(user_id),
        )
    finally:
        # Luôn đảm bảo dọn dẹp state và kết thúc conversation
        context.user_data.pop("login_email", None)

    return ConversationHandler.END


async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Hủy quá trình đăng nhập."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    context.user_data.pop("login_email", None)
    await context.bot.send_message(
        chat_id=chat_id,
        text="🚫 Đã hủy thao tác đăng nhập.",
        reply_markup=get_main_keyboard(user_id),
    )
    return ConversationHandler.END


# -------------------------------------------------------------
# MEDIA HANDLERS (ẢNH & VIDEO)
# -------------------------------------------------------------

def extract_emoji_and_caption(raw_text: str):
    """Phân tách emoji ở đầu nếu có và nội dung caption."""
    if not raw_text:
        return None, ""
    raw_text = raw_text.strip()
    parts = raw_text.split(" ", 1)
    first_token = parts[0]
    if any(ord(char) > 127 for char in first_token) and len(first_token) <= 4:
        icon = first_token
        caption = parts[1] if len(parts) > 1 else ""
        return icon, caption
    return None, raw_text


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý khi người dùng gửi ảnh."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_authorized(user_id):
        return await unauthorized_reply(update)

    client = get_client(user_id)
    if not client.is_authenticated():
        await update.message.reply_text(
            "⚠️ Tài khoản Locket chưa đăng nhập.\nHãy bấm nút **[🔑 Đăng nhập Locket]** bên dưới trước nhé!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(user_id),
        )
        return

    photo = update.message.photo[-1]
    raw_caption = update.message.caption or ""
    icon, clean_caption = extract_emoji_and_caption(raw_caption)

    status_msg = await update.message.reply_text("📥 Đang nhận ảnh từ Telegram...")
    file_id = f"photo_{uuid.uuid4().hex[:8]}.jpg"
    temp_file_path = TEMP_DIR / file_id

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(custom_path=temp_file_path)

        await safe_edit_or_send(status_msg, "⚙️ Đang xử lý ảnh & upload lên Locket Camera...", context, chat_id)

        kwargs = {
            "image": str(temp_file_path),
            "caption": clean_caption,
        }
        if icon:
            kwargs["icon"] = icon

        moment = await asyncio.to_thread(client.moments.post, **kwargs)
        moment_id = moment.get("canonical_uid", moment.get("uid", "Thành công"))

        caption_display = f' với caption: "{clean_caption}"' if clean_caption else ""
        icon_display = f" [Icon: {icon}]" if icon else ""

        await safe_edit_or_send(
            status_msg,
            f"✅ **Đã đăng ảnh lên Locket thành công!** 🎉\n\n"
            f"• **Moment ID:** `{moment_id}`\n"
            f"• **Nội dung:** {caption_display}{icon_display}\n"
            f"• **Chế độ:** Bạn bè trên Locket đều sẽ thấy!",
            context,
            chat_id,
            parse_mode=ParseMode.MARKDOWN,
        )
    except LocketAPIError as e:
        await safe_edit_or_send(status_msg, f"❌ **Lỗi API Locket:** `{e}`", context, chat_id, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Lỗi khi xử lý ảnh: {e}", exc_info=True)
        await safe_edit_or_send(status_msg, f"❌ **Đã xảy ra lỗi:** `{e}`", context, chat_id, parse_mode=ParseMode.MARKDOWN)
    finally:
        if temp_file_path.exists():
            try:
                temp_file_path.unlink()
            except Exception:
                pass


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý khi người dùng gửi video."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_authorized(user_id):
        return await unauthorized_reply(update)

    client = get_client(user_id)
    if not client.is_authenticated():
        await update.message.reply_text(
            "⚠️ Tài khoản Locket chưa đăng nhập.\nHãy bấm nút **[🔑 Đăng nhập Locket]** bên dưới trước nhé!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(user_id),
        )
        return

    video = update.message.video
    raw_caption = update.message.caption or ""
    icon, clean_caption = extract_emoji_and_caption(raw_caption)

    if video.file_size and video.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ Video quá nặng (vượt quá 20MB). Locket hỗ trợ video ngắn tốt nhất dưới 10-15MB.")
        return

    status_msg = await update.message.reply_text("📥 Đang nhận video từ Telegram...")
    file_id = f"video_{uuid.uuid4().hex[:8]}.mp4"
    temp_file_path = TEMP_DIR / file_id

    try:
        tg_file = await context.bot.get_file(video.file_id)
        await tg_file.download_to_drive(custom_path=temp_file_path)

        await safe_edit_or_send(status_msg, "🎬 Đang chuẩn bị nén & upload video lên Locket...", context, chat_id)

        loop = asyncio.get_running_loop()
        last_edit = [0.0]

        def on_video_progress(pct, text):
            now = time.time()
            if now - last_edit[0] >= 2.5 or pct == 100:
                last_edit[0] = now
                filled = min(10, max(0, pct // 10))
                bar = "█" * filled + "░" * (10 - filled)
                msg_text = (
                    f"🎬 **Đang nén video chuẩn Apple HEVC ({pct}%):**\n"
                    f"`[{bar}]` {pct}%\n\n"
                    f"💡 _Locket giới hạn < 6MB nên bot đang tự động ép dung lượng giữ trọn độ nét Full HD..._"
                )
                async def _update_progress():
                    try:
                        await status_msg.edit_text(msg_text, parse_mode=ParseMode.MARKDOWN)
                    except Exception:
                        pass
                asyncio.run_coroutine_threadsafe(_update_progress(), loop)

        kwargs = {
            "video": str(temp_file_path),
            "caption": clean_caption,
            "progress_callback": on_video_progress,
        }
        if icon:
            kwargs["icon"] = icon

        moment = await asyncio.to_thread(client.moments.post, **kwargs)
        moment_id = moment.get("canonical_uid", moment.get("uid", "Thành công"))

        await safe_edit_or_send(
            status_msg,
            f"✅ **Đã đăng video lên Locket thành công!** 🎥\n\n"
            f"• **Moment ID:** `{moment_id}`\n"
            f"• **Caption:** {clean_caption or '(Không có)'}\n",
            context,
            chat_id,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Lỗi khi xử lý video: {e}", exc_info=True)
        await safe_edit_or_send(status_msg, f"❌ **Lỗi khi đăng video:** `{e}`", context, chat_id, parse_mode=ParseMode.MARKDOWN)
    finally:
        if temp_file_path.exists():
            try:
                temp_file_path.unlink()
            except Exception:
                pass


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý khi người dùng gửi ảnh dạng file không nén."""
    doc = update.message.document
    if not doc or not doc.mime_type:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_authorized(user_id):
        return await unauthorized_reply(update)

    client = get_client(user_id)
    if not client.is_authenticated():
        await update.message.reply_text(
            "⚠️ Tài khoản Locket chưa đăng nhập.\nHãy bấm nút **[🔑 Đăng nhập Locket]** bên dưới trước nhé!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(user_id),
        )
        return

    if doc.mime_type.startswith("image/"):
        status_msg = await update.message.reply_text("📥 Đang nhận ảnh gốc từ Document...")
        ext = doc.file_name.split(".")[-1] if doc.file_name and "." in doc.file_name else "jpg"
        temp_file_path = TEMP_DIR / f"doc_{uuid.uuid4().hex[:8]}.{ext}"

        try:
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(custom_path=temp_file_path)

            raw_caption = update.message.caption or ""
            icon, clean_caption = extract_emoji_and_caption(raw_caption)

            await safe_edit_or_send(status_msg, "⚙️ Đang tối ưu hoá & đăng lên Locket...", context, chat_id)
            kwargs = {"image": str(temp_file_path), "caption": clean_caption}
            if icon:
                kwargs["icon"] = icon

            moment = await asyncio.to_thread(client.moments.post, **kwargs)
            moment_id = moment.get("canonical_uid", moment.get("uid", "Thành công"))
            await safe_edit_or_send(
                status_msg,
                f"✅ **Đã đăng ảnh gốc lên Locket thành công!** 🎉\n• **Moment ID:** `{moment_id}`",
                context,
                chat_id,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await safe_edit_or_send(status_msg, f"❌ Lỗi: `{e}`", context, chat_id, parse_mode=ParseMode.MARKDOWN)
        finally:
            if temp_file_path.exists():
                try:
                    temp_file_path.unlink()
                except Exception:
                    pass


async def unknown_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Phản hồi hướng dẫn khi người dùng gửi văn bản ngẫu nhiên."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return await unauthorized_reply(update)

    await update.message.reply_text(
        "💡 Để đăng bài lên Locket, bạn hãy **gửi trực tiếp Ảnh hoặc Video** vào đây kèm caption nhé!\n"
        "Hoặc chọn các tính năng trên bàn phím menu bên dưới.",
        reply_markup=get_main_keyboard(user_id),
    )


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log exception chưa bắt để bot không bao giờ bị dừng."""
    logger.error("Exception unhandled trong bot:", exc_info=context.error)


# -------------------------------------------------------------
# MAIN ENTRYPOINT
# -------------------------------------------------------------

def run_bot():
    # Xây dựng bot Telegram với post_init để đăng ký command menu
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init_callback).build()

    # 1. Conversation Handler cho quy trình đăng nhập 2 bước riêng biệt
    login_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("login", login_start),
            MessageHandler(filters.Regex("^(🔑 Đăng nhập Locket)$"), login_start),
        ],
        states={
            WAIT_EMAIL: [
                CommandHandler("cancel", login_cancel),
                CommandHandler("start", login_cancel),
                MessageHandler(filters.Regex("^(❌ Hủy thao tác)$"), login_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_receive_email),
            ],
            WAIT_PASSWORD: [
                CommandHandler("cancel", login_cancel),
                CommandHandler("start", login_cancel),
                MessageHandler(filters.Regex("^(❌ Hủy thao tác)$"), login_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_receive_password),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", login_cancel),
            CommandHandler("start", login_cancel),
            MessageHandler(filters.Regex("^(❌ Hủy thao tác)$"), login_cancel),
        ],
        allow_reentry=True,
    )
    app.add_handler(login_conv_handler)

    # 2. Xử lý các lệnh Slash
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", me_command))
    app.add_handler(CommandHandler("me", me_command))
    app.add_handler(CommandHandler("friends", friends_command))
    app.add_handler(CommandHandler("logout", logout_command))

    # 3. Xử lý khi người dùng ấn vào các nút bấm tiếng Việt trên bàn phím
    app.add_handler(MessageHandler(filters.Regex("^(👤 Thông tin tài khoản)$"), me_command))
    app.add_handler(MessageHandler(filters.Regex("^(👥 Danh sách bạn bè)$"), friends_command))
    app.add_handler(MessageHandler(filters.Regex("^(📸 Hướng dẫn đăng bài)$"), help_command))
    app.add_handler(MessageHandler(filters.Regex("^(🚪 Đăng xuất)$"), logout_command))

    # 4. Đăng ký nhận file ảnh / video / document
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))

    # 5. Phản hồi các tin nhắn văn bản tự do
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message_handler))

    # Đăng ký global error handler
    app.add_error_handler(global_error_handler)

    logger.info("Bot đã sẵn sàng nhận tin nhắn! Đang chạy chế độ Polling...")
    app.run_polling(drop_pending_updates=False)


def main():
    logger.info("=" * 60)
    logger.info("🚀 KHỞI ĐỘNG LOCKET CAMERA TELEGRAM BOT (V2)")
    logger.info("=" * 60)

    # Khởi động cổng HTTP phục vụ Render / Cloud Health Check
    start_health_check_server()

    if not TELEGRAM_BOT_TOKEN:
        logger.error("Chưa cấu hình TELEGRAM_BOT_TOKEN trong file .env!")
        sys.exit(1)

    while True:
        try:
            run_bot()
            logger.warning("run_bot() đã dừng, đang tự động khởi động lại sau 3 giây...")
            time.sleep(3)
        except KeyboardInterrupt:
            logger.info("Bot đã dừng bởi người dùng (KeyboardInterrupt).")
            break
        except Exception as e:
            logger.error(f"Bot gặp sự cố ({e}). Đang tự động kết nối lại sau 5 giây...", exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
