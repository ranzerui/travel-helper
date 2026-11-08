import asyncio
from contextlib import asynccontextmanager, ExitStack, suppress
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request


def edge_command(executable, profile, port):
    command = [str(executable), "--no-first-run", "--no-default-browser-check",
               "--disable-gpu", "--disable-crash-reporter", "--disable-extensions",
               "--disable-sync", "--remote-debugging-address=127.0.0.1",
               f"--remote-debugging-port={port}", f"--user-data-dir={profile}"]
    if os.environ.get("TRAVEL_BROWSER_HEADLESS", "1") != "0":
        command.append("--headless=new")
    if os.environ.get("TRAVEL_BROWSER_NO_SANDBOX") == "1":
        command.extend(["--no-sandbox", "--disable-gpu-sandbox"])
    return command + ["about:blank"]


async def wait_for_debugger(process, profile):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def read_version():
        port = int((Path(profile) / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0])
        endpoint = f"http://127.0.0.1:{port}"
        with opener.open(endpoint + "/json/version", timeout=0.5) as response:
            return json.load(response)["webSocketDebuggerUrl"]

    for attempt in range(60):
        if process.poll() is not None:
            raise RuntimeError(f"独立 Edge 启动失败，退出码 {process.returncode}；详见 edge.log")
        try:
            return await asyncio.to_thread(read_version)
        except (OSError, ValueError, KeyError, IndexError):
            await asyncio.sleep(0.25)
    raise TimeoutError("独立 Edge 调试连接未就绪；详见 edge.log")


@asynccontextmanager
async def open_browser(folder):
    from browser_use import Browser

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    process = None
    browser = None
    with ExitStack() as resources:
        try:
            cdp_url = os.environ.get("BROWSER_USE_CDP_URL", "")
            if not cdp_url:
                executable = Path(os.environ.get("BROWSER_USE_EXECUTABLE",
                                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
                if not executable.is_file():
                    raise FileNotFoundError("未找到 Edge；请配置 BROWSER_USE_EXECUTABLE")
                profile = resources.enter_context(tempfile.TemporaryDirectory(
                    prefix="travel-price-edge-", ignore_cleanup_errors=True))
                browser_log = resources.enter_context((folder / "edge.log").open("wb"))
                process = subprocess.Popen(
                    edge_command(executable, profile, 0), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=browser_log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                cdp_url = await wait_for_debugger(process, profile)
            browser = Browser(cdp_url=cdp_url, keep_alive=True,
                              downloads_path=str(folder / "downloads"), enable_default_extensions=False)
            await browser.start()
            yield browser
        finally:
            if browser is not None:
                if process is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(browser.cdp_client.send.Browser.close(), timeout=3)
                with suppress(Exception):
                    await asyncio.wait_for(browser.stop(), timeout=5)
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)
