#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hotmail/Outlook 独享资产自动化产号机 - 独立 Web 控制面板 (OutlookRegister Control Panel)
提供可视化任务配置、实时进度追踪、住宅代理监控、实时控制台日志、4 段式卡密一键管理以及在线收件箱查看与验证码智能提取。
"""

import collections
import email as email_lib
import email.header as email_header
import html
import imaplib
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from data_saver import GLOBAL_TRAFFIC_METER


def check_graphical_display() -> dict[str, Any]:
    """校验当前服务进程能否连接到实际 X11/Wayland 显示服务。

    注册浏览器以 headed 模式运行；仅检查环境变量不够，因此同时尝试连接显示套接字。
    不会启动浏览器、代理或产生外网请求。
    """
    display = os.environ.get("DISPLAY", "").strip()
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "").strip()

    def can_connect_unix(path: str) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                sock.connect(path)
            return True
        except (OSError, ValueError):
            return False

    if display:
        normalized = display
        if normalized.startswith("unix/"):
            normalized = normalized[5:]
        match = re.match(r"^(?:(?P<host>[^:]+))?:(?P<number>\d+)(?:\.\d+)?$", normalized)
        if match:
            host = (match.group("host") or "").lower()
            number = int(match.group("number"))
            if not host or host in ("unix", "localhost", "127.0.0.1"):
                candidates = [f"/tmp/.X11-unix/X{number}", f"/run/X11/X{number}"]
                if any(can_connect_unix(path) for path in candidates):
                    return {"available": True, "backend": "X11", "display": display, "message": "检测到可连接的 X11 显示服务。"}
                # Linux X11 也可能只暴露 abstract namespace socket。
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                        sock.settimeout(0.5)
                        sock.connect(f"\0/tmp/.X11-unix/X{number}")
                    return {"available": True, "backend": "X11", "display": display, "message": "检测到可连接的 X11 显示服务。"}
                except OSError:
                    pass
            elif host and host != "":
                try:
                    with socket.create_connection((host, 6000 + number), timeout=0.5):
                        return {"available": True, "backend": "X11", "display": display, "message": "检测到可连接的远程 X11 显示服务。"}
                except OSError:
                    pass

    if wayland_display:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        wayland_path = wayland_display if os.path.isabs(wayland_display) else os.path.join(runtime_dir, wayland_display)
        if runtime_dir and can_connect_unix(wayland_path):
            return {"available": True, "backend": "Wayland", "display": wayland_display, "message": "检测到可连接的 Wayland 显示服务。"}

    return {
        "available": False,
        "backend": "none",
        "display": display or wayland_display,
        "message": "当前服务进程无法连接图形显示服务（X11/Wayland）。任务已阻止，未启动住宅代理。请在有可用桌面显示会话的环境中启动面板，再重试。",
    }


# 将当前目录加入模块查找路径
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

RESULTS_DIR = os.path.join(CURRENT_DIR, "Results")
os.makedirs(RESULTS_DIR, exist_ok=True)
CARDS_FILE = os.path.join(RESULTS_DIR, "hotmail_4part_cards.txt")
RECOVERY_FILE = os.path.join(RESULTS_DIR, "recovery_pool.txt")
SECURED_FILE = os.path.join(RESULTS_DIR, "secured_accounts.txt")

# 全局任务状态管理器
class TaskManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.is_running = False
        self.stop_requested = False
        self.target_count = 0
        self.succeeded_count = 0
        self.failed_count = 0
        self.current_attempt = 0
        self.start_time = 0.0
        self.current_node: dict[str, Any] = {}
        self.logs: collections.deque = collections.deque(maxlen=400)
        self.listeners: list[queue.Queue] = []
        self.worker_thread: threading.Thread | None = None
        self.bridge_instance = None

    def add_log(self, level: str, message: str, meta: dict | None = None):
        with self.lock:
            ts = time.strftime("%H:%M:%S")
            entry = {
                "id": f"{int(time.time()*1000)}_{len(self.logs)}",
                "time": ts,
                "level": level,  # info, success, warn, error
                "message": message,
                "meta": meta or {},
            }
            self.logs.append(entry)
            dead_queues = []
            for q in self.listeners:
                try:
                    q.put_nowait({"type": "log", "data": entry})
                except Exception:
                    dead_queues.append(q)
            for dq in dead_queues:
                if dq in self.listeners:
                    self.listeners.remove(dq)

    def broadcast_state(self):
        state = self.get_summary()
        with self.lock:
            dead_queues = []
            for q in self.listeners:
                try:
                    q.put_nowait({"type": "state", "data": state})
                except Exception:
                    dead_queues.append(q)
            for dq in dead_queues:
                if dq in self.listeners:
                    self.listeners.remove(dq)

    def get_summary(self) -> dict[str, Any]:
        with self.lock:
            total_cards = 0
            if os.path.exists(CARDS_FILE):
                try:
                    with open(CARDS_FILE, "r", encoding="utf-8") as f:
                        total_cards = sum(1 for line in f if "----" in line)
                except Exception:
                    pass

            elapsed = int(time.time() - self.start_time) if self.is_running and self.start_time > 0 else 0
            return {
                "is_running": self.is_running,
                "stop_requested": self.stop_requested,
                "target_count": self.target_count,
                "succeeded_count": self.succeeded_count,
                "failed_count": self.failed_count,
                "current_attempt": self.current_attempt,
                "elapsed_seconds": elapsed,
                "current_node": self.current_node,
                "total_cards_in_db": total_cards,
                "traffic_stats": GLOBAL_TRAFFIC_METER.get_summary(),
            }

TASK_MGR = TaskManager()


# ============================================================
# 邮件编解码与验证码智能提取模块
# ============================================================

def _decode_mime_header(val: str | None) -> str:
    if not val:
        return ""
    decoded = []
    try:
        for text_bytes, enc in email_header.decode_header(val):
            if isinstance(text_bytes, bytes):
                try:
                    decoded.append(text_bytes.decode(enc or "utf-8", errors="backslashreplace"))
                except Exception:
                    decoded.append(text_bytes.decode("utf-8", errors="replace"))
            else:
                decoded.append(str(text_bytes))
        return "".join(decoded).strip()
    except Exception:
        return str(val)


def _format_date_short(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        parsed = email_lib.utils.parsedate_to_datetime(date_str)
        return parsed.strftime("%m-%d %H:%M")
    except Exception:
        return date_str[:16]


def _extract_email_body(msg: email_lib.message.Message) -> tuple[str, list]:
    body = ""
    attachments = []
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                fname = part.get_filename()
                if fname:
                    attachments.append((_decode_mime_header(fname), ctype))
                    continue
                if "attachment" in disp:
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="replace")
                except Exception:
                    text = payload.decode("utf-8", errors="replace")
                if ctype == "text/plain" and not body:
                    body = text
                elif ctype == "text/html" and not body:
                    cleaned = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text)
                    cleaned = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", cleaned)
                    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
                    body = html.unescape(cleaned)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    body = payload.decode(charset, errors="replace")
                except Exception:
                    body = payload.decode("utf-8", errors="replace")
                if msg.get_content_type() == "text/html":
                    cleaned = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", body)
                    cleaned = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", cleaned)
                    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
                    body = html.unescape(cleaned)
    except Exception as e:
        body = f"(解析邮件正文异常: {e})"
    body = re.sub(r"\s+", " ", body).strip()
    return body, attachments


def extract_verification_code(subject: str, body: str, sender: str) -> tuple[str, str]:
    """
    智能提取验证码及所属平台标签
    返回: (code, platform_name)
    """
    text = f"{subject}\n{body}"
    sender_low = sender.lower()

    # 1. Grok / SpaceXAI / x.ai
    if "x.ai" in sender_low or "spacexai" in sender_low or "grok" in text.lower():
        m = re.search(r"(?:code|code:)\s*[:：]?\s*([0-9]{3}-[0-9]{3})", text, re.IGNORECASE)
        if m:
            return m.group(1), "xAI / Grok"
        m2 = re.search(r"\b([0-9]{6})\b", text)
        if m2:
            return m2.group(1), "xAI / Grok"

    # 2. OpenAI / ChatGPT
    if "openai" in sender_low or "chatgpt" in text.lower():
        m = re.search(r"(?:verification code|login code|code is|code:)\s*[:：]?\s*([0-9]{6})", text, re.IGNORECASE)
        if m:
            return m.group(1), "OpenAI / ChatGPT"
        m2 = re.search(r"\b([0-9]{6})\b", text)
        if m2:
            return m2.group(1), "OpenAI / ChatGPT"

    # 2. Anthropic / Claude
    if "anthropic" in sender_low or "claude" in text.lower():
        m = re.search(r"(?:code|verification)\s*[:：]?\s*([0-9]{6})", text, re.IGNORECASE)
        if m:
            return m.group(1), "Anthropic Claude"
        m2 = re.search(r"\b([0-9]{6})\b", text)
        if m2:
            return m2.group(1), "Anthropic Claude"

    # 3. Cursor
    if "cursor" in sender_low or "cursor" in text.lower():
        m = re.search(r"(?:one-time code is|code:)\s*[:：]?\s*([0-9]{6})", text, re.IGNORECASE)
        if m:
            return m.group(1), "Cursor"
        m2 = re.search(r"\b([0-9]{6})\b", text)
        if m2:
            return m2.group(1), "Cursor"

    # 4. Microsoft
    if "microsoft" in sender_low or "accountprotection" in sender_low or "微软" in text:
        m = re.search(r"(?:安全代码|security code|代码)\s*[:：]?\s*([0-9]{6,7})", text, re.IGNORECASE)
        if m:
            return m.group(1), "Microsoft"

    # 5. Twitter / X
    if "x.com" in sender_low or "twitter" in sender_low:
        m = re.search(r"\b([0-9]{6})\b", text)
        if m:
            return m.group(1), "X (Twitter)"

    # 6. 通用常见匹配
    patterns = [
        (r"(?:验证码|动态码|校验码|安全码|verification code|security code|confirm code|pin code)\s*[:：\s]\s*([0-9a-zA-Z]{4,8})\b", "动态验证码"),
        (r"(?:code is|code:|is your code)\s*[:：\s]\s*([0-9]{4,8})\b", "通用验证码"),
        (r"【([0-9]{4,8})】", "短信/验证码"),
        (r"\b([0-9]{6})\b", "6位数字验证码"),
    ]
    for pat, label in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1)
            if val not in ("2024", "2025", "2026", "2027"):
                return val, label

    return "", ""


def _update_card_refresh_token(target_email: str, new_rt: str):
    """当微软 OAuth2 发生令牌轮转时，自动写回卡密文件"""
    if not os.path.exists(CARDS_FILE) or not new_rt:
        return
    try:
        with open(CARDS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            parts = line.strip().split("----")
            if parts and parts[0].strip().lower() == target_email.strip().lower() and len(parts) >= 4:
                parts[3] = new_rt
                new_lines.append("----".join(parts) + "\n")
            else:
                new_lines.append(line)
        with open(CARDS_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception:
        pass


def fetch_mailbox_messages_for_account(email: str, max_fetch: int = 15) -> dict[str, Any]:
    """读取指定 Hotmail 邮箱收件箱邮件与验证码"""
    account_info = None
    if os.path.exists(CARDS_FILE):
        with open(CARDS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "----" not in line:
                    continue
                parts = line.split("----")
                if parts[0].strip().lower() == email.strip().lower():
                    account_info = {
                        "email": parts[0].strip(),
                        "password": parts[1].strip() if len(parts) > 1 else "",
                        "client_id": parts[2].strip() if len(parts) > 2 else "",
                        "refresh_token": parts[3].strip() if len(parts) > 3 else "",
                    }
                    break

    if not account_info:
        return {"ok": False, "error": f"未在卡密库中找到该邮箱凭据: {email}"}

    client_id = account_info["client_id"]
    refresh_token = account_info["refresh_token"]

    access_token = None
    oauth_err = ""
    if client_id and refresh_token:
        ep = "https://login.live.com/oauth20_token.srf"
        data = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        try:
            req = urllib.request.Request(
                ep,
                data=urllib.parse.urlencode(data).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
                access_token = parsed.get("access_token")
                new_rt = parsed.get("refresh_token")
                if new_rt and new_rt != refresh_token:
                    _update_card_refresh_token(email, new_rt)
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="replace")
            oauth_err = f"HTTP {he.code}: {err_body[:100]}"
        except Exception as exc:
            oauth_err = str(exc)

    if not access_token:
        return {
            "ok": False,
            "error": f"换取 OAuth2 令牌失败: {oauth_err} (若刚导入请点击资产工具箱补签协议)",
            "is_auth_error": True,
        }

    messages = []
    try:
        mail = imaplib.IMAP4_SSL("outlook.office365.com", 993, timeout=15)
        auth_str = f"user={email}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
        mail.authenticate("XOAUTH2", lambda _: auth_str)
        status, cnt_data = mail.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP select INBOX 失败: {status}")

        total = int(cnt_data[0].decode()) if cnt_data and cnt_data[0] else 0
        if total <= 0:
            mail.logout()
            return {
                "ok": True,
                "email": email,
                "total_msgs": 0,
                "messages": [],
                "latest_code": "",
                "latest_platform": "",
            }

        start_seq = max(1, total - max_fetch + 1)
        st, fetch_data = mail.fetch(f"{start_seq}:{total}", "(RFC822)")
        if st == "OK" and fetch_data:
            for part in reversed(fetch_data):
                if not (isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes)):
                    continue
                msg = email_lib.message_from_bytes(part[1])
                subj = _decode_mime_header(msg.get("Subject"))
                sender = _decode_mime_header(msg.get("From"))
                dt_str = _format_date_short(msg.get("Date") or "")
                body_text, _ = _extract_email_body(msg)
                code, platform = extract_verification_code(subj, body_text, sender)
                messages.append({
                    "date": dt_str,
                    "from": sender,
                    "subject": subj,
                    "snippet": body_text[:240],
                    "body": body_text[:2000],
                    "extracted_code": code,
                    "platform": platform,
                })
        mail.logout()

        latest_code = ""
        latest_platform = ""
        for m in messages:
            if m.get("extracted_code"):
                latest_code = m["extracted_code"]
                latest_platform = m.get("platform", "")
                break

        return {
            "ok": True,
            "email": email,
            "total_msgs": total,
            "messages": messages,
            "latest_code": latest_code,
            "latest_platform": latest_platform,
        }
    except Exception as e:
        err_str = str(e)
        is_not_connected = "User is authenticated but not connected" in err_str
        return {
            "ok": False,
            "error": err_str,
            "is_not_connected": is_not_connected,
            "hint": "新账号处于微软 Exchange 外部 IMAP 12-24 小时冷冻期，建议使用账密登录网页端 (outlook.live.com) 或静置后自动开放。" if is_not_connected else "",
        }


# ============================================================
# 后台自动化产号与修复调度逻辑
# ============================================================

def _run_registration_worker(cfg: dict[str, Any]):
    # 第二道保险：线程入口再次预检，必须早于代理桥初始化/取节点。
    display_check = check_graphical_display()
    if not display_check["available"]:
        TASK_MGR.add_log("error", f"🛑 注册任务未启动：{display_check['message']}")
        with TASK_MGR.lock:
            TASK_MGR.is_running = False
            TASK_MGR.stop_requested = False
        TASK_MGR.broadcast_state()
        return

    from novproxy_bridge import (
        NovProxyBridge,
        cleanup_leftover_browsers,
        fetch_verified_novproxy_nodes,
        load_used_ips,
        mark_ip_used,
    )
    from controllers.patchright_controller import PatchrightController
    from main import process_single_flow

    target_count = int(cfg.get("target_count", 5))
    use_novproxy = bool(cfg.get("use_novproxy", True))
    enable_data_saver = bool(cfg.get("enable_data_saver", True))
    regions = cfg.get("regions") or ["TW", "JP", "KR", "DE", "CA", "FR", "AU", "SG", "GB", "NL"]
    email_suffix = str(cfg.get("email_suffix", "@hotmail.com")).strip()

    TASK_MGR.add_log(
        "info",
        f"🚀 产号任务正式启动! 计划产出: {target_count} 个 | 邮箱后缀: {email_suffix} | 极速省流量模式: {'⚡ 已启用(节约75%+)' if enable_data_saver else '已关闭'}"
    )
    cleanup_leftover_browsers()

    proxy_url = ""
    bridge = None

    if use_novproxy:
        try:
            bridge = NovProxyBridge()
            proxy_url = bridge.start()
            TASK_MGR.bridge_instance = bridge
            TASK_MGR.add_log("info", f"✅ NovProxy 住宅代理桥接已就绪，端口分配在 {proxy_url}")
        except Exception as e:
            TASK_MGR.add_log("error", f"❌ 启动 NovProxy 代理桥接失败: {e}")
            with TASK_MGR.lock:
                TASK_MGR.is_running = False
            TASK_MGR.broadcast_state()
            return
    else:
        proxy_url = str(cfg.get("custom_proxy", "")).strip()
        TASK_MGR.add_log("info", f"ℹ️ 使用固定/自定义代理: {proxy_url or '本机直连'}")

    batch_round = 0
    while not TASK_MGR.stop_requested and TASK_MGR.succeeded_count < target_count and batch_round < 30:
        batch_round += 1
        nodes = []
        if use_novproxy and bridge:
            TASK_MGR.add_log("info", f"🌐 [第 {batch_round} 轮] 正在获取并校验全球优质无污染住宅 IP 节点池...")
            TASK_MGR.broadcast_state()
            nodes = fetch_verified_novproxy_nodes(regions=tuple(regions), per_region=3)
            TASK_MGR.add_log("info", f"⚡ 成功获取并就绪 {len(nodes)} 个高匿名原生住宅节点")
            if not nodes:
                time.sleep(4)
                continue
        else:
            nodes = [{"node": "custom", "exit_ip": "custom", "country": "CUSTOM", "isp": "Custom Proxy", "timezone": "UTC"}]

        for node_info in nodes:
            if TASK_MGR.stop_requested or TASK_MGR.succeeded_count >= target_count:
                break

            with TASK_MGR.lock:
                TASK_MGR.current_attempt += 1
                TASK_MGR.current_node = node_info
            TASK_MGR.broadcast_state()

            exit_ip = node_info.get("exit_ip", "unknown")
            country = node_info.get("country", "")
            isp = node_info.get("isp", "")
            node_key = node_info.get("node", "")

            if use_novproxy and bridge and exit_ip != "unknown":
                mark_ip_used(exit_ip)
                bridge.set_novproxy_node(node_key)

            TASK_MGR.add_log(
                "info",
                f"🎯 尝试 #{TASK_MGR.current_attempt} | 切换节点: {country} {exit_ip} ({isp})",
                meta={"ip": exit_ip, "country": country},
            )

            ctrl = PatchrightController()
            ctrl.proxy = proxy_url
            ctrl.email_suffix = email_suffix
            ctrl.current_timezone = node_info.get("timezone", "UTC")
            ctrl.enable_data_saver = enable_data_saver

            attempt_ok = False
            try:
                attempt_ok = process_single_flow(ctrl)
            except Exception as exc:
                TASK_MGR.add_log("warn", f"⚠️ 尝试 #{TASK_MGR.current_attempt} 发生异常中断: {exc}")

            if attempt_ok:
                with TASK_MGR.lock:
                    TASK_MGR.succeeded_count += 1
                TASK_MGR.add_log(
                    "success",
                    f"🎉 [产号成功] 已达成 +{TASK_MGR.succeeded_count}/{target_count} 个独享卡密！已自动完成协议签署与邮箱挂载！",
                )
            else:
                with TASK_MGR.lock:
                    TASK_MGR.failed_count += 1
                TASK_MGR.add_log("warn", f"[-] 尝试 #{TASK_MGR.current_attempt} 未能完成产号，自动流转至下一住宅节点。")

            TASK_MGR.broadcast_state()
            time.sleep(1.5)

    if bridge:
        try:
            bridge.stop()
        except Exception:
            pass
        TASK_MGR.bridge_instance = None

    cleanup_leftover_browsers()
    with TASK_MGR.lock:
        final_succ = TASK_MGR.succeeded_count
        TASK_MGR.is_running = False
        TASK_MGR.stop_requested = False
    TASK_MGR.add_log("success" if final_succ >= target_count else "info", f"🏁 产号调度流程结束。本次累计产出: {final_succ} 个，失败: {TASK_MGR.failed_count} 次。")
    TASK_MGR.broadcast_state()


def _run_repair_worker():
    from account_repair import get_accounts_to_repair, repair_single_account
    from patchright.sync_api import sync_playwright

    accounts_to_repair = get_accounts_to_repair(CARDS_FILE)
    if not accounts_to_repair:
        TASK_MGR.add_log("warn", "⚠️ 卡密库为空，无可供修复的账号")
        return

    TASK_MGR.add_log("info", f"🛠️ 开始对存量自建账号执行自动化协议补签与收件箱激活 (共 {len(accounts_to_repair)} 个)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist"],
        )
        recovered = 0
        for email, pwd in accounts_to_repair:
            TASK_MGR.add_log("info", f"正在检查并激活: {email} ...")
            res = repair_single_account(browser, email, pwd)
            st = res.get("status")
            if st in ("success", "imap_pending") and "card_4part" in res:
                recovered += 1
                TASK_MGR.add_log("success", f"✅ {email} 成功补签隐私协议，完成 Mailbox 挂载！")
            else:
                TASK_MGR.add_log("warn", f"❌ {email} 无法激活: {res.get('msg')}")
            time.sleep(1)
        browser.close()

    TASK_MGR.add_log("success", f"🎉 存量账号修复结束，共成功激活 {recovered} 个账号！")
    TASK_MGR.broadcast_state()


class PanelHTTPHandler(BaseHTTPRequestHandler):
    server_version = "OutlookRegisterPanel/2.1"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            html_content = DASHBOARD_HTML_TEMPLATE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_content)))
            self.end_headers()
            self.wfile.write(html_content)
            return

        if path == "/api/status":
            self._send_json({"ok": True, "data": TASK_MGR.get_summary()})
            return

        if path == "/api/logs":
            with TASK_MGR.lock:
                logs_list = list(TASK_MGR.logs)
            self._send_json({"ok": True, "logs": logs_list})
            return

        # SSE 实时日志流
        if path == "/api/logs/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q: queue.Queue = queue.Queue(maxsize=100)
            with TASK_MGR.lock:
                TASK_MGR.listeners.append(q)
                replay = list(TASK_MGR.logs)[-25:]

            for r in replay:
                msg = f"event: log\ndata: {json.dumps(r, ensure_ascii=False)}\n\n"
                try:
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    return

            try:
                while True:
                    item = q.get()
                    ev_type = item.get("type", "log")
                    payload = json.dumps(item.get("data", {}), ensure_ascii=False)
                    msg = f"event: {ev_type}\ndata: {payload}\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
            except Exception:
                with TASK_MGR.lock:
                    if q in TASK_MGR.listeners:
                        TASK_MGR.listeners.remove(q)
            return

        if path == "/api/cards":
            cards = []
            if os.path.exists(CARDS_FILE):
                with open(CARDS_FILE, "r", encoding="utf-8") as f:
                    for idx, line in enumerate(f, 1):
                        line = line.strip()
                        if "----" in line:
                            parts = line.split("----")
                            cards.append({
                                "id": idx,
                                "raw": line,
                                "email": parts[0] if len(parts) > 0 else "",
                                "password": parts[1] if len(parts) > 1 else "",
                                "client_id": parts[2] if len(parts) > 2 else "",
                                "refresh_token": parts[3] if len(parts) > 3 else "",
                            })
            self._send_json({"ok": True, "count": len(cards), "cards": cards})
            return

        # 获取指定邮箱的邮件列表与最新提取验证码
        if path == "/api/mailbox/messages":
            qs = urllib.parse.parse_qs(parsed.query)
            email_addr = (qs.get("email") or [""])[0].strip()
            if not email_addr:
                self._send_json({"ok": False, "error": "缺少 email 参数"}, 400)
                return
            res = fetch_mailbox_messages_for_account(email_addr)
            self._send_json(res)
            return

        if path == "/api/export":
            if os.path.exists(CARDS_FILE):
                with open(CARDS_FILE, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="hotmail_4part_cards.txt"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send_json({"error": "卡密文件尚不存在"}, 404)
            return

        self._send_json({"error": f"Not found: {path}"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = {}
        try:
            clen = int(self.headers.get("Content-Length", 0))
            if clen > 0:
                raw_body = self.rfile.read(clen).decode("utf-8")
                body = json.loads(raw_body)
        except Exception:
            body = {}

        if path == "/api/start":
            # 前置检查必须发生在状态变更、工作线程和住宅代理初始化之前。
            display_check = check_graphical_display()
            if not display_check["available"]:
                message = display_check["message"]
                TASK_MGR.add_log("error", f"🛑 拒绝启动注册任务：{message}")
                TASK_MGR.broadcast_state()
                self._send_json({
                    "ok": False,
                    "error": message,
                    "code": "DISPLAY_UNAVAILABLE",
                    "display_check": display_check,
                }, 409)
                return

            with TASK_MGR.lock:
                if TASK_MGR.is_running:
                    self._send_json({"ok": False, "error": "当前已有任务正在运行中，请勿重复启动"})
                    return
                TASK_MGR.is_running = True
                TASK_MGR.stop_requested = False
                TASK_MGR.target_count = int(body.get("target_count", 5))
                TASK_MGR.succeeded_count = 0
                TASK_MGR.failed_count = 0
                TASK_MGR.current_attempt = 0
                TASK_MGR.start_time = time.time()
                TASK_MGR.logs.clear()

            t = threading.Thread(target=_run_registration_worker, args=(body,), daemon=True)
            TASK_MGR.worker_thread = t
            t.start()
            TASK_MGR.broadcast_state()
            self._send_json({"ok": True, "message": f"成功启动产号任务，目标达成 {TASK_MGR.target_count} 个"})
            return

        if path == "/api/stop":
            with TASK_MGR.lock:
                if not TASK_MGR.is_running:
                    self._send_json({"ok": False, "error": "当前没有正在执行的任务"})
                    return
                TASK_MGR.stop_requested = True
            TASK_MGR.add_log("warn", "⏹ 用户请求中止产号任务，正在等待当前浏览器生命周期安全退出...")
            TASK_MGR.broadcast_state()
            self._send_json({"ok": True, "message": "已发送中止信号，正在优雅收尾"})
            return

        if path == "/api/repair-unactivated":
            t = threading.Thread(target=_run_repair_worker, daemon=True)
            t.start()
            self._send_json({"ok": True, "message": "已在后台启动存量账号协议补签与激活流程"})
            return

        self._send_json({"error": f"Not found: {path}"}, 404)


# ============================================================
# 现代化极简深色 Web UI (含在线收件箱与验证码实时提取抽屉)
# ============================================================

DASHBOARD_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>OutlookRegister 独享邮箱产号中枢面板</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: { 50: '#eff6ff', 500: '#3b82f6', 600: '#2563eb', 700: '#1d4ed8' },
            darkbg: '#0B0F19',
            cardbg: '#111827',
            cardborder: '#1F2937'
          }
        }
      }
    }
  </script>
  <style>
    @keyframes pulse-subtle { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
    .animate-pulse-subtle { animation: pulse-subtle 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
    .terminal-scroll::-webkit-scrollbar, .drawer-scroll::-webkit-scrollbar { width: 6px; }
    .terminal-scroll::-webkit-scrollbar-track, .drawer-scroll::-webkit-scrollbar-track { background: #0B0F19; }
    .terminal-scroll::-webkit-scrollbar-thumb, .drawer-scroll::-webkit-scrollbar-thumb { background: #374151; border-radius: 3px; }
  </style>
</head>
<body class="bg-darkbg text-slate-100 min-h-screen font-sans antialiased flex flex-col selection:bg-brand-500 selection:text-white relative overflow-x-hidden">

  <!-- 顶部导航栏 -->
  <header class="border-b border-cardborder bg-cardbg/80 backdrop-blur-md sticky top-0 z-40 px-6 py-4 flex items-center justify-between">
    <div class="flex items-center space-x-3">
      <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-brand-600 to-indigo-500 flex items-center justify-center shadow-lg shadow-brand-500/20">
        <svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path>
        </svg>
      </div>
      <div>
        <h1 class="text-lg font-bold tracking-tight text-white flex items-center gap-2">
          OutlookRegister <span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-brand-500/10 text-brand-400 border border-brand-500/20">Control Center</span>
        </h1>
        <p class="text-xs text-slate-400">自建独享 Hotmail 自动化产号 · 在线收件箱接码 · 4段卡密库</p>
      </div>
    </div>

    <!-- 状态指示灯 -->
    <div class="flex items-center space-x-4">
      <div id="statusBadge" class="flex items-center space-x-2 px-3 py-1.5 rounded-full border bg-slate-800/80 border-slate-700 text-xs">
        <span id="statusDot" class="w-2.5 h-2.5 rounded-full bg-slate-400"></span>
        <span id="statusText" class="font-medium text-slate-300">系统空闲</span>
      </div>
      <button onclick="refreshAll()" class="p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition" title="刷新状态">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
      </button>
    </div>
  </header>

  <!-- 主体工作区 -->
  <main class="flex-1 p-6 max-w-7xl w-full mx-auto space-y-6">

    <!-- 第一排：实时指标统计卡片 (5 列自适应网格) -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
      <div class="bg-cardbg border border-cardborder rounded-2xl p-5 shadow-sm">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-1">
          <span>存量有效卡密总库</span>
          <svg class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
        </div>
        <div class="text-2xl font-bold text-white" id="statTotalCards">0 <span class="text-xs text-slate-500 font-normal">个账号</span></div>
        <p class="text-xs text-emerald-400/80 mt-1 flex items-center gap-1">4段式卡密就绪</p>
      </div>

      <div class="bg-cardbg border border-cardborder rounded-2xl p-5 shadow-sm">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-1">
          <span>本次任务产出目标</span>
          <svg class="w-4 h-4 text-brand-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
        </div>
        <div class="text-2xl font-bold text-brand-400" id="statTargetProgress">0 / 0</div>
        <p class="text-xs text-slate-400 mt-1" id="statElapsed">耗时: 00:00:00</p>
      </div>

      <div class="bg-cardbg border border-cardborder rounded-2xl p-5 shadow-sm">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-1">
          <span>成功 / 失败总试次</span>
          <svg class="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
        </div>
        <div class="text-2xl font-bold text-white flex items-center gap-2">
          <span class="text-emerald-400" id="statSucceeded">0</span>
          <span class="text-slate-600">/</span>
          <span class="text-rose-400" id="statFailed">0</span>
        </div>
        <p class="text-xs text-slate-400 mt-1" id="statAttempts">总尝试轮次: 0 次</p>
      </div>

      <div class="bg-cardbg border border-cardborder rounded-2xl p-5 shadow-sm">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-1">
          <span>当前出口住宅 IP</span>
          <svg class="w-4 h-4 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
        </div>
        <div class="text-lg font-bold text-indigo-300 truncate" id="statNodeIp">等待就绪</div>
        <p class="text-xs text-slate-400 mt-1 truncate" id="statNodeDetail">NovProxy 住宅网关待命中</p>
      </div>

      <div class="bg-cardbg border border-cardborder rounded-2xl p-5 shadow-sm">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-1">
          <span>住宅流量已节约</span>
          <svg class="w-4 h-4 text-teal-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
        </div>
        <div class="text-2xl font-bold text-teal-400" id="statSavedTraffic">~0.0 <span class="text-xs text-slate-500 font-normal">MB</span></div>
        <p class="text-xs text-slate-400 mt-1 truncate" id="statBlockedReqs">已拦截 0 次冗余请求</p>
      </div>
    </div>

    <!-- 第二排：左侧控制台配置 + 右侧实时滚屏日志流 -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">

      <!-- 左侧：参数配置与控制 (4列) -->
      <div class="lg:col-span-4 space-y-6">
        <div class="bg-cardbg border border-cardborder rounded-2xl p-5 shadow-sm space-y-4">
          <h2 class="text-sm font-bold text-white flex items-center gap-2 border-b border-cardborder pb-3">
            <svg class="w-4 h-4 text-brand-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"></path></svg>
            产号任务参数配置
          </h2>

          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">目标注册产出数</label>
            <input type="number" id="inputTargetCount" value="5" min="1" max="100" class="w-full bg-darkbg border border-cardborder rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500 transition">
            <p class="text-[11px] text-slate-500 mt-1">达成指定数量的有效 4 段式卡密后自动安全收尾</p>
          </div>

          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">邮箱域名后缀</label>
            <select id="inputSuffix" class="w-full bg-darkbg border border-cardborder rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500 transition">
              <option value="@hotmail.com">@hotmail.com (推荐 · 兼容度最高)</option>
              <option value="@outlook.com">@outlook.com</option>
            </select>
          </div>

          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">代理网络策略</label>
            <select id="inputProxyMode" onchange="toggleProxyInput()" class="w-full bg-darkbg border border-cardborder rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-500 transition">
              <option value="novproxy">NovProxy 自动全球住宅 IP 轮换 (推荐)</option>
              <option value="custom">指定代理 / 本机网络直连</option>
            </select>
          </div>

          <div id="customProxyWrap" class="hidden">
            <label class="block text-xs font-medium text-slate-300 mb-1">自定义代理 URL</label>
            <input type="text" id="inputCustomProxy" placeholder="http://user:pass@ip:port 或 socks5://..." class="w-full bg-darkbg border border-cardborder rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-brand-500 transition">
          </div>

          <!-- 极速省流量模式 Toggle 开关 -->
          <div class="p-3 rounded-xl bg-darkbg border border-cardborder flex items-center justify-between">
            <div class="pr-2">
              <label for="inputDataSaver" class="text-xs font-semibold text-white flex items-center gap-1.5 cursor-pointer">
                <span>⚡ 极速省流量优化</span>
                <span class="text-[10px] px-1.5 py-0.2 rounded bg-teal-500/10 text-teal-300 border border-teal-500/30 font-medium">省75%+</span>
              </label>
              <p class="text-[11px] text-slate-400 mt-0.5">拦截字体/视频/Aria遥测，智能放行验证码</p>
            </div>
            <label class="relative inline-flex items-center cursor-pointer shrink-0">
              <input type="checkbox" id="inputDataSaver" checked class="sr-only peer">
              <div class="w-9 h-5 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-teal-500"></div>
            </label>
          </div>

          <div class="pt-2 flex flex-col gap-2">
            <button id="btnStart" onclick="startTask()" class="w-full py-2.5 px-4 rounded-xl bg-gradient-to-r from-brand-600 to-indigo-600 hover:from-brand-500 hover:to-indigo-500 text-white font-semibold text-sm shadow-lg shadow-brand-500/20 transition flex items-center justify-center gap-2">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
              启动自动化产号
            </button>
            <button id="btnStop" onclick="stopTask()" disabled class="w-full py-2.5 px-4 rounded-xl bg-slate-800 hover:bg-rose-900/60 text-slate-400 hover:text-rose-200 border border-cardborder font-medium text-sm transition flex items-center justify-center gap-2 disabled:opacity-40 disabled:pointer-events-none">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z"></path></svg>
              紧急停止任务
            </button>
          </div>
        </div>

        <!-- 辅助工具箱 -->
        <div class="bg-cardbg border border-cardborder rounded-2xl p-5 shadow-sm space-y-3">
          <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider">资产工具箱</h3>
          <div class="flex flex-col gap-2">
            <button onclick="repairUnactivated()" class="w-full py-2 px-3 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs text-slate-200 border border-slate-700 transition flex items-center justify-between">
              <span>🛠️ 一键补签存量账号隐私协议</span>
              <span class="text-[10px] text-amber-400">解除未激活</span>
            </button>
            <a href="/api/export" target="_blank" class="w-full py-2 px-3 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs text-slate-200 border border-slate-700 transition flex items-center justify-between">
              <span>💾 导出全部 4 段式卡密 (.txt)</span>
              <span class="text-[10px] text-brand-400">下载</span>
            </a>
          </div>
        </div>
      </div>

      <!-- 右侧：实时终端滚屏日志流 (8列) -->
      <div class="lg:col-span-8 flex flex-col">
        <div class="bg-cardbg border border-cardborder rounded-2xl flex-1 flex flex-col overflow-hidden shadow-sm">
          <div class="px-4 py-3 border-b border-cardborder bg-slate-900/60 flex items-center justify-between">
            <div class="flex items-center space-x-2">
              <span class="w-3 h-3 rounded-full bg-rose-500/80 inline-block"></span>
              <span class="w-3 h-3 rounded-full bg-amber-500/80 inline-block"></span>
              <span class="w-3 h-3 rounded-full bg-emerald-500/80 inline-block"></span>
              <span class="text-xs font-mono text-slate-400 ml-2">Console Live Log</span>
            </div>
            <div class="flex items-center space-x-2 text-xs">
              <button onclick="clearLogsUI()" class="text-slate-400 hover:text-white transition">清空屏幕</button>
              <span class="text-slate-600">|</span>
              <span id="sseIndicator" class="text-emerald-400 font-mono text-[11px] flex items-center gap-1">
                <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping"></span> Live SSE
              </span>
            </div>
          </div>

          <div id="terminalBody" class="p-4 flex-1 bg-darkbg font-mono text-xs overflow-y-auto terminal-scroll max-h-[460px] min-h-[420px] space-y-1.5 selection:bg-brand-500">
            <div class="text-slate-500 italic">正在连接控制台实时日志流...</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 第三排：卡密资产库卡片列表 -->
    <div class="bg-cardbg border border-cardborder rounded-2xl p-5 shadow-sm space-y-4">
      <div class="flex items-center justify-between border-b border-cardborder pb-4">
        <div>
          <h2 class="text-base font-bold text-white flex items-center gap-2">
            <span>📦 独享 Hotmail 4段式卡密库</span>
            <span id="cardsCountBadge" class="text-xs font-medium px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">0 份</span>
          </h2>
          <p class="text-xs text-slate-400 mt-0.5">点击每个账号右侧的【📬 收件箱】，可在当前面板直接提取该邮箱收到的验证码！</p>
        </div>
        <div class="flex items-center gap-2">
          <button onclick="copyAllCards()" class="px-3 py-1.5 rounded-lg bg-brand-600 hover:bg-brand-500 text-xs font-semibold text-white transition flex items-center gap-1 shadow-sm">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
            一键复制全部卡密
          </button>
          <button onclick="loadCards()" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 border border-slate-700 transition">
            刷新列表
          </button>
        </div>
      </div>

      <!-- 卡密数据表格 -->
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead class="text-slate-400 border-b border-cardborder bg-slate-900/40">
            <tr>
              <th class="py-2.5 px-3">#</th>
              <th class="py-2.5 px-3">邮箱地址</th>
              <th class="py-2.5 px-3">密码</th>
              <th class="py-2.5 px-3">Client ID</th>
              <th class="py-2.5 px-3">Refresh Token 凭证</th>
              <th class="py-2.5 px-3 text-right">在线功能与操作</th>
            </tr>
          </thead>
          <tbody id="cardsTableBody" class="divide-y divide-cardborder text-slate-300 font-mono">
            <tr><td colspan="6" class="text-center py-6 text-slate-500">正在读取卡密库...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

  </main>

  <!-- ============================================================ -->
  <!-- 在线收件箱与验证码实时提取抽屉组件 (Mailbox Drawer) -->
  <!-- ============================================================ -->
  <div id="drawerOverlay" class="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 hidden transition-opacity duration-300" onclick="closeMailboxDrawer()"></div>

  <div id="mailboxDrawer" class="fixed top-0 right-0 bottom-0 w-full max-w-xl bg-cardbg border-l border-cardborder shadow-2xl z-50 transform translate-x-full transition-transform duration-300 flex flex-col">
    <!-- 抽屉头部 -->
    <div class="px-6 py-4 border-b border-cardborder bg-slate-900/70 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-8 h-8 rounded-lg bg-brand-600/20 text-brand-400 flex items-center justify-center border border-brand-500/30">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path></svg>
        </div>
        <div>
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <span>在线收件箱</span>
            <span id="drawerMailboxTag" class="text-[10px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300">0 封邮件</span>
          </h3>
          <p id="drawerEmailTitle" class="text-xs text-brand-400 font-mono select-all">user@hotmail.com</p>
        </div>
      </div>
      <div class="flex items-center space-x-2">
        <button onclick="copyCurrentDrawerEmail()" class="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs transition" title="复制邮箱地址">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
        </button>
        <button onclick="refreshDrawerMailbox()" class="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs transition" title="重新拉取邮件">
          <svg id="drawerRefreshIcon" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
        </button>
        <button onclick="closeMailboxDrawer()" class="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white text-xs transition">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
      </div>
    </div>

    <!-- 接码大卡片与监听工具条 -->
    <div class="p-6 border-b border-cardborder bg-gradient-to-b from-slate-900/40 to-transparent space-y-4">
      <!-- 大号验证码高亮展示卡 -->
      <div id="latestCodeCard" class="hidden relative overflow-hidden rounded-2xl p-5 border border-emerald-500/30 bg-gradient-to-r from-emerald-950/40 via-cardbg to-indigo-950/40 shadow-lg">
        <div class="flex items-center justify-between text-xs text-emerald-400 font-medium mb-1">
          <span class="flex items-center gap-1.5">
            <span class="w-2 h-2 rounded-full bg-emerald-400 animate-ping"></span>
            <span id="latestCodeServiceLabel">最新捕获验证码</span>
          </span>
          <span class="text-[10px] text-slate-400">已就绪</span>
        </div>
        <div class="flex items-center justify-between mt-2">
          <div id="latestCodeVal" class="text-4xl font-extrabold tracking-widest text-emerald-300 font-mono select-all">------</div>
          <button onclick="copyExtractedCode()" class="py-2 px-4 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs shadow-md shadow-emerald-600/30 transition flex items-center gap-1.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3"></path></svg>
            一键复制验证码
          </button>
        </div>
      </div>

      <!-- 实时接码监听操作栏 -->
      <div class="flex items-center justify-between gap-3">
        <button id="btnWatchCode" onclick="toggleCodeWatcher()" class="flex-1 py-2 px-3 rounded-xl bg-indigo-600/80 hover:bg-indigo-600 text-xs font-semibold text-white border border-indigo-500/30 transition flex items-center justify-center gap-2">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
          <span id="watchCodeBtnText">启动 60s 实时接码监听</span>
        </button>
        <a id="btnWebmailDirect" href="https://outlook.live.com/mail/0/" target="_blank" class="py-2 px-3 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 border border-slate-700 transition flex items-center gap-1">
          <span>网页登录</span>
          <svg class="w-3 h-3 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
        </a>
      </div>
    </div>

    <!-- 邮件列表滚动区 -->
    <div id="drawerMessageList" class="flex-1 overflow-y-auto drawer-scroll p-6 space-y-3">
      <div class="text-center py-12 text-slate-500 text-xs">正在连接邮件服务器拉取最新邮件...</div>
    </div>
  </div>

  <footer class="border-t border-cardborder py-4 text-center text-xs text-slate-500">
    OutlookRegister 独享全自动产号中枢 · 协议补签防封增强版 · 在线收件箱接码 · 端口 8890
  </footer>

  <!-- 前端交互核心 JS -->
  <script>
    let sseSource = null;
    let allCardsData = [];
    let currentDrawerEmail = '';
    let watcherTimer = null;
    let watcherRemaining = 0;
    let currentExtractedCode = '';

    function toggleProxyInput() {
      const mode = document.getElementById('inputProxyMode').value;
      const wrap = document.getElementById('customProxyWrap');
      if (mode === 'custom') {
        wrap.classList.remove('hidden');
      } else {
        wrap.classList.add('hidden');
      }
    }

    function initSSE() {
      if (sseSource) {
        sseSource.close();
      }
      sseSource = new EventSource('/api/logs/stream');
      const indicator = document.getElementById('sseIndicator');

      sseSource.onopen = () => {
        indicator.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping"></span> Live SSE';
        indicator.className = 'text-emerald-400 font-mono text-[11px] flex items-center gap-1';
      };

      sseSource.addEventListener('log', (e) => {
        const item = JSON.parse(e.data);
        appendLogItem(item);
      });

      sseSource.addEventListener('state', (e) => {
        const state = JSON.parse(e.data);
        renderState(state);
      });

      sseSource.onerror = () => {
        indicator.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-rose-400"></span> 重连中';
        indicator.className = 'text-rose-400 font-mono text-[11px] flex items-center gap-1';
      };
    }

    function appendLogItem(item) {
      const tb = document.getElementById('terminalBody');
      const div = document.createElement('div');
      div.className = 'flex items-start space-x-2 leading-relaxed';

      let colorClass = 'text-slate-300';
      if (item.level === 'success') colorClass = 'text-emerald-400 font-medium';
      if (item.level === 'warn') colorClass = 'text-amber-300';
      if (item.level === 'error') colorClass = 'text-rose-400 font-bold';

      div.innerHTML = `<span class="text-slate-500 select-none">[${item.time}]</span> <span class="${colorClass}">${escapeHtml(item.message)}</span>`;
      tb.appendChild(div);
      tb.scrollTop = tb.scrollHeight;
    }

    function clearLogsUI() {
      document.getElementById('terminalBody').innerHTML = '<div class="text-slate-500 italic">控制台已清空。</div>';
    }

    function escapeHtml(text) {
      const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
      return String(text).replace(/[&<>"']/g, m => map[m]);
    }

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const json = await res.json();
        if (json.ok) {
          renderState(json.data);
        }
      } catch (e) {
        console.error(e);
      }
    }

    function renderState(s) {
      const isRunning = s.is_running;
      document.getElementById('statTotalCards').innerHTML = `${s.total_cards_in_db} <span class="text-xs text-slate-500 font-normal">个账号</span>`;
      document.getElementById('statTargetProgress').innerText = `${s.succeeded_count} / ${s.target_count || 0}`;
      document.getElementById('statSucceeded').innerText = s.succeeded_count;
      document.getElementById('statFailed').innerText = s.failed_count;
      document.getElementById('statAttempts').innerText = `总尝试轮次: ${s.current_attempt} 次`;

      const sec = s.elapsed_seconds || 0;
      const h = String(Math.floor(sec / 3600)).padStart(2, '0');
      const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0');
      const sc = String(sec % 60).padStart(2, '0');
      document.getElementById('statElapsed').innerText = `耗时: ${h}:${m}:${sc}`;

      const tf = s.traffic_stats || { saved_mb: 0, blocked_count: 0 };
      document.getElementById('statSavedTraffic').innerHTML = `~${tf.saved_mb} <span class="text-xs text-slate-500 font-normal">MB</span>`;
      document.getElementById('statBlockedReqs').innerText = `已拦截 ${tf.blocked_count} 次冗余请求`;

      const node = s.current_node || {};
      if (node.exit_ip) {
        document.getElementById('statNodeIp').innerText = `${node.country || ''} ${node.exit_ip}`;
        document.getElementById('statNodeDetail').innerText = `${node.isp || '住宅宽带'} · ${node.timezone || 'UTC'}`;
      } else {
        document.getElementById('statNodeIp').innerText = isRunning ? '切换中...' : '等待就绪';
        document.getElementById('statNodeDetail').innerText = 'NovProxy 住宅网关待命中';
      }

      const badge = document.getElementById('statusBadge');
      const dot = document.getElementById('statusDot');
      const txt = document.getElementById('statusText');
      const btnStart = document.getElementById('btnStart');
      const btnStop = document.getElementById('btnStop');

      if (isRunning) {
        badge.className = 'flex items-center space-x-2 px-3 py-1.5 rounded-full border bg-emerald-500/10 border-emerald-500/30 text-xs';
        dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse';
        txt.className = 'font-medium text-emerald-300';
        txt.innerText = '正在产号中...';
        btnStart.disabled = true;
        btnStart.classList.add('opacity-40', 'pointer-events-none');
        btnStop.disabled = false;
      } else {
        badge.className = 'flex items-center space-x-2 px-3 py-1.5 rounded-full border bg-slate-800/80 border-slate-700 text-xs';
        dot.className = 'w-2.5 h-2.5 rounded-full bg-slate-400';
        txt.className = 'font-medium text-slate-300';
        txt.innerText = '系统空闲';
        btnStart.disabled = false;
        btnStart.classList.remove('opacity-40', 'pointer-events-none');
        btnStop.disabled = true;
      }
    }

    async function loadCards() {
      try {
        const res = await fetch('/api/cards');
        const json = await res.json();
        if (json.ok) {
          allCardsData = json.cards || [];
          document.getElementById('cardsCountBadge').innerText = `${json.count} 份`;
          const tbody = document.getElementById('cardsTableBody');
          if (allCardsData.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-slate-500">当前卡密库尚无数据，点击左上角【启动自动化产号】开始生产。</td></tr>';
            return;
          }
          tbody.innerHTML = allCardsData.map(c => `
            <tr class="hover:bg-slate-800/40 transition group">
              <td class="py-2.5 px-3 text-slate-500">${c.id}</td>
              <td class="py-2.5 px-3 font-semibold text-white select-all">${escapeHtml(c.email)}</td>
              <td class="py-2.5 px-3 text-slate-300 select-all">${escapeHtml(c.password)}</td>
              <td class="py-2.5 px-3 text-slate-400 truncate max-w-[130px]" title="${escapeHtml(c.client_id)}">${escapeHtml(c.client_id)}</td>
              <td class="py-2.5 px-3 text-slate-400 truncate max-w-[200px]" title="${escapeHtml(c.refresh_token)}">${escapeHtml(c.refresh_token.slice(0, 30))}...</td>
              <td class="py-2.5 px-3 text-right space-x-2 whitespace-nowrap">
                <button onclick="openMailboxDrawer('${escapeHtml(c.email)}')" class="px-2.5 py-1 rounded-lg bg-indigo-600/80 hover:bg-indigo-600 text-white font-medium shadow-sm transition flex-inline items-center gap-1">
                  📬 收件箱
                </button>
                <button onclick="copySingleCard('${escapeHtml(c.raw)}')" class="px-2 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white transition" title="复制4段式卡密">复制</button>
              </td>
            </tr>
          `).join('');
        }
      } catch (e) {
        console.error(e);
      }
    }

    function copySingleCard(raw) {
      navigator.clipboard.writeText(raw);
      alert('已复制该账号 4 段式卡密到剪贴板！');
    }

    function copyAllCards() {
      if (!allCardsData || allCardsData.length === 0) {
        alert('当前无卡密可供复制');
        return;
      }
      const allText = allCardsData.map(c => c.raw).join('\\n');
      navigator.clipboard.writeText(allText);
      alert(`已成功复制全部 ${allCardsData.length} 份 4 段式卡密！`);
    }

    async function startTask() {
      const targetCount = parseInt(document.getElementById('inputTargetCount').value) || 5;
      const suffix = document.getElementById('inputSuffix').value;
      const mode = document.getElementById('inputProxyMode').value;
      const customProxy = document.getElementById('inputCustomProxy').value;
      const dataSaverEl = document.getElementById('inputDataSaver');
      const enableDataSaver = dataSaverEl ? dataSaverEl.checked : true;

      try {
        const res = await fetch('/api/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            target_count: targetCount,
            email_suffix: suffix,
            use_novproxy: mode === 'novproxy',
            custom_proxy: customProxy,
            enable_data_saver: enableDataSaver
          })
        });
        const json = await res.json();
        if (!json.ok) {
          alert('启动失败: ' + json.error);
        } else {
          fetchStatus();
        }
      } catch (e) {
        alert('请求出错: ' + e);
      }
    }

    async function stopTask() {
      if (!confirm('确定要终止当前的产号任务吗？')) return;
      try {
        const res = await fetch('/api/stop', { method: 'POST' });
        const json = await res.json();
        alert(json.message || '已发送停止指令');
        fetchStatus();
      } catch (e) {
        alert('请求出错: ' + e);
      }
    }

    async function repairUnactivated() {
      if (!confirm('即将对存量自建账号启动无头浏览器批量补签隐私政策，确定执行？')) return;
      try {
        const res = await fetch('/api/repair-unactivated', { method: 'POST' });
        const json = await res.json();
        alert(json.message || '已在后台执行');
      } catch (e) {
        alert('请求出错: ' + e);
      }
    }

    // ============================================================
    // 在线收件箱抽屉逻辑
    // ============================================================

    function openMailboxDrawer(email) {
      currentDrawerEmail = email;
      document.getElementById('drawerEmailTitle').innerText = email;
      document.getElementById('drawerOverlay').classList.remove('hidden');
      document.getElementById('mailboxDrawer').classList.remove('translate-x-full');
      document.getElementById('latestCodeCard').classList.add('hidden');
      currentExtractedCode = '';
      stopCodeWatcher();
      loadMailboxMessages(email);
    }

    function closeMailboxDrawer() {
      document.getElementById('drawerOverlay').classList.add('hidden');
      document.getElementById('mailboxDrawer').classList.add('translate-x-full');
      stopCodeWatcher();
    }

    function copyCurrentDrawerEmail() {
      if (currentDrawerEmail) {
        navigator.clipboard.writeText(currentDrawerEmail);
        alert('已复制邮箱地址: ' + currentDrawerEmail);
      }
    }

    function copyExtractedCode() {
      if (currentExtractedCode) {
        navigator.clipboard.writeText(currentExtractedCode);
        alert('🎉 验证码已成功复制: ' + currentExtractedCode);
      }
    }

    async function loadMailboxMessages(email) {
      const container = document.getElementById('drawerMessageList');
      const icon = document.getElementById('drawerRefreshIcon');
      icon.classList.add('animate-spin');

      try {
        const res = await fetch(`/api/mailbox/messages?email=${encodeURIComponent(email)}`);
        const json = await res.json();
        icon.classList.remove('animate-spin');

        if (!json.ok) {
          if (json.is_not_connected) {
            container.innerHTML = `
              <div class="rounded-xl border border-amber-500/30 bg-amber-950/20 p-4 space-y-2 text-xs">
                <div class="font-bold text-amber-300 flex items-center gap-1.5">
                  <span>⏳ 提示：该账号处于微软新号 IMAP 冷冻期</span>
                </div>
                <p class="text-slate-300 leading-relaxed">
                  微软对新注册的个人 Hotmail 账号在初期的 12~24 小时内对非官方客户端实施外部会话限制。
                </p>
                <div class="pt-2 flex items-center gap-2">
                  <a href="https://outlook.live.com/mail/0/" target="_blank" class="px-3 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-500 text-white font-medium transition inline-flex items-center gap-1">
                    点击打开网页端登录查看邮件
                  </a>
                </div>
              </div>
            `;
          } else {
            container.innerHTML = `
              <div class="rounded-xl border border-rose-500/30 bg-rose-950/20 p-4 space-y-1 text-xs text-rose-300">
                <div class="font-bold flex items-center gap-1"><span>❌ 读取失败</span></div>
                <p class="text-slate-400 font-mono">${escapeHtml(json.error || '未知网络错误')}</p>
                ${json.hint ? `<p class="text-amber-400 pt-1">${escapeHtml(json.hint)}</p>` : ''}
              </div>
            `;
          }
          document.getElementById('drawerMailboxTag').innerText = '连接受阻';
          return;
        }

        const total = json.total_msgs || 0;
        document.getElementById('drawerMailboxTag').innerText = `${total} 封邮件`;

        // 处理大号验证码高亮展示卡片
        const latestCodeCard = document.getElementById('latestCodeCard');
        if (json.latest_code) {
          currentExtractedCode = json.latest_code;
          document.getElementById('latestCodeVal').innerText = json.latest_code;
          document.getElementById('latestCodeServiceLabel').innerText = json.latest_platform ? `来自 ${json.latest_platform} 的最新验证码` : '最新检出验证码';
          latestCodeCard.classList.remove('hidden');
        } else {
          latestCodeCard.classList.add('hidden');
        }

        const msgs = json.messages || [];
        if (msgs.length === 0) {
          container.innerHTML = '<div class="text-center py-12 text-slate-500 text-xs">收件箱空空如也，暂未收到任何邮件。</div>';
          return;
        }

        container.innerHTML = msgs.map((m, idx) => `
          <div class="rounded-xl border border-cardborder bg-darkbg/70 p-4 space-y-2 hover:border-slate-700 transition">
            <div class="flex items-start justify-between gap-2">
              <div class="flex items-center space-x-2">
                <div class="w-6 h-6 rounded-full bg-slate-800 text-slate-300 flex items-center justify-center font-bold text-[10px]">
                  ${escapeHtml((m.from || 'M')[0].toUpperCase())}
                </div>
                <span class="font-semibold text-white text-xs truncate max-w-[200px]" title="${escapeHtml(m.from)}">${escapeHtml(m.from)}</span>
              </div>
              <span class="text-[11px] text-slate-500 font-mono whitespace-nowrap">${escapeHtml(m.date)}</span>
            </div>

            <div class="text-xs font-medium text-slate-200">${escapeHtml(m.subject || '(无主题)')}</div>

            ${m.extracted_code ? `
              <div class="inline-flex items-center gap-2 px-2.5 py-1 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs font-mono font-bold">
                <span>🔑 验证码: ${escapeHtml(m.extracted_code)}</span>
                <button onclick="navigator.clipboard.writeText('${escapeHtml(m.extracted_code)}'); alert('已复制验证码: ${escapeHtml(m.extracted_code)}');" class="text-[10px] text-slate-400 hover:text-white underline">复制</button>
              </div>
            ` : ''}

            <p class="text-[11px] text-slate-400 leading-relaxed font-mono line-clamp-3 select-text">${escapeHtml(m.snippet || m.body)}</p>
          </div>
        `).join('');

      } catch (err) {
        icon.classList.remove('animate-spin');
        container.innerHTML = `<div class="text-center py-8 text-rose-400 text-xs">网络请求异常: ${escapeHtml(err)}</div>`;
      }
    }

    function refreshDrawerMailbox() {
      if (currentDrawerEmail) {
        loadMailboxMessages(currentDrawerEmail);
      }
    }

    function toggleCodeWatcher() {
      if (watcherTimer) {
        stopCodeWatcher();
      } else {
        startCodeWatcher();
      }
    }

    function startCodeWatcher() {
      watcherRemaining = 60;
      updateWatcherBtnUI();
      refreshDrawerMailbox();
      watcherTimer = setInterval(async () => {
        watcherRemaining -= 3;
        if (watcherRemaining <= 0) {
          stopCodeWatcher();
          return;
        }
        updateWatcherBtnUI();
        if (currentDrawerEmail) {
          await loadMailboxMessages(currentDrawerEmail);
          if (currentExtractedCode) {
            stopCodeWatcher();
            navigator.clipboard.writeText(currentExtractedCode);
            alert(`🎉 [自动捕获] 成功收到最新验证码: ${currentExtractedCode} (已自动复制到剪贴板)`);
          }
        }
      }, 3000);
    }

    function stopCodeWatcher() {
      if (watcherTimer) {
        clearInterval(watcherTimer);
        watcherTimer = null;
      }
      watcherRemaining = 0;
      updateWatcherBtnUI();
    }

    function updateWatcherBtnUI() {
      const btn = document.getElementById('btnWatchCode');
      const text = document.getElementById('watchCodeBtnText');
      if (watcherTimer) {
        btn.className = 'flex-1 py-2 px-3 rounded-xl bg-amber-600 hover:bg-amber-500 text-xs font-semibold text-white border border-amber-400/30 transition flex items-center justify-center gap-2 animate-pulse';
        text.innerText = `正在监听新验证码 (${watcherRemaining}s)... [点击取消]`;
      } else {
        btn.className = 'flex-1 py-2 px-3 rounded-xl bg-indigo-600/80 hover:bg-indigo-600 text-xs font-semibold text-white border border-indigo-500/30 transition flex items-center justify-center gap-2';
        text.innerText = '启动 60s 实时接码监听';
      }
    }

    function refreshAll() {
      fetchStatus();
      loadCards();
    }

    window.addEventListener('DOMContentLoaded', () => {
      initSSE();
      fetchStatus();
      loadCards();
      setInterval(fetchStatus, 3000);
    });
  </script>
</body>
</html>
"""


def main():
    port = int(os.environ.get("PANEL_PORT", 8890))
    host = "0.0.0.0"
    server = ThreadingHTTPServer((host, port), PanelHTTPHandler)
    print(f"================================================================")
    print(f"🚀 OutlookRegister 独立产号控制面板已启动!")
    print(f"   本地访问地址: http://127.0.0.1:{port}")
    print(f"   局域网/公网地址: http://<你的IP>:{port}")
    print(f"   卡密保存路径: {CARDS_FILE}")
    print(f"================================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] 面板服务已退出。")
        server.server_close()


if __name__ == "__main__":
    main()
