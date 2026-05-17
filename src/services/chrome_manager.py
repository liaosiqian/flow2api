"""Chrome process manager for Extension Generation Proxy.

Manages Chrome lifecycle: auto-start, health check, auto-restart.
Launches Chrome in non-headless mode WITHOUT automation markers
to avoid bot detection by reCAPTCHA and Google services.
"""

import asyncio
import os
import signal
import subprocess
import time
from typing import Optional, List, Dict

from ..core.logger import debug_logger


class ChromeManager:
    _instance: Optional["ChromeManager"] = None

    CHROME_BINARY = (
        "/usr/local/lib/python3.11/site-packages/playwright/driver/"
        "package/.local-browsers/chromium-1217/chrome-linux64/chrome"
    )
    EXTENSION_PATH = "/app/extension_v2"
    PROFILE_BASE_DIR = "/app/profiles"
    DEFAULT_PROFILE = "/app/extension_profile"
    START_URL = "https://labs.google/fx/tools/flow"

    HEALTH_CHECK_INTERVAL = 30
    RESTART_DELAY = 5

    def __init__(self, db=None):
        self._db = db
        self._instances: Dict[str, "_ChromeInstance"] = {}
        self._monitor_task: Optional[asyncio.Task] = None
        self._should_run = False

    @classmethod
    async def get_instance(cls, db=None) -> "ChromeManager":
        if cls._instance is None:
            cls._instance = cls(db)
        return cls._instance

    def _build_chrome_args(self, user_data_dir: str) -> list:
        """Build Chrome launch arguments with anti-detection flags."""
        display = os.environ.get("DISPLAY", ":99")

        args = [
            self.CHROME_BINARY,

            # Extension loading
            f"--disable-extensions-except={self.EXTENSION_PATH}",
            f"--load-extension={self.EXTENSION_PATH}",

            # User data (persistent cookies/session)
            f"--user-data-dir={user_data_dir}",

            # Anti-detection: NO automation markers
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",

            # Environment
            f"--display={display}",
            "--window-size=1440,900",
            "--window-position=0,0",
            "--lang=en-US",

            # Docker compatibility
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",

            # Prevent first-run dialogs
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--disable-popup-blocking",
            "--disable-translate",
            "--disable-sync",
            "--disable-background-networking",

            # Stability
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-hang-monitor",

            # Start URL
            self.START_URL,
        ]
        return args

    async def _get_token_emails(self) -> List[Dict]:
        """Query token table to get email-to-id mapping."""
        if not self._db:
            return []
        try:
            tokens = await self._db.get_all_tokens()
            return [
                {"id": t.id, "email": getattr(t, "email", None) or f"token_{t.id}"}
                for t in tokens
                if getattr(t, "is_active", True)
            ]
        except Exception as e:
            debug_logger.log_warning(f"[ChromeManager] Failed to query tokens: {e}")
            return []

    async def start_with_monitor(self):
        """Start Chrome instance(s) and begin health monitoring."""
        self._should_run = True

        tokens = await self._get_token_emails()

        if not tokens:
            instance = _ChromeInstance(
                label="default",
                user_data_dir=self.DEFAULT_PROFILE,
                chrome_args_builder=self._build_chrome_args,
            )
            success = await instance.start()
            if success:
                self._instances["default"] = instance
                print(
                    f"[ChromeManager] Chrome started "
                    f"(PID={instance.pid}, profile=default)"
                )
            else:
                print("[ChromeManager] Failed to start Chrome (default profile)")
        else:
            token_info = tokens[0]
            email = token_info["email"]
            token_id = token_info["id"]
            label = f"token_{token_id}"

            user_data_dir = self.DEFAULT_PROFILE
            instance = _ChromeInstance(
                label=label,
                user_data_dir=user_data_dir,
                chrome_args_builder=self._build_chrome_args,
            )
            success = await instance.start()
            if success:
                self._instances[label] = instance
                print(
                    f"[ChromeManager] Chrome started "
                    f"(PID={instance.pid}, token_id={token_id}, "
                    f"email={email})"
                )
            else:
                print(
                    f"[ChromeManager] Failed to start Chrome "
                    f"(token_id={token_id}, email={email})"
                )

        self._monitor_task = asyncio.create_task(self._health_monitor())

    async def stop(self):
        """Stop all Chrome instances."""
        self._should_run = False

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        for label, instance in self._instances.items():
            await instance.stop()
            print(f"[ChromeManager] Chrome stopped (label={label})")

        self._instances.clear()

    async def _health_monitor(self):
        """Background task: check all Chrome instances and restart if crashed."""
        while self._should_run:
            try:
                await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)

                if not self._should_run:
                    break

                for label, instance in list(self._instances.items()):
                    if not instance.is_running():
                        uptime = instance.uptime_str
                        print(
                            f"[ChromeManager] Chrome died "
                            f"(label={label}{uptime}), "
                            f"restarting in {self.RESTART_DELAY}s..."
                        )
                        debug_logger.log_warning(
                            f"[ChromeManager] Chrome process died: {label}"
                        )

                        await asyncio.sleep(self.RESTART_DELAY)

                        if self._should_run:
                            success = await instance.start()
                            if success:
                                print(
                                    f"[ChromeManager] Chrome restarted "
                                    f"(label={label}, PID={instance.pid})"
                                )
                            else:
                                print(
                                    f"[ChromeManager] Restart failed "
                                    f"(label={label}), will retry next cycle"
                                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_logger.log_warning(
                    f"[ChromeManager] Health monitor error: {e}"
                )
                await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)

    @property
    def status(self) -> dict:
        instances_status = {}
        for label, inst in self._instances.items():
            instances_status[label] = {
                "running": inst.is_running(),
                "pid": inst.pid,
                "restart_count": inst.restart_count,
                "uptime_seconds": inst.uptime_seconds,
            }
        return {
            "total_instances": len(self._instances),
            "instances": instances_status,
        }


class _ChromeInstance:
    """Represents a single Chrome process."""

    def __init__(self, label: str, user_data_dir: str, chrome_args_builder):
        self.label = label
        self.user_data_dir = user_data_dir
        self._build_args = chrome_args_builder
        self._process: Optional[subprocess.Popen] = None
        self._last_start_time: Optional[float] = None
        self.restart_count = 0

    def is_running(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        if self._process and self.is_running():
            return self._process.pid
        return None

    @property
    def uptime_seconds(self) -> int:
        if self.is_running() and self._last_start_time:
            return int(time.time() - self._last_start_time)
        return 0

    @property
    def uptime_str(self) -> str:
        if self._last_start_time:
            return f", was up {time.time() - self._last_start_time:.0f}s"
        return ""

    async def start(self) -> bool:
        if self.is_running():
            return True

        chrome_bin = ChromeManager.CHROME_BINARY
        if not os.path.exists(chrome_bin):
            debug_logger.log_warning(
                f"[ChromeInstance:{self.label}] Binary not found: {chrome_bin}"
            )
            return False

        ext_path = ChromeManager.EXTENSION_PATH
        if not os.path.isdir(ext_path):
            debug_logger.log_warning(
                f"[ChromeInstance:{self.label}] Extension not found: {ext_path}"
            )
            return False

        os.makedirs(self.user_data_dir, exist_ok=True)

        args = self._build_args(self.user_data_dir)

        try:
            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
            self._last_start_time = time.time()
            self.restart_count += 1

            await asyncio.sleep(2)

            if self.is_running():
                debug_logger.log_info(
                    f"[ChromeInstance:{self.label}] Started PID={self._process.pid}"
                )
                return True
            else:
                rc = self._process.returncode
                debug_logger.log_warning(
                    f"[ChromeInstance:{self.label}] Exited immediately code={rc}"
                )
                self._process = None
                return False

        except Exception as e:
            debug_logger.log_warning(
                f"[ChromeInstance:{self.label}] Start failed: {e}"
            )
            self._process = None
            return False

    async def stop(self):
        if self._process and self.is_running():
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                    self._process.wait(timeout=3)
            except (ProcessLookupError, OSError):
                pass
        self._process = None
