"""Chrome process manager for Extension Generation Proxy.

Manages Chrome lifecycle: auto-start, health check, auto-restart.
Launches Chrome in non-headless mode WITHOUT automation markers.
Supports multi-instance: one Chrome per token with isolated profiles.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, List, Dict

from ..core.config import config
from ..core.logger import debug_logger


class ChromeManager:
    _instance: Optional["ChromeManager"] = None

    CHROME_BINARY = os.environ.get("CHROME_BINARY", "")
    EXTENSION_TEMPLATE = "/app/extension_v2"
    PROFILE_BASE = Path("/app/profiles")
    LEGACY_PROFILE = Path("/app/extension_profile")
    START_URL = "https://labs.google/fx/tools/flow"

    HEALTH_CHECK_INTERVAL = 30
    RESTART_DELAY = 5
    STARTUP_WAIT = 3

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

    @staticmethod
    def _resolve_chrome_binary() -> str:
        configured = (ChromeManager.CHROME_BINARY or "").strip()
        if configured:
            return configured

        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]

        for path in candidates:
            if os.path.exists(path):
                return path

        browsers_root = Path(
            os.environ.get(
                "PLAYWRIGHT_BROWSERS_PATH",
                "/usr/local/lib/python3.11/site-packages/playwright/driver/package/.local-browsers",
            )
        )
        if str(browsers_root) == "0":
            browsers_root = Path(
                "/usr/local/lib/python3.11/site-packages/playwright/driver/package/.local-browsers"
            )
        for path in sorted(browsers_root.glob("chromium-*/chrome-linux64/chrome"), reverse=True):
            if path.exists():
                return str(path)

        return ""

    @staticmethod
    def _build_chrome_args(
        extension_dir: str,
        user_data_dir: str,
        chrome_binary: str,
        window_offset: int = 0,
    ) -> list:
        """Build Chrome launch arguments with anti-detection flags."""
        display = os.environ.get("DISPLAY", ":99")

        args = [
            chrome_binary,

            # Extension loading (per-instance copy with baked routeKey)
            f"--disable-extensions-except={extension_dir}",
            f"--load-extension={extension_dir}",

            # User data (persistent cookies/session per token)
            f"--user-data-dir={user_data_dir}",

            # Anti-detection: NO automation markers
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",

            # Environment
            f"--display={display}",
            "--window-size=1440,900",
            f"--window-position={window_offset * 50},{window_offset * 30}",
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
            ChromeManager.START_URL,
        ]
        return args

    def _prepare_extension_copy(self, route_key: str, profile_dir: Path) -> Path:
        """Create a per-token copy of the extension with routeKey baked in."""
        ext_dir = profile_dir / "extension"

        if not ext_dir.exists():
            shutil.copytree(self.EXTENSION_TEMPLATE, ext_dir)

        bg_file = ext_dir / "background.js"
        if bg_file.exists():
            content = bg_file.read_text()
            content = re.sub(
                r'routeKey:\s*"[^"]*"',
                f'routeKey: "{route_key}"',
                content,
            )
            content = re.sub(
                r'clientLabel:\s*"[^"]*"',
                f'clientLabel: "{route_key}"',
                content,
            )
            content = re.sub(
                r'apiKey:\s*"[^"]*"',
                f'apiKey: {json.dumps(str(config.api_key or ""))}',
                content,
            )
            bg_file.write_text(content)

        return ext_dir

    async def _get_active_tokens(self) -> List[Dict]:
        """Query all active tokens from database."""
        if not self._db:
            return []
        try:
            tokens = await self._db.get_all_tokens()
            return [
                {
                    "id": t.id,
                    "email": getattr(t, "email", None) or f"token_{t.id}",
                    "is_active": getattr(t, "is_active", True),
                }
                for t in tokens
                if getattr(t, "is_active", True)
            ]
        except Exception as e:
            debug_logger.log_warning(f"[ChromeManager] Failed to query tokens: {e}")
            return []

    def _get_profile_dir(self, token_id: int, email: str) -> Path:
        """Get or create profile directory for a token."""
        safe_name = re.sub(r'[^a-zA-Z0-9@._-]', '_', email)
        profile_dir = self.PROFILE_BASE / f"token_{token_id}_{safe_name}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "data").mkdir(exist_ok=True)
        return profile_dir

    async def start_with_monitor(self):
        """Start Chrome instance(s) for all active tokens."""
        self._should_run = True
        self.PROFILE_BASE.mkdir(parents=True, exist_ok=True)

        tokens = await self._get_active_tokens()

        if not tokens:
            print("[ChromeManager] No active tokens, starting with legacy profile")
            route_key = "default"
            ext_dir = self.EXTENSION_TEMPLATE
            user_data_dir = str(self.LEGACY_PROFILE)

            instance = _ChromeInstance(
                label=route_key,
                extension_dir=ext_dir,
                user_data_dir=user_data_dir,
                window_offset=0,
            )
            success = await instance.start()
            if success:
                self._instances[route_key] = instance
                print(
                    f"[ChromeManager] Chrome started "
                    f"(PID={instance.pid}, profile=legacy/default)"
                )
        else:
            for idx, token_info in enumerate(tokens):
                token_id = token_info["id"]
                email = token_info["email"]
                route_key = f"token_{token_id}"

                profile_dir = self._get_profile_dir(token_id, email)

                # Migrate: if legacy profile exists and this is the first token,
                # symlink or copy the data
                data_dir = profile_dir / "data"
                if (
                    idx == 0
                    and self.LEGACY_PROFILE.exists()
                    and not any(data_dir.iterdir())
                ):
                    try:
                        shutil.copytree(
                            self.LEGACY_PROFILE,
                            data_dir,
                            dirs_exist_ok=True,
                        )
                        print(
                            f"[ChromeManager] Migrated legacy profile to "
                            f"{profile_dir.name}"
                        )
                    except Exception as e:
                        debug_logger.log_warning(
                            f"[ChromeManager] Legacy migration failed: {e}"
                        )

                ext_dir = self._prepare_extension_copy(route_key, profile_dir)

                instance = _ChromeInstance(
                    label=route_key,
                    extension_dir=str(ext_dir),
                    user_data_dir=str(data_dir),
                    window_offset=idx,
                )
                success = await instance.start()
                if success:
                    self._instances[route_key] = instance
                    # Auto-set extension_route_key in DB for routing
                    await self._ensure_route_key_in_db(token_id, route_key)
                    print(
                        f"[ChromeManager] Chrome started "
                        f"(PID={instance.pid}, {route_key}, "
                        f"email={email})"
                    )
                else:
                    print(
                        f"[ChromeManager] Failed to start Chrome "
                        f"({route_key}, email={email})"
                    )

                # Stagger startups to avoid resource contention
                if idx < len(tokens) - 1:
                    await asyncio.sleep(2)

        total = len(self._instances)
        print(f"[ChromeManager] Total running instances: {total}")
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

        count = len(self._instances)
        self._instances.clear()
        print(f"[ChromeManager] All {count} Chrome instance(s) stopped")

    async def _ensure_route_key_in_db(self, token_id: int, route_key: str):
        """Ensure token's extension_route_key matches the Chrome instance label."""
        if not self._db:
            return
        try:
            await self._db.update_token(token_id, extension_route_key=route_key)
        except Exception as e:
            debug_logger.log_warning(
                f"[ChromeManager] Failed to set route_key for token {token_id}: {e}"
            )

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
                            f"[ChromeManager] Chrome died: {label}"
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
                                    f"[ChromeManager] Restart failed: {label}"
                                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_logger.log_warning(
                    f"[ChromeManager] Monitor error: {e}"
                )
                await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)

    async def start_instance(self, token_id: int, email: str) -> bool:
        """Dynamically start a Chrome instance for a single token (no container restart needed)."""
        route_key = f"token_{token_id}"

        if route_key in self._instances and self._instances[route_key].is_running():
            print(f"[ChromeManager] Instance already running: {route_key}")
            return True

        profile_dir = self._get_profile_dir(token_id, email)
        ext_dir = self._prepare_extension_copy(route_key, profile_dir)
        data_dir = profile_dir / "data"

        window_offset = len(self._instances)
        instance = _ChromeInstance(
            label=route_key,
            extension_dir=str(ext_dir),
            user_data_dir=str(data_dir),
            window_offset=window_offset,
        )
        success = await instance.start()
        if success:
            self._instances[route_key] = instance
            await self._ensure_route_key_in_db(token_id, route_key)
            print(f"[ChromeManager] Dynamic start: {route_key} (PID={instance.pid}, email={email})")
        else:
            print(f"[ChromeManager] Dynamic start failed: {route_key}")
        return success

    async def stop_instance(self, token_id: int) -> bool:
        """Dynamically stop a Chrome instance for a single token."""
        route_key = f"token_{token_id}"
        instance = self._instances.pop(route_key, None)
        if instance:
            await instance.stop()
            print(f"[ChromeManager] Dynamic stop: {route_key}")
            return True
        print(f"[ChromeManager] No running instance for: {route_key}")
        return False

    @property
    def status(self) -> dict:
        return {
            "total_instances": len(self._instances),
            "instances": {
                label: {
                    "running": inst.is_running(),
                    "pid": inst.pid,
                    "restart_count": inst.restart_count,
                    "uptime_seconds": inst.uptime_seconds,
                }
                for label, inst in self._instances.items()
            },
        }


class _ChromeInstance:
    """A single Chrome process bound to one token."""

    def __init__(
        self,
        label: str,
        extension_dir: str,
        user_data_dir: str,
        window_offset: int = 0,
    ):
        self.label = label
        self.extension_dir = extension_dir
        self.user_data_dir = user_data_dir
        self.window_offset = window_offset
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

        chrome_bin = ChromeManager._resolve_chrome_binary()
        if not os.path.exists(chrome_bin):
            debug_logger.log_warning(
                f"[Chrome:{self.label}] Binary not found: {chrome_bin or '(auto-detect failed)'}"
            )
            return False

        if not os.path.isdir(self.extension_dir):
            debug_logger.log_warning(
                f"[Chrome:{self.label}] Extension not found: {self.extension_dir}"
            )
            return False

        os.makedirs(self.user_data_dir, exist_ok=True)

        # Clean stale Chrome singleton locks (left by old containers with different hostnames)
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_path = os.path.join(self.user_data_dir, lock_name)
            if os.path.exists(lock_path) or os.path.islink(lock_path):
                try:
                    os.remove(lock_path)
                except OSError:
                    pass

        args = ChromeManager._build_chrome_args(
            extension_dir=self.extension_dir,
            user_data_dir=self.user_data_dir,
            chrome_binary=chrome_bin,
            window_offset=self.window_offset,
        )

        try:
            self._process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
            self._last_start_time = time.time()
            self.restart_count += 1

            await asyncio.sleep(ChromeManager.STARTUP_WAIT)

            if self.is_running():
                debug_logger.log_info(
                    f"[Chrome:{self.label}] Started PID={self._process.pid}"
                )
                return True
            else:
                rc = self._process.returncode
                debug_logger.log_warning(
                    f"[Chrome:{self.label}] Exited immediately code={rc}"
                )
                self._process = None
                return False

        except Exception as e:
            debug_logger.log_warning(
                f"[Chrome:{self.label}] Start failed: {e}"
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
            debug_logger.log_info(f"[Chrome:{self.label}] Stopped")
        self._process = None
