import json
import re
import httpx
from pathlib import Path
from urllib.parse import quote
from nonebot import on_message, on_command, get_driver
from nonebot.rule import Rule
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import MessageEvent, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.log import logger

# 导入简繁转换库
try:
    from opencc import OpenCC
except ImportError:
    raise ImportError("请先安装 opencc-python-reimplemented 库 (pip install opencc-python-reimplemented)")

# =================================================================
# Section 1: 核心配置与数据管理
# =================================================================

converter = OpenCC('t2s')
DATA_DIR = Path("data/language_tools")
DATA_DIR.mkdir(parents=True, exist_ok=True)
GROUP_SETTINGS_FILE = DATA_DIR / "group_settings.json"
USER_BLACKLIST_FILE = DATA_DIR / "user_blacklist.json"

URL_PATTERN = re.compile(r'(https?://|www\.)\S+', re.IGNORECASE)

group_settings: dict[str, dict[str, bool]] = {}
user_blacklist: set[str] = set()

def load_data():
    global group_settings, user_blacklist
    if GROUP_SETTINGS_FILE.exists():
        try:
            with open(GROUP_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                group_settings = json.load(f)
            logger.info(f"语言工具-群组设定加载成功，共 {len(group_settings)} 个。")
        except json.JSONDecodeError: group_settings = {}
    if USER_BLACKLIST_FILE.exists():
        try:
            with open(USER_BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                user_blacklist = set(json.load(f))
            logger.info(f"语言工具-用户黑名单加载成功，共 {len(user_blacklist)} 个。")
        except json.JSONDecodeError: user_blacklist = set()

def save_group_settings():
    with open(GROUP_SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(group_settings, f, ensure_ascii=False, indent=2)

def save_user_blacklist():
    with open(USER_BLACKLIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(user_blacklist), f, ensure_ascii=False, indent=2)

# =================================================================
# Section 2: 核心功能函数与规则
# =================================================================

async def _do_translation(text: str, target_lang: str = "auto") -> str | None:
    if len(text) > 200: return "文本过长，请不要超过200个字符。"
    if not text: return None
    api_url = f"https://60s.viki.moe/v2/fanyi?text={quote(text)}&from=auto&to={target_lang}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(api_url, timeout=20.0)
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") == 200 and data.get("data"):
            source = data["data"].get("source", {})
            target = data["data"].get("target", {})
            return (f"原文 ({source.get('type_desc', '未知')}):\n{source.get('text', text)}\n"
                    f"--------------------\n"
                    f"译文 ({target.get('type_desc', '未知')}):\n{target.get('text', '翻译失败')}")
        else: return None
    except Exception: return None

def is_foreign_language(text: str) -> bool:
    if len(text) <= 5: return False
    for char in text:
        if '\u3040' <= char <= '\u30FF': return True
    total_countable_chars = sum(1 for char in text if not char.isdigit())
    if total_countable_chars == 0: return False
    non_cjk_count = sum(1 for char in text if not char.isdigit() and not '\u4e00' <= char <= '\u9fa5')
    return (non_cjk_count / total_countable_chars) >= 0.8

def is_mostly_chinese(text: str) -> bool:
    if len(text) < 2: return False
    chinese_chars = sum(1 for char in text if '\u4e00' <= char <= '\u9fa5')
    return (chinese_chars / len(text)) >= 0.6

# [!! 新功能 !!] 自定义规则，用于在匹配前就忽略指令
def not_a_command_rule(event: MessageEvent) -> bool:
    return not event.get_plaintext().strip().startswith(('/', '!'))

# =================================================================
# Section 3: 统一的自动语言处理器
# =================================================================

# [!! 已修改 !!] 应用了新的规则
language_processor = on_message(rule=Rule(not_a_command_rule), priority=99, block=False)
@language_processor.handle()
async def handle_language_processing(event: MessageEvent):
    if str(event.user_id) in user_blacklist: return
    
    original_text = event.get_plaintext().strip()
    if not original_text: return

    # [!! 已修改 !!] 移除了对指令的检查，只检查网址
    if URL_PATTERN.search(original_text): return

    # --- 私聊逻辑 ---
    if not isinstance(event, GroupMessageEvent):
        if is_foreign_language(original_text):
            result = await _do_translation(original_text)
            if result: await language_processor.send(result)
            return
        converted_text = converter.convert(original_text)
        if original_text != converted_text: await language_processor.send(converted_text)
        return

    # --- 群聊逻辑 ---
    group_id = str(event.group_id)
    settings = group_settings.get(group_id, {})
    if not settings: return

    if settings.get("standard") and is_foreign_language(original_text):
        result = await _do_translation(original_text)
        if result: await language_processor.send(result)
    elif settings.get("cte") and is_mostly_chinese(original_text):
        result = await _do_translation(original_text, target_lang="en")
        if result: await language_processor.send(result)
    elif settings.get("standard"):
        converted_text = converter.convert(original_text)
        if original_text != converted_text: await language_processor.send(converted_text)

# =================================================================
# Section 4: 手动指令与管理指令
# =================================================================

manual_translator = on_command("翻译", aliases={"fy"}, priority=5, block=True)
@manual_translator.handle()
async def handle_manual_translation(event: MessageEvent, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    if event.reply: text = event.reply.message.extract_plain_text().strip()
    if not text: await manual_translator.finish("用法：/翻译 <文本> 或 回复消息后输入 /翻译")
    if URL_PATTERN.search(text):
        await manual_translator.finish("内容包含网址，为防止链接损坏，不进行翻译。")

    result = await _do_translation(text)
    if result: await manual_translator.send(result)
    else: await manual_translator.finish("翻译失败，请检查网络或稍后再试。")

lang_tools_admin = on_command("lang", aliases={"语言工具"}, permission=SUPERUSER, priority=10, block=True)
@lang_tools_admin.handle()
async def handle_lang_tools_admin(args: Message = CommandArg()):
    arg_list = args.extract_plain_text().strip().split()
    if len(arg_list) < 1:
        await lang_tools_admin.finish(
            "语言工具管理指令:\n"
            "--- 功能开关 ---\n"
            "/lang enable <模式> [群号]\n"
            "/lang disable <模式> [群号]\n"
            "可用模式: standard, cte\n"
            "--- 状态查询 ---\n"
            "/lang status [群号]\n"
            "/lang list_groups\n"
            "--- 用户黑名单 ---\n"
            "/lang add_user [QQ号]\n"
            "/lang remove_user [QQ号]\n"
            "/lang list_users"
        )
        return

    command, *params = arg_list
    command = command.lower()
    
    mode = params[0].lower() if params else None
    target_id = params[1] if len(params) > 1 and params[1].isdigit() else (params[0] if len(params) == 1 and params[0].isdigit() else None)

    if command in ['enable', 'disable'] and mode in ['standard', 'cte'] and target_id:
        settings = group_settings.setdefault(target_id, {})
        settings[mode] = (command == 'enable')
        save_group_settings()
        action = "启用" if command == 'enable' else "禁用"
        await lang_tools_admin.send(f"成功在群 {target_id} 中 {action} '{mode}' 模式。")
    elif command == 'status' and target_id:
        s = group_settings.get(target_id, {})
        msg = f"群 {target_id} 的状态：\n- Standard 模式: {'开启' if s.get('standard') else '关闭'}\n- CTE 模式: {'开启' if s.get('cte') else '关闭'}"
        await lang_tools_admin.send(msg)
    elif command == 'list_groups':
        if not group_settings: await lang_tools_admin.send("当前没有任何群组设定。")
        else:
            msg = "已设定的群组列表：\n" + "\n".join([f"- {gid}: S={'✅' if s.get('standard') else '❌'}, C={'✅' if s.get('cte') else '❌'}" for gid, s in group_settings.items()])
            await lang_tools_admin.send(msg)
    elif command == 'add_user' and target_id:
        if target_id in user_blacklist: await lang_tools_admin.finish(f"用户 {target_id} 已在黑名单中。")
        user_blacklist.add(target_id)
        save_user_blacklist()
        await lang_tools_admin.send(f"成功将用户 {target_id} 加入黑名单。")
    elif command == 'remove_user' and target_id:
        if target_id not in user_blacklist: await lang_tools_admin.finish(f"用户 {target_id} 不在黑名单中。")
        user_blacklist.remove(target_id)
        save_user_blacklist()
        await lang_tools_admin.send(f"成功将用户 {target_id} 从黑名单中移除。")
    elif command == 'list_users':
        if not user_blacklist: await lang_tools_admin.send("当前用户黑名单为空。")
        else: await lang_tools_admin.send("用户黑名单列表：\n" + "\n".join(user_blacklist))
    else:
        await lang_tools_admin.finish("参数错误或未知的子命令。")

# =================================================================
# Section 5: 启动任务
# =================================================================
driver = get_driver()
@driver.on_startup
async def _():
    load_data()