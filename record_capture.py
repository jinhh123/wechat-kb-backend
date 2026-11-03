# -*- coding: utf-8 -*-
"""
record_capture.py —— 只针对「群聊的聊天记录」窗口的 OCR 抓取工具

背景：
    微信 4.x 的聊天记录窗口是 Qt 自绘，UIA 读不到文本，Ctrl+A/C 也复制不出来
    （实测剪贴板长度不变）。唯一可行路线是「截图 + Windows 自带 OCR」。

用法：
    1. 微信里双击打开「群聊的聊天记录」窗口；
    2. 运行：
           .\\venv\\Scripts\\python.exe record_capture.py
           .\\venv\\Scripts\\python.exe record_capture.py --pages 1   # 只抓当前屏
           .\\venv\\Scripts\\python.exe record_capture.py --dry-run   # 只打印，不写文件
    3. 结果写入 D:\\KnowledgeBase\\YYYY-MM-DD-合并转发-HHMM.md

安全边界：
    - 只做窗口置前、截图、滚轮翻页（读取操作）；
    - 不点击任何消息、不发送消息、不修改微信数据；
    - 写操作只发生在 D:\\KnowledgeBase。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import psutil
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image, ImageChops, ImageGrab

from clipboard_guard import ClipboardGuard
from focus_guard import ForegroundGuard
from privacy import contains_blacklisted

OUTPUT_DIR = Path(r"D:\KnowledgeBase")
IMAGES_DIR = OUTPUT_DIR / "images"
STAGING_ROOT = OUTPUT_DIR / ".staging"
WORK_IMAGES_DIR = IMAGES_DIR

# OCR 前把截图放大，Windows OCR 对小字号中文的识别率会明显提升
OCR_ZOOM = 2

# 记录窗口内部最多向下扫描几屏（用于找全图片/发送者）
MAX_VISUAL_PAGES = 20


# =============================================================================
# 基础工具
# =============================================================================
def prepare_stdout() -> None:
    """
    控制台默认 GBK，正文里出现零宽字符/emoji 时 print 会抛 UnicodeEncodeError。
    这里统一切 UTF-8 并用 replace，保证采集流程不会因为一句日志崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def enable_dpi_awareness() -> None:
    """避免高 DPI 下截图与窗口坐标错位。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def find_record_windows() -> list[tuple[int, str]]:
    """标题含「聊天记录」且属于 Weixin.exe 的可见窗口。"""
    found: list[tuple[int, str]] = []

    def callback(hwnd: int, _extra) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if "聊天记录" not in title:
            return

        # 只认微信进程，避免误抓浏览器里标题含“聊天记录”的页面
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() not in ("weixin.exe", "wechat.exe"):
                return
        except Exception:
            return

        found.append((hwnd, title))

    win32gui.EnumWindows(callback, None)
    return found


def focus_window(hwnd: int) -> bool:
    """
    尽力把记录窗口置前；失败也不影响采集。

    因为截图走的是 PrintWindow（后台渲染），滚轮也是发给鼠标下的窗口，
    都不依赖前台焦点，所以这里不再强制中止。
    """
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as exc:
        print(f"[提示] 置前失败（{exc}），继续用后台截图，不影响采集")

    time.sleep(0.5)
    if win32gui.GetForegroundWindow() != hwnd:
        print("[提示] 记录窗口不在前台，但不影响 PrintWindow 截图，继续执行")
    return True


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return left, top, right, bottom


def enlarge_window(hwnd: int, width: int = 1000, height: int = 1000):
    """
    临时把记录窗口放大再截图：窗口越宽，文字渲染越大，OCR 越完整。

    返回原始 (left, top, width, height) 供还原；没变化则返回 None。
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.5)

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    cur_w, cur_h = right - left, bottom - top

    screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    screen_h = ctypes.windll.user32.GetSystemMetrics(1)
    new_w = min(width, max(cur_w, screen_w - left - 40))
    new_h = min(height, max(cur_h, screen_h - top - 80))

    if new_w <= cur_w and new_h <= cur_h:
        return None

    win32gui.SetWindowPos(
        hwnd,
        0,
        left,
        top,
        new_w,
        new_h,
        win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
    )
    time.sleep(0.8)
    return (left, top, cur_w, cur_h)


def restore_window(hwnd: int, rect: tuple[int, int, int, int]) -> None:
    left, top, width, height = rect
    win32gui.SetWindowPos(
        hwnd,
        0,
        left,
        top,
        width,
        height,
        win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
    )


def capture_window(hwnd: int) -> Image.Image:
    """
    优先用 PrintWindow(PW_RENDERFULLCONTENT) 后台截图：
      - 不需要窗口处于前台，被遮挡也能拿到完整内容；
      - Qt 4.x 实测可正常渲染（不是黑图）。
    失败（黑图/返回 0）时回退到屏幕截图。
    """
    left, top, right, bottom = window_rect(hwnd)
    if right <= left or bottom <= top:
        raise RuntimeError(f"窗口尺寸异常：{left},{top},{right},{bottom}")

    width, height = right - left, bottom - top
    image: Image.Image | None = None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        candidate = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        )

        gray = candidate.convert("L").resize((16, 16))
        mean = sum(gray.tobytes()) / 256.0
        if ok == 1 and mean > 5:
            image = candidate
    except Exception as exc:
        print(f"  [警告] PrintWindow 截图失败，回退屏幕截图：{exc}")
    finally:
        try:
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            win32gui.DeleteObject(bitmap.GetHandle())
        except Exception:
            pass

    if image is None:
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    return image


def save_page_image(image: Image.Image, when: datetime) -> str:
    """保存整页截图到本次运行的临时目录，返回 Markdown 相对路径。"""
    WORK_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"record-{when.strftime('%Y%m%d-%H%M%S')}.png"
    path = WORK_IMAGES_DIR / name
    image.save(path, format="PNG")
    return f"./images/{name}"


def _find_wechat_window_by_title(title: str) -> int | None:
    found: list[int] = []

    def callback(hwnd: int, _extra) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetWindowText(hwnd) != title:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() not in ("weixin.exe", "wechat.exe"):
                return
        except Exception:
            return
        found.append(hwnd)

    win32gui.EnumWindows(callback, None)
    return found[0] if found else None


def _wait_for_window(title: str, timeout: float = 6.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = _find_wechat_window_by_title(title)
        if hwnd:
            return hwnd
        time.sleep(0.25)
    return None


def _find_save_dialog(timeout: float = 6.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found: list[int] = []

        def callback(hwnd: int, _extra) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetClassName(hwnd) != "#32770":
                return
            if win32gui.GetWindowText(hwnd) != "保存":
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if psutil.Process(pid).name().lower() not in ("weixin.exe", "wechat.exe"):
                    return
            except Exception:
                return
            found.append(hwnd)

        win32gui.EnumWindows(callback, None)
        if found:
            return found[0]
        time.sleep(0.25)
    return None


def _double_click(x: int, y: int) -> None:
    win32api.SetCursorPos((x, y))
    time.sleep(0.2)
    for _ in range(2):
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.12)


def _press_ctrl_s() -> None:
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("S"), 0, 0, 0)
    win32api.keybd_event(ord("S"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def save_image_original(
    hwnd: int,
    box: tuple[int, int, int, int],
    when: datetime | None = None,
    dry_run: bool = False,
) -> str | None:
    """
    把记录窗口里的一张图片保存为原图。

    流程（已在微信 4.2 + 150% DPI 实测）：
        双击图片 -> 预览窗口「图片和视频」-> Ctrl+S -> 保存对话框
        -> UIA 填文件名（全路径）-> 点「保存」-> 关闭预览

    box 是相对记录窗口截图的 (left, top, right, bottom)。
    返回 Markdown 可用的相对路径，失败返回 None。
    """
    import uiautomation as auto

    when = when or datetime.now()
    if dry_run:
        print("  [DRY-RUN] 将保存原图（不点击）")
        return None

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    image = capture_window(hwnd)
    scale = image.width / max(1, right - left)
    x = left + int((box[0] + box[2]) / 2 * scale)
    y = top + int((box[1] + box[3]) / 2 * scale)

    old_pos = win32api.GetCursorPos()
    preview: int | None = None
    dialog: int | None = None
    try:
        # 连续保存多张图时，记录窗口可能已失去焦点；先尽力拉回前台
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.3)

        _double_click(x, y)
        preview = _wait_for_window("图片和视频")
        if preview is None:
            print("  [警告] 双击后没有出现图片预览窗口")
            return None

        try:
            win32gui.SetForegroundWindow(preview)
        except Exception:
            pass
        time.sleep(0.5)
        _press_ctrl_s()

        dialog = _find_save_dialog()
        if dialog is None:
            print("  [警告] 没有出现保存对话框")
            return None

        WORK_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        target = WORK_IMAGES_DIR / f"record-image-{when.strftime('%Y%m%d-%H%M%S')}.png"
        index = 2
        while target.exists():
            target = WORK_IMAGES_DIR / (
                f"record-image-{when.strftime('%Y%m%d-%H%M%S')}-{index}.png"
            )
            index += 1

        dlg = auto.ControlFromHandle(dialog)
        combo = dlg.ComboBoxControl(AutomationId="FileNameControlHost")
        edit = combo.EditControl(searchDepth=4)
        edit.GetValuePattern().SetValue(str(target))
        time.sleep(0.2)
        dlg.ButtonControl(AutomationId="1").Click()

        deadline = time.time() + 6
        while time.time() < deadline:
            if target.is_file() and target.stat().st_size > 0:
                print(f"  原图已保存：{target}")
                return f"./images/{target.name}"
            time.sleep(0.25)

        print("  [警告] 保存超时，文件没有出现")
        return None
    finally:
        win32api.SetCursorPos(old_pos)
        try:
            if dialog is not None and win32gui.IsWindow(dialog):
                win32gui.PostMessage(dialog, win32con.WM_CLOSE, 0, 0)
            if preview is not None and win32gui.IsWindow(preview):
                win32gui.PostMessage(preview, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass


def detect_image_regions(image) -> list[tuple[int, int, int, int]]:
    """
    在记录窗口截图里找“大块图片”。

    基于 Canny + 轮廓，过滤掉文字行（太宽/太扁）和头像（太小）。
    当前参数在微信 4.2 + 150% DPI 下实测能框住二维码图片卡片。
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("  [警告] 未安装 opencv，跳过原图检测")
        return []

    array = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 40, 120)
    kernel = np.ones((5, 5), np.uint8)
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(
        closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    height, width = gray.shape
    boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, box_w, box_h = cv2.boundingRect(contour)
        if box_w < 120 or box_h < 100 or box_w * box_h < 15000:
            continue
        if x < 20 or y < 80 or x + box_w > width - 20:
            continue
        if box_h > height * 0.75:
            continue
        ratio = box_w / max(1, box_h)
        if ratio < 0.35 or ratio > 3.0:
            continue
        boxes.append((x, y, x + box_w, y + box_h))

    # 去掉被大框完全包含的小框
    boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    result: list[tuple[int, int, int, int]] = []
    for box in boxes:
        if any(
            box[0] >= keep[0]
            and box[1] >= keep[1]
            and box[2] <= keep[2]
            and box[3] <= keep[3]
            for keep in result
        ):
            continue
        result.append(box)
    return result


def _scroll_record(hwnd: int, delta: int, notches: int = 4) -> None:
    """向记录窗口发送滚轮消息（不移动鼠标）。"""
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    x = left + int((right - left) / 2)
    y = top + int((bottom - top) / 2)
    lparam = win32api.MAKELONG(x, y)
    wparam = (delta << 16) & 0xFFFFFFFF
    for _ in range(notches):
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)
        time.sleep(0.08)


def _dedup_asset_files(paths: list[str]) -> list[str]:
    """按文件内容去重，跨屏重复出现的同一张图只保留一份。"""
    seen: set[str] = set()
    unique: list[str] = []
    for rel in paths:
        path = WORK_IMAGES_DIR / Path(rel).name
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        seen.add(digest)
        unique.append(rel)
    return unique


def collect_visual_assets(
    hwnd: int, when: datetime, args: argparse.Namespace
) -> tuple[str | None, list[str], list[str]]:
    """
    从记录窗口顶部逐屏向下扫描，收集：
        - 首屏整页截图
        - 每屏的发送者头像/昵称裁剪
        - 每屏检测到的图片原图
    结束后滚回顶部。

    正文仍以 Ctrl+A 剪贴板为准，这里只补视觉资产。
    """
    if args.dry_run:
        return None, [], []

    # Ctrl+Home 是全局按键，先确保记录窗口在前台，避免发到别的程序
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.3)
    _press_ctrl(0x24)  # VK_HOME
    time.sleep(0.6)
    first = capture_window(hwnd)

    image_rel: str | None = None
    try:
        image_rel = save_page_image(first, when)
    except Exception as exc:
        print(f"  [警告] 保存整页截图失败：{exc}")

    if args.no_original_images and args.no_sender_crops:
        return image_rel, [], []

    headers: list[str] = []
    originals: list[str] = []
    current = first
    scrolled = 0
    max_pages = max(1, int(getattr(args, "max_visual_pages", MAX_VISUAL_PAGES)))
    notches = max(1, int(getattr(args, "visual_scroll_notches", 12)))
    prev_texts = {box[4] for box in asyncio.run(ocr_boxes(current))}

    while scrolled < max_pages:
        if not args.no_sender_crops:
            try:
                headers.extend(save_sender_header_crops(current, when))
            except Exception as exc:
                print(f"  [警告] 裁剪发送者区域失败：{exc}")

        if not args.no_original_images:
            for box in detect_image_regions(current):
                try:
                    rel = save_image_original(hwnd, box, when)
                    if rel:
                        originals.append(rel)
                except Exception as exc:
                    print(f"  [警告] 保存原图失败：{exc}")

        _scroll_record(hwnd, -120, notches)
        scrolled += 1
        time.sleep(0.9)
        nxt = capture_window(hwnd)

        diff = ImageChops.difference(current.convert("L"), nxt.convert("L"))
        data = diff.tobytes()
        mean = sum(data) / max(1, len(data))

        next_texts = {box[4] for box in asyncio.run(ocr_boxes(nxt))}
        if not next_texts:
            print("  OCR 没有文本，判定已到底")
            break
        if not (next_texts - prev_texts) and mean <= 1.0:
            print("  OCR 文本无新增且画面趋于稳定，判定已到底")
            break

        current = nxt
        prev_texts = next_texts

    _scroll_record(hwnd, 120, notches * scrolled)
    time.sleep(0.6)
    print(f"  记录窗口共扫描 {scrolled} 屏")
    return image_rel, _dedup_asset_files(headers), _dedup_asset_files(originals)


def _looks_like_time(text: str) -> bool:
    return bool(
        re.search(r"\d{1,4}\s*年|\d{1,2}\s*月|\d{1,2}\s*[:：]\s*\d{2}", text)
    )


def save_sender_header_crops(image: Image.Image, when: datetime) -> list[str]:
    """
    把每条消息的「头像 + 昵称 + 时间」那一行裁成图片，避免 OCR 认错名字。

    识别方式：左侧短行（昵称）必须能在同一行右侧找到时间文本，
    这样正文行（没有同行时间）不会被误判成姓名。
    """
    try:
        boxes = asyncio.run(ocr_boxes(image))
    except Exception as exc:
        print(f"  [警告] 发送者裁剪 OCR 失败：{exc}")
        return []

    width, height = image.size

    # 只认右侧时间行：昵称可能是单字（OCR 经常漏），但时间不会被漏掉。
    # 时间行所在的那一整行，正好就是「头像 + 昵称 + 时间」。
    headers: list[tuple[int, int, int, int]] = []
    for top, left, _right, bottom, text in boxes:
        if left <= width * 0.5:
            continue
        if not _looks_like_time(text):
            continue
        if top < height * 0.08:  # 排除标题
            continue
        crop_top = max(0, int(top - 14))
        crop_bottom = min(height, int(bottom + 14))
        headers.append((crop_top, 0, width, crop_bottom))

    if not headers:
        return []

    # 合并重叠的检测结果，避免同一行裁两次
    headers.sort()
    merged: list[tuple[int, int, int, int]] = []
    for box in headers:
        if merged and box[0] <= merged[-1][3]:
            merged[-1] = (
                merged[-1][0],
                0,
                width,
                max(merged[-1][3], box[3]),
            )
        else:
            merged.append(box)

    WORK_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    stamp = when.strftime("%Y%m%d-%H%M%S")
    for index, (top, _left, right, bottom) in enumerate(merged, start=1):
        crop = image.crop((0, top, right, bottom))
        name = f"record-header-{stamp}-{index}.png"
        crop.save(WORK_IMAGES_DIR / name, format="PNG")
        paths.append(f"./images/{name}")
    return paths


def scroll_window(hwnd: int, clicks: int = 5) -> None:
    """在窗口中心滚动鼠标滚轮，截图后恢复鼠标位置。"""
    left, top, right, bottom = window_rect(hwnd)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    old_pos = win32api.GetCursorPos()
    try:
        win32api.SetCursorPos((cx, cy))
        time.sleep(0.1)
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -120 * clicks, 0)
        time.sleep(0.6)
    finally:
        win32api.SetCursorPos(old_pos)


# =============================================================================
# Windows OCR
# =============================================================================
async def ocr_boxes(image) -> list[tuple[float, float, float, float, str]]:
    """返回 [(top, left, right, bottom, text)]，用于定位可点击的文字行。"""
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(buffer.getvalue())
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
    if engine is None:
        engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        return []

    result = await engine.recognize_async(bitmap)

    boxes: list[tuple[float, float, float, float, str]] = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue
        left = min(w.bounding_rect.x for w in words)
        top = min(w.bounding_rect.y for w in words)
        right = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
        bottom = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
        text = clean_ocr_line(line.text or "")
        if text:
            boxes.append((top, left, right, bottom, text))
    return boxes


async def ocr_lines(image) -> list[tuple[float, float, str]]:
    """返回 [(top, left, text)]，按纵向位置排序。"""
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(buffer.getvalue())
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
    if engine is None:
        engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("没有可用的 OCR 引擎（缺少中文语言包）")

    result = await engine.recognize_async(bitmap)

    lines: list[tuple[float, float, str]] = []
    for line in result.lines:
        words = list(line.words)
        if words:
            top = min(w.bounding_rect.y for w in words)
            left = min(w.bounding_rect.x for w in words)
        else:
            top, left = 0.0, 0.0
        text = (line.text or "").strip()
        if text:
            lines.append((top, left, text))

    lines.sort(key=lambda item: (item[0], item[1]))
    return lines


_CJK = r"\u4e00-\u9fff"
_FULL_PUNCT = r"，。！？、；：“”‘’（）《》【】…—～"


def clean_ocr_line(text: str) -> str:
    """
    Windows OCR 会在每个汉字之间插空格（“群 聊 的 聊 天 记 录”），
    这里去掉「中日韩字符 / 全角标点 / 数字」之间的空格，
    但保留英文单词之间的正常空格（如 “PPT 答辩”）。
    """
    text = text.strip()
    if not text:
        return ""

    text = re.sub(rf"(?<=[{_CJK}{_FULL_PUNCT}])\s+(?=[{_CJK}{_FULL_PUNCT}])", "", text)
    text = re.sub(rf"(?<=[{_CJK}{_FULL_PUNCT}])\s+(?=\d)", "", text)
    text = re.sub(rf"(?<=\d)\s+(?=[{_CJK}{_FULL_PUNCT}])", "", text)
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    text = re.sub(r"(?<=\d)\s+(?=[:：])", "", text)
    text = re.sub(r"(?<=[:：])\s+(?=\d)", "", text)
    text = re.sub(r"(?<=\d)：(?=\d)", ":", text)  # 15：40 -> 15:40
    return text.strip()


def dpi_scale(hwnd: int) -> float:
    try:
        return ctypes.windll.user32.GetDpiForWindow(hwnd) / 96.0
    except Exception:
        return 1.0


def page_text(hwnd: int, image, window_title: str = "", zoom: int = OCR_ZOOM) -> list[str]:
    """
    OCR 一屏并去掉标题栏文字。

    记录窗口顶部约 44 逻辑像素是自绘标题栏（含“群聊的聊天记录”），
    直接丢掉这一带的识别结果，并按文本再兜底过滤一次窗口标题。
    """
    scale = dpi_scale(hwnd)
    title_cut = 44 * scale * zoom
    title_key = clean_ocr_line(window_title)

    big = image.resize((image.width * zoom, image.height * zoom), Image.LANCZOS)
    lines = asyncio.run(ocr_lines(big))

    cleaned: list[str] = []
    for top, _left, text in lines:
        if top < title_cut:
            continue
        value = clean_ocr_line(text)
        if not value:
            continue
        if title_key and value.startswith(title_key):
            continue  # 窗口标题不属于聊天内容
        if re.fullmatch(r"[，。、；：！？,.;:!?]{1,3}", value):
            continue  # 纯标点噪声
        cleaned.append(value)
    return cleaned


_CLIP_SENTINEL = "__wechat_record_capture_probe__"


def _click_at(x: int, y: int) -> None:
    win32api.SetCursorPos((x, y))
    time.sleep(0.15)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.3)


def _press_ctrl(key: int) -> None:
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(key, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def _read_clipboard_text() -> str:
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return ""
    try:
        return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
    except Exception:
        return ""
    finally:
        win32clipboard.CloseClipboard()


def _write_clipboard_text(text: str) -> None:
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
    except Exception:
        pass
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def capture_via_clipboard(
    hwnd: int, image: Image.Image, window_title: str = ""
) -> str | None:
    """
    首选方案：点击内容区聚焦 -> Ctrl+A -> Ctrl+C -> 读剪贴板。

    关键点（踩过的坑）：
      - 进程必须 DPI 感知，否则 1.5 倍缩放下点击坐标会偏；
      - 必须先点一下正文，焦点进了文本区，Ctrl+A 才会作用于消息内容；
      - 微信只把正文写进剪贴板（不含发送人/时间），但完全没有 OCR 错字。

    返回 None 表示剪贴板抓取失败，由调用方回退到 OCR。
    """
    boxes = asyncio.run(ocr_boxes(image))
    content_top = image.height * 0.11  # 标题栏以下才算正文
    candidates = [
        box for box in boxes if box[0] > content_top and len(box[4]) >= 4
    ]

    left, top, right, bottom = window_rect(hwnd)
    points: list[tuple[int, int]] = []
    for line_top, line_left, line_right, line_bottom, _text in candidates[:3]:
        points.append(
            (
                left + int((line_left + line_right) / 2),
                top + int((line_top + line_bottom) / 2),
            )
        )
    if not points:
        points.append(
            (
                left + int((right - left) * 0.45),
                top + int((bottom - top) * 0.35),
            )
        )

    old_pos = win32api.GetCursorPos()

    # 无论成功失败，退出时都把用户原来的文字/图片写回剪贴板
    try:
        with ClipboardGuard():
            for click_x, click_y in points:
                for _attempt in range(2):
                    _write_clipboard_text(_CLIP_SENTINEL)
                    _click_at(click_x, click_y)
                    _press_ctrl(ord("A"))
                    time.sleep(0.3)
                    _press_ctrl(ord("C"))
                    time.sleep(0.7)

                    text = _read_clipboard_text()
                    if text and text != _CLIP_SENTINEL and any(
                        "\u4e00" <= ch <= "\u9fff" for ch in text
                    ):
                        return re.sub(r"\n{3,}", "\n\n", text).strip()
                    time.sleep(0.2)
    finally:
        win32api.SetCursorPos(old_pos)

    return None


def collect_text(
    hwnd: int,
    pages: int,
    window_title: str = "",
    save_image: bool = True,
    zoom: int = OCR_ZOOM,
) -> tuple[str, str | None]:
    """逐屏 OCR，按行去重拼接；返回 (正文, 首屏截图相对路径)。"""
    collected: list[str] = []
    seen: set[str] = set()
    image_rel: str | None = None

    for page in range(1, pages + 1):
        image = capture_window(hwnd)
        if page == 1 and save_image:
            try:
                image_rel = save_page_image(image, datetime.now())
            except Exception as exc:
                print(f"  [警告] 保存整页截图失败：{exc}")
        lines = page_text(hwnd, image, window_title, zoom)

        new_count = 0
        for line in lines:
            key = " ".join(line.split())
            if not key or key in seen:
                continue
            seen.add(key)
            collected.append(line)
            new_count += 1

        print(f"  第 {page} 屏：识别 {len(lines)} 行，新增 {new_count} 行")

        if page == 1:
            # 第一屏之后稍等界面稳定再滚动
            time.sleep(0.2)
        if new_count == 0 and page > 1:
            print("  没有新内容，停止翻页")
            break
        if page < pages:
            scroll_window(hwnd)

    return "\n".join(collected).strip(), image_rel


# =============================================================================
# 落盘已全部交给 ingest.py；这里不再保留独立 Markdown 写入逻辑
# =============================================================================


# =============================================================================
# 入口
# =============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="用截图 + Windows OCR 抓取微信「群聊的聊天记录」窗口"
    )
    parser.add_argument(
        "--pages", type=int, default=5, help="最多抓几屏（默认 5，1 表示只抓当前屏）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印识别结果，不写文件")
    parser.add_argument(
        "--no-enlarge",
        action="store_true",
        help="不临时放大窗口（放大能显著提升 OCR 完整度，默认开启）",
    )
    parser.add_argument(
        "--no-sender-crops",
        action="store_true",
        help="不裁剪发送者头像/昵称（默认会裁剪并附在 Markdown 里）",
    )
    parser.add_argument(
        "--no-original-images",
        action="store_true",
        help="不保存记录内图片的原图（默认会检测并保存）",
    )
    parser.add_argument(
        "--max-visual-pages",
        type=int,
        default=20,
        help="记录窗口最多向下扫描几屏（默认 20）",
    )
    parser.add_argument(
        "--visual-scroll-notches",
        type=int,
        default=12,
        help="每屏向下滚动几格（默认 12，约保留 30%% 重叠）",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="交给 ingest 统一管线落盘（推荐，chat_watcher 使用）",
    )
    parser.add_argument("--source", default="锦", help="来源会话名")
    parser.add_argument(
        "--record-hwnd",
        default="",
        help="指定记录窗口句柄（十六进制/十进制），供 chat_watcher 精确绑定",
    )
    return parser.parse_args(argv)


def cleanup_work_images() -> None:
    """只清理本次运行的 .staging 子目录，绝不碰正式 images/。"""
    if (
        WORK_IMAGES_DIR != IMAGES_DIR
        and WORK_IMAGES_DIR.parent == STAGING_ROOT
    ):
        shutil.rmtree(WORK_IMAGES_DIR, ignore_errors=True)


def submit_to_ingest(
    text: str,
    image_rel: str | None,
    header_images: list[str],
    when: datetime,
    args: argparse.Namespace,
    original_images: list[str] | None = None,
) -> int:
    """把抓取结果交给统一管线，避免两套写盘/去重逻辑。"""
    try:
        import ingest
    except ImportError:
        print("无法导入 ingest.py，退回本地写盘")
        return -1

    pipeline = ingest.Pipeline(dry_run=args.dry_run)
    attachments: list[str] = []
    all_images = ([image_rel] if image_rel else []) + list(header_images) + list(
        original_images or []
    )
    for rel in all_images:
        path = WORK_IMAGES_DIR / Path(rel).name
        if path.is_file():
            attachments.append(str(path))

    try:
        ok = pipeline.submit(
            ingest.IngestItem(
                channel="record",
                source=args.source,
                text=text,
                attachments=attachments,
                immediate=True,
            )
        )
        print("已提交到 ingest 统一管线" if ok else "重复内容，ingest 已跳过")
        return 0
    finally:
        pipeline.close()
        cleanup_work_images()


def main(argv: list[str] | None = None) -> int:
    global WORK_IMAGES_DIR

    prepare_stdout()
    args = parse_args(argv)
    enable_dpi_awareness()

    if not args.dry_run:
        # 本次抓取的图片先放临时目录，Markdown 写成功后才由 ingest 移入 images/
        WORK_IMAGES_DIR = STAGING_ROOT / f"record-{uuid.uuid4().hex[:8]}"

    if args.record_hwnd:
        try:
            hwnd = int(args.record_hwnd, 0)
        except ValueError:
            print(f"无效的 --record-hwnd：{args.record_hwnd!r}")
            return 1
        if not win32gui.IsWindow(hwnd):
            print(f"指定的记录窗口不存在：0x{hwnd:X}")
            return 1
        title = win32gui.GetWindowText(hwnd)
        print(f"使用指定记录窗口：hwnd=0x{hwnd:X} title={title!r}")
    else:
        windows = find_record_windows()
        if not windows:
            print("没有找到「聊天记录」窗口。请先双击打开一个群聊的合并转发记录。")
            return 1

        print(f"找到 {len(windows)} 个记录窗口：")
        for item_hwnd, item_title in windows:
            print(f"  hwnd=0x{item_hwnd:X} title={item_title!r}")

        hwnd, title = windows[0]
    if not focus_window(hwnd):
        return 2

    image = capture_window(hwnd)
    when = datetime.now()
    image_rel: str | None = None
    header_images: list[str] = []
    original_images: list[str] = []

    print(f"\n先尝试剪贴板抓取：{title!r}")
    clip_text = capture_via_clipboard(hwnd, image, title)

    if clip_text:
        text = clip_text
        print("  剪贴板抓取成功（原文，无 OCR 错字）")
    else:
        print("  剪贴板抓取失败，回退截图 OCR…")
        original_rect = None
        try:
            if not args.no_enlarge:
                original_rect = enlarge_window(hwnd)
                if original_rect:
                    print("  已临时放大窗口以提升 OCR 完整度（结束后会自动还原）")
            # 窗口已放大时用 1x 原始像素即可；没放大时才用 2x 插值补分辨率
            zoom = 1 if original_rect else OCR_ZOOM
            text, image_rel = collect_text(
                hwnd,
                max(1, args.pages),
                title,
                save_image=not args.dry_run,
                zoom=zoom,
            )
        finally:
            if original_rect:
                restore_window(hwnd, original_rect)

    if not text:
        print("没有识别到文字（窗口可能是空白，或 OCR 语言包缺失）。")
        return 3

    # 隐私优先：命中黑名单时连预览都不打印，避免敏感内容进日志
    if contains_blacklisted(text):
        print("  命中隐私黑名单，整条丢弃（不调用 AI、不落盘）")
        cleanup_work_images()
        return 0

    print("\n" + "-" * 70)
    print(text[:1500])
    print("-" * 70)

    # 先判重：内容已归档过就不再保存原图，避免产生孤儿图片。
    # 写盘/去重已全部交给 ingest，这里只做“提前判重”避免白存图。
    duplicate = False
    try:
        import ingest

        store = ingest.FingerprintStore(ingest.PROCESSED_FILE)
        # 必须和 Pipeline.submit 用同一套归一化，否则 hash 对不上
        normalized = ingest.normalize_text(text)
        duplicate = (
            ingest.content_fingerprint(args.source, normalized, when) in store
        )
    except Exception:
        duplicate = False

    if duplicate:
        print("  内容已归档过，跳过原图保存")
    elif not args.dry_run:
        try:
            image_rel, header_images, original_images = collect_visual_assets(
                hwnd, when, args
            )
            print(
                f"  视觉资产：发送者 {len(header_images)} 张，原图 {len(original_images)} 张"
            )
        except Exception as exc:
            print(f"  [警告] 视觉资产采集失败：{exc}")

    # 唯一写盘入口：ingest。不再保留 record_capture 自己的 Markdown 写入分支。
    return submit_to_ingest(
        text, image_rel, header_images, when, args, original_images
    )


if __name__ == "__main__":
    with ForegroundGuard():
        sys.exit(main())
