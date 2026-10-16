import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from controllers.patchright_controller import PatchrightController
from controllers.playwright_controller import PlaywrightController
from get_token import get_access_token
from utils import generate_strong_password, load_config, random_email


def sync_card_to_mailsieve(card_line: str, urls: list[str]) -> None:
    """将自建注册成功的 4 段式 Hotmail 卡密自动同步至 Mail-Sieve 面板并触发审查。"""
    payload = json.dumps({
        "text": card_line,
        "batch_no": "自建Hotmail_独享",
        "source_info": "OutlookRegister自产",
        "auto_audit": True,
        "target_service": "openai",
    }).encode("utf-8")
    for base_url in urls:
        if not base_url:
            continue
        try:
            req = urllib.request.Request(
                base_url.rstrip("/") + "/api/import-text",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=12).read()
            print(f"[Synced -> Mail-Sieve] {base_url}")
        except Exception as e:
            print(f"[Warn: Mail-Sieve sync skipped ({base_url})] {e}")


def process_single_flow(controller):
    page = None
    try:
        page = controller.get_thread_page()

        email = random_email()
        password = generate_strong_password()

        result = controller.outlook_register(page, email, password)

        if result and not controller.enable_oauth2:
            return True
        elif not result:
            return False

        token_result = get_access_token(page, email)
        if token_result[0]:
            refresh_token, access_token, expire_at = token_result
            full_email = f"{email}{controller.email_suffix}"
            client_id = controller.config.get("oauth2", {}).get("client_id") or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
            results_dir = os.path.join(os.path.dirname(__file__), "Results")

            # 1. 原版 6 段格式
            with open(os.path.join(results_dir, "secured_tokens.txt"), "a", encoding="utf-8") as f2:
                f2.write(f"{full_email}---{password}---{refresh_token}---{access_token}---{expire_at}---{getattr(page, 'recovery_email', '')}\n")

            # 2. 注册机与 Mail-Sieve 通用 4 段式标准卡密格式
            card_4part = f"{full_email}----{password}----{client_id}----{refresh_token}"
            with open(os.path.join(results_dir, "hotmail_4part_cards.txt"), "a", encoding="utf-8") as f4:
                f4.write(card_4part + "\n")

            # 3. 自动滚雪球加入辅助邮箱池 recovery_pool.txt
            with open(os.path.join(results_dir, "recovery_pool.txt"), "a", encoding="utf-8") as fp:
                fp.write(card_4part + "\n")

            print(f"[Success: 独享Hotmail产号+Token提取成功] -> {card_4part[:75]}...")

            # 4. 自动同步至 Mail-Sieve 面板（本地 + 甲骨文新加坡）
            sync_urls = controller.config.get("sync_to_mailsieve_urls") or []
            sync_card_to_mailsieve(card_4part, sync_urls)
            return True
        else:
            return False

    except Exception as e:
        # 面板 stdout 重定向到文件时是块缓冲，异常必须 flush 才不会被吞掉
        print(f"[process_single_flow 异常] {type(e).__name__}: {e}", flush=True)
        return False

    finally:
        controller.clean_up(page, "done_browser")


def run_concurrent_flows(controller, concurrent_flows=2, max_tasks=10):
    task_counter = 0
    succeeded_tasks = 0
    failed_tasks = 0

    with ThreadPoolExecutor(max_workers=concurrent_flows) as executor:
        running_futures = set()

        while task_counter < max_tasks or len(running_futures) > 0:
            done_futures = {f for f in running_futures if f.done()}
            for future in done_futures:
                try:
                    if future.result():
                        succeeded_tasks += 1
                    else:
                        failed_tasks += 1
                except Exception as e:
                    failed_tasks += 1
                    print(e)
                running_futures.remove(future)

            while len(running_futures) < concurrent_flows and task_counter < max_tasks:
                new_future = executor.submit(process_single_flow, controller)
                running_futures.add(new_future)
                task_counter += 1
                print(f"已提交 {task_counter}/{max_tasks} 自建邮箱注册任务.")

            time.sleep(0.5)

    print(f"\n[Result] - 共: {max_tasks}, 成功 {succeeded_tasks}, 失败 {failed_tasks}")


if __name__ == "__main__":
    data = load_config()
    os.makedirs("Results", exist_ok=True)

    max_tasks = data["max_tasks"]
    concurrent_flows = data["concurrent_flows"]

    if data["choose_browser"] == "patchright":
        selected_controller = PatchrightController()
    elif data["choose_browser"] == "playwright":
        selected_controller = PlaywrightController()
    else:
        raise SystemExit("不支持的浏览器类型，请填写 patchright 或 playwright")

    try:
        run_concurrent_flows(selected_controller, concurrent_flows, max_tasks)
    finally:
        selected_controller.clean_up(type="all_browser")
