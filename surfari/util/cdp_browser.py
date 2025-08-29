import os
import subprocess
import asyncio
import signal
import platform
from typing import Optional, ClassVar
import threading
import pathlib
from playwright.async_api import async_playwright, BrowserContext, Page

import surfari.util.config as config
import surfari.util.surfari_logger as surfari_logger

logger = surfari_logger.getLogger(__name__)

REMOTE_DEBUGGING_PORT = 9222

screen_width = config.CONFIG["app"].get("browser_width", 1712)
screen_height = config.CONFIG["app"].get("browser_height", 1072)

USER_DATA_DIR = os.path.join(config.PROJECT_ROOT, "playwright_chrome_profile")

# Platform-specific Chrome paths
if platform.system() == "Darwin":
    DEFAULT_CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

elif platform.system() == "Windows":
    DEFAULT_CHROME_PATH = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    if not os.path.isfile(DEFAULT_CHROME_PATH):
        DEFAULT_CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

elif platform.system() == "Linux":
    DEFAULT_CHROME_PATH = "/usr/bin/chromium"
    if not os.path.isfile(DEFAULT_CHROME_PATH):
        DEFAULT_CHROME_PATH = "/usr/bin/chromium-browser"

else:
    raise NotImplementedError("Unsupported platform")


init_script_text = """
(() => {
  // === Patch performance.now() to be consistent ===
  const originalNow = performance.now.bind(performance);
  const startOffset = originalNow();
  performance.now = () => originalNow() - startOffset;

  // === Patch console methods to avoid object getter triggers ===
  const safeConsole = ['log', 'debug', 'info', 'warn', 'error', 'dir'];
  for (const method of safeConsole) {
    const original = console[method];
    console[method] = (...args) => {
      const safeArgs = args.map(arg => {
        if (typeof arg === 'object' && arg !== null) {
          try { return JSON.parse(JSON.stringify(arg)); }
          catch (e) { return '[Object]'; }
        }
        return arg;
      });
      return original.apply(console, safeArgs);
    };
  }

  // === Patch debugger timing trap ===
  let lastDebuggerTime = performance.now();
  Object.defineProperty(window, 'debuggerTrap', {
    get() {
      const now = performance.now();
      const delta = now - lastDebuggerTime;
      lastDebuggerTime = now;
      return delta <= 100;
    }
  });

  // === Force all window.open() to open in same tab ===
  /*
  window.open = (url) => {
    if (url) window.location.href = url;
    return null;
  };

  // === Strip target="_blank" from all links ===
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('a[target="_blank"]').forEach(a => a.removeAttribute('target'));
  });
  */
})();
"""

class ChromiumManager:
    _instance: ClassVar[Optional["ChromiumManager"]] = None
    _instance_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(
        self,
        use_system_chrome: bool = False,
        remote_debugging_port: int = REMOTE_DEBUGGING_PORT,
        user_data_dir: str = USER_DATA_DIR,
        logger=logger,
        wait_for_browser_start: float = 2.0,
        shutdown_timeout: float = 10.0,
    ):
        self.use_system_chrome = use_system_chrome
        self.remote_debugging_port = remote_debugging_port
        self.user_data_dir = user_data_dir
        self.logger = logger
        self.wait_for_browser_start = wait_for_browser_start
        self.shutdown_timeout = shutdown_timeout

        self.chrome_process: Optional[subprocess.Popen] = None
        self.playwright = None
        self.browser_context: Optional[BrowserContext] = None

        self._loop = asyncio.get_event_loop()
        self._signals_installed = False
        self.stopped = False
        self.logger.info(f"Browser instance initialized with Screen resolution: {screen_width}x{screen_height}")

    @classmethod
    async def get_instance(cls, use_system_chrome=False) -> "ChromiumManager":
        async with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(use_system_chrome=use_system_chrome)
                await cls._instance.start()
        return cls._instance

    @classmethod
    async def stop_instance(cls) -> None:
        async with cls._instance_lock:
            if cls._instance:
                await cls._instance.stop()
                cls._instance = None

    async def get_new_page(self) -> Page:
        if not self.browser_context:
            raise RuntimeError("Browser context not yet initialized or closed")
        page = await self.browser_context.new_page()
        await page.add_init_script(init_script_text)
        self.logger.info("New tab created.")
        return page

    async def __aenter__(self):
        raise RuntimeError("Use ChromiumManager.get_instance() instead of context manager.")

    async def __aexit__(self, *args):
        pass

    async def start(self) -> None:
        self.logger.info("Starting ChromiumManager...")
        await self._install_signal_handlers()
        if not self.running_in_container():
            await self._launch_browser()
        await self._connect_over_cdp()

    async def stop(self) -> None:
        if self.stopped:
            self.logger.info("ChromiumManager already stopped.")
            return
        self.logger.info("Stopping ChromiumManager and marking as stopped.")
        self.stopped = True
        await self._close_browser_context()
        await self._shutdown_browser()

    def running_in_container(self):
        try:
            if pathlib.Path("/.dockerenv").exists():
                return True
            with open("/proc/1/cgroup", "r") as f:
                return any(x in f.read() for x in ["docker", "kubepods", "lxc"])
        except Exception:
            return False

    async def _install_signal_handlers(self) -> None:
        if not self._signals_installed and threading.current_thread() is threading.main_thread():
            def handle_signal(sig):
                self.logger.warning(f"Received signal {sig}, shutting down...")
                asyncio.create_task(self.stop())
            if platform.system() in ("Darwin", "Linux"):
                self._loop.add_signal_handler(signal.SIGINT, lambda: handle_signal("SIGINT"))
                self._loop.add_signal_handler(signal.SIGTERM, lambda: handle_signal("SIGTERM"))
            self._signals_installed = True

    def _build_chrome_args(self, executable_path: str) -> list[str]:
        args = [
            executable_path,
            f"--remote-debugging-port={self.remote_debugging_port}",
            "--remote-debugging-address=localhost",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-webrtc",
            "--disable-background-networking",
            "--disable-features=WebRtcHideLocalIpsWithMdns",
            "--window-position=0,0",
            f"--window-size={screen_width},{screen_height}",
            "--log-level=3",
            f"--user-data-dir={self.user_data_dir}"
        ]
        if platform.system() == "Linux" and not self.use_system_chrome and os.environ.get("WAYLAND_DISPLAY"):
            args += ["--ozone-platform=wayland", "--ozone-platform-hint=auto"]
        return args

    async def _launch_browser(self) -> None:
        if self.use_system_chrome:
            await self._launch_system_chrome()
        else:
            await self._launch_bundled_chromium()
            
        self.logger.info(f"Chrome process started with PID: {self.chrome_process.pid if self.chrome_process else 'N/A'}")
        await asyncio.sleep(self.wait_for_browser_start)

    async def _launch_system_chrome(self) -> None:
        if not os.path.isfile(DEFAULT_CHROME_PATH):
            raise FileNotFoundError(f"System Chrome not found at '{DEFAULT_CHROME_PATH}'")
        chrome_args = self._build_chrome_args(DEFAULT_CHROME_PATH)
        self.logger.info("Launching system Chrome with args: %s", chrome_args)
        self.chrome_process = await asyncio.create_subprocess_exec(*chrome_args)

    async def _launch_bundled_chromium(self) -> None:
        async with async_playwright() as p:
            chromium_path = p.chromium.executable_path
        if not os.path.isfile(chromium_path):
            self.logger.warning(f"Bundled Chromium not found at '{chromium_path}', using system Chrome instead.")
            await self._launch_system_chrome()
            return
        chrome_args = self._build_chrome_args(chromium_path)
        self.logger.info("Launching bundled Chromium with args: %s", chrome_args)
        self.chrome_process = await asyncio.create_subprocess_exec(*chrome_args)

    async def _connect_over_cdp(self) -> None:
        for attempt in range(1, 4):
            try:
                self.logger.info(f"Attempt {attempt}: Connecting over CDP...")
                self.playwright = await async_playwright().start()
                browser = await self.playwright.chromium.connect_over_cdp(
                    f"http://localhost:{self.remote_debugging_port}"
                )

                contexts = browser.contexts
                if contexts:
                    self.browser_context = contexts[0]
                    self.logger.info("Reusing existing browser context.")
                else:
                    self.browser_context = await browser.new_context()
                    self.logger.info("Created a new browser context.")

                self.browser_context.on(
                    "close",
                    lambda: (
                        self.logger.info("BrowserContext closed."),
                        setattr(self, 'browser_context', None)
                    )
                )
                await self.browser_context.add_init_script(init_script_text)
                self.logger.info("Connected over CDP. BrowserContext is ready.")
                return  # success
            except Exception as e:
                self.logger.error(f"Attempt {attempt} failed to connect over CDP: {e}")
                if attempt == 3:
                    raise
                await asyncio.sleep(3)

    async def _close_browser_context(self) -> None:
        self.logger.info("Closing BrowserContext called.")
        if self.browser_context:
            self.logger.info("Closing BrowserContext...")
            try:
                await self.browser_context.close()
            except Exception as e:
                self.logger.error(f"Error closing BrowserContext: {e}")
            self.browser_context = None

        if self.playwright:
            self.logger.info("Stopping Playwright...")
            await self.playwright.stop()
            self.playwright = None
        # await asyncio.sleep(3)

    async def _shutdown_browser(self) -> None:
        self.logger.info("Shutting down browser called.")
        if self.chrome_process:
            self.logger.info(f"Terminating browser process with PID: {self.chrome_process.pid}")
            try:
                if self.chrome_process.returncode is None:
                    self.logger.info("Waiting for browser to terminate gracefully...")
                    self.chrome_process.terminate()
                    # await asyncio.wait_for(self.chrome_process.wait(), timeout=self.shutdown_timeout)
                    self.logger.info("Browser terminated gracefully.")
                else:
                    self.logger.info("Browser already terminated with exit code: %d", self.chrome_process.returncode)
            except asyncio.TimeoutError:
                self.logger.warning("Force killing browser...")
                self.chrome_process.kill()
            except Exception as e:
                self.logger.error(f"Shutdown error: {e}")
            finally:
                self.chrome_process = None

