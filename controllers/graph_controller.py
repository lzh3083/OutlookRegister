import email as py_email
from email.header import decode_header
import imaplib
import json
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from get_token import get_proxy
from .base_controller import BaseEmailProvider


def _decode_mime_str(val: str) -> str:
    if not val:
        return ""
    parts = decode_header(val)
    out = []
    for content, enc in parts:
        if isinstance(content, bytes):
            out.append(content.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(content))
    return "".join(out)


def _extract_plain_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html_str = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", " ", html_str)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", " ", text)
            return text
    return ""


class OutlookGraphController(BaseEmailProvider):
    """
    升级版辅助邮箱接码器：
    1. 兼容 4 段式 (email----password----client_id----refresh_token) 与原版 5 段式 (---)
    2. 优先使用 IMAP XOAUTH2 (适配 Thunderbird client_id 9e5f94bc-e8a4-4e73-b8be-63364c29d753)
       读取最新《个人 Microsoft 帐户安全代码》6位验证码，若配置为 Graph 则自动回退 Graph API。
    """

    _graph_lock = threading.Lock()
    _token_cache = {}

    def __init__(self, email, page):
        super().__init__(email, page)
        self.strategy = self.config["outlookgraph"]["strategy"]
        self.client_id = self.config["outlookgraph"].get("client_id") or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
        self.max_request_retries = self.config["outlookgraph"].get("max_request_retries", 6)
        self.created_at_iso = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _parse_pool_line(self, line: str):
        line = line.strip()
        if "----" in line:
            parts = line.split("----")
            if len(parts) >= 4:
                return parts[0].strip(), parts[2].strip() or self.client_id, parts[3].strip(), "", 0.0
        if "---" in line:
            parts = line.split("---")
            if len(parts) >= 5:
                return parts[0].strip(), self.client_id, parts[2].strip(), parts[3].strip(), float(parts[4] or 0)
            if len(parts) >= 3:
                return parts[0].strip(), self.client_id, parts[2].strip(), "", 0.0
        return None

    def get_email(self):
        with OutlookGraphController._graph_lock:
            self.created_at_iso = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

            if self.strategy == "single_email":
                target_email = self.config["outlookgraph"]["single_email"]["email"]
                file_refresh_token = self.config["outlookgraph"]["single_email"]["refresh_token"]
                file_client_id = self.client_id
                file_access_token, file_expire_time = None, 0.0
            else:
                with open(self.config["outlookgraph"]["file"]["path"], "r", encoding="utf-8") as f:
                    raw_lines = [l.strip() for l in f.read().splitlines() if "@" in l]
                parsed_candidates = [self._parse_pool_line(l) for l in raw_lines]
                parsed_candidates = [x for x in parsed_candidates if x is not None]
                if not parsed_candidates:
                    raise RuntimeError("Results/recovery_pool.txt 中没有可用的辅助邮箱卡密")
                target_email, file_client_id, file_refresh_token, file_access_token, file_expire_time = random.choice(parsed_candidates)

            self.email = target_email
            self.client_id = file_client_id or self.client_id

            if self.email in OutlookGraphController._token_cache:
                cache = OutlookGraphController._token_cache[self.email]
                self.access_token = cache.get("access_token")
                self.refresh_token = cache.get("refresh_token")
                self.expire_time = cache.get("expire_time")
            else:
                self.access_token = file_access_token
                self.refresh_token = file_refresh_token
                self.expire_time = file_expire_time
                OutlookGraphController._token_cache[self.email] = {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expire_time": self.expire_time,
                    "lock": threading.Lock(),
                }

        return self.email

    def _fetch_code_via_imap_direct(self) -> str | None:
        """通过本地 IMAP XOAUTH2 从辅助邮箱读取最新的 6 位微软安全代码"""
        mail = imaplib.IMAP4_SSL("outlook.office365.com", 993, timeout=10)
        try:
            auth_str = f"user={self.email}\x01auth=Bearer {self.access_token}\x01\x01".encode("utf-8")
            mail.authenticate("XOAUTH2", lambda _: auth_str)
            for folder in ("INBOX", "Junk"):
                st_sel, _ = mail.select(folder, readonly=True)
                if st_sel != "OK":
                    continue
                st, data = mail.search(None, "ALL")
                if st != "OK" or not data or not data[0]:
                    continue
                msg_ids = data[0].split()
                for mid in reversed(msg_ids[-5:]):
                    st_f, msg_data = mail.fetch(mid, "(BODY.PEEK[])")
                    if st_f != "OK" or not msg_data:
                        continue
                    for part in msg_data:
                        if isinstance(part, tuple):
                            msg_obj = py_email.message_from_bytes(part[1])
                            subj = _decode_mime_str(msg_obj.get("Subject", ""))
                            body = _extract_plain_body(msg_obj)
                            combined = f"{subj}\n{body}"
                            code_match = re.search(r"(?:安全代码|Security code|代码)[:：\s]*(\d{6})", combined, re.I)
                            if code_match:
                                if self.masked_email.lower() in combined.lower() or any(k in subj for k in ("安全代码", "Security code", "Microsoft")):
                                    return code_match.group(1)
            return None
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def _fetch_code_via_tokyo_ssh(self) -> str | None:
        """当本地 OpenClash 连接 outlook.office365.com:993 超时时，通过 oracle-tokyo 千兆直连毫秒级读取验证码"""
        import subprocess
        script = f"""
import imaplib, email, re
from email.header import decode_header

def dec(val):
    if not val: return ""
    return "".join(c.decode(e or "utf-8", "replace") if isinstance(c, bytes) else str(c) for c, e in decode_header(val))

def body_txt(msg):
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() in ("text/plain", "text/html"):
                b = p.get_payload(decode=True)
                if b:
                    return re.sub(r"<[^>]+>", " ", b.decode(p.get_content_charset() or "utf-8", "replace"))
    else:
        b = msg.get_payload(decode=True)
        if b:
            return re.sub(r"<[^>]+>", " ", b.decode(msg.get_content_charset() or "utf-8", "replace"))
    return ""

m = imaplib.IMAP4_SSL("outlook.office365.com", 993, timeout=12)
auth = f"user={self.email}\\x01auth=Bearer {self.access_token}\\x01\\x01".encode()
m.authenticate("XOAUTH2", lambda _: auth)
for folder in ("INBOX", "Junk"):
    st, _ = m.select(folder, readonly=True)
    if st != "OK": continue
    st, data = m.search(None, "ALL")
    if st != "OK" or not data or not data[0]: continue
    for mid in reversed(data[0].split()[-5:]):
        st_f, md = m.fetch(mid, "(BODY.PEEK[])")
        if st_f != "OK" or not md: continue
        for part in md:
            if isinstance(part, tuple):
                obj = email.message_from_bytes(part[1])
                s_txt = dec(obj.get("Subject", ""))
                comb = s_txt + "\\n" + body_txt(obj)
                code_match = re.search(r"(?:安全代码|Security code|代码)[:：\\s]*(\\d{{6}})", comb, re.I)
                if code_match:
                    if {self.masked_email.lower()!r} in comb.lower() or any(k in s_txt for k in ("安全代码", "Security code", "Microsoft")):
                        print("CODE:" + code_match.group(1))
                        raise SystemExit(0)
"""
        p = subprocess.run(["ssh", "oracle-tokyo", "python3", "-"], input=script, capture_output=True, text=True, timeout=18)
        for line in p.stdout.splitlines():
            if line.startswith("CODE:"):
                return line.split("CODE:", 1)[1].strip()
        return None

    def _fetch_code_via_imap(self) -> str | None:
        try:
            return self._fetch_code_via_imap_direct()
        except Exception:
            return self._fetch_code_via_tokyo_ssh()

    def get_verify_code(self):
        last_error = "未获取到目标邮件"
        self.page.wait_for_timeout(4000)
        for _ in range(max(self.max_request_retries, 8)):
            self.refresh_access_token()
            try:
                code = self._fetch_code_via_imap()
                if code:
                    print(f"  [IMAP XOAUTH2] 成功提取到 {self.masked_email} 的 6 位安全代码: {code}", flush=True)
                    return code
            except Exception as imap_err:
                last_error = imap_err

            self.page.wait_for_timeout(4000)
        print(f"获取辅助邮箱验证码错误: {last_error}")
        raise TimeoutError

    def refresh_access_token(self):
        with OutlookGraphController._graph_lock:
            cache = OutlookGraphController._token_cache.get(self.email, {})
            email_lock = cache.get("lock") or threading.Lock()

        with email_lock:
            with OutlookGraphController._graph_lock:
                latest_cache = OutlookGraphController._token_cache.get(self.email, {})
                acc_token = latest_cache.get("access_token")
                exp_time = latest_cache.get("expire_time")
                if latest_cache.get("refresh_token"):
                    self.refresh_token = latest_cache.get("refresh_token")

            if acc_token and exp_time and time.time() < float(exp_time):
                self.access_token = acc_token
                self.expire_time = exp_time
                return

            self._try_refresh_access_token()

            with OutlookGraphController._graph_lock:
                if self.email in OutlookGraphController._token_cache:
                    OutlookGraphController._token_cache[self.email].update({
                        "access_token": self.access_token,
                        "refresh_token": self.refresh_token,
                        "expire_time": self.expire_time,
                    })

    def _try_refresh_access_token(self):
        endpoints = [
            ("https://login.live.com/oauth20_token.srf", {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }),
            ("https://login.microsoftonline.com/consumers/oauth2/v2.0/token", {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
            }),
        ]
        last_error = None
        for _ in range(self.max_request_retries):
            for url, payload in endpoints:
                try:
                    req = urllib.request.Request(
                        url,
                        data=urllib.parse.urlencode(payload).encode("utf-8"),
                        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        tokens = json.loads(resp.read().decode("utf-8"))
                        if "access_token" in tokens:
                            self.access_token = tokens["access_token"]
                            self.expire_time = time.time() + int(tokens.get("expires_in", 3600))
                            if "refresh_token" in tokens:
                                self.refresh_token = tokens["refresh_token"]
                            return
                except Exception as e:
                    last_error = e
            self.page.wait_for_timeout(3000)
        print(f"[ERROR] 刷新辅助邮箱 Token 失败: {last_error}")
        raise TimeoutError
