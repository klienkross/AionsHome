"""
摄像头监控：CameraMonitor 类、Sentinel 分析、Core 唤醒、监控日志读写
"""

import json, time, re, base64, asyncio, threading, sqlite3, random
from pathlib import Path

import cv2, httpx, aiosqlite

from config import (
    DB_PATH, SCREENSHOTS_DIR, MONITOR_LOGS_DIR,
    get_key, load_worldbook, load_chat_status, load_cam_config, save_cam_config, DEFAULT_MODEL, SETTINGS,
)
from database import get_db
from ws import manager
from ai_providers import stream_ai
from memory import recall_memories
from tts import TTSStreamer


# ── 监控日志文件读写 ──────────────────────────────
def _today_log_path() -> Path:
    return MONITOR_LOGS_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"


def append_monitor_log(entry: dict):
    path = _today_log_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_monitor_logs(date_str: str = None) -> list:
    if not date_str:
        date_str = time.strftime('%Y-%m-%d')
    path = MONITOR_LOGS_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except:
                    pass
    return entries


def read_logs_since(since_ts: float) -> list:
    import datetime as _dt
    since_date = _dt.date.fromtimestamp(since_ts)
    result = []
    for logfile in sorted(MONITOR_LOGS_DIR.glob("*.jsonl")):
        # 按文件名日期跳过不可能包含目标时间戳的旧文件
        try:
            if _dt.date.fromisoformat(logfile.stem) < since_date:
                continue
        except ValueError:
            pass
        with open(logfile, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) >= since_ts:
                        result.append(entry)
                except:
                    pass
    return result


def cleanup_old_logs(keep_days: int = 3):
    import datetime
    cutoff = datetime.date.today() - datetime.timedelta(days=keep_days)
    for logfile in MONITOR_LOGS_DIR.glob("*.jsonl"):
        try:
            file_date = datetime.date.fromisoformat(logfile.stem)
            if file_date < cutoff:
                logfile.unlink()
        except:
            pass


def get_last_user_msg_time() -> float:
    """同步版本（仅供非 async 上下文使用）"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute("SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


async def async_get_last_user_msg_time() -> float:
    """异步版本，避免在事件循环中阻塞"""
    async with get_db() as db:
        cur = await db.execute("SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else 0


def detect_cameras(max_test: int = 5, skip_index: int = -1) -> list:
    """扫描可用摄像头（DirectShow 后端 + 实际读帧验证）
    skip_index: 跳过正在使用的摄像头，避免抢占设备导致采集线程中断
    """
    available = []
    for i in range(max_test):
        if i == skip_index:
            available.append(i)  # 正在用的摄像头直接视为可用
            continue
        try:
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None and frame.mean() > 1:
                    available.append(i)
                cap.release()
                time.sleep(0.3)
            else:
                cap.release()
        except Exception:
            pass
    return available


# ── 摄像头监控类 ──────────────────────────────────
class CameraMonitor:
    def __init__(self):
        self.cfg = load_cam_config()
        self.cap = None
        self.running = False
        self.monitoring = False
        self._thread = None
        self._monitor_thread = None
        self._latest_frame = None
        self._lock = threading.Lock()
        self._cam_op_lock = threading.Lock()   # 防止 open/close 并发
        self._cancel_verify = False            # 允许取消验证
        self._loop = None
        self._next_capture_at = 0
        # 画面裁剪状态：zoom=放大倍数, cx/cy=裁剪中心(0~1)
        self.crop_zoom = 1.0
        self.crop_cx = 0.5
        self.crop_cy = 0.5

    def set_event_loop(self, loop):
        self._loop = loop

    def set_crop(self, zoom: float, cx: float, cy: float):
        self.crop_zoom = max(1.0, min(10.0, zoom))
        self.crop_cx = max(0.0, min(1.0, cx))
        self.crop_cy = max(0.0, min(1.0, cy))

    def get_crop(self) -> dict:
        return {"zoom": self.crop_zoom, "cx": self.crop_cx, "cy": self.crop_cy}

    def _apply_crop(self, frame):
        """根据 zoom/cx/cy 裁剪帧，zoom=1 时返回原图"""
        if self.crop_zoom <= 1.0:
            return frame
        h, w = frame.shape[:2]
        crop_w = int(w / self.crop_zoom)
        crop_h = int(h / self.crop_zoom)
        # 根据 cx/cy 计算裁剪区域中心，并 clamp 到合法范围
        center_x = int(self.crop_cx * w)
        center_y = int(self.crop_cy * h)
        x1 = max(0, min(center_x - crop_w // 2, w - crop_w))
        y1 = max(0, min(center_y - crop_h // 2, h - crop_h))
        return frame[y1:y1 + crop_h, x1:x1 + crop_w]

    def open_camera(self, index: int = None):
        if not self._cam_op_lock.acquire(blocking=False):
            print("[Camera] 操作进行中，忽略重复请求")
            return False
        try:
            if index is not None:
                self.cfg["camera_index"] = index
                save_cam_config(self.cfg)
            self._close_camera_internal()

            idx = self.cfg["camera_index"]
            self._cancel_verify = False
            self.cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            if not self._verify_camera(max_wait=10):
                if self.cap:
                    try: self.cap.release()
                    except: pass
                    self.cap = None
                print(f"[Camera] 摄像头 index={idx} 打开失败")
                return False

            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self.running = True
            self._thread = threading.Thread(target=self._capture_loop, daemon=True)
            self._thread.start()
            print(f"[Camera] 摄像头已启动 index={idx}")
            return True
        finally:
            self._cam_op_lock.release()

    def _verify_camera(self, max_wait: int = 4) -> bool:
        """验证摄像头：最多等 max_wait 秒，读到非垃圾帧才算成功，可被 _cancel_verify 中断"""
        if not self.cap or not self.cap.isOpened():
            return False
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if self._cancel_verify:
                print("[Camera] 验证被取消")
                return False
            try:
                ret, frame = self.cap.read()
            except Exception:
                ret = False
            if ret and frame is not None:
                avg = frame.mean()
                if avg > 5:
                    print(f"[Camera] 验证通过 (avg_pixel={avg:.1f})")
                    with self._lock:
                        self._latest_frame = frame
                    return True
            time.sleep(0.15)
        print(f"[Camera] 验证失败：{max_wait}s 内未获取到有效帧")
        return False

    def close_camera(self):
        if not self._cam_op_lock.acquire(blocking=False):
            print("[Camera] 操作进行中，忽略重复请求")
            return
        try:
            self._close_camera_internal()
        finally:
            self._cam_op_lock.release()

    def _close_camera_internal(self):
        """内部关闭方法（调用者需持有 _cam_op_lock）"""
        self._cancel_verify = True  # 中断正在进行的验证
        self.monitoring = False
        self.running = False
        # 等待采集线程退出（它内部会检查 self.running）
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                print("[Camera] 警告: 采集线程未在 10s 内退出")
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=3)
        self._thread = None
        self._monitor_thread = None
        if self.cap:
            try:
                self.cap.release()
            except Exception as e:
                print(f"[Camera] 释放摄像头异常: {e}")
            self.cap = None
        with self._lock:
            self._latest_frame = None

    def _capture_loop(self):
        """采集循环：读帧失败或绿屏超时后触发重连（只重连用户配置的摄像头）"""
        fail_count = 0
        max_fails = 100  # ~10 秒
        reconnect_attempts = 0
        while self.running:
            if not self.cap or not self.cap.isOpened():
                idx = self.cfg["camera_index"]
                reconnect_attempts += 1
                wait_time = min(30, 2 * reconnect_attempts)
                print(f"[Camera] 设备断开，{wait_time}s 后尝试重连 index={idx}（第{reconnect_attempts}次）...")
                if self.cap:
                    try: self.cap.release()
                    except: pass
                    self.cap = None
                for _ in range(wait_time * 2):
                    if not self.running:
                        return
                    time.sleep(0.5)
                if not self.running:
                    return
                self.cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
                self._cancel_verify = False
                if self._verify_camera(max_wait=10):
                    self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                    self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                    fail_count = 0
                    reconnect_attempts = 0
                    print(f"[Camera] 重连成功 index={idx}")
                else:
                    if self.cap:
                        try: self.cap.release()
                        except: pass
                        self.cap = None
                    print(f"[Camera] 重连失败 index={idx}")
                continue
            try:
                ret, frame = self.cap.read()
            except Exception:
                ret = False
            # 用中心 32x32 区域采样代替全帧 mean()，大幅减少 CPU 开销
            valid = False
            if ret and frame is not None:
                h, w = frame.shape[:2]
                cy, cx = h // 2, w // 2
                valid = frame[cy-16:cy+16, cx-16:cx+16].mean() > 5
            if valid:
                fail_count = 0
                with self._lock:
                    self._latest_frame = frame
            else:
                fail_count += 1
                if fail_count % 100 == 0:
                    print(f"[Camera] 已连续 {fail_count} 帧无效，持续尝试...")
                if fail_count >= max_fails:
                    print(f"[Camera] 连续 {max_fails} 帧无效，触发重连")
                    if self.cap:
                        try: self.cap.release()
                        except: pass
                        self.cap = None
                    fail_count = 0
                time.sleep(0.1)
                continue
            # 监控活跃时 ~30fps，空闲时 ~10fps 减少 CPU
            time.sleep(0.033 if self.monitoring else 0.1)

    def get_frame_jpeg(self) -> bytes | None:
        with self._lock:
            if self._latest_frame is None:
                return None
            frame = self._apply_crop(self._latest_frame)
            _, buf = cv2.imencode(".jpg", frame)
            return buf.tobytes()

    def save_screenshot(self) -> str | None:
        with self._lock:
            if self._latest_frame is None:
                return None
            frame = self._apply_crop(self._latest_frame).copy()
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"cam_{ts}.jpg"
        filepath = SCREENSHOTS_DIR / filename
        cv2.imwrite(str(filepath), frame)
        self._cleanup()
        return filename

    def _cleanup(self):
        max_keep = self.cfg.get("max_screenshots", 200)
        if max_keep <= 0:
            return
        # 只清理监控截图(cam_YYYYMMDD_*)，不清理 Core 主动查看截图(cam_check_*)
        files = sorted(f for f in SCREENSHOTS_DIR.glob("cam_*.jpg") if not f.name.startswith("cam_check_"))
        if len(files) <= max_keep:
            return
        for f in files[:len(files) - max_keep]:
            f.unlink(missing_ok=True)

    def _random_interval_seconds(self) -> int:
        """根据配置的分钟区间随机生成一个间隔（秒）"""
        lo = max(1, self.cfg.get("auto_interval_min", 10))
        hi = max(lo, self.cfg.get("auto_interval_max", 20))
        return random.randint(lo, hi) * 60

    def start_monitoring(self):
        if self.monitoring:
            return
        if not self.running:
            self.open_camera()
        self.monitoring = True
        self.cfg["monitor_enabled"] = True
        save_cam_config(self.cfg)
        self._next_capture_at = time.time() + self._random_interval_seconds()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop_monitoring(self):
        self.monitoring = False
        self._next_capture_at = 0
        self.cfg["monitor_enabled"] = False
        save_cam_config(self.cfg)

    def _is_quiet_hours(self) -> bool:
        """检查当前是否处于静默时段"""
        if not self.cfg.get("quiet_hours_enabled", False):
            return False
        start_str = self.cfg.get("quiet_hours_start", "00:00")
        end_str = self.cfg.get("quiet_hours_end", "09:00")
        try:
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
        except (ValueError, AttributeError):
            return False
        now = time.localtime()
        cur = now.tm_hour * 60 + now.tm_min
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= end:
            return start <= cur < end
        else:  # 跨午夜，例如 23:00 ~ 07:00
            return cur >= start or cur < end

    def _monitor_loop(self):
        print("[Monitor] 监控线程已启动")
        while self.monitoring and self.running:
            now = time.time()
            if now < self._next_capture_at:
                time.sleep(0.5)
                continue
            self._next_capture_at = time.time() + self._random_interval_seconds()
            if self._is_quiet_hours():
                print("[Monitor] 当前处于静默时段，跳过截图")
                continue
            filename = self.save_screenshot()
            if filename and self._loop:
                print(f"[Monitor] 截图已保存: {filename}, 开始 Sentinel 分析")
                asyncio.run_coroutine_threadsafe(
                    self._analyze_and_log(filename), self._loop
                )
            elif not filename:
                print("[Monitor] 截图失败: 无可用画面")
        print(f"[Monitor] 监控线程退出 (monitoring={self.monitoring}, running={self.running})")

    async def _analyze_and_log(self, screenshot_filename: str):
        filepath = SCREENSHOTS_DIR / screenshot_filename
        if not filepath.exists():
            print(f"[Monitor] 截图文件不存在: {filepath}")
            return

        cleanup_old_logs(3)

        wb = load_worldbook()
        user_name = wb.get("user_name", "你")
        ai_name = wb.get("ai_name", "AI")
        now_str = time.strftime("%Y年%m月%d日  %H时:%M分:%S秒")
        last_user_ts = await async_get_last_user_msg_time()
        last_user_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_user_ts)) if last_user_ts > 0 else "未知"

        recent_logs = read_logs_since(time.time() - 3600 * 6)
        log_history = ""
        if recent_logs:
            log_lines = [f"[{e.get('time','')}] {e.get('monitoringlog','')}" for e in recent_logs[-20:]]
            log_history = "\n".join(log_lines)

        chat_status_data = load_chat_status()
        chat_status_text = chat_status_data.get("status", "")

        # 获取位置信息
        location_text = ""
        try:
            from location import format_location_for_prompt
            location_text = format_location_for_prompt()
        except Exception:
            pass

        # 获取最近 10 条聊天上下文，帮助哨兵更好地了解用户近况
        recent_chat_text = ""
        try:
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT c.id FROM conversations ORDER BY c.updated_at DESC LIMIT 1"
                )
                conv = await cur.fetchone()
                if conv:
                    cur2 = await db.execute(
                        "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 10",
                        (conv["id"],)
                    )
                    chat_rows = await cur2.fetchall()
                    if chat_rows:
                        lines = []
                        for r in reversed(chat_rows):
                            name = user_name if r["role"] == "user" else ai_name
                            # 截断过长消息，避免 prompt 膨胀
                            text = r["content"][:200] + "..." if len(r["content"]) > 200 else r["content"]
                            lines.append(f"{name}: {text}")
                        recent_chat_text = "\n".join(lines)
        except Exception:
            recent_chat_text = ""

        # 获取最近 1 小时的设备活动摘要（6 条）
        activity_summary_text = ""
        try:
            from activity import get_activity_summary_for_prompt
            activity_summary_text = get_activity_summary_for_prompt(6)
        except Exception:
            pass

        prompt = f"""你是一个监控画面分析师，同时也是{user_name}的恋人。分析当前画面，并根据历史日志和当前状况，决定是否调用伴侣职权。

当前时间：{now_str}
{user_name}最后一次和你聊天的时间：{last_user_time_str}
{user_name}最后的聊天状态：{chat_status_text if chat_status_text else "（暂无）"}
{(chr(10) + location_text) if location_text else ""}

最近的聊天记录：
{recent_chat_text if recent_chat_text else "（暂无聊天记录）"}

{user_name}近一小时的设备使用动态（手机/电脑应用使用情况，每10分钟一条摘要）：
{activity_summary_text if activity_summary_text else "（暂无设备活动记录）"}

历史监控日志：
{log_history if log_history else "（暂无历史日志）"}

请严格按照以下JSON格式回复，不要包含其他任何内容：
{{"monitoringlog":"用恋人的视角分析{user_name}当前在做什么，所处的状态，位置，以及如果看得到用户的电脑，注意用户屏幕上的内容。例如：{user_name}穿着毛绒睡衣，正在电脑桌前，看起来有些困。电脑屏幕上播放着一部小动物电影。","summary":"根据历史日志，概括{user_name}这段时间以来的整体状况，去掉重复无用的信息，保留关键事件和状态变化，一两句话即可。","call_core":false,"core_reason":""}}

字段说明：
- monitoringlog: 当前画面的客观描述，禁止胡编猜测。没有看到人就说没看到，如果最后状态没有说去睡觉，则不能猜测{user_name}可能去睡觉了。
- summary: 综合最后的聊天状态和上下文内容，概括{user_name}这段时间的整体状态变化和关键事件，禁止胡编猜测。{user_name}
- call_core: 是否唤醒主脑主动联系{user_name}
- core_reason: 仅当call_core为true时填写，说明为什么要主动联系{user_name}，让核心了解情况

call_core判断依据：
- false: {user_name}一切正常 /夜间在睡觉 /前不久才发过消息 /状态良好。
- true: {user_name}处于危险状态 / 故意引起注意 / 距离最后一次和{ai_name}聊天的时间已经过去很久 / 长时间同一姿势需提醒活动 / 长时间未看到{user_name} / 或你觉得可以主动联系{user_name}（例如看起来状态不佳，或者发现用户在偷看其他帅哥😈）主动联系大概可以在20%的概率，不要太频繁，但也不能完全没有。
- 结合设备活动动态综合判断：根据上下文分析，如果动态显示{user_name}不符合上下文讨论到的内容，例如：说去睡觉了，却在刷抖音小红书。说去工作了，却在浏览不相干的内容，进行评估，自行决定是否向Core报告情况。"""

        img_b64 = base64.b64encode(filepath.read_bytes()).decode()
        sentinel_model = "gemini-3.1-flash-lite-preview"
        gemini_key = get_key("gemini_free")
        if not gemini_key:
            print("[Monitor] Gemini API Key 未配置，跳过分析")
            return

        contents = [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}
        ]}]

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{sentinel_model}:generateContent?key={gemini_key}"
        payload = {"contents": contents}
        print(f"[Monitor] 正在调用 Sentinel 模型: {sentinel_model}")

        monitoring_log = ""
        call_core = False

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"]

            cleaned = raw_text.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```\w*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
                cleaned = cleaned.strip()
            parsed = json.loads(cleaned)
            monitoring_log = parsed.get("monitoringlog", raw_text)
            call_core = bool(parsed.get("call_core", False))
            summary = parsed.get("summary", "")
            core_reason = parsed.get("core_reason", "")
        except json.JSONDecodeError:
            monitoring_log = raw_text.strip() if 'raw_text' in dir() else "[Sentinel 无响应]"
            summary = ""
            core_reason = ""
        except Exception as e:
            monitoring_log = f"[Sentinel 分析失败] {e}"
            print(f"[Monitor] Sentinel API 调用异常: {e}")
            summary = ""
            core_reason = ""

        print(f"[Monitor] 分析完成, call_core={call_core}, log长度={len(monitoring_log)}")
        now = time.time()
        log_entry = {
            "timestamp": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "monitoringlog": monitoring_log,
            "summary": summary,
            "call_core": call_core,
            "core_reason": core_reason,
            "screenshot": screenshot_filename,
        }
        append_monitor_log(log_entry)
        await manager.broadcast({"type": "monitor_log", "data": log_entry})

        if call_core:
            await self._call_core(monitoring_log, last_user_ts, summary, core_reason, recent_logs)

    async def _call_core(self, trigger_log: str, last_user_ts: float, summary: str = "", core_reason: str = "", cached_logs: list = None):
        wb = load_worldbook()
        user_name = wb.get("user_name", "你")
        ai_name = wb.get("ai_name", "AI")

        if last_user_ts > 0:
            elapsed = time.time() - last_user_ts
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            time_ago = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
        else:
            time_ago = "很长时间"

        # 复用 _analyze_and_log 已加载的日志，避免重复读文件
        if cached_logs is not None:
            all_logs = cached_logs[-24:]
        else:
            all_logs = read_logs_since(last_user_ts if last_user_ts > 0 else time.time() - 3600 * 6)
            all_logs = all_logs[-24:]
        recent_detail = "\n".join([f"[{e.get('time','')}] {e.get('monitoringlog','')}" for e in all_logs[-5:]])
        if not recent_detail:
            recent_detail = trigger_log

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1")
            conv = await cur.fetchone()
            if not conv:
                return
            conv_id = conv["id"]
            model_key = conv["model"] or DEFAULT_MODEL

            cur = await db.execute(
                "SELECT role, content, attachments FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 20",
                (conv_id,)
            )
            rows = await cur.fetchall()
            history = []
            for r in reversed(rows):
                d = dict(r)
                # 哨兵唤醒 Core 时不携带历史图片，只带文本上下文
                d["attachments"] = []
                history.append(d)

        prefix = []
        if wb.get("ai_persona"):
            prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
            prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
        if wb.get("user_persona"):
            prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
            prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

        core_parts = [f"【{user_name}】已经{time_ago}没有和你说话了。"]
        if core_reason:
            core_parts.append(f"哨兵唤醒你的原因：{core_reason}")
        if summary:
            core_parts.append(f"这段时间{user_name}的整体状况：{summary}")
        core_parts.append(f"最新一条监控日志原文（哨兵看到的画面完整描述）：{trigger_log}")
        core_parts.append(f"最近的监控记录：\n{recent_detail}")
        # 注入位置和天气信息
        try:
            from location import format_location_for_prompt
            loc_info = format_location_for_prompt()
            if loc_info:
                core_parts.append(f"\n{loc_info}")
        except Exception:
            pass
        core_prompt = "\n".join(core_parts)

        recall_query = core_prompt[:300]
        recalled, _ = await recall_memories(recall_query)
        mem_inject = []
        if recalled:
            mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
            mem_inject = [
                {"role": "user", "content": f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"},
                {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"}
            ]

        # 播放提示音 + 5秒延迟，给用户准备时间，然后重新截图
        await manager.broadcast({"type": "monitor_alert", "data": {"content": f"哨兵唤醒了{ai_name}查看监控"}})
        await asyncio.sleep(5)

        # 重新截图：5秒后的画面才是用户准备好的状态
        fresh_fname = ""
        fresh_jpg = self.get_frame_jpeg() if self.running else None
        if fresh_jpg:
            from config import UPLOADS_DIR
            ts_str = time.strftime("%Y%m%d_%H%M%S")
            fresh_fname = f"core_wake_{ts_str}.jpg"
            (UPLOADS_DIR / fresh_fname).write_bytes(fresh_jpg)
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            (SCREENSHOTS_DIR / fresh_fname).write_bytes(fresh_jpg)

        last_msg = {"role": "user", "content": core_prompt}
        if fresh_fname:
            last_msg["attachments"] = [f"/uploads/{fresh_fname}"]
            core_prompt += "\n\n（附带了最新的监控截图，请结合画面内容回应。）"
            last_msg["content"] = core_prompt

        messages = prefix + mem_inject + history + [last_msg]

        # 预生成 msg_id（TTS 分段文件命名需要）
        core_msg_id = f"msg_{int(time.time()*1000)}_cr"

        # TTS：检查是否有前端开了 TTS
        core_tts = None
        if manager.any_tts_enabled():
            tts_voice = manager.get_tts_voice()
            if tts_voice:
                core_tts = TTSStreamer(core_msg_id, tts_voice, manager)

        full_text = ""
        try:
            _temp = SETTINGS.get("temperature")
            async for chunk in stream_ai(messages, model_key, temperature=_temp):
                full_text += chunk
                if core_tts:
                    core_tts.feed(chunk)
        except Exception as e:
            full_text = f"[Core 回复失败] {e}"

        if not full_text.strip():
            return

        now = time.time()
        trigger_msg_id = f"msg_{int(now*1000)}_ct"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (trigger_msg_id, conv_id, "cam_trigger", core_prompt, now, "[]")
            )
            # 插入系统提示：哨兵唤醒了Core
            sys_now = time.time()
            sys_msg_id = f"msg_{int(sys_now*1000)}_sw"
            sys_content = f"{ai_name}偷偷查看了监控"
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]")
            )
            await db.commit()
        sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
                   "content": sys_content, "created_at": sys_now, "attachments": []}
        await manager.broadcast({"type": "msg_created", "data": sys_msg})

        async with get_db() as db:
            now2 = time.time()
            await db.execute(
                "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                (core_msg_id, conv_id, "assistant", full_text, now2, "[]")
            )
            await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
            await db.commit()

        core_msg = {"id": core_msg_id, "conv_id": conv_id, "role": "assistant",
                    "content": full_text, "created_at": now2, "attachments": []}
        await manager.broadcast({"type": "msg_created", "data": core_msg})

        # 刷新 TTS 剩余文本
        if core_tts:
            try:
                await core_tts.flush()
            except Exception:
                pass

        # 延迟导入避免循环
        from routes.files import export_conversation
        await export_conversation(conv_id)

        core_log = {
            "timestamp": now2,
            "time": time.strftime("%H:%M:%S", time.localtime(now2)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now2)),
            "monitoringlog": f"🧠 Core已唤醒并回复：{full_text[:80]}...",
            "call_core": False,
            "screenshot": "",
        }
        append_monitor_log(core_log)
        await manager.broadcast({"type": "monitor_log", "data": core_log})


cam = CameraMonitor()

# ── Core 主动查看监控 [CAM_CHECK] ─────────────────
CAM_CHECK_CMD = "[CAM_CHECK]"

async def perform_cam_check(conv_id: str, model_key: str):
    """Core 在聊天中主动请求查看监控画面：截图 → 发给 Core → 保存为新消息"""
    jpg_bytes = cam.get_frame_jpeg()
    if not jpg_bytes:
        return

    from config import UPLOADS_DIR
    ts = time.strftime("%Y%m%d_%H%M%S")
    fname = f"cam_check_{ts}.jpg"
    fpath = UPLOADS_DIR / fname
    fpath.write_bytes(jpg_bytes)

    # 同时保存到 screenshots 目录
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    (SCREENSHOTS_DIR / fname).write_bytes(jpg_bytes)

    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")

    # 构建人设前缀
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    # 获取最近对话上下文
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content, attachments FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 6",
            (conv_id,)
        )
        rows = await cur.fetchall()
    recent = []
    for r in reversed(rows):
        d = dict(r)
        # Core 主动查看监控时不携带历史图片，只带文本上下文
        d["attachments"] = []
        recent.append(d)

    cam_prompt = (
        f"你刚才想看看{user_name}在干什么，这是系统从监控摄像头抓取的实时画面。"
        f"请根据画面内容，自然地描述你看到的情况并和{user_name}互动。"
        f"不需要再说\"让我看看\"之类的话，直接说你看到了什么。"
    )
    messages = prefix + recent + [
        {"role": "user", "content": cam_prompt, "attachments": [f"/uploads/{fname}"]}
    ]

    # 预生成 msg_id（TTS 分段文件命名需要）
    msg_id = f"msg_{int(time.time()*1000)}_cc"

    # TTS：检查是否有前端开了 TTS
    cam_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            cam_tts = TTSStreamer(msg_id, tts_voice, manager)

    full_text = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            full_text += chunk
            if cam_tts:
                cam_tts.feed(chunk)
    except Exception as e:
        full_text = f"[监控查看失败] {e}"

    if not full_text.strip():
        return

    wb = load_worldbook()
    ai_name = wb.get("ai_name", "AI")

    # 插入系统提示：查看了监控画面
    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_cc_sys"
    sys_content = f"{ai_name}查看了监控画面"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]")
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_content, "created_at": sys_now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", full_text, now, "[]")
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": ai_msg})

    # 刷新 TTS 剩余文本
    if cam_tts:
        try:
            await cam_tts.flush()
        except Exception:
            pass

    from routes.files import export_conversation
    await export_conversation(conv_id)
