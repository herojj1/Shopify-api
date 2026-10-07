# recaptcha_solver.py
# Original: your recaptchasolver (1).py
# Only change: demo __main__ block removed so import is side-effect-free.

import time
import base64
import re
import os
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

import requests
from PIL import Image

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
    TimeoutException,
)

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("solver")

NOCAPTCHA_API_BASE = "https://api.nocaptchaai.com"
NOCAPTCHA_SOLVE_ENDPOINT = f"{NOCAPTCHA_API_BASE}/solve"


@dataclass
class Settings:
    captcha_enabled: bool = True
    recaptcha_enabled: bool = True
    auto_open: bool = True
    auto_solve: bool = True
    api_key: Optional[str] = None


@dataclass
class SolveStats:
    attempts: int = 0
    successes: int = 0
    failures: int = 0


@dataclass
class ChallengeData:
    task: str
    type: int
    cells: List[Any] = field(default_factory=list)
    images: List[Any] = field(default_factory=list)
    wait_after_solve: bool = False
    driver: Any = None


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def debug_log(*args) -> None:
    pass


def show_toast(message: str, message_type: str = "INFO") -> None:
    pass


def post_to_event_hook(payload: Dict[str, Any]) -> None:
    pass


def click_element(driver, selector: str) -> None:
    try:
        el = driver.find_element(By.CSS_SELECTOR, selector)
        el.click()
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        pass


def is_canvas_blank(image: Image.Image,
                    low_threshold: int = 0,
                    high_threshold: int = 230,
                    ratio_threshold: float = 0.99) -> bool:
    try:
        img = image.convert("RGB")
        pixels = img.getdata()
        total = len(pixels)
        if total == 0:
            return True
        count = 0
        for r, g, b in pixels:
            if (r <= low_threshold and g <= low_threshold and b <= low_threshold) or \
               (r >= high_threshold and g >= high_threshold and b >= high_threshold):
                count += 1
        return (count / total) > ratio_threshold
    except Exception:
        return True


def image_to_base64(driver, image_element) -> Optional[str]:
    script = """
    const img = arguments[0];
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth || img.width || 100;
    canvas.height = img.naturalHeight || img.height || 100;
    const ctx = canvas.getContext('2d');
    try {
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        ctx.getImageData(0, 0, 1, 1);
        return canvas.toDataURL('image/jpeg', 0.9);
    } catch (e) {
        return null;
    }
    """
    try:
        data_url = driver.execute_script(script, image_element)
        if not data_url:
            return None
        return re.sub(r"^data:image/[a-z]+;base64,", "", data_url)
    except WebDriverException:
        return None


class NoCaptchaAIError(Exception):
    pass


class NoCaptchaAI:
    def __init__(self, api_key: Optional[str]):
        if not api_key:
            raise NoCaptchaAIError("NoCaptcha AI API key required")
        self.api_key = api_key

    def solve_recaptcha(
        self,
        task: str,
        image_b64_list: List[str],
        grid: str,
        page_url: str = "",
        max_wait: int = 120,
    ) -> Dict[str, Any]:
        payload = {
            "key": self.api_key,
            "method": "recaptcha",
            "task": task,
            "grid": grid,
            "images": image_b64_list,
            "url": page_url,
        }
        headers = {
            "Content-Type": "application/json",
            "apikey": self.api_key,
        }
        try:
            resp = requests.post(
                NOCAPTCHA_SOLVE_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise NoCaptchaAIError(str(e))

        if "task_id" in data or data.get("status") == "pending":
            task_id = data.get("task_id") or data.get("id")
            data = self._poll_result(task_id, max_wait)

        if data.get("status") == "solved" or data.get("solution"):
            solution = data.get("solution") or data
            return {
                "success": True,
                "result": {
                    "tileSelections": solution.get("tileSelections")
                                     or solution.get("solution")
                                     or solution.get("answer"),
                    "token": solution.get("token") or solution.get("gRecaptchaResponse"),
                },
            }
        elif data.get("error"):
            return {"success": False, "error": data["error"]}
        else:
            return {"success": True, "result": data}

    def _poll_result(self, task_id: str, max_wait: int) -> Dict[str, Any]:
        url = f"{NOCAPTCHA_API_BASE}/result/{task_id}"
        headers = {"apikey": self.api_key}
        waited = 0
        while waited < max_wait:
            try:
                r = requests.get(url, headers=headers, timeout=30)
                r.raise_for_status()
                data = r.json()
                if data.get("status") in ("solved", "failed"):
                    return data
            except requests.RequestException:
                pass
            time.sleep(3)
            waited += 3
        raise NoCaptchaAIError("timeout")


class RecaptchaSolver:
    def __init__(self, driver, settings: Optional[Settings] = None):
        self.driver = driver
        self.settings = settings or Settings()
        self.handling = False
        self.stats = SolveStats()
        api_key = self.settings.api_key or os.environ.get("NOCAPTCHA_API_KEY")
        if not api_key:
            raise NoCaptchaAIError("NoCaptcha AI API key required")
        self.nocaptcha = NoCaptchaAI(api_key)

    def is_anchor_frame(self) -> bool:
        url = self.driver.current_url
        return "google.com" in url and "/anchor" in url

    def is_challenge_frame(self) -> bool:
        url = self.driver.current_url
        return "google.com" in url and ("/bframe" in url or "/reload" in url)

    def has_recaptcha_checkbox(self) -> bool:
        if self.is_challenge_frame():
            return False
        try:
            self.driver.find_element(By.CSS_SELECTOR, ".recaptcha-checkbox")
            return True
        except NoSuchElementException:
            return False

    def is_checkbox_checked(self) -> bool:
        try:
            el = self.driver.find_element(By.CSS_SELECTOR, ".recaptcha-checkbox")
        except NoSuchElementException:
            return False
        cls = el.get_attribute("class") or ""
        aria = el.get_attribute("aria-checked")
        return "recaptcha-checkbox-checked" in cls or aria == "true"

    def click_recaptcha_checkbox(self) -> bool:
        try:
            el = self.driver.find_element(By.CSS_SELECTOR, ".recaptcha-checkbox")
        except NoSuchElementException:
            return False
        try:
            el.click()
        except WebDriverException:
            try:
                self.driver.execute_script("arguments[0].click();", el)
            except WebDriverException:
                return False
        sleep(1.5)
        return self.is_checkbox_checked()

    def has_recaptcha_challenge(self) -> bool:
        def _q(sel: str) -> bool:
            try:
                self.driver.find_element(By.CSS_SELECTOR, sel)
                return True
            except NoSuchElementException:
                return False

        if self.is_challenge_frame():
            instr = _q(".rc-imageselect-instructions")
            target = _q(".rc-imageselect, .rc-imageselect-target")
            table = _q("table.rc-imageselect-table")
            imgs = len(self.driver.find_elements(By.CSS_SELECTOR, "table tr td img")) > 0
            return instr or target or (table and imgs)
        else:
            return (_q(".rc-imageselect") or _q(".rc-imageselect-target")
                    or _q(".rc-imageselect-instructions"))

    def is_recaptcha_solved(self) -> bool:
        try:
            ta = self.driver.find_element(By.CSS_SELECTOR, 'textarea[name="g-recaptcha-response"]')
            if ta.get_attribute("value"):
                return True
        except NoSuchElementException:
            pass
        if self.is_checkbox_checked() and not self.has_recaptcha_challenge():
            return True
        return False

    def is_dos_challenge(self) -> bool:
        try:
            self.driver.find_element(By.CSS_SELECTOR, ".rc-doscaptcha-header")
            return True
        except NoSuchElementException:
            return False

    def is_verify_disabled(self) -> bool:
        try:
            btn = self.driver.find_element(By.CSS_SELECTOR, "#recaptcha-verify-button")
            return btn.get_attribute("disabled") is not None
        except NoSuchElementException:
            return False

    def get_image_challenge(self) -> Optional[ChallengeData]:
        try:
            instr_el = self.driver.find_element(
                By.CSS_SELECTOR, ".rc-imageselect-instructions"
            )
        except NoSuchElementException:
            return None

        task = " ".join((instr_el.text or "").split("\n")[:2]).strip()
        task = re.sub(r"\s+", " ", task)

        sleep(0.5)

        def _collect():
            cells = self.driver.find_elements(By.CSS_SELECTOR, "table tr td")
            imgs = []
            for c in cells:
                try:
                    img = c.find_element(By.CSS_SELECTOR, "img")
                    src = img.get_attribute("src") or ""
                    if src.strip():
                        imgs.append(img)
                except NoSuchElementException:
                    continue
            return cells, imgs

        cells, images = _collect()

        if len(cells) not in (9, 16):
            sleep(1.0)
            cells, images = _collect()
            if len(cells) not in (9, 16):
                return None

        if len(images) not in (9, 16):
            return None

        if len(cells) == 16:
            ctype = 2
        elif any("rc-image-tile-11" in (img.get_attribute("class") or "") for img in images):
            ctype = 1
        else:
            ctype = 0

        if len(cells) == 9:
            ctype = 0

        line_count = len((instr_el.text or "").split("\n"))
        wait_after_solve = (line_count == 3 and ctype != 2)

        return ChallengeData(
            task=task,
            type=ctype,
            cells=cells,
            images=images,
            wait_after_solve=wait_after_solve,
            driver=self.driver,
        )

    def solve_image_challenge(self, challenge: ChallengeData) -> bool:
        try:
            fresh_cells = self.driver.find_elements(By.CSS_SELECTOR, "table tr td")
            fresh_imgs = []
            for c in fresh_cells:
                try:
                    img = c.find_element(By.CSS_SELECTOR, "img")
                    if (img.get_attribute("src") or "").strip():
                        fresh_imgs.append(img)
                except NoSuchElementException:
                    continue
        except WebDriverException:
            fresh_cells, fresh_imgs = [], []

        if len(fresh_imgs) in (9, 16):
            challenge.cells = fresh_cells
            challenge.images = fresh_imgs

        tile_info = []
        for idx, img in enumerate(challenge.images):
            try:
                w = self.driver.execute_script(
                    "return arguments[0].naturalWidth || arguments[0].width || 0;", img
                )
                h = self.driver.execute_script(
                    "return arguments[0].naturalHeight || arguments[0].height || 0;", img
                )
            except WebDriverException:
                w = h = 0
            tile_info.append({"index": idx, "w": w, "h": h, "is100": w == 100 and h == 100})

        has_100 = any(t["is100"] for t in tile_info)

        filtered_imgs: List[Any] = []
        filtered_cells: List[Any] = []
        eff_type = challenge.type

        if has_100:
            eff_type = 1
            for idx, img in enumerate(challenge.images):
                if tile_info[idx]["is100"]:
                    filtered_imgs.append(img)
                    filtered_cells.append(challenge.cells[idx])
        else:
            filtered_imgs = [challenge.images[0]]
            filtered_cells = [challenge.cells[0]]

        if not filtered_imgs:
            return False

        b64_list: List[str] = []
        for img in filtered_imgs:
            b64 = image_to_base64(self.driver, img)
            if not b64:
                continue
            b64_list.append(b64)

        if not b64_list:
            return False

        grid_map = {0: "3x3", 1: "1x1", 2: "4x4"}
        grid = grid_map.get(eff_type, "3x3")

        try:
            response = self.nocaptcha.solve_recaptcha(
                task=challenge.task,
                image_b64_list=b64_list,
                grid=grid,
                page_url=self.driver.current_url,
            )
        except NoCaptchaAIError:
            return False

        if not response.get("success"):
            return False

        result = response.get("result", {})
        selections = result.get("tileSelections")

        if selections and isinstance(selections, list):
            if eff_type == 1 and filtered_cells:
                return self.click_dynamic_tiles(challenge, selections, filtered_cells)
            else:
                return self.click_tiles(challenge, selections)
        elif result.get("token"):
            return True
        else:
            return False

    def click_dynamic_tiles(self, challenge: ChallengeData,
                            selections: List[Any],
                            filtered_cells: List[Any]) -> bool:
        all_cells = self.driver.find_elements(By.CSS_SELECTOR, "table tr td")
        grid_size = 4 if challenge.type == 2 else 3
        wait_after = challenge.wait_after_solve

        clicked = 0
        for i, sel in enumerate(selections):
            if i >= len(filtered_cells):
                break
            should_select = sel is True or sel == 1 or sel == "1"
            cell = filtered_cells[i]
            try:
                is_selected = "rc-imageselect-tileselected" in (cell.get_attribute("class") or "")
                orig_idx = all_cells.index(cell)
            except (StaleElementReferenceException, ValueError):
                continue

            if should_select != is_selected:
                row = orig_idx // grid_size + 1
                col = orig_idx % grid_size + 1
                selector = f"tr:nth-child({row}) td:nth-child({col})"
                click_element(self.driver, selector)
                clicked += 1
                sleep(0.15)

        sleep(0.3)

        any_selected = any(s is True or s == 1 or s == "1" for s in selections)

        if wait_after and any_selected:
            self.wait_for_dynamic_tiles()
        else:
            sleep(0.2)
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, "#recaptcha-verify-button")
                if btn.get_attribute("disabled") is None:
                    btn.click()
            except (NoSuchElementException, WebDriverException):
                pass
        return True

    def click_tiles(self, challenge: ChallengeData, selections: List[Any]) -> bool:
        cells = self.driver.find_elements(By.CSS_SELECTOR, "table tr td")
        grid_size = 4 if challenge.type == 2 else 3
        wait_after = challenge.wait_after_solve

        clicked = 0
        for i, sel in enumerate(selections):
            if i >= len(cells):
                break
            should_select = sel is True or sel == 1 or sel == "1"
            cell = cells[i]
            try:
                is_selected = "rc-imageselect-tileselected" in (cell.get_attribute("class") or "")
            except StaleElementReferenceException:
                continue

            if should_select != is_selected:
                row = i // grid_size + 1
                col = i % grid_size + 1
                selector = f"tr:nth-child({row}) td:nth-child({col})"
                click_element(self.driver, selector)
                clicked += 1
                sleep(0.15)

        sleep(0.3)

        any_selected = any(s is True or s == 1 or s == "1" for s in selections)
        should_verify = (not wait_after) or (not any_selected)

        if should_verify:
            sleep(0.2)
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, "#recaptcha-verify-button")
                if btn.get_attribute("disabled") is None:
                    btn.click()
            except (NoSuchElementException, WebDriverException):
                pass
        else:
            self.wait_for_dynamic_tiles()
        return True

    def wait_for_dynamic_tiles(self) -> None:
        sleep(0.5)
        tries = 0
        while tries < 10:
            dyn = self.driver.find_elements(
                By.CSS_SELECTOR, ".rc-imageselect-dynamic-selected"
            )
            if not dyn:
                break
            sleep(1.0)
            tries += 1

        sleep(1.5)

        imgs = self.driver.find_elements(By.CSS_SELECTOR, "table tr td img")
        for img in imgs:
            try:
                complete = self.driver.execute_script("return arguments[0].complete;", img)
                if not complete:
                    for _ in range(20):
                        sleep(0.1)
                        complete = self.driver.execute_script(
                            "return arguments[0].complete;", img
                        )
                        if complete:
                            break
            except WebDriverException:
                continue

    def handle_recaptcha(self) -> bool:
        if self.handling:
            return False

        s = self.settings
        if not (s.captcha_enabled and s.recaptcha_enabled and s.auto_solve):
            return False

        self.handling = True
        try:
            sleep(1.0)
            if self.is_recaptcha_solved():
                return True

            if self.has_recaptcha_checkbox():
                checked = self.click_recaptcha_checkbox()
                if checked:
                    challenge_appeared = False
                    for _ in range(10):
                        sleep(0.5)
                        if self.is_recaptcha_solved():
                            return True
                        if self.has_recaptcha_challenge():
                            challenge_appeared = True
                            break
                    if not challenge_appeared and self.is_recaptcha_solved():
                        return True
            return False
        finally:
            self.handling = False

    def handle_challenge_frame(self) -> bool:
        if self.handling:
            return False

        s = self.settings
        if not (s.captcha_enabled and s.recaptcha_enabled and s.auto_solve):
            return False

        self.handling = True
        try:
            sleep(2.0)
            max_attempts = 10
            for attempt in range(max_attempts):
                self.stats.attempts += 1

                guard = 0
                while (self.is_dos_challenge() or self.is_verify_disabled()) and guard < 30:
                    sleep(1.0)
                    guard += 1

                challenge = self.get_image_challenge()
                if challenge:
                    ok = self.solve_image_challenge(challenge)
                    if ok:
                        self.stats.successes += 1
                        sleep(3.0)
                        if not self.has_recaptcha_challenge():
                            return True
                        else:
                            sleep(2.0)
                            continue
                    else:
                        self.stats.failures += 1
                        sleep(2.0)
                else:
                    sleep(2.0)

            return False
        finally:
            self.handling = False

    def run(self) -> None:
        if self.is_anchor_frame():
            for _ in range(20):
                if self.has_recaptcha_checkbox():
                    break
                sleep(0.5)
            if self.settings.auto_open:
                self.handle_recaptcha()

        elif self.is_challenge_frame():
            for _ in range(30):
                if self.has_recaptcha_challenge():
                    break
                sleep(1.0)
            if self.settings.auto_solve:
                self.handle_challenge_frame()