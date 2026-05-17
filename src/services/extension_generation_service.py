"""Extension-first generation execution helper.

Routes HTTP requests through the Chrome extension's browser context,
bypassing server-side curl_cffi and its fingerprint/reCAPTCHA mismatch.
"""

import json
from typing import Any, Dict, Optional

from ..core.logger import debug_logger
from .browser_captcha_extension import ExtensionCaptchaService


class ExtensionGenerationService:

    def __init__(self, db=None):
        self.db = db

    async def submit_generation(
        self,
        *,
        url: str,
        method: str,
        headers: Dict[str, Any],
        json_data: Optional[Dict[str, Any]],
        timeout_seconds: int,
        token_id: Optional[int],
    ) -> Dict[str, Any]:
        svc = await ExtensionCaptchaService.get_instance(self.db)
        debug_logger.log_info(f"[EXT-GEN] submit via extension: {method} {url}")
        result = await svc.submit_generation_via_extension(
            url=url,
            method=method,
            headers=headers,
            json_data=json_data or {},
            timeout=timeout_seconds,
            token_id=token_id,
        )
        return self._unwrap_extension_response(result)

    async def poll_generation(
        self,
        *,
        url: str,
        method: str,
        headers: Dict[str, Any],
        json_data: Optional[Dict[str, Any]],
        timeout_seconds: int,
        token_id: Optional[int],
    ) -> Dict[str, Any]:
        svc = await ExtensionCaptchaService.get_instance(self.db)
        debug_logger.log_info(f"[EXT-GEN] poll via extension: {method} {url}")
        result = await svc.poll_generation_via_extension(
            url=url,
            method=method,
            headers=headers,
            json_data=json_data or {},
            timeout=timeout_seconds,
            token_id=token_id,
        )
        return self._unwrap_extension_response(result)

    @staticmethod
    def _unwrap_extension_response(result: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(result, dict):
            raise RuntimeError("Invalid extension response payload")
        relay_status = str(result.get("status") or "")
        if relay_status and relay_status != "success":
            relay_error = str(result.get("error") or "").strip()
            raise RuntimeError(relay_error or "Extension relay returned an error")
        status_code = int(result.get("response_status") or 0)
        if status_code >= 400:
            response_text = str(result.get("response_text") or "").strip()
            raise RuntimeError(response_text or f"HTTP Error {status_code}")
        response_json = result.get("response_json")
        if isinstance(response_json, dict):
            return response_json
        response_text = str(result.get("response_text") or "").strip()
        if response_text:
            try:
                parsed = json.loads(response_text)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        raise RuntimeError(response_text or "Extension response missing JSON body")
