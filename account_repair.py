#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自建 Hotmail 存量资产协议补签与 Exchange 收件箱自动化激活模块
专用于批量解决 "User is authenticated but not connected"（已认证但未连接）
"""

import imaplib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

from patchright.sync_api import sync_playwright
from get_token import generate_code_challenge, generate_code_verifier

CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
REDIRECT_URI = "https://localhost"
SCOPES = [
    "offline_access",
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/POP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
]


def test_imap_xoauth2(email: str, access_token: str) -> tuple[bool, str]:
    mail = imaplib.IMAP4_SSL("outlook.office365.com", 993, timeout=15)
    try:
        auth_str = f"user={email}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
        mail.authenticate("XOAUTH2", lambda _: auth_str)
        status, _ = mail.select("INBOX", readonly=True)
        mail.logout()
        if status == "OK":
            return True, "IMAP 认证与收件箱绑定成功 (OK)"
        return False, f"IMAP select INBOX 失败: {status}"
    except Exception as e:
        return False, str(e)


def repair_single_account(browser, email: str, password: str) -> dict[str, Any]:
    """通过无头浏览器自动签署隐私协议，并初始化 Webmail 挂载"""
    context = browser.new_context(
        locale="zh-CN",
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    )
    page = context.new_page()

    captured_code = []

    def _on_request(request):
        url = request.url
        if url.startswith(REDIRECT_URI) and "code=" in url:
            parsed = urllib.parse.urlparse(url)
            code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
            if code and not captured_code:
                captured_code.append(code)

    page.on("request", _on_request)

    try:
        # 1. 登录微软账号
        page.goto("https://login.live.com/", timeout=30000)
        page.fill('input[type="email"], input[name="loginfmt"]', email)
        page.click('button[type="submit"], input[type="submit"], #idSIButton9')
        page.wait_for_timeout(2500)

        # 检查是否已被锁定
        body_initial = page.locator("body").inner_text()
        if "locked" in body_initial.lower() or "abuse" in body_initial.lower() or "锁定" in body_initial:
            return {"email": email, "status": "locked", "msg": "账号在输入邮箱后提示被锁定"}

        # 如果出现辅助邮箱选项，切换回密码
        for btn_txt in ("Use your password", "使用密码", "Other ways to sign in", "其他登录方式"):
            use_pwd = page.get_by_text(btn_txt)
            if use_pwd.count() > 0 and use_pwd.first.is_visible():
                try:
                    use_pwd.first.click()
                    page.wait_for_timeout(2000)
                    break
                except Exception:
                    pass

        # 2. 填入密码
        pwd_input = page.locator('input[type="password"]')
        for _ in range(8):
            if pwd_input.count() > 0 and pwd_input.first.is_visible():
                break
            page.wait_for_timeout(500)
        if pwd_input.count() > 0:
            pwd_input.first.fill(password)
            page.click('button[type="submit"], input[type="submit"], #idSIButton9')
            page.wait_for_timeout(4000)
        else:
            return {"email": email, "status": "no_pwd_field", "msg": f"未能定位到密码框: {body_initial[:120]}"}

        # 检查提交密码后是否提示锁定
        body_after_pwd = page.locator("body").inner_text()
        if "locked" in body_after_pwd.lower() or "abuse" in body_after_pwd.lower() or "锁定" in body_after_pwd:
            return {"email": email, "status": "locked", "msg": "提交密码后提示账号被锁定 (Abuse/Locked)"}

        # 3. 处理隐私须知弹窗 (privacynotice) 和 保持登录 (KMSI)
        for _ in range(15):
            curr_url = page.url or ""
            if "privacynotice" in curr_url or "notice" in curr_url:
                btn = page.locator('button:has-text("OK"), button:has-text("确定"), #idBtn_Accept, [data-testid="primaryButton"]')
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    page.wait_for_timeout(3000)
                    continue

            kmsi_btn = page.locator('#idSIButton9, button:has-text("Yes"), button:has-text("No"), button:has-text("是"), button:has-text("否")')
            if kmsi_btn.count() > 0 and kmsi_btn.first.is_visible():
                kmsi_btn.first.click()
                page.wait_for_timeout(2500)
                continue

            if "outlook.live.com" in curr_url or "account.microsoft.com" in curr_url:
                break
            page.wait_for_timeout(1000)

        # 4. 显式打开 Outlook Webmail 激活 Exchange Mailbox Store
        page.goto("https://outlook.live.com/mail/0/", timeout=30000)
        for _ in range(20):
            page.wait_for_timeout(1500)
            u = page.url or ""
            if "notice" in u:
                btn = page.locator('button:has-text("OK"), button:has-text("确定"), #idBtn_Accept')
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    page.wait_for_timeout(2000)
                    continue

            if page.locator('[aria-label="新邮件"], [aria-label="New mail"], [data-folder-name="inbox"], [title*="收件箱"], [title*="Inbox"]').count() > 0:
                break

        page.wait_for_timeout(3000)

        # 5. 执行 OAuth2 授权并提取全新 refresh_token
        code_verifier = generate_code_verifier()
        code_challenge = generate_code_challenge(code_verifier)
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": " ".join(SCOPES),
            "response_mode": "query",
            "login_hint": email,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = (
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"
            + "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params.items())
        )

        try:
            page.goto(authorize_url, timeout=20000)
        except Exception:
            pass

        for _ in range(15):
            if captured_code:
                break
            consent_btn = page.locator(
                '#idBtn_Accept, [data-testid="appConsentPrimaryButton"], button:has-text("Accept"), button:has-text("接受"), button:has-text("Yes"), button:has-text("是")'
            )
            if consent_btn.count() > 0 and consent_btn.first.is_visible():
                consent_btn.first.click()
                page.wait_for_timeout(1500)
            page.wait_for_timeout(1000)

        if not captured_code:
            return {"email": email, "status": "no_code", "msg": f"未能拦截到 OAuth2 授权回调 code (URL: {page.url})"}

        auth_code = captured_code[0]
        token_payload = urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
            "scope": " ".join(SCOPES),
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            tokens = json.loads(resp.read().decode("utf-8"))

        new_rt = tokens.get("refresh_token")
        access_tok = tokens.get("access_token")

        if not new_rt or not access_tok:
            return {"email": email, "status": "token_err", "msg": f"响应未包含有效令牌: {tokens}"}

        # 6. 测试 IMAP 连接
        imap_ok, imap_detail = test_imap_xoauth2(email, access_tok)
        card_4part = f"{email}----{password}----{CLIENT_ID}----{new_rt}"

        return {
            "email": email,
            "status": "success" if imap_ok else "imap_pending",
            "card_4part": card_4part,
            "imap_ok": imap_ok,
            "imap_detail": imap_detail,
        }

    except Exception as e:
        return {"email": email, "status": "error", "msg": str(e)}
    finally:
        try:
            page.close()
            context.close()
        except Exception:
            pass


def get_accounts_to_repair(cards_path: str) -> list[tuple[str, str]]:
    """从卡密库读取待检查与修复的账号密码列表"""
    accounts = []
    if os.path.exists(cards_path):
        with open(cards_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "----" in line:
                    parts = line.split("----")
                    if len(parts) >= 2 and parts[0] and parts[1]:
                        accounts.append((parts[0].strip(), parts[1].strip()))
    return accounts
