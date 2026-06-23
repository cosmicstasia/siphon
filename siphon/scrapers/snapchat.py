"""Snapchat Spotlight scraper."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import cv2
import easyocr
import insightface
import numpy as np
import typer
from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from beanie import Document
from dotenv import load_dotenv
from PIL import Image
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from siphon import storage
from siphon.scripts import appium_server

load_dotenv()

SCREENSHOTS_DIR = Path("screenshots")
LOGS_DIR = Path("logs")

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB / S3
# ---------------------------------------------------------------------------


class SpotlightRecord(Document):
    username: str
    timestamp: datetime
    run_id: str
    iteration: int
    spotlight_image_id: str
    spotlight_face_image_id: Optional[str] = None

    class Settings:
        name = "spotlight_records"


# ---------------------------------------------------------------------------
# NLP
# ---------------------------------------------------------------------------

_PROFILE_WORDS = {
    "our", "friendship", "public", "profile", "private", "chat", "wallpaper",
    "screenshotting", "notification", "notifications", "aquarius", "birthday",
    "contains", "following", "followers", "spotlight", "stories", "friends",
    "both", "you", "and", "will", "see", "the", "pick", "color", "your",
    "name", "this", "just", "like", "snaps", "add", "friend", "jan", "feb",
    "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
}

_NOISE_RE = re.compile(
    r'^\d+:\d+$'
    r'|^\d+$'
    r'|^[A-Z0-9]{1,3}$'
    r'|^[a-z]{1,2}$'
)

_USERNAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_.\-]{2,14}$')


def extract_username_from_text(text: str) -> str:
    tokens = text.split()
    past_display_name = False
    for token in tokens:
        clean = re.sub(r'[^a-zA-Z0-9_.\-]', '', token)
        if not clean or _NOISE_RE.match(clean):
            continue
        if re.match(r'^[A-Z][a-z]{2,}$', clean):
            past_display_name = True
            continue
        if not past_display_name:
            continue
        if _USERNAME_RE.match(clean) and clean.lower() not in _PROFILE_WORDS:
            return clean
    return ""


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

_reader: easyocr.Reader | None = None


def _get_reader() -> easyocr.Reader:
    global _reader
    if _reader is None:
        _reader = easyocr.Reader(["en"], gpu=False)
    return _reader


def extract_username_from_image(image_path: Path) -> str:
    img = Image.open(image_path)
    w, h = img.size
    crop = img.crop((
        int(w * 0.18),
        int(h * 0.24),
        int(w * 0.90),
        int(h * 0.29),
    ))
    reader = _get_reader()
    results = reader.readtext(np.array(crop), detail=0)
    text = " ".join(results).strip()
    return text.split("·")[0].strip()


# ---------------------------------------------------------------------------
# Face detection
# ---------------------------------------------------------------------------

_face_app: insightface.app.FaceAnalysis | None = None


def _get_face_app() -> insightface.app.FaceAnalysis:
    global _face_app
    if _face_app is None:
        _face_app = insightface.app.FaceAnalysis(
            name="buffalo_l", providers=["CPUExecutionProvider"]
        )
        _face_app.prepare(ctx_id=0, det_size=(512, 512))
    return _face_app


def crop_largest_face(image_path: Path) -> bytes | None:
    img = cv2.imread(str(image_path))
    if img is None:
        return None
    faces = _get_face_app().get(img)
    if not faces:
        return None
    largest = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    x1, y1, x2, y2 = (int(v) for v in largest.bbox)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(img.shape[1], x2), min(img.shape[0], y2)
    crop = img[y1:y2, x1:x2]
    success, buf = cv2.imencode(".png", crop)
    return buf.tobytes() if success else None


# ---------------------------------------------------------------------------
# Appium steps
# ---------------------------------------------------------------------------

_SPOTLIGHT_NAV_ID = "com.snapchat.android:id/ngs_spotlight_icon_container"
_NON_USERNAME_TEXTS = {"share", "react", "more", "save", "send", "original sound"}


def scroll(driver, direction: str = "up", duration_ms: int = 800) -> None:
    size = driver.get_window_size()
    cx = size["width"] // 2
    if direction == "up":
        sy, ey = int(size["height"] * 0.75), int(size["height"] * 0.25)
    else:
        sy, ey = int(size["height"] * 0.25), int(size["height"] * 0.75)
    driver.swipe(cx, sy, cx, ey, duration_ms)


def click(driver, resource_id: str) -> None:
    driver.find_element(by=AppiumBy.ID, value=resource_id).click()


def tap(driver, x: int, y: int) -> None:
    actions = ActionChains(driver)
    actions.w3c_actions = ActionBuilder(
        driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch")
    )
    actions.w3c_actions.pointer_action.move_to_location(x, y)
    actions.w3c_actions.pointer_action.pointer_down()
    actions.w3c_actions.pointer_action.release()
    actions.perform()


def is_ad(driver) -> bool:
    try:
        return len(driver.find_elements(
            by=AppiumBy.XPATH,
            value='//*[@text="Sponsored" or @text="Ad" or @content-desc="Sponsored" or @content-desc="Ad"]',
        )) > 0
    except Exception as exc:
        log.debug(f"is_ad check failed, assuming not an ad: {exc}")
        return False


def scroll_skip_ads(driver, direction: str = "up", duration_ms: int = 800, max_skips: int = 5) -> int:
    skipped = 0
    scroll(driver, direction, duration_ms)
    time.sleep(1.0)
    while is_ad(driver) and skipped < max_skips:
        log.info(f"  ad detected, skipping ({skipped + 1}/{max_skips})")
        scroll(driver, direction, duration_ms)
        time.sleep(1.0)
        skipped += 1
    return skipped


def tap_creator_username(driver) -> str:
    size = driver.get_window_size()
    h = size["height"]
    lower_threshold = int(h * 0.75)

    candidates = driver.find_elements(
        by=AppiumBy.XPATH,
        value='//android.widget.TextView[@clickable="true"]',
    )

    def _looks_like_username(el) -> bool:
        text = (el.get_attribute("text") or "").strip().lower()
        if not text or text in _NON_USERNAME_TEXTS or text.startswith("reply"):
            return False
        return not text.replace(",", "").replace(".", "").isdigit()

    bottom_els = [
        el for el in candidates
        if el.location.get("y", 0) > lower_threshold and _looks_like_username(el)
    ]

    if not bottom_els:
        log.warning("  no creator username element found, falling back to coordinate tap")
        tap(driver, int(size["width"] * 0.13), int(h * 0.87))
        return ""

    target = max(bottom_els, key=lambda el: el.location.get("y", 0))
    username = (target.get_attribute("text") or "").strip()
    log.debug(f"  tapping creator element text={username!r}")
    target.click()
    return username


def get_profile_username(driver, timeout: float = 6.0) -> str:
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((AppiumBy.XPATH, '//*[@resource-id="upp-username"]'))
        )
        return el.get_attribute("text").strip()
    except TimeoutException:
        log.warning(f"  upp-username not found after {timeout}s, trying display-name fallback")

    els = driver.find_elements(by=AppiumBy.XPATH, value='//*[@resource-id="upp-display-name"]')
    if els:
        text = els[0].get_attribute("text").strip()
        log.debug(f"  fell back to upp-display-name: {text!r}")
        return text

    log.warning("  could not find any username element on profile page")
    return ""


def back(driver) -> None:
    driver.back()


def ensure_following(driver) -> None:
    for attempt in range(3):
        if driver.find_elements(
            by=AppiumBy.XPATH,
            value='//android.widget.TextView[@text="Following" and @selected="true"]',
        ):
            return

        following_tab = driver.find_elements(
            by=AppiumBy.XPATH,
            value='//android.widget.LinearLayout[@clickable="true" and .//android.widget.TextView[@text="Following"]]',
        )
        if following_tab:
            following_tab[0].click()
            time.sleep(1.0)
            log.info("  navigated to Following sub-tab")
            return

        els = driver.find_elements(by=AppiumBy.ID, value=_SPOTLIGHT_NAV_ID)
        if els:
            els[0].click()
            time.sleep(1.5)
            log.info(f"  recovered: tapped Spotlight nav (attempt {attempt + 1})")
        else:
            log.warning("  could not find Spotlight nav — app may be in an unexpected state")
            return

    log.warning("  ensure_following: max retries reached without landing on Following tab")


def wait(seconds: float) -> None:
    time.sleep(seconds)


def screenshot(driver, run_dir: Path, index: int, label: str) -> Path:
    path = run_dir / f"{index:02d}_{label}.png"
    driver.save_screenshot(str(path))
    return path


# ---------------------------------------------------------------------------
# Default step list
# ---------------------------------------------------------------------------
 
DEFAULT_STEPS: list[tuple[str, dict]] = [
    ("wait",                  {"seconds": 1.5}),
    ("screenshot",            {"label": "spotlight_video"}),
    ("click",                 {"resource_id": "com.snapchat.android:id/username"}),
    ("read_profile_username", {}),
    ("screenshot",            {"label": "creator_profile"}),
    ("back",                  {}),
    ("ensure_following",      {}),
    ("scroll_skip_ads",       {"direction": "up", "duration_ms": 800}),
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _setup_logging(run_id: str) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)
    fh = logging.FileHandler(LOGS_DIR / f"{run_id}.log")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(fh)


def _build_options() -> UiAutomator2Options:
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.app_package = "com.snapchat.android"
    options.app_activity = ".LandingPageActivity"
    options.no_reset = True
    return options


def _recover(driver) -> None:
    for name, fn in [
        ("back", lambda: driver.back()),
        ("ensure_following", lambda: ensure_following(driver)),
        ("scroll", lambda: scroll_skip_ads(driver, direction="up", duration_ms=800)),
    ]:
        try:
            fn()
        except Exception as exc:
            log.debug(f"Recovery step {name!r} failed (non-fatal): {exc}")


async def _process_iteration(
    run_id: str,
    s3_prefix: str,
    iteration: int,
    username: str,
    spotlight_path: Path,
    profile_path: Path,
) -> None:
    try:
        face_bytes = crop_largest_face(spotlight_path)
    except Exception as exc:
        log.warning(f"  face detection failed: {exc}")
        face_bytes = None

    try:
        spotlight_image_id = await storage.upload_file(spotlight_path, s3_prefix)
    except Exception as exc:
        log.error(f"  failed to upload spotlight screenshot: {exc}", exc_info=True)
        return

    face_image_id = None
    if face_bytes:
        try:
            face_image_id = await storage.upload_bytes(face_bytes, s3_prefix)
        except Exception as exc:
            log.warning(f"  failed to upload face crop: {exc}")

    try:
        record = SpotlightRecord(
            username=username,
            timestamp=datetime.now(timezone.utc),
            run_id=run_id,
            iteration=iteration,
            spotlight_image_id=spotlight_image_id,
            spotlight_face_image_id=face_image_id,
        )
        await record.insert()
        log.info(f"  saved record id={record.id}")
    except Exception as exc:
        log.error(f"  failed to save record to DB: {exc}", exc_info=True)


async def _run_async(appium_url: str, max_iterations: Optional[int], step_list: list) -> None:
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    s3_prefix = f"snapchat_{run_id}"
    _setup_logging(run_id)
    log.info(f"Run {run_id}  |  screenshots → {SCREENSHOTS_DIR / run_id}  |  s3 → {s3_prefix}/")

    try:
        await storage.init_db([SpotlightRecord])
    except Exception as exc:
        log.error(f"DB init failed: {exc}", exc_info=True)
        return

    run_dir = SCREENSHOTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        appium_server.start(appium_url)
    except Exception as exc:
        log.error(f"Failed to start Appium: {exc}", exc_info=True)
        return

    try:
        driver = webdriver.Remote(appium_url, options=_build_options())
    except Exception as exc:
        log.error(f"Failed to connect to Appium at {appium_url}: {exc}", exc_info=True)
        appium_server.stop()
        return

    screenshot_index = 1
    iteration = 0

    try:
        ensure_following(driver)
        scroll_skip_ads(driver, direction="up", duration_ms=800)
    except Exception as exc:
        log.error(f"Startup navigation failed: {exc}", exc_info=True)
        driver.quit()
        return

    log.info("Running — press Ctrl+C to stop")
    try:
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            current: dict[str, Path] = {}
            current_username = ""
            current_step = "(init)"
            log.info(f"--- iteration {iteration} ---")

            try:
                for step_name, kwargs in step_list:
                    current_step = step_name
                    if step_name == "scroll":
                        scroll(driver, **kwargs)
                    elif step_name == "scroll_skip_ads":
                        skipped = scroll_skip_ads(driver, **kwargs)
                        if skipped:
                            log.info(f"  skipped {skipped} ad(s)")
                    elif step_name == "click":
                        click(driver, **kwargs)
                    elif step_name == "tap":
                        tap(driver, **kwargs)
                    elif step_name == "tap_creator_username":
                        feed_username = tap_creator_username(driver)
                        if feed_username:
                            current_username = feed_username
                            log.info(f"  username (feed)={current_username!r}")
                    elif step_name == "read_profile_username":
                        profile_username = get_profile_username(driver)
                        if profile_username:
                            current_username = profile_username
                            log.info(f"  username (profile)={current_username!r}")
                        elif current_username:
                            log.info(f"  username (feed fallback)={current_username!r}")
                        else:
                            log.warning("  could not determine username from any source")
                    elif step_name == "back":
                        back(driver)
                    elif step_name == "ensure_following":
                        ensure_following(driver)
                    elif step_name == "wait":
                        wait(**kwargs)
                    elif step_name == "screenshot":
                        label = kwargs["label"]
                        path = screenshot(driver, run_dir, screenshot_index, label)
                        current[label] = path
                        screenshot_index += 1
                        log.info(f"  saved {path.name}")
                    else:
                        raise ValueError(f"Unknown step: {step_name!r}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.error(
                    f"Iteration {iteration} failed at step {current_step!r}: {exc}",
                    exc_info=True,
                )
                _recover(driver)
                continue

            if "spotlight_video" in current and "creator_profile" in current:
                await _process_iteration(
                    run_id, s3_prefix, iteration, current_username,
                    current["spotlight_video"], current["creator_profile"],
                )
            else:
                missing = [k for k in ("spotlight_video", "creator_profile") if k not in current]
                log.warning(f"  iteration {iteration} incomplete — missing: {missing}")

    except KeyboardInterrupt:
        log.info(f"Stopped after {iteration} iteration(s). Screenshots in {run_dir}")
    finally:
        driver.quit()
        log.info("Driver closed.")
        appium_server.stop()


# ---------------------------------------------------------------------------
# Typer sub-app (registered in siphon/main.py)
# ---------------------------------------------------------------------------

app = typer.Typer(help="Scrape Snapchat Spotlight")


@app.command()
def scrape(
    appium_url: Annotated[str, typer.Option("--appium-url", "-u", help="Appium server URL")] = "http://localhost:4723",
    max_iterations: Annotated[Optional[int], typer.Option("--max", "-n", help="Stop after N iterations (default: run forever)")] = None,
) -> None:
    """Run the Snapchat Spotlight scraper."""
    asyncio.run(_run_async(appium_url, max_iterations, DEFAULT_STEPS))
