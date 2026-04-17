#!/usr/bin/env python3
"""
Proxy diagnostic script.
Tests the proxy at three levels: requests, Playwright direct, crawl4ai.
Also tests the local-forwarder workaround for Chromium proxy auth issues.

Usage:
    python test_proxy.py --proxy "http://user:pass@host:port"
    python test_proxy.py  # tests without proxy
"""

import argparse
import asyncio
import sys
from urllib.parse import urlparse


def parse_proxy(proxy_url: str) -> dict:
    p = urlparse(proxy_url)
    return {
        "server": f"{p.scheme}://{p.hostname}:{p.port}",
        "username": p.username or "",
        "password": p.password or "",
    }


# ---------------------------------------------------------------------------
# Level 1: requests (pure HTTP)
# ---------------------------------------------------------------------------

def test_requests(proxy_url: str | None):
    print("\n── Level 1: requests library ──────────────────────")
    try:
        import requests
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        r = requests.get("https://www.google.com", proxies=proxies, timeout=15)
        print(f"  OK  status={r.status_code}  via={'proxy' if proxy_url else 'direct'}")
    except Exception as e:
        print(f"  FAIL  {e}")


# ---------------------------------------------------------------------------
# Level 2: Playwright direct (no crawl4ai)
# ---------------------------------------------------------------------------

async def test_playwright(proxy_url: str | None):
    print("\n── Level 2: Playwright direct ─────────────────────")
    try:
        from playwright.async_api import async_playwright
        proxy_cfg = parse_proxy(proxy_url) if proxy_url else None

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                proxy=proxy_cfg,
            )
            page = await browser.new_page()
            print(f"  Browser launched OK  proxy={proxy_cfg['server'] if proxy_cfg else 'none'}")

            try:
                resp = await page.goto(
                    "https://www.google.com",
                    timeout=30_000,
                    wait_until="domcontentloaded",
                )
                print(f"  google.com  status={resp.status if resp else 'no response'}")
            except Exception as e:
                print(f"  FAIL google.com  {e}")

            try:
                resp = await page.goto(
                    "https://www.google.com/maps/search/coffee/@51.5074,-0.1278,14z",
                    timeout=60_000,
                    wait_until="domcontentloaded",
                )
                print(f"  maps URL    status={resp.status if resp else 'no response'}")
                title = await page.title()
                print(f"  page title: {title}")
            except Exception as e:
                print(f"  FAIL maps URL  {e}")

            await browser.close()
    except Exception as e:
        print(f"  FAIL (browser launch)  {e}")


# ---------------------------------------------------------------------------
# Level 3: crawl4ai
# ---------------------------------------------------------------------------

async def test_crawl4ai(proxy_url: str | None):
    print("\n── Level 3: crawl4ai ──────────────────────────────")
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
        from crawl4ai.cache_context import CacheMode

        browser_cfg = BrowserConfig(
            headless=True,
            verbose=False,
            proxy=proxy_url,
        )
        run_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=60_000,
            verbose=False,
        )

        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await crawler.arun(
                url="https://www.google.com",
                config=run_cfg,
            )
            if result.success:
                print(f"  OK  html_length={len(result.html)}")
            else:
                print(f"  FAIL  {result.error_message}")
    except Exception as e:
        print(f"  FAIL  {e}")


# ---------------------------------------------------------------------------
# Local proxy forwarder (pure Python, no dependencies)
# ---------------------------------------------------------------------------

import base64

class LocalProxyForwarder:
    """
    Minimal HTTP/CONNECT proxy that forwards to an authenticated upstream.
    Chromium connects to localhost (no auth), this adds Proxy-Authorization
    for the upstream — bypassing Chromium's broken headless proxy auth.
    """

    def __init__(self, upstream_host: str, upstream_port: int,
                 username: str, password: str, local_port: int = 18888):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.auth_header = (
            b"Proxy-Authorization: Basic "
            + base64.b64encode(f"{username}:{password}".encode())
            + b"\r\n"
        )
        self.local_port = local_port
        self._server = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.local_port
        )

    def stop(self) -> None:
        if self._server:
            self._server.close()

    async def _handle(self, client_r, client_w):
        try:
            first_line = await client_r.readline()
            if not first_line:
                return
            headers: list[bytes] = []
            while True:
                line = await client_r.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                headers.append(line)

            up_r, up_w = await asyncio.open_connection(
                self.upstream_host, self.upstream_port
            )
            up_w.write(first_line)
            up_w.write(self.auth_header)
            for h in headers:
                if not h.lower().startswith(b"proxy-authorization"):
                    up_w.write(h)
            up_w.write(b"\r\n")
            await up_w.drain()

            # Forward upstream response back to client
            resp = await up_r.readline()
            client_w.write(resp)
            while True:
                line = await up_r.readline()
                client_w.write(line)
                if line in (b"\r\n", b"\n", b""):
                    break
            await client_w.drain()

            # Bidirectional pipe
            await asyncio.gather(
                self._pipe(client_r, up_w),
                self._pipe(up_r, client_w),
            )
        except Exception:
            pass
        finally:
            client_w.close()

    @staticmethod
    async def _pipe(src, dst):
        try:
            while chunk := await src.read(65536):
                dst.write(chunk)
                await dst.drain()
        except Exception:
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass


def build_forwarder(proxy_url: str, local_port: int = 18888) -> LocalProxyForwarder:
    p = urlparse(proxy_url)
    return LocalProxyForwarder(
        upstream_host=p.hostname,
        upstream_port=p.port,
        username=p.username or "",
        password=p.password or "",
        local_port=local_port,
    )


# ---------------------------------------------------------------------------
# Level 4: local forwarder → Playwright without auth
# ---------------------------------------------------------------------------

async def test_local_forwarder(proxy_url: str, local_port: int = 18888):
    print(f"\n── Level 4: local forwarder (pure Python) → Playwright ─")
    try:
        from playwright.async_api import async_playwright

        forwarder = build_forwarder(proxy_url, local_port)
        await forwarder.start()
        print(f"  Forwarder running on localhost:{local_port}")

        local_proxy = {"server": f"http://localhost:{local_port}"}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, proxy=local_proxy)
            page = await browser.new_page()

            try:
                resp = await page.goto("https://www.google.com", timeout=30_000,
                                       wait_until="domcontentloaded")
                print(f"  google.com  status={resp.status if resp else 'no response'}")
            except Exception as e:
                print(f"  FAIL google.com  {e}")

            try:
                resp = await page.goto(
                    "https://www.google.com/maps/search/coffee/@51.5074,-0.1278,14z",
                    timeout=60_000, wait_until="domcontentloaded")
                print(f"  maps URL    status={resp.status if resp else 'no response'}")
                title = await page.title()
                print(f"  page title: {title}")
            except Exception as e:
                print(f"  FAIL maps URL  {e}")

            await browser.close()

        forwarder.stop()
    except Exception as e:
        print(f"  FAIL  {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default=None,
                        help='Proxy URL e.g. "http://user:pass@host:port"')
    parser.add_argument("--local-port", type=int, default=18888,
                        help="Local port for the forwarder (default: 18888)")
    args = parser.parse_args()

    proxy = args.proxy
    print(f"Proxy: {proxy or 'none (direct connection)'}")

    test_requests(proxy)
    await test_playwright(proxy)
    await test_crawl4ai(proxy)
    if proxy:
        await test_local_forwarder(proxy, args.local_port)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
