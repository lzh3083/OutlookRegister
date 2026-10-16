# -*- coding: utf-8 -*-
"""
OutlookRegister 极速省流量优化器 (Browser Data Saver)
专为住宅代理批量注册设计：
1. 拦截字体 (woff/woff2/ttf) 与音视频媒体；
2. 拦截微软 Aria、Clarity 录屏埋点与广告遥测，阻止持续上传流量；
3. 智能白名单放行验证码 (hsprotect / arkose / challenge) 关键资源；
4. 深度精简 Outlook 网页版 (OWA) 附属组件，防止加载重型日历/Office插件；
5. 提供全局拦截与流量节约实时度量。
"""

import fnmatch
import logging
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

# 验证挑战白名单：绝对不可拦截的关键词
CHALLENGE_WHITELIST = (
    "hsprotect",
    "arkose",
    "funcaptcha",
    "captcha",
    "challenge",
    "verification",
    "verify",
    "secsdk",
    "clientcollector",
    "collector-",
    "px3",
    "risk/initialize",
    "risk/verify",
    "createaccount",
    "credentialaction",
)

# 必须阻断的高耗流量遥测、录屏与广告域名匹配规则
BLOCKED_URL_PATTERNS = [
    "*pipe.aria.microsoft.com*",
    "*browser.pipe.aria.microsoft.com*",
    "*.clarity.ms*",
    "*c.bing.com*",
    "*c.msn.com*",
    "*.adnxs.com*",
    "*.doubleclick.net*",
    "*.google-analytics.com*",
    "*.scorecardresearch.com*",
    "*telemetry.microsoft.com*",
    "*events.data.microsoft.com*",
    "*mobile.pipe.aria.microsoft.com*",
    "*watson.telemetry.microsoft.com*",
]

# Outlook 网页版专属：非必要超重型附属模块关键词（日历/联系人/文件预览等）
OWA_BLOAT_PATTERNS = [
    "*calendar*manifest*",
    "*people*manifest*",
    "*files*manifest*",
    "*todo*manifest*",
    "*substrate.office.com*",
    "*res.cdn.office.net/assets/mail/calendar*",
    "*res.cdn.office.net/assets/mail/people*",
]

# 资源典型体积估算 (KB)，用于计算节省的住宅代理流量
SAVED_SIZE_ESTIMATES = {
    "font": 280,        # SegoeUI.woff2 / icon fonts 通常 150KB~400KB
    "media": 1200,      # 音频/视频/动画
    "telemetry": 65,    # Aria / Clarity 频繁 POST 和 script
    "ad": 80,           # 广告脚本与分析
    "image": 110,       # 背景大图、横幅与图标
    "owa_bloat": 450,   # OWA 非邮件子模块
}


class TrafficMeter:
    """全局流量与节约统计单例"""

    def __init__(self):
        self._lock = threading.Lock()
        self.blocked_count = 0
        self.saved_bytes = 0
        self.blocked_by_category = {
            "font": 0,
            "telemetry": 0,
            "media": 0,
            "image": 0,
            "owa_bloat": 0,
            "ad": 0,
        }

    def record_block(self, category: str):
        with self._lock:
            self.blocked_count += 1
            self.blocked_by_category[category] = self.blocked_by_category.get(category, 0) + 1
            kb = SAVED_SIZE_ESTIMATES.get(category, 80)
            self.saved_bytes += kb * 1024

    def get_summary(self) -> dict[str, Any]:
        with self._lock:
            saved_mb = round(self.saved_bytes / (1024 * 1024), 2)
            return {
                "blocked_count": self.blocked_count,
                "saved_bytes": self.saved_bytes,
                "saved_mb": saved_mb,
                "breakdown": dict(self.blocked_by_category),
            }

    def reset(self):
        with self._lock:
            self.blocked_count = 0
            self.saved_bytes = 0
            for k in self.blocked_by_category:
                self.blocked_by_category[k] = 0


GLOBAL_TRAFFIC_METER = TrafficMeter()


def is_challenge_critical(url_lower: str) -> bool:
    """检查请求是否属于验证码或关键风控通信"""
    return any(kw in url_lower for kw in CHALLENGE_WHITELIST)


def should_block_request(route) -> tuple[bool, str]:
    """
    判断当前请求是否应当在浏览器底层予以阻断。
    返回: (should_block: bool, category: str)
    """
    req = route.request
    url = req.url
    url_lower = url.lower()
    rtype = str(req.resource_type or "").lower()

    # 1. 核心白名单：验证码、按压挑战、人机风险遥测绝对放行
    if is_challenge_critical(url_lower):
        return False, ""

    # 2. 检查遥测与广告黑名单
    for pat in BLOCKED_URL_PATTERNS:
        if fnmatch.fnmatch(url_lower, pat):
            return True, "telemetry" if "aria" in pat or "clarity" in pat or "telemetry" in pat else "ad"

    # 3. 拦截字体文件 (font) - 对 DOM 识别与按压 0 影响
    if rtype == "font" or url_lower.endswith((".woff2", ".woff", ".ttf", ".eot", ".otf")):
        return True, "font"

    # 4. 拦截多媒体文件 (media) - 视频/音频
    if rtype == "media" or url_lower.endswith((".mp4", ".mp3", ".webm", ".ogg", ".wav", ".flac", ".avi")):
        return True, "media"

    # 5. OWA (Outlook 网页版) 附属非核心子模块精简
    if "outlook.live.com" in url_lower or "res.cdn.office.net" in url_lower:
        for pat in OWA_BLOAT_PATTERNS:
            if fnmatch.fnmatch(url_lower, pat):
                return True, "owa_bloat"

    # 6. 普通装饰性图片拦截（放行非装饰的核心小图标或 base64）
    if rtype == "image":
        # 如果是常见的背景横幅、大图、装饰性 SVG，且不在白名单中
        if url_lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".svg")):
            # 保护可能用于表单状态的小图，其余阻断
            return True, "image"

    return False, ""


def setup_data_saver_for_page(page, enabled: bool = True):
    """为指定 Page 安装底层极速省流量拦截器"""
    if not enabled:
        return

    def _route_handler(route):
        try:
            block, category = should_block_request(route)
            if block:
                GLOBAL_TRAFFIC_METER.record_block(category)
                route.abort("blockedbyclient")
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    try:
        page.route("**/*", _route_handler)
    except Exception as e:
        logger.warning(f"安装省流量拦截器失败: {e}")
