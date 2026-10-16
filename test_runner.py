import asyncio
import json
import time
from loguru import logger
import requests
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

# Configure Loguru for thread-safe structured output
logger.remove()
logger.add(sys.stderr if 'sys' in globals() else logger.add, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>")
logger.add("results.jsonl", format="{message}", level="INFO") # Appends JSON lines directly

import sys

def fetch_and_validate_proxies():
    """Dynamically fetches public proxies and performs pre-flight validation."""
    logger.info("Fetching live public proxy pool from API...")
    raw_proxies = []
    try:
        response = requests.get(
            "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text",
            timeout=10
        )
        if response.status_code == 200:
            raw_proxies = [line.strip() for line in response.text.splitlines() if line.strip()]
            logger.info(f"Retrieved {len(raw_proxies)} raw endpoints. Running pre-flight health check...")
    except Exception as e:
        logger.error(f"Failed to fetch proxy list: {e}")
        return ["http://136.226.118.194:9480", "http://104.129.203.85:9480"] # Fallback array

    # Rapid pre-flight validation (filter out dead nodes)
    valid_proxies = []
    for p in raw_proxies[:40]:  # Sample top 40 for speed
        proxy_url = f"http://{p}"
        try:
            test_resp = requests.get(
                "https://api.ipify.org?format=json",
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=3
            )
            if test_resp.status_code == 200:
                valid_proxies.append(proxy_url)
                if len(valid_proxies) >= 15:  # Keep top 15 verified live nodes
                    break
        except Exception:
            continue

    logger.info(f"Pre-flight complete. Verified active proxy count: {len(valid_proxies)}")
    if not valid_proxies:
        # Fallback if public pool is completely unresponsive
        valid_proxies = ["http://136.226.118.194:9480"]
    return valid_proxies

async def simulate_session(session_id: int, target_url: str, proxy_pool: list):
    """Executes a single browser worker session with stealth patching and self-healing retries."""
    max_retries = 3
    success = False
    start_time = time.time()
    assigned_proxy = "Direct/None"
    
    async with async_playwright() as p:
        for attempt in range(1, max_retries + 1):
            if not proxy_pool:
                break
            
            # Select proxy via round-robin index rotation
            assigned_proxy = proxy_pool[(session_id + attempt) % len(proxy_pool)]
            
            browser = None
            try:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu"
                    ],
                    proxy={"server": assigned_proxy}
                )
                
                # Isolated context configuration with randomized headers
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720}
                )
                
                page = await context.new_page()
                
                # Apply elite stealth fingerprint patches
                await stealth_async(page)
                
                logger.info(f"[Worker {session_id}] Attempt {attempt} | Routing via {assigned_proxy} -> Navigating...")
                
                # Navigate to target endpoint with strict timeout bounds
                response = await page.goto(target_url, timeout=25000)
                status_code = response.status if response else 0
                
                if status_code in [200, 301, 302]:
                    await asyncio.sleep(4)  # Observation/Telemetry window
                    success = True
                    latency = round(time.time() - start_time, 2)
                    
                    # Log structured success telemetry
                    telemetry = {
                        "worker_id": session_id,
                        "status": "SUCCESS",
                        "proxy": assigned_proxy,
                        "http_status": status_code,
                        "latency_seconds": latency,
                        "attempt": attempt
                    }
                    logger.info(f"[Worker {session_id}] COMPLETED successfully in {latency}s.")
                    logger.info(json.dumps(telemetry))
                    break
                else:
                    raise Exception(f"Received blocked/error HTTP status code: {status_code}")

            except Exception as e:
                logger.warning(f"[Worker {session_id}] Attempt {attempt} failed using {assigned_proxy}: {e}")
                if assigned_proxy in proxy_pool:
                    proxy_pool.remove(assigned_proxy)  # Blacklist dead proxy immediately
                await asyncio.sleep(1)
                
            finally:
                if browser:
                    await browser.close()

    if not success:
        latency = round(time.time() - start_time, 2)
        telemetry = {
            "worker_id": session_id,
            "status": "FAILED",
            "proxy": assigned_proxy,
            "latency_seconds": latency,
            "attempt": max_retries
        }
        logger.error(f"[Worker {session_id}] PERMANENTLY FAILED after {max_retries} attempts.")
        logger.info(json.dumps(telemetry))

async def batch_orchestrator():
    target_url = "https://httpbin.org/ip"  # Replace with your specific target endpoint
    total_instances = 100                  # Total queue size
    concurrency_limit = 10                 # Strict semaphore throttle limit
    
    proxy_pool = fetch_and_validate_proxies()
    semaphore = asyncio.Semaphore(concurrency_limit)

    async def bounded_worker(i):
        async with semaphore:
            await simulate_session(i, target_url, proxy_pool)

    logger.info(f"Starting batch execution: {total_instances} tasks with max concurrency {concurrency_limit}.")
    tasks = [bounded_worker(i) for i in range(total_instances)]
    await asyncio.gather(*tasks)
    logger.info("Batch execution cycle completed. Telemetry exported to results.jsonl.")

if __name__ == "__main__":
    asyncio.run(batch_orchestrator())
