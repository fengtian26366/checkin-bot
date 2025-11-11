# -*- coding: utf-8 -*-
# 打卡机器人 · 班次版（07:00/19:00）
# 支持：按钮选择 + 文字直启（吃饭/抽烟/厕所），“回来/回坐/back”结束
# 稳定：重试/去抖/用户锁/仅本人/话题兼容/编辑失败降级；超时强@提醒

import asyncio, os, json, tempfile, shutil, random, traceback
from datetime import datetime, timedelta, timezone, time as dtime, date as ddate
from typing import Dict, Any, Optional, Callable, Awaitable

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeAllGroupChats, Message,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError, TelegramError
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# =============== 配置 ===============
BOT_TOKEN       = "8474574984:AAEhnOaPbT0gx5C-wKMXHqcTcrQchOkSsK0"
ALERT_USERNAME  = "Knor1130"   # 超时要提醒的人（不带@）
ALERT_USER_ID   = 7736035882            # 如果知道对方ID，填整数（更稳）；不知道先留 None

# 项目：内部英文键，界面中文
KINDS = {
    "wc":    {"label": "厕所", "emoji": "🚽", "limit": 5, "maxm": 10},
    "smoke": {"label": "抽烟", "emoji": "🚬", "limit": 5, "maxm": 10},
    "meal":  {"label": "吃饭", "emoji": "🍽️", "limit": 3, "maxm": 30},
}

# 文字直启关键词（小写对比；包含即可触发）
START_WORDS = {
    "wc":    {"wc","厕所","上厕所","卫生间","洗手间"},
    "smoke": {"smoke","抽烟","抽煙","吸烟","吸煙","点烟","點煙","烟","煙"},
    "meal":  {"meal","吃饭","吃飯","开饭","開飯","去吃饭","去吃飯","吃","饭","飯"},
}
# 结束关键词
BACK_WORDS = {w.lower() for w in [
    "回来","回來","回坐","返岗","返崗","到位","back","i am back","i'm back","回来了","回來了"
]}

# 其它
DATA_FILE      = "data.json"
KEEP_PERIODS   = 60     # 保留最近多少个“班次”（两班/天，60≈30天）
DEBOUNCE_SEC   = 1.0
VERSION        = "shift-stable-2.1"

# =============== 时区/班次 ===============
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Phnom_Penh")
except Exception:
    TZ = timezone(timedelta(hours=7))

DAY_START   = dtime(7, 0, 0)    # 07:00
NIGHT_START = dtime(19, 0, 0)   # 19:00

def now(): return datetime.now(TZ)
def mention(u):
    name = u.full_name or (f"@{u.username}" if u.username else f"用户{u.id}")
    return f'<a href="tg://user?id={u.id}">{name}</a>'
def sec_txt(s):
    m, x = divmod(int(s), 60)
    return f"{m}分{x}秒" if m else f"{x}秒"

def current_period_key(ts: Optional[datetime]=None) -> str:
    ts = ts or now()
    t = ts.timetz(); d = ts.date()
    if t >= NIGHT_START: return f"{d.isoformat()}_N"    # 夜班
    if t >= DAY_START:   return f"{d.isoformat()}_D"    # 日班
    y = d - timedelta(days=1)
    return f"{y.isoformat()}_N"                         # 凌晨归昨夜班

def next_boundary_time(ts: Optional[datetime]=None) -> datetime:
    ts = ts or now(); d = ts.date(); t = ts.timetz()
    if t < DAY_START:   return datetime.combine(d, DAY_START, tzinfo=TZ)      # 下个7点
    if t < NIGHT_START: return datetime.combine(d, NIGHT_START, tzinfo=TZ)    # 下个19点
    return datetime.combine(d + timedelta(days=1), DAY_START, tzinfo=TZ)      # 明早7点

def period_title(key: str) -> str:
    try:
        d, tag = key.split("_",1)
        return f"{d}（{'日班' if tag=='D' else '夜班'}）"
    except Exception:
        return key or "当前班次"

def thread_kwargs(update: Update) -> dict:
    mtid = None
    if update.message and update.message.message_thread_id:
        mtid = update.message.message_thread_id
    elif update.callback_query and update.callback_query.message and update.callback_query.message.message_thread_id:
        mtid = update.callback_query.message.message_thread_id
    return {"message_thread_id": mtid} if mtid else {}

# =============== 数据 I/O ===============
def atomic_save(path: str, data: Dict[str, Any]) -> None:
    tmp = tempfile.mktemp(prefix="chk_", suffix=".json", dir=os.path.dirname(os.path.abspath(path)) or ".")
    with open(tmp,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)
    shutil.move(tmp, path)

def load() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"sessions":{}, "counts":{}, "dur":{}, "owners":{}}
    try:
        with open(DATA_FILE,"r",encoding="utf-8") as f: d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("sessions",{}); d.setdefault("counts",{}); d.setdefault("dur",{}); d.setdefault("owners",{})
    return d

def save(d: Dict[str, Any]) -> None:
    # 只留最近 KEEP_PERIODS 个班次
    all_keys = set()
    for bucket in ("counts","dur"):
        for _, per_user in d[bucket].items(): all_keys.update(per_user.keys())
    def key_start(k: str) -> datetime:
        try:
            ds, tag = k.split("_",1); day = ddate.fromisoformat(ds)
            return datetime.combine(day, DAY_START if tag=="D" else NIGHT_START, tzinfo=TZ)
        except Exception:
            return now()
    keep = set(sorted(all_keys, key=key_start, reverse=True)[:KEEP_PERIODS])
    for bucket in ("counts","dur"):
        for u, per in list(d[bucket].items()):
            for k in list(per.keys()):
                if k not in keep: per.pop(k, None)
            if not per: d[bucket].pop(u, None)
    atomic_save(DATA_FILE, d)

# =============== UI ===============
def kb_menu():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{KINDS['wc']['emoji']} {KINDS['wc']['label']}",     callback_data="act:start:wc"),
        InlineKeyboardButton(f"{KINDS['smoke']['emoji']} {KINDS['smoke']['label']}", callback_data="act:start:smoke"),
        InlineKeyboardButton(f"{KINDS['meal']['emoji']} {KINDS['meal']['label']}",   callback_data="act:start:meal"),
    ]])
def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏪ 回来（仅本人）", callback_data="act:back")]])

# =============== 强化稳定 ===============
async def safe_call(fn: Callable[..., Awaitable], *args, retries: int=4, base: float=0.25, jitter: float=0.2, **kwargs):
    n = 0
    while True:
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e,"retry_after",1.0)) + random.uniform(0, jitter))
        except (TimedOut, NetworkError):
            if n >= retries: raise
            await asyncio.sleep(base * (2**n) + random.uniform(0, jitter)); n += 1

def get_lock(ctx: ContextTypes.DEFAULT_TYPE, u: str) -> asyncio.Lock:
    locks = ctx.application.bot_data.setdefault("locks", {})
    if u not in locks: locks[u] = asyncio.Lock()
    return locks[u]
def debounced(ctx: ContextTypes.DEFAULT_TYPE, u: str, key: str, window=DEBOUNCE_SEC) -> bool:
    book = ctx.application.bot_data.setdefault("debounce", {})
    ts = datetime.utcnow().timestamp()
    last = book.get((u, key), 0)
    if ts - last < window: return True
    book[(u, key)] = ts
    return False

# =============== 强@ 解析 ===============
async def resolve_alert_mention(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> str:
    """
    返回一个能真正@到人的 mention：
    - 配了 ALERT_USER_ID：用 tg://user?id= 强制 @；
    - 否则尝试从群管理员里找 ALERT_USERNAME；
    - 实在拿不到就退回 '@用户名' 文本。
    """
    if ALERT_USER_ID:
        uname = ALERT_USERNAME or "提醒"
        return f'<a href="tg://user?id={ALERT_USER_ID}">@{uname}</a>'
    try:
        admins = await safe_call(context.bot.get_chat_administrators, chat_id)
        for a in admins:
            if a.user.username and a.user.username.lower() == (ALERT_USERNAME or "").lower():
                return f'<a href="tg://user?id={a.user.id}">@{a.user.username}</a>'
    except Exception:
        pass
    return f"@{ALERT_USERNAME}" if ALERT_USERNAME else "（未配置提醒人）"

# =============== 共用流程 ===============
async def start_flow(context: ContextTypes.DEFAULT_TYPE, user, chat_id: int, kind_key: str, th: dict,
                     menu_msg_id: Optional[int]=None):
    """触发开始：来源可为按钮或文字。"""
    u = str(user.id)
    d = load()

    if u in d["sessions"]:
        await safe_call(context.bot.send_message, chat_id,
                        f"{mention(user)} 你有进行中的打卡，请先“回来”。",
                        parse_mode=ParseMode.HTML, **th)
        return

    pkey = current_period_key()
    c_today = d["counts"].setdefault(u, {}).setdefault(pkey, {"wc":0,"smoke":0,"meal":0})
    used, limit = c_today[kind_key], KINDS[kind_key]["limit"]
    if used >= limit:
        reset_at = next_boundary_time().strftime("%m-%d %H:%M")
        await safe_call(context.bot.send_message, chat_id,
                        f"{mention(user)} 本班次【{KINDS[kind_key]['label']}】次数已达上限（{limit}/{limit}）。下次重置：{reset_at}",
                        parse_mode=ParseMode.HTML, **th)
        return

    task = (
        f"{mention(user)} 开始【{KINDS[kind_key]['label']}】计时（单次上限 {KINDS[kind_key]['maxm']} 分）。\n"
        "点击下方“回来”结束；也可直接发送“回坐/回来/back”。"
    )
    sent: Message = await safe_call(context.bot.send_message, chat_id, task,
                                    reply_markup=kb_back(), parse_mode=ParseMode.HTML, **th)

    d["sessions"][u] = {"kind": kind_key, "start": now().isoformat(),
                        "chat_id": sent.chat_id, "msg_id": sent.message_id, "period": pkey}
    d["owners"][f"{sent.chat_id}:{sent.message_id}"] = u
    save(d)

    if menu_msg_id is not None:
        try: await safe_call(context.bot.delete_message, chat_id=chat_id, message_id=menu_msg_id, **th)
        except Exception: pass

async def finish_for_user(context: ContextTypes.DEFAULT_TYPE, user, th: dict):
    u = str(user.id)
    d = load()
    sess = d["sessions"].get(u)
    if not sess: return

    kind_key = sess["kind"]; kind = KINDS[kind_key]
    start = datetime.fromisoformat(sess["start"])
    used_sec = int((now() - start).total_seconds())
    used_min = used_sec // 60
    pkey = sess.get("period") or current_period_key()

    c_today = d["counts"].setdefault(u, {}).setdefault(pkey, {"wc":0,"smoke":0,"meal":0})
    c_today[kind_key] += 1
    d_today = d["dur"].setdefault(u, {}).setdefault(pkey, {"wc":0,"smoke":0,"meal":0,"__total__":0})
    d_today[kind_key] += used_sec
    d_today["__total__"] += used_sec

    d["sessions"].pop(u, None)
    save(d)

    # 强@提醒
    alert = await resolve_alert_mention(context, sess["chat_id"])
    maxm = kind["maxm"]
    status = (
        f"✅ 本次【{kind['label']}】结束，用时 {sec_txt(used_sec)}。"
        if used_min <= maxm
        else f"⚠️ 本次【{kind['label']}】超时（上限 {maxm} 分，实际 {used_min} 分）。 {alert}"
    )

    title = period_title(pkey)
    result = (
        f"{mention(user)}\n{status}\n\n"
        f"— 本班次统计 [{title}] —\n"
        f"{KINDS['wc']['emoji']} {KINDS['wc']['label']}：{c_today['wc']} 次｜累计 {sec_txt(d_today['wc'])}\n"
        f"{KINDS['smoke']['emoji']} {KINDS['smoke']['label']}：{c_today['smoke']} 次｜累计 {sec_txt(d_today['smoke'])}\n"
        f"{KINDS['meal']['emoji']} {KINDS['meal']['label']}：{c_today['meal']} 次｜累计 {sec_txt(d_today['meal'])}\n"
        f"🧮 本班次总计：{sec_txt(d_today['__total__'])}"
    )

    try:
        await safe_call(context.bot.edit_message_text, chat_id=sess["chat_id"], message_id=sess["msg_id"],
                        text=result, parse_mode=ParseMode.HTML, **th)
    except Exception:
        await safe_call(context.bot.send_message, sess["chat_id"], result, parse_mode=ParseMode.HTML, **th)

    d = load()
    d["owners"].pop(f"{sess['chat_id']}:{sess['msg_id']}", None)
    save(d)

# =============== /start 菜单 ===============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    th = thread_kwargs(update)

    per = current_period_key(); title = period_title(per)
    text = (
        f"{mention(user)}\n"
        f"请选择要打卡的项目（仅本人可操作）👇  [{title}]  ({VERSION})\n"
        f"{KINDS['wc']['emoji']} {KINDS['wc']['label']}：{KINDS['wc']['limit']}次/班 ≤{KINDS['wc']['maxm']}分\n"
        f"{KINDS['smoke']['emoji']} {KINDS['smoke']['label']}：{KINDS['smoke']['limit']}次/班 ≤{KINDS['smoke']['maxm']}分\n"
        f"{KINDS['meal']['emoji']} {KINDS['meal']['label']}：{KINDS['meal']['limit']}次/班 ≤{KINDS['meal']['maxm']}分"
    )
    sent: Message = await safe_call(context.bot.send_message, chat_id, text, reply_markup=kb_menu(), parse_mode=ParseMode.HTML, **th)

    d = load()
    d["owners"][f"{sent.chat_id}:{sent.message_id}"] = str(user.id)
    save(d)

    try:
        if update.message:
            await safe_call(context.bot.delete_message, chat_id=chat_id, message_id=update.message.message_id, **th)
    except Exception: pass

# =============== 按钮回调 ===============
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    th = thread_kwargs(update)
    data = q.data or ""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "act":
        try: await q.answer("无效回调")
        except Exception: pass
        return
    action = parts[1]; arg = parts[2] if len(parts) >= 3 else None

    try:
        if action == "start" and arg in KINDS: await q.answer(f"开始：{KINDS[arg]['label']}")
        elif action == "back": await q.answer("收到：回来")
        else: await q.answer("处理中…")
    except Exception: pass

    u = str(q.from_user.id); chat_id = q.message.chat_id; msg_id = q.message.message_id
    if debounced(context, u, f"{chat_id}:{msg_id}:{data}"): return
    lock = get_lock(context, u)
    async with lock:
        try:
            d = load()
            owner = d["owners"].get(f"{chat_id}:{msg_id}")
            if owner and owner != u:
                try: await q.answer("这不是你的打卡卡片，不能操作。", show_alert=True)
                except Exception: pass
                return

            if action == "start" and arg in KINDS:
                await start_flow(context, q.from_user, chat_id, arg, th, menu_msg_id=msg_id)
                return
            if action == "back":
                await finish_for_user(context, q.from_user, th)
                return
        except Exception as e:
            print("[ERR on_button]", e, traceback.format_exc(limit=3))
            try: await safe_call(context.bot.send_message, chat_id, f"❌ 回调出错：{e}", **th)
            except Exception: pass

# =============== 文字触发（开始 & 回来） ===============
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    th = thread_kwargs(update)
    u = str(update.effective_user.id)
    lock = get_lock(context, u)

    if any(kw in text for kw in BACK_WORDS):
        async with lock:
            await finish_for_user(context, update.effective_user, th)
        return

    for kind_key, words in START_WORDS.items():
        if any(w in text for w in words):
            async with lock:
                await start_flow(context, update.effective_user, update.effective_chat.id, kind_key, th)
            return
    # 其他文本忽略

# =============== 其它命令 ===============
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"{v['emoji']} {v['label']}：{v['limit']}次/班 ≤{v['maxm']}分" for v in KINDS.values()]
    await update.message.reply_text(
        "当前配置（重置：07:00 / 19:00）\n" + "\n".join(lines) + f"\n超时@：@{ALERT_USERNAME}\n版本：{VERSION}"
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"pong ✅ ({VERSION})")

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"你的 user_id 是：{update.effective_user.id}")

async def post_init(app):
    cmds = [
        BotCommand("start","打开打卡菜单（仅本人）"),
        BotCommand("config","查看配置"),
        BotCommand("ping","自检"),
        BotCommand("id","查看自己的 user_id"),
    ]
    await app.bot.set_my_commands(cmds, scope=BotCommandScopeAllGroupChats())

# =============== main ===============
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("ping",   cmd_ping))
    app.add_handler(CommandHandler("id",     cmd_id))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))
    print(f"✅ 打卡机器人 {VERSION} 已启动。按 Ctrl+C 退出")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
