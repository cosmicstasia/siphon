import logging
import shutil
import subprocess
import time
import urllib.request
from urllib.error import URLError

log = logging.getLogger(__name__)

_proc: subprocess.Popen | None = None


def start(url: str = "http://localhost:4723", timeout: float = 30.0) -> None:
    """Launch Appium in the background and block until its /status endpoint responds."""
    global _proc

    if not shutil.which("appium"):
        raise RuntimeError("appium not found on PATH — install it with: npm i -g appium")

    log.info("Starting Appium server…")
    _proc = subprocess.Popen(
        ["appium"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    status_url = url.rstrip("/") + "/status"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(status_url, timeout=2):
                log.info(f"Appium ready at {url}")
                return
        except (URLError, OSError):
            time.sleep(0.5)

    stop()
    raise RuntimeError(f"Appium did not become ready within {timeout}s")


def stop() -> None:
    global _proc
    if _proc is not None:
        log.info("Stopping Appium server…")
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
        _proc = None
