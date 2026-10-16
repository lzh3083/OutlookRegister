import os
import time
import json
import random
import threading
from faker import Faker
from abc import ABC, abstractmethod
from utils import load_config

class BaseBrowserController(ABC):
    """
    所有浏览器通用的接口和共享逻辑
    """

    def __init__(self):
        data = load_config()

        self.config = data
        self.choosed_mail = data['choosed_mail']
        self.wait_time = data['bot_protection_wait'] * 1000
        self.max_captcha_retries = data['max_captcha_retries']
        self.enable_oauth2 = data["oauth2"]['enable_oauth2']
        self.proxy = data['proxy']
        self.email_suffix = data['email_suffix']

        self.thread_local = threading.local()
        self.cleanup_lock = threading.Lock()
        self.active_resources = []  # 记录资源以便关闭

        self.results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Results')
        os.makedirs(self.results_dir, exist_ok=True)

    @abstractmethod
    def launch_browser(self):
        """
        获取浏览器实例,返回playwright_instance, browser_instance
        """
        pass

    @abstractmethod
    def handle_captcha(self, page):
        """
        验证码处理流程
        """
        pass

    @abstractmethod 
    def clean_up(self, page=None, type="all_browser"):
        """
        清理自己创建的内容
        一个是单进程结束后关闭进程，另一个是程序结束后清除所有内容
        """
        pass

    @abstractmethod
    def get_thread_page(self):
        """
        返回页面
        """

    def get_last_pos(self):
        """获取当前线程的上一次鼠标位置 (x, y)"""
        return getattr(self.thread_local, 'last_pos', None)

    def set_last_pos(self, x, y):
        """设置当前线程的鼠标位置 (x, y)"""
        self.thread_local.last_pos = (float(x), float(y))

    def reset_last_pos(self):
        """重置当前线程的坐标历史"""
        if hasattr(self.thread_local, 'last_pos'):
            del self.thread_local.last_pos

    def wait_random_ratio(self, page, min_ratio, delta=0.02):

        actual_ratio = random.uniform(min_ratio, min_ratio + delta)
        page.wait_for_timeout(actual_ratio * self.wait_time)

    def smooth_move_to(self, page, target_x, target_y, steps=None):
        """从上一次坐标滑动到目标坐标"""
        last_pos = self.get_last_pos()
        if not last_pos:
            last_pos = (random.uniform(150, 450), random.uniform(100, 350))
            try:
                page.mouse.move(last_pos[0], last_pos[1])
            except Exception:
                pass

        if steps is None:
            steps = random.randint(6, 14)

        try:
            page.mouse.move(target_x, target_y, steps=steps)
        except Exception:
            pass

        self.set_last_pos(target_x, target_y)

    def smooth_click(self, page, locator, offset_range=5, click_delay_range=(60, 160)):
        """点击方法"""
        try:
            box = locator.bounding_box()
            if not box:
                locator.click()
                return False

            tx = box['x'] + box['width'] / 2 + random.uniform(-offset_range, offset_range)
            ty = box['y'] + box['height'] / 2 + random.uniform(-offset_range, offset_range)

            self.smooth_move_to(page, tx, ty)

            pause_ms = random.randint(click_delay_range[0], click_delay_range[1])
            page.wait_for_timeout(pause_ms)

            page.mouse.click(tx, ty)
            self.set_last_pos(tx, ty)
            return True
        except Exception:
            try:
                locator.click()
            except Exception:
                pass
            return False

    def smooth_type(self, page, locator, text, click_first=True):
        """输入方法"""
        if click_first:
            self.smooth_click(page, locator)

        for char in text:
            try:
                locator.type(char, delay=random.randint(40, 110))
            except Exception:
                break


    def get_thread_browser(self):
        """
        通用逻辑:获取不同进程的浏览器
        """
        if not hasattr(self.thread_local, "browser"):
            p, b = self.launch_browser()
            if not p:
                return False

            self.thread_local.playwright = p
            self.thread_local.browser = b

            with self.cleanup_lock:
                self.active_resources.append((p, b))

        return self.thread_local.browser

    def get_mail_provider(self, email, page):
        """
        通用逻辑:获取辅助邮箱的类
        """

        from .moemail_controller import MoemailController
        from .graph_controller import OutlookGraphController

        providers = {
            "moemail": MoemailController,
            "outlookgraph": OutlookGraphController
        }
        provider_cls = providers.get(self.choosed_mail)
        return provider_cls(email, page) if provider_cls else None

    def outlook_register(self, page, email, password):
        """
        通用逻辑:注册邮箱
        """

        self.reset_last_pos()
        fake = Faker()

        lastname = fake.last_name()
        firstname = fake.first_name()
        year = str(random.randint(1975, 2002))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 28))

        try:
            page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=40000, wait_until="domcontentloaded")
            start_time = time.time()
            # 仅中国大陆 IP 首次会弹出“个人数据导出许可 (同意并继续)”，海外家宽直接进入“新建电子邮件”
            for _ in range(40):
                if page.get_by_text("同意并继续").count() > 0:
                    start_time = time.time()
                    self.wait_random_ratio(page, 0.06)
                    self.smooth_click(page, page.get_by_text("同意并继续"))
                    break
                if page.locator('[aria-label="新建电子邮件"]').count() > 0:
                    start_time = time.time()
                    break
                page.wait_for_timeout(800)
            page.locator('[aria-label="新建电子邮件"]').wait_for(state="visible", timeout=25000)
        except Exception:
            print("[Error: IP] - IP质量不佳，无法进入注册界面。")
            return False

        try:
            if self.email_suffix == "@hotmail.com":
                self.wait_random_ratio(page, 0.06)
                domain_btn = page.get_by_text("@outlook.com")
                self.smooth_click(page, domain_btn)
                option_btn = page.locator(f'[role="option"]:text-is("@hotmail.com")')
                self.smooth_click(page, option_btn)


            email_input = page.locator('[aria-label="新建电子邮件"]')
            self.smooth_type(page, email_input, email)

            primary_btn = page.locator('[data-testid="primaryButton"]')
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.04)

            pwd_input = page.locator('[type="password"]')
            pwd_input.wait_for(state='visible', timeout=25000)
            self.smooth_type(page, pwd_input, password)
            self.wait_random_ratio(page, 0.03)
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.03)

            if page.get_by_text("请重试。如果仍然不起作用，请稍后再试。").count() > 0:
                print("[Error: IP or browser] - 当前IP注册频率过快。检查IP与是否为指纹浏览器并关闭了无头模式。")
                return False

            year_input = page.locator('[name="BirthYear"]')
            year_input.wait_for(state='visible', timeout=25000)
            if year_input.count() > 0:
                self.smooth_click(page, year_input)
                year_input.fill(year)

            month_btn = page.locator('[name="BirthMonth"]')
            self.smooth_click(page, month_btn)
            self.wait_random_ratio(page, 0.03)
            m_opt = page.locator(f'[role="option"]:text-is("{month}月")')
            self.smooth_click(page, m_opt)

            self.wait_random_ratio(page, 0.03)
            day_btn = page.locator('[name="BirthDay"]')
            self.smooth_click(page, day_btn)
            self.wait_random_ratio(page, 0.03)

            d_opt = page.locator(f'[role="option"]:text-is("{day}日")')
            if d_opt.count() > 0:
                try:
                    d_opt.scroll_into_view_if_needed()
                except Exception:
                    pass
            self.smooth_click(page, d_opt)

            self.smooth_click(page, primary_btn)

            lname_input = page.locator('#lastNameInput')
            lname_input.wait_for(state='visible', timeout=20000)
            self.smooth_type(page, lname_input, lastname)

            self.wait_random_ratio(page, 0.02)
            fname_input = page.locator('#firstNameInput')
            fname_input.wait_for(state='visible', timeout=20000)
            self.smooth_type(page, fname_input, firstname)

            # 黄金等待 1：确保 collector-*.hsprotect.net/api/v2/msft 至少完成 3 次遥测上报（_px3 签发与绑定）
            for _ in range(45):
                if getattr(page, "msft_count", 0) >= 3:
                    break
                page.wait_for_timeout(400)
            page.wait_for_timeout(random.randint(600, 1100))

            # 黄金等待 2：确保从进入页面到提交姓名表单的总时长 >= bot_protection_wait (16s)，防止因海外无弹窗填表过快触发 403
            elapsed_ms = (time.time() - start_time) * 1000
            if elapsed_ms < self.wait_time:
                page.wait_for_timeout(self.wait_time - elapsed_ms + random.randint(400, 900))

            self.smooth_click(page, primary_btn)
            page.locator('span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]').wait_for(state='detached', timeout=22000)
            page.wait_for_timeout(400)

            if page.get_by_text('一些异常活动').count() or page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count() > 0:
                print("[Error: IP or browser] - 当前IP注册频率过快。检查IP与是否为指纹浏览器并关闭了无头模式。")
                return False

            if page.locator('iframe#enforcementFrame').count() > 0:
                print("[Error: FunCaptcha] - 验证码类型错误，非按压验证码。")
                return False

            captcha_result = self.handle_captcha(page)
            if not captcha_result:
                raise TimeoutError

        except Exception:
            print("[Error: IP] - 加载超时或因触发机器人检测导致按压次数达到最大仍未通过。")
            return False

        # --- 验证码通过后，绑定辅助邮箱服务 ---
        try:
            current_full_email = f"{email}{self.email_suffix}"
            mail_provider = self.get_mail_provider(current_full_email, page)
            if not mail_provider:
                print(f"[Error: Config] - 未配置或不支持的邮箱系统: {self.choosed_mail}")
                return False

            # 挂载到 page 对象上，方便写入 secured_tokens.txt
            recovery_email = mail_provider.get_email()
            page.recovery_email = recovery_email

            # 新版 interrupt/credentialaction 引导页：必须先点开“添加电子邮件”入口，输入框才会显示。
            # 关键：这里要判断「可见性」而不是「存在性」。该页输入框常已存在于 DOM 但处于隐藏状态，
            # 若沿用 count()>0 会直接跳过点击步骤，导致下方 wait_for(state="visible") 必然 20s 超时，
            # 表现为「账号已注册成功，但辅助邮箱绑定失败」。
            email_selector = '#EmailAddress, input[type="email"], input.fui-Input__input, input[id^="floatingLabelInput"]'
            for entry_round in range(4):
                try:
                    if page.locator(email_selector).first.is_visible():
                        break
                except Exception:
                    pass

                clicked = False
                for btn in (
                    page.get_by_text("添加电子邮件", exact=True),
                    page.locator('button:has-text("添加电子邮件")'),
                    page.locator('[data-testid="primaryButton"]'),
                ):
                    try:
                        if btn.count() > 0 and btn.first.is_visible():
                            self.wait_random_ratio(page, 0.03)
                            self.smooth_click(page, btn.first)
                            try:
                                btn.first.evaluate("el => el.click()")
                            except Exception:
                                pass
                            clicked = True
                            break
                    except Exception:
                        continue
                print(
                    f"  [Proof Debug] 入口轮次 {entry_round + 1}/4: 已点击={clicked} "
                    f"输入框数量={page.locator(email_selector).count()}",
                    flush=True,
                )
                page.wait_for_timeout(2000)

            email_input = page.locator('#EmailAddress, input[type="email"], input.fui-Input__input, input[id^="floatingLabelInput"], input[type="text"]').first
            email_input.wait_for(state="visible", timeout=20000)
            self.smooth_type(page, email_input, recovery_email)

            self.wait_random_ratio(page, 0.03)
            next_btn = page.locator('[data-testid="primaryButton"], #iNext, button[type="submit"], input[type="submit"]').first
            self.smooth_click(page, next_btn)

            # 等待进入验证码输入界面
            for _ in range(30):
                page.wait_for_timeout(500)
                try:
                    btxt = page.locator("body").inner_text()
                    if "添加电子邮件地址" not in btxt and any(k in btxt for k in ("代码", "验证", "安全", "Code", "code")):
                        break
                except Exception:
                    pass

            verify_code = mail_provider.get_verify_code()
            if not verify_code:
                raise RuntimeError("未能从辅助邮箱收到 6 位微软安全代码")

            vis_inputs = [
                el for el in page.locator('#iOttText, input.fui-Input__input, input[type="tel"], input[type="number"], input[type="text"]').all()
                if el.is_visible()
            ]
            if len(vis_inputs) >= 6:
                self.smooth_click(page, vis_inputs[0])
                for idx_c, ch in enumerate(verify_code[:6]):
                    try:
                        vis_inputs[idx_c].fill(ch)
                        page.wait_for_timeout(90)
                    except Exception:
                        page.keyboard.type(ch, delay=90)
            elif len(vis_inputs) >= 1:
                self.smooth_type(page, vis_inputs[0], verify_code)
            else:
                code_input = page.locator("input").first
                code_input.wait_for(state="visible", timeout=15000)
                self.smooth_type(page, code_input, verify_code)

            self.wait_random_ratio(page, 0.03)
            finish_btn = page.locator('[data-testid="primaryButton"], #iNext, button[type="submit"], input[type="submit"]')
            if finish_btn.count() > 0 and finish_btn.first.is_visible():
                try:
                    if not finish_btn.first.is_disabled():
                        self.smooth_click(page, finish_btn.first)
                except Exception:
                    self.smooth_click(page, finish_btn.first)

            # 如果跳转后出现“保持登录状态？”等二次确认提示（KMSI）或微软账户隐私政策须知（Privacy Notice），自动点击通过
            for _ in range(35):
                page.wait_for_timeout(1000)
                curr_url = page.url or ""
                if "privacynotice" in curr_url or "notice" in curr_url:
                    notice_btn = page.locator('button:has-text("OK"), button:has-text("确定"), #idBtn_Accept, [data-testid="primaryButton"]')
                    if notice_btn.count() > 0 and notice_btn.first.is_visible():
                        try:
                            self.smooth_click(page, notice_btn.first)
                            page.wait_for_timeout(1500)
                            continue
                        except Exception:
                            pass

                if page.locator('[aria-label="新邮件"], [aria-label="New mail"]').count() > 0 or "outlook.live.com/mail" in curr_url:
                    break

                for btn_txt in ("是", "继续", "确定", "下一步", "跳过", "接受", "OK", "Got it", "Accept", "Next", "Yes", "Continue", "Skip"):
                    b_loc = page.locator(f'button:has-text("{btn_txt}"), input[value="{btn_txt}"], [data-testid="primaryButton"], #idSIButton9')
                    if b_loc.count() > 0 and b_loc.first.is_visible():
                        try:
                            self.smooth_click(page, b_loc.first)
                            page.wait_for_timeout(1000)
                            break
                        except Exception:
                            pass

        except Exception as e:
            print(f"[Error: Proof Email] - {email}{self.email_suffix}注册成功，但辅助邮箱绑定失败: {e}")
            try:
                page.screenshot(path="/tmp/proof_error_debug.png")
                print(f"  [Proof Debug URL]: {page.url}")
                for el in page.locator("input, button, a, [role='button']").all():
                    print(f"  [Proof Debug EL]: {el.evaluate('e => e.outerHTML')[:220]}")
            except Exception:
                pass
            filename = os.path.join(self.results_dir, 'unbound_accounts.txt')
            with open(filename, 'a', encoding='utf-8') as f:
                f.write(f"{email}{self.email_suffix}: {password}\n")
            return False

        filename = os.path.join(self.results_dir, 'secured_accounts.txt' if self.enable_oauth2 else 'secured_no_token.txt')
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(f"{email}{self.email_suffix}: {password}---{recovery_email}\n")
        print(f'[Success: Email Registration & Security Bound] - {email}{self.email_suffix}: {password}---{recovery_email}')

        if not self.enable_oauth2:
            return True

        try:
            # 确保浏览器完全进入 Outlook 网页版并完成底层 Mailbox Store 初始化挂载 (Provisioning)
            for loop_idx in range(3):
                curr_u = page.url or ""
                if "privacynotice" in curr_u or "notice" in curr_u:
                    notice_btn = page.locator('button:has-text("OK"), button:has-text("确定"), #idBtn_Accept, [data-testid="primaryButton"]')
                    if notice_btn.count() > 0 and notice_btn.first.is_visible():
                        self.smooth_click(page, notice_btn.first)
                        page.wait_for_timeout(2000)

                for btn_txt in ("是", "继续", "确定", "下一步", "跳过", "接受", "OK", "Got it", "Accept", "Next", "Yes"):
                    b_loc = page.locator(f'button:has-text("{btn_txt}"), input[value="{btn_txt}"], [data-testid="primaryButton"], #idSIButton9')
                    if b_loc.count() > 0 and b_loc.first.is_visible():
                        try:
                            self.smooth_click(page, b_loc.first)
                            page.wait_for_timeout(1000)
                            break
                        except Exception:
                            pass

                if "outlook.live.com" not in page.url:
                    page.goto("https://outlook.live.com/mail/0/", timeout=25000, wait_until="domcontentloaded")

                try:
                    page.locator('[aria-label="新邮件"], [aria-label="New mail"], [data-folder-name="inbox"], [title*="收件箱"], [title*="Inbox"]').first.wait_for(timeout=15000)
                    print(f"[Success: Mailbox Store Ready] - 邮箱已完成首次 Web 初始化挂载: {email}{self.email_suffix}")
                    page.wait_for_timeout(4000)  # 给予后台 Exchange 数据分区 4 秒绑定缓冲
                    return True
                except Exception:
                    page.wait_for_timeout(2000)

            return True
        except Exception:
            print('[Warn: Mailbox UI Slow] - 邮箱前端加载较慢，继续尝试提取 OAuth2 Token...')
            return True


class BaseEmailProvider(ABC):
    """
    邮件通用接口
    """

    def __init__(self, email, page):

        self.page = page
        self.current_email = email
        self.regex = r"安全代码: (\d{6})"
        self.subject = "个人 Microsoft 帐户安全代码"
        self.masked_email = f"{self.current_email[:2]}**{self.current_email.split('@')[0][-1]}@{self.current_email.split('@')[1]}"
        self.config = load_config()

    @abstractmethod
    def get_email(self):
        """获取邮箱"""
        pass

    @abstractmethod
    def get_verify_code(self) -> str:
        """获取验证码，注意用掩码(36^3)"""
        pass

