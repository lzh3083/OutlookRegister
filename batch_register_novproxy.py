import os
import sys
import time
from novproxy_bridge import (
    NovProxyBridge,
    cleanup_leftover_browsers,
    fetch_verified_novproxy_nodes,
    load_used_ips,
    mark_ip_used,
)
from controllers.patchright_controller import PatchrightController
from main import process_single_flow

PRE_USED_IPS = [
    "121.145.115.172",
    "99.217.182.75",
    "47.55.3.34",
    "124.171.128.175",
    "79.212.228.98",
    "92.209.157.77",
    "37.67.157.68",
    "77.175.6.42",
    "62.45.65.90",
    "94.34.1.247",
    "5.225.1.73",
    "212.57.74.139",
    "142.118.170.144",
    "192.249.132.35",
    "93.229.132.9",
    "92.208.228.57",
    "99.253.225.78",
    "118.160.131.165",
    "35.146.184.7",
    "38.178.121.48",
    "163.227.253.74",
    "1.146.114.181",
]


def main(target_count: int = 10):
    cleanup_leftover_browsers()
    existing = load_used_ips()
    for ip in PRE_USED_IPS:
        if ip not in existing:
            mark_ip_used(ip)

    bridge = NovProxyBridge()
    proxy_url = bridge.start()
    print(f"[*] NovProxyBridge started at {proxy_url}", flush=True)

    cards_file = os.path.join(os.path.dirname(__file__), "Results", "hotmail_4part_cards.txt")
    initial_existing = 0
    if os.path.exists(cards_file):
        with open(cards_file, "r", encoding="utf-8") as f:
            initial_existing = sum(1 for line in f if "----" in line)
    
    final_target = initial_existing + target_count
    new_succeeded = 0
    print(f"[*] 当前已有卡密: {initial_existing} 个，本次计划新注册: {target_count} 个，目标达成总数: {final_target} 个", flush=True)

    attempts = 0
    batch_round = 0

    try:
        while new_succeeded < target_count and batch_round < 25:
            batch_round += 1
            print(f"\n[*] [Round {batch_round}] Fetching fresh unused global NovProxy residential nodes...", flush=True)
            nodes = fetch_verified_novproxy_nodes(
                regions=("TW", "JP", "KR", "DE", "CA", "CH", "AT", "FR", "AU", "SG", "NZ", "SE", "NO", "FI", "IT", "ES", "NL", "BE"),
                per_region=4,
            )
            print(f"[*] [Round {batch_round}] Got {len(nodes)} verified unused residential IPs.", flush=True)
            if not nodes:
                time.sleep(5)
                continue

            for n in nodes:
                if new_succeeded >= target_count:
                    break
                attempts += 1
                mark_ip_used(n["exit_ip"])
                bridge.set_novproxy_node(n["node"])

                ctrl = PatchrightController()
                ctrl.proxy = proxy_url
                ctrl.current_timezone = n["timezone"]
                print(
                    f"\n=== [Attempt #{attempts} | Progress: +{new_succeeded}/{target_count} (Total: {initial_existing + new_succeeded}/{final_target})] "
                    f"Node={n['node']} | IP={n['exit_ip']} ({n['country']} - {n['isp']}) ===",
                    flush=True,
                )
                try:
                    ok = process_single_flow(ctrl)
                    if ok:
                        new_succeeded += 1
                        print(f"🎉 [BATCH PROGRESS] 本次新产出: {new_succeeded}/{target_count} (当前总计: {initial_existing + new_succeeded}/{final_target})", flush=True)
                    else:
                        print(f"[-] Attempt #{attempts} failed. Current Progress: +{new_succeeded}/{target_count}", flush=True)
                except Exception as e:
                    print(f"[!] Unexpected error on attempt #{attempts}: {e}", flush=True)
                finally:
                    ctrl.clean_up(type="all_browser")
    finally:
        bridge.stop()
        print(f"\n[=== FINAL SUMMARY ===] 本次成功新注册: {new_succeeded}/{target_count} (当前总计: {initial_existing + new_succeeded}) 尝试次数: {attempts}", flush=True)


if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    main(target)
