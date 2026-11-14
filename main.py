import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi.responses import HTMLResponse, Response

from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_body_monitor"
PLUGIN_DIR = Path(__file__).resolve().parent
UI_FILE = PLUGIN_DIR / "pages" / "touch" / "index.html"
UI_FILE_OLD = PLUGIN_DIR / "ui.html"
IMAGE_DIR = PLUGIN_DIR / "images"
"""内置底图等只读资源；用户数据一律写到 data/plugin_data/<插件名>/ 下。"""
DEFAULT_POS_FILE = PLUGIN_DIR / "default_positions.json"
"""出厂默认地图，只读资源。"""


def _default_data_dir() -> Path:
    # 插件固定装在 <AstrBot>/data/plugins/<名字>，因此 data/plugin_data 是可推导的；
    # 这样不依赖启动目录，也符合 AstrBot 的插件数据规范。
    if PLUGIN_DIR.parent.name == "plugins":
        return PLUGIN_DIR.parent.parent / "plugin_data" / PLUGIN_NAME
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
    except Exception:
        return PLUGIN_DIR.parent.parent / "plugin_data" / PLUGIN_NAME


DATA_DIR = _default_data_dir()
CUSTOM_IMAGE_DIR = DATA_DIR / "images"
PRESET_IMAGE_DIR = CUSTOM_IMAGE_DIR / "presets"
POS_FILE = DATA_DIR / "positions.json"
SESSION_FILE = DATA_DIR / "session.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
STATE_FILE = DATA_DIR / "body_state.json"


def _set_data_dir(path):
    """测试/迁移用：重定向运行时数据目录。"""
    global DATA_DIR, CUSTOM_IMAGE_DIR, PRESET_IMAGE_DIR
    global POS_FILE, SESSION_FILE, SETTINGS_FILE, STATE_FILE
    DATA_DIR = Path(path)
    CUSTOM_IMAGE_DIR = DATA_DIR / "images"
    PRESET_IMAGE_DIR = CUSTOM_IMAGE_DIR / "presets"
    POS_FILE = DATA_DIR / "positions.json"
    SESSION_FILE = DATA_DIR / "session.json"
    SETTINGS_FILE = DATA_DIR / "settings.json"
    STATE_FILE = DATA_DIR / "body_state.json"


def _ensure_data_dir() -> bool:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CUSTOM_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except Exception as exc:
        logger.warning("[body_monitor] 创建数据目录失败: %s", exc)
        return False


PRESET_DEFAULT = "默认预设"
DEFAULT_PROMPT = "你被{username}用{noun}{adj}{verb}了{detail}，这是一次远程且无法回避的触碰,请你对此进行一次回复，符合自身人设，评估生理变化,考虑上下文剧情,当前正在做的事,和此时的情绪，不要在回复中一直重复被触碰的部位,不需要描述部位被触碰后的具体变化,应当回复被触碰后的想法及此时的感受,确保回复通顺不突兀。"
DEFAULT_STATE_PROMPT = "你现在是角色身体状态的记录器，如实记录身体状态。\n现在时间：{time}\n\n当前身体部位和状态（体温基线 {baseline}°C；有状态的部位带剩余秒数和严重度，其余部位只列编号和名称）：\n{states}\n\n本次事件：{action}\n角色回复：{reply}\n{history}本次重点部位 rid：{focus}\n\n要求：\n1. 先判断这次事件里人物身体会有什么反应，再对照上面的部位清单，把所有该出现变化的部位挑出来。可以只有一两个，也可以连带动一片（例如大腿被戳 → 大腿有点痒、心跳加快、左右脸发烫），但该变的一个都不能漏，不该变的不要硬凑。\n2. 每个返回的部位都要给 temp（摄氏度；正常 {baseline}，发热或脸红可以上调，手冷脚冷可以降低）。\n3. 每条状态给 text、duration（秒，{min_duration}–{max_duration}）和 level（0=轻绿，1=中黄，2=重红）；notes 是该部位更新后的完整状态列表，最多 {max_notes} 条，消失的状态要删掉；备注只写状态本身，不要带时间或括号。\n4. 该部位已有未结束的状态，再次互动要叠加加重（有点痒 → 痒 → 很痒难受），并适当延长 duration。\n5. 持续时间宁长勿短：轻微状态一般 45–120 秒，中等 2–15 分钟，严重 15 分钟以上；除非明确马上消失，不要低于 45 秒。水渍会干、红痕会退、疼痛会减轻，明显失效的状态要删掉。\n6. 只输出 JSON，不要解释，格式：{\"updates\":[{\"rid\":\"...\",\"temp\":36.8,\"notes\":[{\"text\":\"有点痒\",\"duration\":50,\"level\":0}]}]}\n7. 状态跟原来完全一样、没有任何实际变化的部位，不要出现在 updates 里。"
USAGE_DEFAULTS = {"noun": "手指", "verb": "戳", "adj": "轻轻的"}
USAGE_DEFAULT_TREE = [{"n": "手指", "adjs": [{"n": "轻轻的", "verbs": ["戳"]}]}]
DEFAULT_COOLDOWN = 6
DEFAULT_COMBO_WINDOW = 2.0
DEFAULT_STATE_TURN_INTERVAL = 3
DEFAULT_STATE_HISTORY_TURNS = 10
MAX_LOG = 100
MAX_PENDING = 20
_PCT_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def decode_param(value):
    """路径参数兜底解码：只有还带着 %XX 时才解一次，普通名字不会被改坏。"""
    text = str(value or "")
    if _PCT_RE.search(text):
        try:
            return unquote(text)
        except Exception:
            return text
    return text


class BodyMonitorPlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.last_session: str | None = None
        self.last_user: str | None = None
        self.pending: list = []
        self.logs: list = []
        self._combo: dict = {}
        self._last_touch: dict = {}
        self._state_lock = asyncio.Lock()
        self._turn_counts: dict = {}
        self._state_busy: set = set()
        self._session_users: dict = {}

    async def initialize(self):
        # AstrBot 把插件页面的接口挂在 /api/v1/plugins/extensions/<插件全名>/ 下面，
        # 所以路由统一带插件全名前缀；不要注册裸路径，避免和别的插件抢命名空间。
        api = "/" + PLUGIN_NAME
        routes = [
            ("/ui", self.serve_ui, ["GET"], "小洛的身体监控界面"),
            ("/image", self.serve_image, ["GET"], "小洛的身体监控底图"),
            ("/current", self.get_custom_image, ["GET"], "当前底图"),
            ("/upload", self.upload_image, ["POST"], "上传底图"),
            ("/image/reset", self.clear_image, ["GET"], "恢复默认底图"),
            ("/touch", self.on_touch_query, ["GET"], "互动触碰事件（查询参数）"),
            ("/touch/<part>", self.on_touch, ["GET"], "互动触碰事件"),
            ("/positions", self.get_positions, ["GET"], "读取坐标"),
            ("/positions/save", self.save_positions_post, ["POST"], "保存全部坐标（POST）"),
            ("/positions/save/<d>", self.save_positions, ["GET"], "保存全部坐标"),
            ("/positions/clear", self.clear_positions, ["GET"], "清空当前预设坐标"),
            ("/positions/reset", self.reset_positions, ["GET"], "恢复默认坐标"),
            ("/states", self.get_states, ["GET"], "身体部位状态"),
            ("/states/clear", self.clear_states, ["POST"], "清空当前预设的身体状态"),
            ("/states/update", self.update_states_now, ["POST"], "手动更新身体状态"),
            ("/pos/<idx>/<x>/<y>", self.set_position, ["GET"], "保存单个坐标"),
            ("/presets", self.get_presets, ["GET"], "预设列表"),
            ("/presets/switch/<name>", self.switch_preset, ["GET"], "切换预设"),
            ("/presets/new/<name>/<copy>", self.new_preset, ["GET"], "新建预设"),
            ("/presets/delete/<name>", self.delete_preset, ["GET"], "删除预设"),
            ("/presets/export/<name>", self.export_preset, ["GET"], "导出预设"),
            ("/presets/export", self.export_preset_post, ["POST"], "导出预设到指定目录"),
            ("/presets/import", self.import_preset, ["POST"], "导入预设"),
            ("/session", self.get_session, ["GET"], "会话通道"),
            ("/session/set", self.set_session, ["POST"], "设置会话通道"),
            ("/session/put/<umo>", self.put_session, ["GET"], "设置会话通道（GET）"),
            ("/settings", self.get_settings, ["GET"], "用法设置"),
            ("/settings", self.save_settings, ["POST"], "保存用法设置"),
            ("/log", self.get_log, ["GET"], "互动日志"),
            ("/log/clear", self.clear_log, ["POST"], "清空互动日志"),
        ]
        for path, handler, methods, desc in routes:
            self.context.register_web_api(api + path, handler, methods, desc)
        _ensure_data_dir()
        self._migrate_legacy_data()
        self._migrate_usage_settings()
        self._load_session()
        self._migrate_current_image()
        logger.info("[body_monitor] 触碰接口已注册（数据目录 %s）", DATA_DIR)

    def _migrate_legacy_data(self):
        """把旧版本写在插件目录里的运行数据搬到 data/plugin_data/<插件名>/。"""
        legacy_files = (
            "positions.json",
            "positions.json.bak",
            "settings.json",
            "session.json",
            "body_state.json",
        )
        moved = []
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            for name in legacy_files:
                old = PLUGIN_DIR / name
                new = DATA_DIR / name
                if not old.exists():
                    continue
                try:
                    if not new.exists():
                        shutil.copy2(old, new)
                        if new.read_bytes() != old.read_bytes():
                            raise OSError("content mismatch")
                    elif new.read_bytes() != old.read_bytes():
                        logger.warning(
                            "[body_monitor] %s 新旧内容不一致，保留旧文件待人工确认", name
                        )
                        continue
                    old.unlink()
                    moved.append(name)
                except Exception as exc:
                    logger.warning("[body_monitor] 迁移 %s 失败（保留旧文件）: %s", name, exc)

            for name in ("custom.bin", "custom.mime", "custom.webp"):
                old = IMAGE_DIR / name
                new = CUSTOM_IMAGE_DIR / name
                if not old.exists():
                    continue
                try:
                    CUSTOM_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
                    if not new.exists():
                        shutil.copy2(old, new)
                        if new.read_bytes() != old.read_bytes():
                            raise OSError("content mismatch")
                    elif new.read_bytes() != old.read_bytes():
                        logger.warning(
                            "[body_monitor] images/%s 新旧内容不一致，保留旧文件待人工确认", name
                        )
                        continue
                    old.unlink()
                    moved.append("images/" + name)
                except Exception as exc:
                    logger.warning("[body_monitor] 迁移 %s 失败（保留旧文件）: %s", name, exc)

            legacy_presets = IMAGE_DIR / "presets"
            if legacy_presets.is_dir():
                PRESET_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
                for item in list(legacy_presets.iterdir()):
                    if not item.is_file():
                        continue
                    try:
                        dest = PRESET_IMAGE_DIR / item.name
                        if not dest.exists():
                            shutil.copy2(item, dest)
                            if dest.read_bytes() != item.read_bytes():
                                raise OSError("content mismatch")
                        elif dest.read_bytes() != item.read_bytes():
                            logger.warning(
                                "[body_monitor] 预设底图 %s 新旧内容不一致，保留旧文件待人工确认",
                                item.name,
                            )
                            continue
                        item.unlink()
                        moved.append("images/presets/" + item.name)
                    except Exception as exc:
                        logger.warning("[body_monitor] 迁移预设底图失败: %s", exc)
                try:
                    legacy_presets.rmdir()
                except OSError:
                    pass

            if moved:
                logger.info("[body_monitor] 已迁移 %d 个旧数据文件到 %s", len(moved), DATA_DIR)
        except Exception as exc:
            logger.warning("[body_monitor] 迁移旧数据目录失败: %s", exc)

    async def get_custom_image(self):
        f = CUSTOM_IMAGE_DIR / "custom.bin"
        mf = CUSTOM_IMAGE_DIR / "custom.mime"
        if not f.exists():
            w = CUSTOM_IMAGE_DIR / "custom.webp"
            if w.exists():
                return {
                    "status": "ok",
                    "data": {"src": "data:image/webp;base64," + base64.b64encode(w.read_bytes()).decode()},
                }
            return {"status": "ok", "data": {"src": ""}}
        mime = "image/jpeg"
        try:
            if mf.exists():
                mime = mf.read_text(encoding="utf-8").strip() or "image/jpeg"
        except Exception:
            pass
        return {
            "status": "ok",
            "data": {"src": "data:" + mime + ";base64," + base64.b64encode(f.read_bytes()).decode()},
        }

    async def upload_image(self):
        from astrbot.api.web import request as req

        try:
            payload = await req.json(default={})
        except Exception:
            payload = {}
        src = (payload or {}).get("src") or ""
        if not isinstance(src, str) or not src.startswith("data:image/"):
            return {"status": "error", "message": "图片格式不对", "data": {}}
        head, _, b64 = src.partition(",")
        mime = head[5:].split(";")[0] or "image/jpeg"
        if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"):
            mime = "image/jpeg"
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return {"status": "error", "message": "解码失败", "data": {}}
        if len(raw) < 1000:
            return {"status": "error", "message": "图片太小", "data": {}}
        if len(raw) > 30 * 1024 * 1024:
            return {"status": "error", "message": "图片超过 30MB", "data": {}}
        CUSTOM_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        (CUSTOM_IMAGE_DIR / "custom.bin").write_bytes(raw)
        (CUSTOM_IMAGE_DIR / "custom.mime").write_text(mime, encoding="utf-8")
        try:
            self._store_preset_image(self._current_preset_name(), raw, mime)
        except Exception as exc:
            logger.warning("[body_monitor] 保存预设底图失败: %s", exc)
        w = CUSTOM_IMAGE_DIR / "custom.webp"
        if w.exists():
            try:
                w.unlink()
            except Exception:
                pass
        logger.info("[body_monitor] 底图已更换 %s bytes %s", len(raw), mime)
        return {"status": "ok", "data": {"size": len(raw)}}

    async def clear_image(self):
        for name in ("custom.bin", "custom.mime", "custom.webp"):
            f = CUSTOM_IMAGE_DIR / name
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass
        try:
            self._delete_preset_image(self._current_preset_name())
        except Exception as exc:
            logger.warning("[body_monitor] 删除预设底图失败: %s", exc)
        return {"status": "ok", "data": {}}

    def _preset_image_stem(self, name: str) -> str:
        return hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()[:16]

    def _preset_image_paths(self, name: str):
        stem = self._preset_image_stem(name)
        return PRESET_IMAGE_DIR / (stem + ".bin"), PRESET_IMAGE_DIR / (stem + ".mime")

    def _preset_has_image(self, name: str) -> bool:
        bin_path, _ = self._preset_image_paths(name)
        return bin_path.exists()

    def _store_preset_image(self, name: str, raw: bytes, mime: str):
        PRESET_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        bin_path, mime_path = self._preset_image_paths(name)
        bin_path.write_bytes(raw)
        mime_path.write_text(mime, encoding="utf-8")

    def _delete_preset_image(self, name: str):
        bin_path, mime_path = self._preset_image_paths(name)
        for path in (bin_path, mime_path):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass

    def _copy_preset_image(self, src: str, dst: str) -> bool:
        if not self._preset_has_image(src):
            return False
        src_bin, src_mime = self._preset_image_paths(src)
        try:
            raw = src_bin.read_bytes()
            mime = "image/jpeg"
            if src_mime.exists():
                mime = src_mime.read_text(encoding="utf-8").strip() or mime
            self._store_preset_image(dst, raw, mime)
            return True
        except Exception as exc:
            logger.warning("[body_monitor] 复制预设底图失败: %s", exc)
            return False

    def _apply_preset_image(self, name: str) -> bool:
        if not self._preset_has_image(name):
            return False
        bin_path, mime_path = self._preset_image_paths(name)
        try:
            raw = bin_path.read_bytes()
            if len(raw) < 1000 or len(raw) > 30 * 1024 * 1024:
                return False
            mime = "image/jpeg"
            if mime_path.exists():
                mime = mime_path.read_text(encoding="utf-8").strip() or mime
            CUSTOM_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            (CUSTOM_IMAGE_DIR / "custom.bin").write_bytes(raw)
            (CUSTOM_IMAGE_DIR / "custom.mime").write_text(mime, encoding="utf-8")
            webp = CUSTOM_IMAGE_DIR / "custom.webp"
            if webp.exists():
                try:
                    webp.unlink()
                except Exception:
                    pass
            return True
        except Exception as exc:
            logger.warning("[body_monitor] 应用预设底图失败: %s", exc)
            return False

    def _current_preset_name(self) -> str:
        try:
            doc = self._load_doc()
            return str(doc.get("preset") or PRESET_DEFAULT)
        except Exception:
            return PRESET_DEFAULT

    def _migrate_current_image(self):
        try:
            marker = PRESET_IMAGE_DIR / ".migrated"
            if marker.exists():
                return
            PRESET_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            if any(PRESET_IMAGE_DIR.glob("*.bin")):
                marker.write_text("1", encoding="utf-8")
                return
            bin_path = CUSTOM_IMAGE_DIR / "custom.bin"
            if bin_path.exists():
                cur = self._current_preset_name()
                if not self._preset_has_image(cur):
                    mime = "image/jpeg"
                    mime_path = CUSTOM_IMAGE_DIR / "custom.mime"
                    if mime_path.exists():
                        mime = mime_path.read_text(encoding="utf-8").strip() or mime
                    self._store_preset_image(cur, bin_path.read_bytes(), mime)
                    logger.info("[body_monitor] 已把当前底图归到预设: %s", cur)
            marker.write_text("1", encoding="utf-8")
        except Exception as exc:
            logger.warning("[body_monitor] 迁移预设底图失败: %s", exc)

    def _norm_doc(self, raw):
        if isinstance(raw, dict) and isinstance(raw.get("presets"), dict) and raw["presets"]:
            presets = {}
            for k, v in raw["presets"].items():
                if not isinstance(k, str) or not k.strip():
                    continue
                key = k.strip()[:20]
                if not isinstance(v, dict):
                    v = {}
                presets[key] = {
                    "groups": v.get("groups") if isinstance(v.get("groups"), list) else [],
                    "list": v.get("list") if isinstance(v.get("list"), list) else [],
                    "ui": v.get("ui") if isinstance(v.get("ui"), dict) else {},
                }
            if not presets:
                return None
            cur = raw.get("preset")
            if cur not in presets:
                cur = next(iter(presets))
            return {"preset": cur, "presets": presets}
        if isinstance(raw, dict) and isinstance(raw.get("list"), list):
            return {"preset": PRESET_DEFAULT, "presets": {PRESET_DEFAULT: {
                "groups": raw.get("groups") if isinstance(raw.get("groups"), list) else [],
                "list": raw["list"],
                "ui": raw.get("ui") if isinstance(raw.get("ui"), dict) else {},
            }}}
        return None

    def _load_doc(self):
        for f in (POS_FILE, DEFAULT_POS_FILE):
            try:
                if f.exists():
                    doc = self._norm_doc(json.loads(f.read_text(encoding="utf-8")))
                    if doc:
                        return doc
            except Exception as exc:
                logger.warning("[body_monitor] 读取 %s 失败：%s", f.name, exc)
        return {"preset": PRESET_DEFAULT, "presets": {PRESET_DEFAULT: {"groups": [], "list": [], "ui": {}}}}

    @staticmethod
    def _tmp_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")

    def _write_doc(self, doc):
        _ensure_data_dir()
        try:
            if POS_FILE.exists():
                POS_FILE.with_suffix(".json.bak").write_text(
                    POS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        tmp = self._tmp_path(POS_FILE)
        try:
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, POS_FILE)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _preset_payload(self, doc):
        cur = doc["presets"].get(doc["preset"]) or {"groups": [], "list": [], "ui": {}}
        return {
            "groups": cur.get("groups") or [],
            "list": cur.get("list") or [],
            "ui": cur.get("ui") or {},
            "preset": doc["preset"],
            "presets": list(doc["presets"].keys()),
            "legacy": {},
        }

    async def get_positions(self):
        return {"status": "ok", "data": self._preset_payload(self._load_doc())}

    async def save_positions(self, d: str = ""):
        return await self._save_from_payload(decode_param(d))

    async def save_positions_post(self):
        from astrbot.api.web import request as req

        payload = await req.json(default={}) or {}
        raw = payload.get("d") if isinstance(payload, dict) else ""
        if not isinstance(raw, str) or not raw.strip():
            return {"status": "error", "message": "缺少坐标数据"}
        return await self._save_from_payload(raw)

    async def _save_from_payload(self, raw: str):
        raw = (raw or "").strip()
        if not raw:
            return {"status": "error", "message": "缺少坐标数据"}
        groups = []
        raw_items = raw
        if raw.startswith("G:") and "|I:" in raw:
            gpart, raw_items = raw[2:].split("|I:", 1)
            for chunk in gpart.split(";"):
                if not chunk.strip():
                    continue
                p = chunk.split("~")
                gid = (p[0] or "").strip()[:24]
                gname = ((p[1] if len(p) > 1 else gid) or gid).strip()[:24]
                gparent = ((p[2] if len(p) > 2 else "") or "").strip()[:24]
                if gid and all(g["id"] != gid for g in groups):
                    groups.append({"id": gid, "name": gname, "parent": gparent})
            groups = groups[:60]
            gidset = set(g["id"] for g in groups)
            for g in groups:
                if g["parent"] and g["parent"] not in gidset:
                    g["parent"] = ""
            # 父级成环会让前端树渲染不出来，这里直接断开
            gbyid = {g["id"]: g for g in groups}
            for g in groups:
                seen = set()
                cur = g["parent"]
                while cur:
                    if cur == g["id"] or cur in seen:
                        g["parent"] = ""
                        break
                    seen.add(cur)
                    cur = (gbyid.get(cur) or {}).get("parent", "")

        ui = {}
        pname = ""
        if "|U:" in raw_items:
            raw_items, upart = raw_items.split("|U:", 1)
            up = upart.split("~")
            try:
                ui = {
                    "g": (up[0] or "")[:24] if len(up) > 0 else "",
                    "e": len(up) > 1 and up[1] == "1",
                    "folded": [s[:24] for s in (up[2].split(",") if len(up) > 2 and up[2] else [])][:60],
                    "s": max(1.0, min(10.0, float(up[3]))) if len(up) > 3 and up[3] else 1.0,
                    "tx": int(float(up[4])) if len(up) > 4 and up[4] else 0,
                    "ty": int(float(up[5])) if len(up) > 5 and up[5] else 0,
                }
                pname = (up[6] or "").strip()[:20] if len(up) > 6 else ""
            except Exception:
                ui = {}

        gids = [g["id"] for g in groups]
        items = []
        for chunk in raw_items.split(";"):
            if not chunk.strip():
                continue
            p = chunk.split("~")
            if len(p) < 8:
                continue
            name = (p[0] or "部位")[:20]
            g = p[1] if p[1] in ("head", "body", "arm", "leg", "other") else "other"
            pid = p[2] or ""
            try:
                x, y, rx, ry, b = [int(float(v)) for v in p[3:8]]
            except Exception:
                continue
            shape = (p[8].strip() if len(p) > 8 else "") or "e"
            if shape not in ("e", "c", "p"):
                shape = "e"

            def _num(idx, default=0):
                try:
                    return int(float(p[idx])) if len(p) > idx and p[idx] != "" else default
                except Exception:
                    return default

            pts = []
            if len(p) > 13 and p[13]:
                for seg in p[13].split("|")[:40]:
                    q = seg.split(",")
                    if len(q) != 2:
                        continue
                    try:
                        pts.append([max(0, min(1000, int(float(q[0])))), max(0, min(1000, int(float(q[1]))))])
                    except Exception:
                        continue
            if shape == "p" and len(pts) < 3:
                shape = "e"
            items.append({
                "n": name,
                "g": g,
                "p": pid,
                "x": max(0, min(1000, x)),
                "y": max(0, min(1000, y)),
                "rx": max(5, min(300, rx)),
                "ry": max(5, min(300, ry)),
                "b": max(4, min(40, b)),
                "sh": shape,
                "rot": max(-180, min(180, _num(9))),
                "x2": max(0, min(1000, _num(10, 500))),
                "y2": max(0, min(1000, _num(11, 500))),
                "rr": max(3, min(200, _num(12, 40))),
                "pts": pts,
                "rid": (p[14].strip()[:24] if len(p) > 14 and p[14] else "") or ("r" + str(len(items) + 1)),
                "col": (p[15].strip() if len(p) > 15 else ""),
            })
        if len(items) > 300:
            items = items[:300]
        valid = set(gids) | set(it["rid"] for it in items)
        seen = set()
        for it in items:
            rid = it["rid"]
            if rid in seen:
                base = rid[:20] or "r"
                n = 2
                while f"{base}_{n}" in seen:
                    n += 1
                rid = f"{base}_{n}"[:24]
                it["rid"] = rid
            seen.add(rid)
        byrid = {it["rid"]: it for it in items}
        gset = set(gids)
        for it in items:
            pp = it["p"]
            guard = 0
            while pp in byrid and guard < 30:
                pp = byrid[pp]["p"]
                guard += 1
            if pp not in gset:
                pp = gids[0] if gids else ""
            it["p"] = pp
        if ui and ui.get("g") and ui["g"] not in gids:
            ui["g"] = gids[0] if gids else ""
        doc = self._load_doc()
        target = doc["preset"]
        if pname:
            if pname not in doc["presets"]:
                logger.warning("[body_monitor] 保存目标预设不存在: %s", pname)
                return {"status": "error", "message": "预设不存在：" + pname}
            target = pname
        doc["presets"][target] = {"groups": groups, "list": items, "ui": ui}
        self._write_doc(doc)
        return {"status": "ok", "data": {"count": len(items), "groups": len(groups), "preset": target}}

    async def set_position(self, idx: str = "", x: str = "", y: str = ""):
        try:
            i = int(idx)
            fx = max(0, min(1000, int(x)))
            fy = max(0, min(1000, int(y)))
        except Exception:
            return {"status": "error", "message": "参数不对"}
        doc = self._load_doc()
        cur = doc["presets"][doc["preset"]]
        items = cur.get("list") or []
        if 0 <= i < len(items):
            items[i]["x"] = fx
            items[i]["y"] = fy
            cur["list"] = items
            doc["presets"][doc["preset"]] = cur
            self._write_doc(doc)
        return {"status": "ok", "data": {"idx": i, "x": fx, "y": fy}}

    async def clear_positions(self):
        try:
            doc = self._load_doc()
            doc["presets"][doc["preset"]] = {"groups": [], "list": [], "ui": {}}
            self._write_doc(doc)
            self._drop_preset_states(doc["preset"])
        except Exception as exc:
            logger.warning("[body_monitor] 清空坐标失败: %s", exc)
            return {"status": "error", "message": "清空失败"}
        return {"status": "ok", "data": self._preset_payload(doc)}

    async def reset_positions(self):
        try:
            doc = self._load_doc()
            base = None
            if DEFAULT_POS_FILE.exists():
                base = self._norm_doc(json.loads(DEFAULT_POS_FILE.read_text(encoding="utf-8")))
            if base:
                src_presets = base["presets"]
                src_preset = src_presets.get(doc["preset"]) or src_presets[base["preset"]]
                doc["presets"][doc["preset"]] = json.loads(json.dumps(src_preset))
            else:
                doc["presets"][doc["preset"]] = {"groups": [], "list": [], "ui": {}}
            self._write_doc(doc)
            self._drop_preset_states(doc["preset"])
        except Exception as exc:
            logger.warning("[body_monitor] 重置坐标失败: %s", exc)
            return {"status": "error", "message": "重置失败"}
        return {"status": "ok", "data": self._preset_payload(doc)}

    async def get_presets(self):
        doc = self._load_doc()
        return {"status": "ok", "data": {"current": doc["preset"], "names": list(doc["presets"].keys())}}

    async def switch_preset(self, name: str = ""):
        name = decode_param(name).strip()[:20]
        doc = self._load_doc()
        if name not in doc["presets"]:
            return {"status": "error", "message": "没有这个预设：" + name}
        doc["preset"] = name
        self._write_doc(doc)
        image_changed = self._apply_preset_image(name)
        data = self._preset_payload(doc)
        data["image_changed"] = image_changed
        return {"status": "ok", "data": data}

    async def new_preset(self, name: str = "", copy: str = "0"):
        raw = decode_param(name).strip()[:20] or "新预设"
        doc = self._load_doc()
        final = raw
        n = 2
        while final in doc["presets"]:
            final = raw[:17] + str(n)
            n += 1
        source = doc["preset"]
        if str(copy) == "1":
            doc["presets"][final] = json.loads(json.dumps(doc["presets"][source]))
            try:
                self._copy_preset_image(source, final)
                self._copy_preset_states(source, final)
            except Exception as exc:
                logger.warning("[body_monitor] 复制预设底图失败: %s", exc)
        else:
            doc["presets"][final] = {"groups": [{"id": "body", "name": "身体", "parent": ""}], "list": [], "ui": {}}
        doc["preset"] = final
        self._write_doc(doc)
        data = self._preset_payload(doc)
        data["name"] = final
        data["image_changed"] = False
        return {"status": "ok", "data": data}

    async def delete_preset(self, name: str = ""):
        name = decode_param(name).strip()[:20]
        doc = self._load_doc()
        if name not in doc["presets"]:
            return {"status": "error", "message": "没有这个预设：" + name}
        if len(doc["presets"]) <= 1:
            return {"status": "error", "message": "至少留一个预设"}
        was_current = doc["preset"] == name
        self._delete_preset_image(name)
        self._drop_preset_states(name)
        doc["presets"].pop(name)
        image_changed = False
        if was_current:
            doc["preset"] = next(iter(doc["presets"]))
            image_changed = self._apply_preset_image(doc["preset"])
        self._write_doc(doc)
        data = self._preset_payload(doc)
        data["image_changed"] = image_changed
        return {"status": "ok", "data": data}

    def _export_image(self, name: str):
        data_file, mime_file = self._preset_image_paths(name)
        if not data_file.exists():
            return None
        try:
            mime = "image/jpeg"
            if mime_file.exists():
                mime = mime_file.read_text(encoding="utf-8").strip() or mime
            raw = data_file.read_bytes()
            if not raw or len(raw) > 30 * 1024 * 1024:
                return None
            return {"mime": mime, "data": base64.b64encode(raw).decode("ascii")}
        except Exception as exc:
            logger.warning("[body_monitor] 导出底图失败: %s", exc)
            return None

    def _export_payload(self, name: str):
        doc = self._load_doc()
        preset = doc["presets"].get(name)
        if preset is None:
            return None
        return {
            "format": "astrbot_plugin_body_monitor",
            "version": 1,
            "name": name,
            "groups": preset.get("groups") or [],
            "list": preset.get("list") or [],
            "ui": preset.get("ui") or {},
            "image": self._export_image(name),
        }

    async def export_preset(self, name: str = ""):
        name = decode_param(name).strip()[:20]
        payload = self._export_payload(name)
        if payload is None:
            return {"status": "error", "message": "没有这个预设：" + name}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="body_monitor_preset.json"'
            },
        )

    async def export_preset_post(self):
        from astrbot.api.web import request as req

        payload = await req.json(default={}) or {}
        name = decode_param(str(payload.get("name") or "")).strip()[:20]
        data = self._export_payload(name)
        if data is None:
            return {"status": "error", "message": "没有这个预设：" + name}
        raw_dir = str(payload.get("dir") or "").strip() or self._export_dir()
        target = self._safe_export_dir(raw_dir)
        if target is None:
            return {
                "status": "error",
                "message": "导出目录不允许：只能选当前用户目录下（如桌面、文档）或插件数据目录",
            }
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return {"status": "error", "message": "导出目录不可用：" + str(exc)}
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", name) or "preset"
        filename = "body_monitor_{}_{}.json".format(
            safe, time.strftime("%Y%m%d_%H%M%S")
        )
        path = target / filename
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            return {"status": "error", "message": "写入失败：" + str(exc)}
        raw = self._settings_raw()
        raw["export_dir"] = str(target)
        self._save_settings_raw(raw)
        logger.info("[body_monitor] 预设已导出: %s", path)
        return {
            "status": "ok",
            "data": {"path": str(path), "dir": str(target), "filename": filename},
        }

    def _sanitize_import_preset(self, preset: dict) -> dict:
        def text(value, limit):
            return str(value or "").strip()[:limit]

        def num(value, default=0, lo=0, hi=1000):
            try:
                return max(lo, min(hi, int(float(value))))
            except Exception:
                return default

        groups = []
        ids = set()
        for raw in (preset.get("groups") or [])[:60]:
            if not isinstance(raw, dict):
                continue
            gid = text(raw.get("id"), 24)
            if not gid or gid in ids:
                continue
            ids.add(gid)
            groups.append({
                "id": gid,
                "name": text(raw.get("name") or gid, 24),
                "parent": text(raw.get("parent"), 24),
            })
        if not groups:
            groups = [{"id": "body", "name": "身体", "parent": ""}]
            ids = {"body"}
        for group in groups:
            if group["parent"] and group["parent"] not in ids:
                group["parent"] = ""
        # 导入文件可能被手改出 A→B→A 的父级环，前端会整棵树渲染不出来，这里断开
        by_id = {g["id"]: g for g in groups}
        for group in groups:
            seen = set()
            cur = group["parent"]
            while cur:
                if cur == group["id"] or cur in seen:
                    group["parent"] = ""
                    break
                seen.add(cur)
                cur = (by_id.get(cur) or {}).get("parent", "")

        items = []
        for raw in (preset.get("list") or [])[:300]:
            if not isinstance(raw, dict):
                continue
            shape = raw.get("sh") if raw.get("sh") in ("e", "c", "p") else "e"
            points = []
            for point in (raw.get("pts") or [])[:40]:
                if isinstance(point, (list, tuple)) and len(point) == 2:
                    points.append([num(point[0], 0, 0, 1000), num(point[1], 0, 0, 1000)])
            if shape == "p" and len(points) < 3:
                shape = "e"
            parent = text(raw.get("p"), 24)
            if parent not in ids:
                parent = groups[0]["id"]
            color = raw.get("col")
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                color = ""
            items.append({
                "n": text(raw.get("n") or "部位", 20),
                "g": raw.get("g") if raw.get("g") in ("head", "body", "arm", "leg", "other") else "other",
                "p": parent,
                "x": num(raw.get("x"), 500, 0, 1000),
                "y": num(raw.get("y"), 500, 0, 1000),
                "rx": num(raw.get("rx"), 40, 5, 300),
                "ry": num(raw.get("ry"), 40, 5, 300),
                "b": num(raw.get("b"), 11, 4, 40),
                "sh": shape,
                "rot": num(raw.get("rot"), 0, -180, 180),
                "x2": num(raw.get("x2"), 500, 0, 1000),
                "y2": num(raw.get("y2"), 500, 0, 1000),
                "rr": num(raw.get("rr"), 40, 3, 200),
                "pts": points,
                "rid": text(raw.get("rid"), 24) or ("r" + str(len(items) + 1)),
                "col": color,
            })

        # rid 是前端定位、状态存储、触碰上报的主键，重复会让热区互相串：强制去重
        seen_rids = set()
        for item in items:
            rid = item["rid"]
            if rid in seen_rids:
                base = rid[:20] or "r"
                n = 2
                while f"{base}_{n}" in seen_rids:
                    n += 1
                rid = f"{base}_{n}"[:24]
                item["rid"] = rid
            seen_rids.add(rid)

        raw_ui = preset.get("ui") if isinstance(preset.get("ui"), dict) else {}
        ui = {}
        gid = raw_ui.get("g")
        if isinstance(gid, str) and gid in ids:
            ui["g"] = gid
        if isinstance(raw_ui.get("e"), bool):
            ui["e"] = raw_ui["e"]
        folded = raw_ui.get("folded")
        if isinstance(folded, list):
            ui["folded"] = [str(x)[:24] for x in folded if isinstance(x, str) and x in ids][:60]
        scale = raw_ui.get("s")
        if isinstance(scale, (int, float)) and not isinstance(scale, bool):
            ui["s"] = round(max(1.0, min(10.0, float(scale))), 2)
        for key in ("tx", "ty"):
            value = raw_ui.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                ui[key] = int(value)
        return {"groups": groups, "list": items, "ui": ui}

    async def import_preset(self):
        from astrbot.api.web import request as req

        payload = await req.json(default={}) or {}
        if not isinstance(payload, dict):
            return {"status": "error", "message": "导入文件格式不对"}
        preset = payload.get("preset") if isinstance(payload.get("preset"), dict) else payload
        if not isinstance(preset.get("list"), list):
            return {"status": "error", "message": "导入文件里没有热区数据"}
        cleaned = self._sanitize_import_preset(preset)
        raw_name = payload.get("name") or preset.get("name") or "导入预设"
        base = str(raw_name).strip()[:20] or "导入预设"
        for ch in "/\\~;|?#%":
            base = base.replace(ch, "")
        base = base or "导入预设"
        doc = self._load_doc()
        final = base
        n = 2
        while final in doc["presets"]:
            final = base[:17] + str(n)
            n += 1
        doc["presets"][final] = cleaned
        doc["preset"] = final
        self._write_doc(doc)

        image_changed = False
        image = payload.get("image")
        if isinstance(image, dict) and isinstance(image.get("data"), str):
            try:
                mime = str(image.get("mime") or "image/jpeg")
                if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"):
                    mime = "image/jpeg"
                raw = base64.b64decode(image["data"], validate=False)
                if 1000 <= len(raw) <= 30 * 1024 * 1024:
                    self._store_preset_image(final, raw, mime)
                    self._apply_preset_image(final)
                    image_changed = True
                else:
                    logger.warning("[body_monitor] 导入底图大小不合适，已跳过")
            except Exception as exc:
                logger.warning("[body_monitor] 导入底图失败: %s", exc)
        # image 为 null 表示这个预设没有底图：按“没底图就保持当前底图”的规则，不动现有底图。

        data = self._preset_payload(doc)
        data["name"] = final
        data["image_changed"] = image_changed
        logger.info("[body_monitor] 已导入预设: %s（%s 个热区）", final, len(cleaned["list"]))
        return {"status": "ok", "data": data}

    async def terminate(self):
        for state in getattr(self, "_combo", {}).values():
            task = state.get("task")
            if task is not None and not task.done():
                task.cancel()
        self._combo = {}

    async def serve_ui(self):
        f = UI_FILE if UI_FILE.exists() else UI_FILE_OLD
        return HTMLResponse(
            f.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    async def serve_image(self):
        exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
        path = None
        for ext in exts:
            cand = IMAGE_DIR / ("body" + ext)
            if cand.exists():
                path = cand
                break
        if path is None:
            files = [f for f in sorted(IMAGE_DIR.glob("*")) if f.is_file() and f.suffix.lower() in exts]
            path = files[0] if files else None
        if path is None:
            return HTMLResponse("missing image", status_code=404)
        media = {
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(path.suffix.lower(), "image/jpeg")
        return Response(content=path.read_bytes(), media_type=media)

    async def on_touch_query(self):
        from astrbot.api.web import request as req

        return await self.on_touch(
            str(req.query.get("part") or ""),
            str(req.query.get("rid") or ""),
        )

    async def on_touch(self, part: str = "", rid: str = ""):
        part = decode_param(part).strip()[:20]
        rid = str(rid or "").strip()[:24]
        doc = self._load_doc()
        try:
            regions = doc["presets"][doc["preset"]]["list"]
        except Exception:
            regions = []
        if rid:
            found = next((r for r in regions if str(r.get("rid") or "") == rid), None)
            if found is not None:
                part = str(found.get("n") or part).strip()[:20] or part
            else:
                rid = ""
        if not part:
            return {"status": "error", "message": "缺少部位"}
        if not rid:
            found = next((r for r in regions if str(r.get("n") or "") == part), None)
            if found is not None:
                rid = str(found.get("rid") or "")
        key = rid or ("name:" + part)
        if not bool(self.config.get("enable", True)):
            self._log(part, "error", f"{part} · 触碰已关闭")
            return {"status": "error", "message": "触碰功能已关闭"}
        session = str(self.config.get("umo", "") or "").strip()
        if not session:
            if not self.last_session:
                self._load_session()
            session = self.last_session or ""
        if not session:
            self._log(part, "error", f"{part} · 没有目标会话")
            return {"status": "error", "message": "还没有目标会话：先在配置里填 UMO，或让机器人收到一条消息"}
        if session.count(":") < 2:
            self._log(part, "error", f"{part} · UMO 格式不对")
            return {"status": "error", "message": "UMO 格式不对：平台:消息类型:会话id"}
        usage_error = self._usage_missing_message()
        if usage_error:
            self._log(part, "error", f"{part} · {usage_error}")
            return {"status": "error", "message": usage_error}
        combo = self._combo.get(session)
        window = self._combo_window()
        if combo is None:
            now = time.time()
            cooldown = self._cooldown_seconds()
            last = float(self._last_touch.get(session, 0) or 0)
            if cooldown > 0 and now - last < cooldown:
                remain = max(1, int(cooldown - (now - last)) + 1)
                msg = f"冷却中，还需要 {remain} 秒"
                logger.info("[body_monitor] %s", msg)
                self._log(part, "cooldown", f"{part} · {msg}")
                return {"status": "error", "message": msg}
            window = self._combo_window()
            if window <= 0:
                await self._fire_touch(session, {key: {"n": part, "rid": rid, "c": 1}}, 1)
                return {"status": "ok", "data": {"part": part, "count": 1}}
            combo = {"count": 0, "parts": {}, "task": None}
            self._combo[session] = combo

        combo["count"] = int(combo.get("count") or 0) + 1
        parts = combo.setdefault("parts", {})
        info = parts.setdefault(key, {"n": part, "rid": rid, "c": 0})
        info["n"] = part
        info["rid"] = rid or info.get("rid") or ""
        info["c"] = int(info.get("c") or 0) + 1
        old_task = combo.get("task")
        if old_task is not None and not old_task.done():
            old_task.cancel()
        combo["task"] = asyncio.create_task(self._settle_combo(session, combo["count"]))
        return {
            "status": "ok",
            "data": {
                "part": part,
                "count": combo["count"],
                "pending": True,
                "window": window,
            },
        }

    async def _settle_combo(self, session: str, token: int):
        """空闲计时结束：把这段时间里的连击合并成一次互动。"""
        try:
            await asyncio.sleep(self._combo_window())
        except asyncio.CancelledError:
            return
        combo = self._combo.get(session)
        if not combo or int(combo.get("count") or 0) != token:
            return
        self._combo.pop(session, None)
        parts = dict(combo.get("parts") or {})
        total = int(combo.get("count") or 1)
        await self._fire_touch(session, parts, total)

    async def _fire_touch(self, session: str, parts: dict, count: int):
        """真正发起一次互动：冷却从这一刻开始算。parts 兼容旧格式 {部位: 次数}。"""
        # 连击等待期间用户可能关掉了触碰开关：结算时再确认一次，避免“关了还发出去”。
        if not self._cfg_bool("enable", True):
            self._log("触碰", "error", "触碰已关闭，本次互动未发送")
            logger.info("[body_monitor] 触碰已关闭，丢弃一次待结算互动")
            return
        usage = self._load_usage()
        usage_error = self._usage_missing_message(usage)
        if usage_error:
            self._log("词库", "error", usage_error)
            logger.info("[body_monitor] 词库不完整，丢弃一次待结算互动: %s", usage_error)
            return
        self._last_touch[session] = time.time()
        normalized = {}
        for key, info in (parts or {}).items():
            if isinstance(info, dict):
                name = str(info.get("n") or "").strip()[:20] or "未指定"
                c = max(1, int(info.get("c") or 1))
                rid = str(info.get("rid") or "").strip()[:24]
            else:
                name = str(key or "").strip()[:20] or "未指定"
                c = max(1, int(info or 1))
                rid = ""
            k = rid or ("name:" + name)
            slot = normalized.setdefault(k, {"n": name, "rid": rid, "c": 0})
            slot["c"] += c
            slot["n"] = name
            if rid:
                slot["rid"] = rid
        if not normalized:
            normalized = {"name:未指定": {"n": "未指定", "rid": "", "c": 1}}
        name_counts = {}
        for info in normalized.values():
            name_counts[info["n"]] = name_counts.get(info["n"], 0) + int(info["c"])
        total = max(1, int(count or sum(name_counts.values())))
        total = max(total, sum(name_counts.values()))
        if len(name_counts) == 1:
            part_name, part_count = next(iter(name_counts.items()))
            part_text = part_name
            detail = f"一下{part_name}" if part_count <= 1 else f"{part_count}次{part_name}"
            log_part = part_text if part_count <= 1 else f"{part_text}×{part_count}"
        else:
            part_text = "、".join(list(name_counts.keys())[:3])
            detail = "、".join(f"{n}次{p}" for p, n in list(name_counts.items())[:3])
            log_part = detail
        username = await self._get_username(session)
        template = str(self.config.get("touch_prompt", DEFAULT_PROMPT) or DEFAULT_PROMPT)
        now_text = time.strftime("%Y-%m-%d %H:%M")
        times = "一下" if total <= 1 else f"{total}次"
        try:
            prompt = template.format(
                part=part_text,
                username=username,
                time=now_text,
                count=total,
                times=times,
                detail=detail,
                **usage,
            )
        except Exception:
            prompt = (
                template.replace("{part}", part_text)
                .replace("{username}", username)
                .replace("{time}", now_text)
                .replace("{count}", str(total))
                .replace("{times}", times)
                .replace("{detail}", detail)
                .replace("{noun}", usage.get("noun", ""))
                .replace("{verb}", usage.get("verb", ""))
                .replace("{adj}", usage.get("adj", ""))
            )
        phrase = f"用{usage.get('noun', '')}{usage.get('adj', '')}{usage.get('verb', '')}".strip()

        state_trigger = None
        if self._state_master() and self._state_on_touch():
            rids = []
            for info in normalized.values():
                rid = str(info.get("rid") or "")
                if rid and rid not in rids:
                    rids.append(rid)
            state_trigger = {
                "preset": self._current_preset_name(),
                "rids": rids,
                "parts": dict(name_counts),
                "phrase": phrase,
                "detail": detail,
                "count": total,
            }

        # 和专业戳一戳一样：把事件交给 AstrBot 自己的流水线，人格、上下文、
        # 记忆插件、回复后处理都由流水线统一处理，插件本身不记账、不加戏。
        if await self._run_via_pipeline(
            session, prompt, username=username, state_trigger=state_trigger
        ):
            logger.info("[body_monitor] 触碰事件已送入流水线: %s（%s）", detail, username)
            self._log(part_text, "ok", f"{log_part} · {phrase} · 已发送")
            return {"status": "ok", "data": {"part": part_text, "pipeline": True}}

        provider = self._pick_provider()
        if provider is None:
            self._log(part_text, "error", f"{log_part} · 没有可用模型")
            return {"status": "error", "message": "没有可用的模型，回复没生成"}
        persona = await self._get_persona(session)
        contexts = await self._get_contexts(session)
        text = ""
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts or None,
                system_prompt=persona or None,
            )
            text = str(getattr(resp, "completion_text", "") or "")
        except Exception as e1:
            logger.warning("[body_monitor] 带上下文生成失败: %s", e1)
            try:
                resp = await provider.text_chat(prompt=prompt, system_prompt=persona or None)
                text = str(getattr(resp, "completion_text", "") or "")
            except Exception as e2:
                logger.warning("[body_monitor] 生成回复失败: %s", e2)
                self._log(part_text, "error", f"{log_part} · 模型回复失败")
                return {"status": "error", "message": "模型回复失败，这次没发出去"}
        text = text.strip().strip('"“”').strip()
        if not text:
            self._log(part_text, "error", f"{log_part} · 模型没有返回内容")
            return {"status": "error", "message": "模型没有返回内容，这次没发出去"}
        try:
            sent = await self.context.send_message(session, MessageChain().message(text))
        except Exception as exc:
            logger.exception("[body_monitor] 发送失败")
            self._queue_pending(session, text)
            self._log(part_text, "error", f"{log_part} · 发送失败")
            return {"status": "error", "message": f"发送失败：{exc}，已排队等下次收到消息补发"}
        if sent is False:
            self._log(part_text, "error", f"{log_part} · 找不到会话对应的平台")
            return {"status": "error", "message": "找不到这个会话对应的平台：" + session}
        self._log(part_text, "ok", f"{log_part} · {phrase} · 已发送")
        if state_trigger:
            self._turn_counts.pop(session, None)
            asyncio.create_task(self._update_states_after_touch(state_trigger, text))
        return {"status": "ok", "data": {"part": part_text, "text": text}}

    def _cooldown_seconds(self) -> int:
        try:
            value = int(self.config.get("cooldown", DEFAULT_COOLDOWN))
        except Exception:
            value = DEFAULT_COOLDOWN
        return max(0, min(3600, value))

    def _combo_window(self) -> float:
        try:
            value = float(self.config.get("combo_window", DEFAULT_COMBO_WINDOW))
        except Exception:
            value = DEFAULT_COMBO_WINDOW
        return max(0.0, min(10.0, value))

    def _cfg_bool(self, key: str, default: bool) -> bool:
        try:
            value = self.config.get(key, default)
            return bool(value)
        except Exception:
            return default

    def _cfg_int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except Exception:
            value = default
        return max(lo, min(hi, value))

    def _state_master(self) -> bool:
        return self._cfg_bool("body_state_enable", True)

    def _state_tool_enable(self) -> bool:
        return self._cfg_bool("state_tool_enable", True)

    def _state_on_touch(self) -> bool:
        return self._cfg_bool("state_on_touch", True)

    def _state_on_mention(self) -> bool:
        return self._cfg_bool("state_on_mention", False)

    def _state_every_turns(self) -> bool:
        return self._cfg_bool("state_every_turns", True)

    def _state_turn_interval(self) -> int:
        return self._cfg_int("state_turn_interval", DEFAULT_STATE_TURN_INTERVAL, 1, 100)

    def _state_history_turns(self) -> int:
        return self._cfg_int("state_history_turns", DEFAULT_STATE_HISTORY_TURNS, 1, 50)

    def _state_target_session(self, session: str) -> bool:
        target = str(self.config.get("umo", "") or "").strip()
        if target:
            return session == target
        if not self.last_session:
            self._load_session()
        return bool(session) and session == (self.last_session or "")

    def _load_states(self) -> dict:
        try:
            if STATE_FILE.exists():
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception as exc:
            logger.warning("[body_monitor] 读取身体状态失败: %s", exc)
        return {}

    def _save_states(self, data: dict):
        _ensure_data_dir()
        tmp = self._tmp_path(STATE_FILE)
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _preset_states(self, preset: str) -> dict:
        data = self._load_states()
        states = data.get(preset)
        return states if isinstance(states, dict) else {}

    def _active_states(self, preset: str) -> dict:
        """读取当前有效状态：补全 36.5°C 基础体温，清掉已过期备注，兼容旧数据。"""
        data = self._load_states()
        raw = data.get(preset)
        if not isinstance(raw, dict):
            raw = {}
        doc = self._load_doc()
        regions = doc["presets"].get(preset, {}).get("list", [])
        now = time.time()
        baseline = self._state_ui_settings()["state_baseline_temp"]
        out = {}
        changed = False
        for r in regions:
            rid = str(r.get("rid") or "")
            if not rid:
                continue
            st = raw.get(rid)
            if not isinstance(st, dict):
                st = {}
                changed = True
            temp = st.get("temp")
            if isinstance(temp, bool) or not isinstance(temp, (int, float)):
                st["temp"] = baseline
                changed = True
            else:
                value = round(max(20.0, min(45.0, float(temp))), 1)
                if value != temp:
                    st["temp"] = value
                    changed = True
            old_notes = st.get("notes")
            if not isinstance(old_notes, list):
                old_notes = []
                changed = True
            notes = []
            for note in old_notes:
                if not isinstance(note, dict):
                    changed = True
                    continue
                text = self._clean_note(note.get("text"))
                if not text:
                    changed = True
                    continue
                if text != note.get("text"):
                    note["text"] = text
                    changed = True
                expires = note.get("expires")
                if isinstance(expires, bool) or not isinstance(expires, (int, float)):
                    expires = now + 300
                    note["expires"] = expires
                    changed = True
                if expires <= now:
                    changed = True
                    continue
                level = note.get("level")
                if isinstance(level, bool) or level not in (0, 1, 2):
                    note["level"] = 1
                    changed = True
                since = note.get("since")
                if isinstance(since, bool) or not isinstance(since, (int, float)):
                    note["since"] = now
                    changed = True
                notes.append(note)
            st["notes"] = notes
            if not notes:
                try:
                    cur_t = float(st.get("temp", baseline))
                except Exception:
                    cur_t = float(baseline)
                last_cool = st.get("cooled")
                if isinstance(last_cool, bool) or not isinstance(last_cool, (int, float)):
                    last_cool = 0.0
                if abs(cur_t - float(baseline)) > 0.05 and now - float(last_cool) >= 15:
                    step = 0.3
                    nt = cur_t - step if cur_t > baseline else cur_t + step
                    if abs(nt - baseline) < step:
                        nt = float(baseline)
                    st["temp"] = round(nt, 1)
                    st["cooled"] = now
                    changed = True
            st.setdefault("updated", now)
            out[rid] = st
        if set(raw.keys()) != set(out.keys()):
            changed = True
        if changed:
            data[preset] = out
            self._save_states(data)
        return out

    def _copy_preset_states(self, src: str, dst: str):
        data = self._load_states()
        states = data.get(src)
        if isinstance(states, dict):
            data[dst] = json.loads(json.dumps(states))
            self._save_states(data)

    def _drop_preset_states(self, name: str):
        data = self._load_states()
        if name in data:
            data.pop(name, None)
            self._save_states(data)

    async def get_states(self):
        doc = self._load_doc()
        preset = doc["preset"]
        states = self._active_states(preset)
        return {"status": "ok", "data": {"preset": preset, "states": states}}

    async def clear_states(self):
        async with self._state_lock:
            data = self._load_states()
            data.pop(self._current_preset_name(), None)
            self._save_states(data)
        return {"status": "ok", "data": {}}

    async def update_states_now(self):
        if not self._state_master():
            return {"status": "error", "message": "身体状态功能已关闭"}
        preset = self._current_preset_name()
        if preset in self._state_busy:
            return {"status": "ok", "data": {"busy": True, "message": "正在更新中，请稍后再试"}}
        self._state_busy.add(preset)
        session = str(self.config.get("umo", "") or "").strip()
        if not session:
            if not self.last_session:
                self._load_session()
            session = self.last_session or ""
        self._turn_counts.pop(session, None)
        try:
            result = await asyncio.wait_for(
                self._manual_state_update(preset, session), timeout=110
            )
        except asyncio.TimeoutError:
            self._state_busy.discard(preset)
            self._log("状态", "error", "手动更新超时")
            return {"status": "error", "message": "状态更新超时，请稍后再试"}
        except Exception as exc:
            self._state_busy.discard(preset)
            logger.exception("[body_monitor] 手动更新状态失败")
            self._log("状态", "error", "手动更新失败")
            return {"status": "error", "message": "状态更新失败：" + str(exc)}
        if not isinstance(result, dict):
            result = {"ok": False, "message": "状态更新没有返回结果"}
        return {
            "status": "ok",
            "data": {
                "busy": False,
                "ok": bool(result.get("ok")),
                "changed": result.get("changed") or [],
                "message": str(result.get("message") or ""),
            },
        }

    async def _manual_state_update(self, preset: str, session: str):
        try:
            result = await self._run_state_update(
                preset,
                action="用户手动点击了「更新状态」，请根据最近的对话和当前时间重新评估全身状态。",
                reply="",
                session=session,
                include_history=True,
            )
            if not isinstance(result, dict):
                result = {"ok": False, "message": "状态更新没有返回结果"}
            if not result.get("ok"):
                self._log(
                    "状态",
                    "error",
                    "手动更新失败：" + (str(result.get("message") or "") or "未知原因"),
                )
            elif not result.get("changed"):
                self._log("状态", "ok", "手动更新：没有变化")
            return result
        finally:
            self._state_busy.discard(preset)

    @filter.llm_tool(name="get_body_state")
    async def get_body_state(self, event, part: str = ""):
        """查看角色当前的身体状态（温度、状态、剩余持续时间）。

        Args:
            part(string): 可选，部位名称；不填返回全身有状态的部位
        """
        if not self._state_master():
            return "身体状态功能当前未开启。"
        if not self._state_tool_enable():
            return "查看身体状态的工具当前未开启。"
        preset = self._current_preset_name()
        states = self._active_states(preset)
        doc = self._load_doc()
        regions = doc["presets"].get(preset, {}).get("list", [])
        now = time.time()
        baseline = self._state_ui_settings()["state_baseline_temp"]
        wanted = str(part or "").strip()
        lines = []
        hidden_normal = 0
        for r in regions:
            rid = str(r.get("rid") or "")
            if not rid:
                continue
            name = str(r.get("n") or "")
            if wanted and wanted not in name:
                continue
            st = states.get(rid) if isinstance(states.get(rid), dict) else {}
            temp = st.get("temp")
            notes = st.get("notes") or []
            temp_ok = isinstance(temp, (int, float)) and not isinstance(temp, bool)
            normal = temp_ok and float(temp) < baseline + 0.5
            if not wanted and not notes and normal:
                hidden_normal += 1
                continue
            bits = []
            if temp_ok:
                bits.append(f"{float(temp):.1f}°C")
            for note in notes:
                if not isinstance(note, dict):
                    continue
                remain = max(0, int(float(note.get("expires") or now) - now))
                level = {0: "轻", 1: "中", 2: "重"}.get(note.get("level"), "中")
                bits.append(f"{note.get('text')}（剩余{remain}秒，{level}）")
            lines.append(f"- {name}：" + ("；".join(bits) if bits else "正常"))
        if not lines:
            return "当前没有记录到身体状态。" if not wanted else f"没有找到部位「{wanted}」的状态。"
        text = "当前身体状态（预设：" + preset + "）：\n" + "\n".join(lines[:60])
        if not wanted and hidden_normal:
            text += f"\n其余 {hidden_normal} 个部位体温正常（{baseline:.1f}°C）。"
        return text

    def _age_text(self, seconds: float) -> str:
        seconds = max(0, float(seconds or 0))
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{int(seconds // 60)}分钟前"
        if seconds < 86400:
            return f"{int(seconds // 3600)}小时前"
        return f"{int(seconds // 86400)}天前"

    def _clean_note(self, text) -> str:
        value = str(text or "").strip()[:60]
        value = re.sub(
            r"[（(]\s*(?:刚刚|\d+\s*(?:秒|分钟|小时|天)前)\s*[）)]\s*$", "", value
        ).strip()
        return value[:40]

    def _state_lines(self, preset: str) -> str:
        doc = self._load_doc()
        regions = doc["presets"].get(preset, {}).get("list", [])
        states = self._active_states(preset)
        now = time.time()
        baseline = self._state_ui_settings()["state_baseline_temp"]
        active_lines = []
        idle_names = []
        for r in regions[:120]:
            rid = str(r.get("rid") or "")
            if not rid:
                continue
            name = str(r.get("n") or "未命名")
            st = states.get(rid) if isinstance(states.get(rid), dict) else {}
            temp = st.get("temp")
            temp_text = f"{float(temp):.1f}°C" if isinstance(temp, (int, float)) else f"{baseline:.1f}°C"
            notes = []
            for note in st.get("notes") or []:
                if not isinstance(note, dict):
                    continue
                text = self._clean_note(note.get("text"))
                if not text:
                    continue
                expires = float(note.get("expires") or now)
                remain = max(0, int(expires - now))
                level = {0: "轻", 1: "中", 2: "重"}.get(note.get("level"), "中")
                notes.append(f"{text}（剩余{remain}秒，{level}）")
            temp_hot = (
                isinstance(temp, (int, float))
                and not isinstance(temp, bool)
                and abs(float(temp) - float(baseline)) >= 0.2
            )
            if notes or temp_hot:
                active_lines.append(
                    f"- rid={rid} 名称={name} 温度={temp_text} 状态={'、'.join(notes) if notes else '无'}"
                )
            else:
                idle_names.append(f"{rid} {name}")
        out = "\n".join(active_lines)
        if idle_names:
            if out:
                out += "\n"
            out += "其余部位当前无状态（只列编号和名称，要改就直接用它的 rid）：" + "、".join(idle_names)
        return out or "（当前预设没有部位）"

    def _history_text(self, contexts, turns: int) -> str:
        if not contexts:
            return ""
        items = contexts[-max(1, int(turns)) * 2:]
        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            content = item.get("content")
            if isinstance(content, list):
                bits = []
                for part in content:
                    if isinstance(part, dict):
                        bits.append(str(part.get("text") or ""))
                    else:
                        bits.append(str(part))
                text = " ".join(b for b in bits if b)
            else:
                text = str(content or "")
            text = text.strip()
            if text:
                lines.append(f"{role}: {text[:500]}")
        return "\n".join(lines)

    def _match_region_rids(self, preset: str, text: str) -> list:
        doc = self._load_doc()
        regions = doc["presets"].get(preset, {}).get("list", [])
        rids = []
        for r in regions:
            rid = str(r.get("rid") or "")
            name = str(r.get("n") or "")
            if rid and name and name in text:
                rids.append(rid)
        return rids

    def _parse_state_updates(self, raw: str) -> list:
        text = str(raw or "").strip()
        if "```" in text:
            text = re.sub(r"```[a-zA-Z]*", "", text).replace("```", "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            obj = json.loads(text[start:end + 1])
        except Exception:
            return []
        updates = obj.get("updates") if isinstance(obj, dict) else None
        if not isinstance(updates, list):
            return []
        return [u for u in updates if isinstance(u, dict)][:120]

    async def _run_state_update(
        self,
        preset: str,
        action: str,
        reply: str,
        focus_rids: list | None = None,
        session: str = "",
        include_history: bool = False,
    ):
        if not self._state_master():
            return {"ok": False, "message": "身体状态功能已关闭"}
        provider = self._pick_state_provider()
        if provider is None:
            logger.warning("[body_monitor] 状态更新没有可用模型")
            return {"ok": False, "message": "没有可用的状态更新模型"}
        doc = self._load_doc()
        regions = doc["presets"].get(preset, {}).get("list", [])
        if not regions:
            return {"ok": False, "message": "当前预设没有部位"}
        settings = self._state_ui_settings()
        baseline = settings["state_baseline_temp"]
        min_duration = settings["state_min_duration"]
        max_duration = settings["state_max_duration"]
        max_notes = settings["state_max_notes"]
        history = ""
        if include_history and session:
            try:
                history = self._history_text(
                    await self._get_contexts(session), self._state_history_turns()
                )
            except Exception:
                history = ""
        focus = [str(r) for r in (focus_rids or []) if r]
        focus_text = "、".join(focus) if focus else "无（可自行判断受影响的部位）"
        template = str(self.config.get("state_prompt", "") or "").strip() or DEFAULT_STATE_PROMPT
        history_block = f"\n最近对话：\n{history}\n" if history else ""
        prompt = template
        for key, value in {
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "states": self._state_lines(preset),
            "action": action,
            "reply": reply,
            "history": history_block,
            "focus": focus_text,
            "min_duration": min_duration,
            "max_duration": max_duration,
            "max_notes": max_notes,
            "baseline": f"{baseline:.1f}",
        }.items():
            prompt = prompt.replace("{" + key + "}", str(value))
        try:
            resp = await asyncio.wait_for(provider.text_chat(prompt=prompt), timeout=90)
            raw = str(getattr(resp, "completion_text", "") or "")
            try:
                logger.info(
                    "[body_monitor] 状态更新用量: prompt_chars=%s output_chars=%s usage=%s resp_fields=%s",
                    len(prompt),
                    len(raw),
                    getattr(resp, "usage", None),
                    list(vars(resp).keys()) if hasattr(resp, "__dict__") else type(resp).__name__,
                )
            except Exception:
                pass
        except asyncio.TimeoutError:
            logger.warning("[body_monitor] 状态更新模型调用超时（90 秒）")
            return {"ok": False, "message": "状态更新模型调用超时"}
        except Exception as exc:
            logger.warning("[body_monitor] 状态更新模型调用失败: %s", exc)
            return {"ok": False, "message": "状态更新模型调用失败"}
        updates = self._parse_state_updates(raw)
        if not updates:
            logger.info(
                "[body_monitor] 状态更新没有返回有效内容，原始回复: %s",
                raw.strip()[:300] or "（空）",
            )
            return {"ok": True, "changed": [], "message": "模型没有返回有效变化"}
        valid = {str(r.get("rid") or ""): str(r.get("n") or "") for r in regions if r.get("rid")}
        name_to_rid = {v: k for k, v in valid.items() if v}
        now = time.time()
        changed = []
        async with self._state_lock:
            preset_states = self._active_states(preset)
            for upd in updates:
                rid = str(upd.get("rid") or "").strip()
                if rid not in valid:
                    rid = name_to_rid.get(str(upd.get("name") or "").strip(), "")
                if not rid or rid not in valid:
                    continue
                cur = preset_states.get(rid)
                if not isinstance(cur, dict):
                    cur = {}
                old_notes = {}
                for note in cur.get("notes") or []:
                    if isinstance(note, dict) and note.get("text"):
                        old_notes[self._clean_note(note["text"])] = note
                notes_changed = False
                if "notes" in upd:
                    notes_value = upd.get("notes")
                    if isinstance(notes_value, str):
                        notes_value = [notes_value]
                    if not isinstance(notes_value, list):
                        notes_value = []
                    new_notes = []
                    for raw_note in notes_value:
                        if isinstance(raw_note, dict):
                            text = self._clean_note(raw_note.get("text") or raw_note.get("note"))
                            duration = raw_note.get("duration")
                            level = raw_note.get("level")
                        else:
                            text = self._clean_note(raw_note)
                            duration = None
                            level = None
                        if not text:
                            continue
                        old = old_notes.get(text) or {}
                        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                            expires = now + max(
                                min_duration, min(max_duration, int(duration))
                            )
                        else:
                            expires = float(old.get("expires") or 0)
                            if expires <= now:
                                expires = now + 60
                            expires = min(expires, now + max_duration)
                        if isinstance(level, bool) or level not in (0, 1, 2):
                            old_level = old.get("level")
                            level = old_level if old_level in (0, 1, 2) else 1
                        new_notes.append({
                            "text": text,
                            "since": float(old.get("since") or now),
                            "expires": expires,
                            "level": int(level),
                        })
                    new_notes = new_notes[:max_notes]
                    old_sig = [
                        (self._clean_note(n.get("text")), int(n.get("level") or 0))
                        for n in (cur.get("notes") or [])
                        if isinstance(n, dict) and n.get("text")
                    ]
                    new_sig = [(n["text"], int(n["level"])) for n in new_notes]
                    if new_sig != old_sig:
                        cur["notes"] = new_notes
                        notes_changed = True
                old_temp = cur.get("temp")
                if "temp" in upd:
                    value = upd.get("temp")
                    if value is None:
                        cur["temp"] = baseline
                    else:
                        try:
                            cur["temp"] = round(max(20.0, min(45.0, float(value))), 1)
                        except Exception:
                            pass
                if isinstance(cur.get("temp"), bool) or not isinstance(cur.get("temp"), (int, float)):
                    cur["temp"] = baseline
                try:
                    temp_changed = abs(float(cur["temp"]) - float(old_temp)) >= 0.05
                except Exception:
                    temp_changed = True
                if not notes_changed and not temp_changed:
                    continue
                cur["updated"] = now
                preset_states[rid] = cur
                changed.append((rid, valid[rid], cur))
            data = self._load_states()
            data[preset] = preset_states
            self._save_states(data)
        names = []
        for rid, name, st in changed:
            if name and name not in names:
                names.append(name)
        if names:
            self._log("状态", "ok", "状态更新: " + "、".join(names))
        return {"ok": True, "changed": names}

    async def _update_states_after_touch(self, trigger: dict, reply: str):
        preset = str(trigger.get("preset") or self._current_preset_name())
        action = f"被{trigger.get('phrase') or '触碰'}了（{trigger.get('detail') or ''}）"
        await self._run_state_update(
            preset,
            action=action,
            reply=reply,
            focus_rids=trigger.get("rids") or [],
        )

    async def _update_states_after_chat(
        self,
        preset: str,
        session: str,
        message: str,
        reply: str,
        focus_rids: list | None = None,
        include_history: bool = False,
    ):
        action = f"用户说：{message}" if message else "普通聊天"
        await self._run_state_update(
            preset,
            action=action,
            reply=reply,
            focus_rids=focus_rids,
            session=session,
            include_history=include_history,
        )

    @filter.on_llm_response()
    async def on_llm_response(self, event, response):
        if not self._state_master():
            return
        if getattr(response, "is_chunk", False):
            return
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            return
        session = str(getattr(event, "unified_msg_origin", "") or "")
        if not self._state_target_session(session):
            return
        try:
            trigger = event.get_extra("_body_monitor_state")
        except Exception:
            trigger = None
        try:
            synthetic = bool(event.get_extra("_body_monitor_synthetic"))
        except Exception:
            synthetic = False

        # 互动触发：只更新一次，并把“正常聊天轮数”清零
        if isinstance(trigger, dict) and trigger:
            try:
                if event.get_extra("_body_monitor_state_done"):
                    return
                event.set_extra("_body_monitor_state_done", True)
            except Exception:
                pass
            self._turn_counts.pop(session, None)
            logger.info(
                "[body_monitor] 互动回复完成，开始更新全身状态（%s）",
                trigger.get("detail") or "",
            )
            asyncio.create_task(self._update_states_after_touch(trigger, text))
            return

        # 互动产生的隐藏对话不算普通聊天轮数，避免刚互动完又立刻触发定时更新
        if synthetic:
            return

        preset = self._current_preset_name()
        message = str(getattr(event, "message_str", "") or "")

        # 提到身体部位：只走这一条，不叠加同轮的定时更新
        if self._state_on_mention() and message:
            rids = self._match_region_rids(preset, message)
            if rids:
                self._turn_counts.pop(session, None)
                asyncio.create_task(self._update_states_after_chat(
                    preset, session, message, text, focus_rids=rids, include_history=True
                ))
                return

        # 按正常聊天轮数更新：更新后计数清零，从 0 重新数
        if self._state_every_turns():
            count = int(self._turn_counts.get(session, 0)) + 1
            self._turn_counts[session] = count
            if count >= self._state_turn_interval():
                self._turn_counts.pop(session, None)
                asyncio.create_task(self._update_states_after_chat(
                    preset, session, message, text, include_history=True
                ))

    def _settings_raw(self) -> dict:
        try:
            if SETTINGS_FILE.exists():
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception as exc:
            logger.warning("[body_monitor] 读取设置失败: %s", exc)
        return {}

    def _save_settings_raw(self, raw: dict):
        _ensure_data_dir()
        tmp = self._tmp_path(SETTINGS_FILE)
        try:
            tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, SETTINGS_FILE)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _clean_word(value, limit=24) -> str:
        return str(value or "").strip()[:limit]

    def _clean_words(self, value) -> list:
        words = []
        if isinstance(value, list):
            for item in value:
                text = self._clean_word(item)
                if text and text not in words:
                    words.append(text)
        return words[:30]

    @staticmethod
    def _default_usage_tree() -> list:
        return json.loads(json.dumps(USAGE_DEFAULT_TREE))

    def _sanitize_usage_tree(self, raw):
        """校验 名词→形容词→动词 三层词库；返回 None 表示 payload 非法/缺失。"""
        if not isinstance(raw, list):
            return None
        tree = []
        seen_names = set()
        for item in raw[:30]:
            if not isinstance(item, dict):
                continue
            name = self._clean_word(item.get("n") or item.get("name"))
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            raw_adjs = item.get("adjs")
            if not isinstance(raw_adjs, list):
                raw_adjs = item.get("adjectives")
            adjs = []
            seen_adjs = set()
            for adj in (raw_adjs or [])[:30]:
                if not isinstance(adj, dict):
                    continue
                adj_name = self._clean_word(adj.get("n") or adj.get("name"))
                if not adj_name or adj_name in seen_adjs:
                    continue
                seen_adjs.add(adj_name)
                verbs = []
                for verb in (adj.get("verbs") or [])[:30]:
                    word = self._clean_word(verb)
                    if word and word not in verbs:
                        verbs.append(word)
                adjs.append({"n": adj_name, "verbs": verbs})
            tree.append({"n": name, "adjs": adjs})
        return tree

    def _select_usage(self, tree, raw) -> dict:
        """选中路径必须真实存在；不存在就回落到该层第一个子项。"""
        selected = {"n": "", "a": "", "v": ""}
        if not isinstance(tree, list) or not tree:
            return selected
        data = raw if isinstance(raw, dict) else {}
        want_noun = self._clean_word(data.get("n") or data.get("noun"))
        node = next((x for x in tree if x.get("n") == want_noun), tree[0])
        selected["n"] = node.get("n", "")
        adjs = node.get("adjs") or []
        if adjs:
            want_adj = self._clean_word(data.get("a") or data.get("adj"))
            adj = next((x for x in adjs if x.get("n") == want_adj), adjs[0])
            selected["a"] = adj.get("n", "")
            verbs = adj.get("verbs") or []
            if verbs:
                want_verb = self._clean_word(data.get("v") or data.get("verb"))
                selected["v"] = want_verb if want_verb in verbs else verbs[0]
        return selected

    def _usage_tree_from_legacy(self, raw) -> list:
        """旧版是三个平铺列表，没有层级关系。

        迁移策略：把全部形容词挂在“当前选中的名词”下，把全部动词挂在
        “当前选中的形容词”下；其余名词/形容词保持为空，方便用户按新结构
        重新整理，也不会凭空造出大量交叉组合。
        """
        data = raw if isinstance(raw, dict) else {}
        nouns = self._clean_words(data.get("noun_options"))
        if not nouns:
            nouns = [self._clean_word(data.get("noun")) or USAGE_DEFAULTS["noun"]]
        adjs = self._clean_words(data.get("adj_options"))
        if not adjs:
            adjs = [self._clean_word(data.get("adj")) or USAGE_DEFAULTS["adj"]]
        verbs = self._clean_words(data.get("verb_options"))
        if not verbs:
            verbs = [self._clean_word(data.get("verb")) or USAGE_DEFAULTS["verb"]]

        selected_noun = self._clean_word(data.get("noun"))
        if selected_noun not in nouns:
            selected_noun = nouns[0]
        selected_adj = self._clean_word(data.get("adj"))
        if selected_adj not in adjs:
            selected_adj = adjs[0]
        selected_verb = self._clean_word(data.get("verb"))
        if selected_verb not in verbs:
            selected_verb = verbs[0]

        tree = []
        for noun in nouns[:30]:
            node = {"n": noun, "adjs": []}
            if noun == selected_noun:
                for adj in adjs[:30]:
                    node["adjs"].append({
                        "n": adj,
                        "verbs": list(verbs[:30]) if adj == selected_adj else [],
                    })
            tree.append(node)
        return tree

    def _load_usage_state(self) -> dict:
        raw = self._settings_raw()
        tree = self._sanitize_usage_tree(raw.get("usage_tree"))
        if tree is None:
            tree = self._usage_tree_from_legacy(raw)
        selected = self._select_usage(tree, raw.get("usage_selected") or {})
        return {"tree": tree, "selected": selected}

    def _load_usage(self) -> dict:
        """当前选中的 名词/形容词/动词；任一层为空则对应值为空字符串。"""
        selected = self._load_usage_state()["selected"]
        return {
            "noun": selected.get("n", ""),
            "adj": selected.get("a", ""),
            "verb": selected.get("v", ""),
        }

    def _usage_missing_message(self, usage=None) -> str:
        usage = usage or self._load_usage()
        noun = usage.get("noun", "")
        adj = usage.get("adj", "")
        verb = usage.get("verb", "")
        if not noun:
            return "词库是空的：先在「互动触碰」里建一个名词，再在它下面加形容词和动词"
        if not adj:
            return f"「{noun}」下还没有形容词：先到「互动触碰」里给它加一个形容词"
        if not verb:
            return f"「{noun} → {adj}」下还没有动词：先到「互动触碰」里给它加一个动词"
        return ""

    def _migrate_usage_settings(self):
        """把旧版平铺词库迁移为三层树，只做一次。"""
        try:
            raw = self._settings_raw()
            if not raw or isinstance(raw.get("usage_tree"), list):
                return
            tree = self._usage_tree_from_legacy(raw)
            selected = self._select_usage(tree, {
                "noun": raw.get("noun"),
                "adj": raw.get("adj"),
                "verb": raw.get("verb"),
            })
            raw["usage_tree"] = tree
            raw["usage_selected"] = selected
            for key in ("noun", "adj", "verb", "noun_options", "adj_options", "verb_options"):
                raw.pop(key, None)
            self._save_settings_raw(raw)
            logger.info("[body_monitor] 旧词库已迁移为 名词→形容词→动词 三层结构")
        except Exception as exc:
            logger.warning("[body_monitor] 迁移旧词库失败: %s", exc)

    def _state_ui_settings(self, raw: dict | None = None) -> dict:
        if raw is None:
            raw = self._settings_raw()

        def number(key, default, lo, hi):
            try:
                value = float(raw.get(key, default))
            except Exception:
                value = float(default)
            return max(lo, min(hi, value))

        baseline = round(number("state_baseline_temp", 36.5, 30, 45), 1)
        min_d = int(number("state_min_duration", 5, 1, 31536000))
        max_d = int(number("state_max_duration", 604800, 1, 31536000))
        if min_d > max_d:
            min_d, max_d = max_d, min_d
        max_notes = int(number("state_max_notes", 6, 1, 20))
        return {
            "state_baseline_temp": baseline,
            "state_min_duration": min_d,
            "state_max_duration": max_d,
            "state_max_notes": max_notes,
        }

    def _allowed_export_roots(self):
        roots = []
        try:
            roots.append(Path.home().resolve())
        except Exception:
            pass
        try:
            roots.append(DATA_DIR.resolve())
        except Exception:
            pass
        return roots

    def _safe_export_dir(self, raw: str = ""):
        """只允许导出到用户目录或插件数据目录，避免任意路径写文件。"""
        text = str(raw or "").strip()
        if not text:
            text = str(DATA_DIR / "exports")
        try:
            target = Path(os.path.expandvars(os.path.expanduser(text)))
            if not target.is_absolute():
                target = DATA_DIR / target
            target = target.resolve()
        except Exception:
            return None
        plugin = PLUGIN_DIR.resolve()
        if target == plugin or plugin in target.parents:
            return None
        if target.exists() and not target.is_dir():
            return None
        if not any(
            target == root or root in target.parents
            for root in self._allowed_export_roots()
        ):
            return None
        return target

    def _export_dir(self) -> str:
        raw = self._settings_raw()
        value = raw.get("export_dir")
        safe = self._safe_export_dir(value if isinstance(value, str) else "")
        return str(safe or (DATA_DIR / "exports"))

    async def get_settings(self):
        state = self._load_usage_state()
        selected = state["selected"]
        data = {
            "usage_tree": state["tree"],
            "usage_selected": selected,
            # 兼容旧调用方；新 UI 只用上面两个字段
            "noun": selected.get("n", ""),
            "adj": selected.get("a", ""),
            "verb": selected.get("v", ""),
            "combo_window": self._combo_window(),
            "export_dir": self._export_dir(),
        }
        data.update(self._state_ui_settings())
        return {"status": "ok", "data": data}

    async def save_settings(self):
        from astrbot.api.web import request as req

        payload = await req.json(default={}) or {}
        payload = payload if isinstance(payload, dict) else {}
        raw = self._settings_raw()
        if "usage_tree" in payload or "usage_selected" in payload:
            current = self._load_usage_state()
            tree = self._sanitize_usage_tree(payload.get("usage_tree"))
            if tree is None:
                tree = current["tree"]
            selected = self._select_usage(tree, payload.get("usage_selected") or {})
            raw["usage_tree"] = tree
            raw["usage_selected"] = selected
            for key in ("noun", "adj", "verb", "noun_options", "adj_options", "verb_options"):
                raw.pop(key, None)
        for key in (
            "state_baseline_temp",
            "state_min_duration",
            "state_max_duration",
            "state_max_notes",
        ):
            if key not in payload:
                continue
            try:
                raw[key] = float(payload[key])
            except Exception:
                continue
        ui_settings = self._state_ui_settings(raw)
        raw.update(ui_settings)
        self._save_settings_raw(raw)

        combo_value = payload.get("combo_window")
        if isinstance(combo_value, (int, float)) and not isinstance(combo_value, bool):
            value = max(0.0, min(10.0, float(combo_value)))
            self.config["combo_window"] = value
            if await self._save_config():
                logger.info("[body_monitor] 连击间隔已更新: %s 秒", value)

        state = self._load_usage_state()
        selected = state["selected"]
        data = {
            "usage_tree": state["tree"],
            "usage_selected": selected,
            "noun": selected.get("n", ""),
            "adj": selected.get("a", ""),
            "verb": selected.get("v", ""),
            "combo_window": self._combo_window(),
            "export_dir": self._export_dir(),
        }
        data.update(ui_settings)
        logger.info(
            "[body_monitor] 词库/设置已更新: 名词=%s 形容词=%s 动词=%s",
            selected.get("n", ""),
            selected.get("a", ""),
            selected.get("v", ""),
        )
        return {"status": "ok", "data": data}

    def _log(self, part: str, status: str, text: str):
        self.logs.append({
            "ts": time.time(),
            "status": status,
            "part": part,
            "text": text,
        })
        if len(self.logs) > MAX_LOG:
            self.logs = self.logs[-MAX_LOG:]

    async def get_log(self):
        items = list(reversed(self.logs))[:MAX_LOG]
        return {"status": "ok", "data": {"items": items}}

    async def clear_log(self):
        self.logs = []
        return {"status": "ok", "data": {}}

    def _queue_pending(self, session: str, text: str):
        self.pending.append((session, text))
        if len(self.pending) > MAX_PENDING:
            self.pending = self.pending[-MAX_PENDING:]

    def _find_platform(self, platform_id: str):
        for p in getattr(self.context.platform_manager, "get_insts", list)() or []:
            try:
                if p.meta().id == platform_id:
                    return p
            except Exception:
                continue
        return None

    def _bot_of(self, inst):
        for name in ("get_client", "bot", "client"):
            bot = getattr(inst, name, None)
            if bot is None:
                continue
            if callable(bot):
                try:
                    bot = bot()
                except Exception:
                    bot = None
            if bot is not None:
                return bot
        return None

    async def _get_username(self, session: str) -> str:
        """取聊天对象的名字，取不到就用「对方」，不阻塞回复。"""
        parts = session.split(":")
        if len(parts) < 3:
            return "对方"
        platform_id, mtype, sid = parts[0], parts[1], ":".join(parts[2:])
        inst = self._find_platform(platform_id)
        if inst is None:
            return "对方"
        bot = self._bot_of(inst)
        uid = str(self._session_users.get(session) or self.last_user or sid)
        if bot is not None and uid.isdigit():
            info = {}
            if "Group" in mtype and sid.isdigit():
                try:
                    info = await bot.get_group_member_info(group_id=int(sid), user_id=int(uid)) or {}
                except Exception:
                    info = {}
            if not info:
                try:
                    info = await bot.get_stranger_info(user_id=int(uid)) or {}
                except Exception:
                    info = {}
            name = info.get("card") or info.get("nickname") or info.get("nick") or ""
            if name:
                return str(name)[:24]
        return "对方"

    async def _get_conversation_obj(self, session: str):
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return None
        try:
            cid = await cm.get_curr_conversation_id(session)
            if not cid:
                cid = await cm.new_conversation(session, session.split(":")[0])
            return await cm.get_conversation(session, cid)
        except Exception as e:
            logger.warning("[body_monitor] 获取会话对象失败: %s", e)
            return None

    async def _run_via_pipeline(
        self,
        session: str,
        prompt: str,
        username: str = "",
        state_trigger: dict | None = None,
    ) -> bool:
        """把这次互动伪装成一条真实消息事件塞进事件队列，走和正常消息一样的完整流水线。"""
        try:
            from astrbot.api.platform import MessageMember
            from astrbot.core.platform.astrbot_message import AstrBotMessage
            from astrbot.core.platform.message_type import MessageType
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except Exception as e:
            logger.warning("[body_monitor] 导入流水线依赖失败: %s", e)
            return False

        queue = getattr(self.context, "_event_queue", None)
        if queue is None or not hasattr(queue, "put_nowait"):
            return False

        parts = session.split(":")
        if len(parts) < 3:
            return False
        platform_id, mtype, sid = parts[0], parts[1], ":".join(parts[2:])
        inst = self._find_platform(platform_id)
        if inst is None:
            logger.warning("[body_monitor] 找不到平台实例: %s", platform_id)
            return False
        meta = inst.meta()
        # 事件注入这条捷径只对 aiocqhttp 安全：其它平台的事件类、发送接口都不一样，
        # 硬塞一个 aiocqhttp 事件会导致回复发不出去，所以统一回退到直接生成。
        platform_name = str(getattr(meta, "name", "") or "")
        if platform_name != "aiocqhttp":
            logger.info(
                "[body_monitor] 平台 %s 不支持事件注入，回退直接生成",
                platform_name or platform_id,
            )
            return False
        bot = self._bot_of(inst)
        sender_id = str(self._session_users.get(session) or self.last_user or sid)
        try:
            msg = AstrBotMessage()
            msg.type = MessageType.GROUP_MESSAGE if "Group" in mtype else MessageType.FRIEND_MESSAGE
            msg.self_id = str(getattr(inst, "self_id", "") or getattr(meta, "id", "") or "")
            msg.session_id = sid
            msg.message_id = "touch-" + str(int(time.time() * 1000))
            msg.group_id = sid if msg.type == MessageType.GROUP_MESSAGE else ""
            msg.sender = MessageMember(user_id=sender_id, nickname=username or sender_id)
            msg.message = []
            msg.message_str = ""
            msg.raw_message = {}
            event = AiocqhttpMessageEvent("", msg, meta, sid, bot)
            event.set_extra("_body_monitor_synthetic", True)
            if state_trigger:
                event.set_extra("_body_monitor_state", state_trigger)
            conv = await self._get_conversation_obj(session)
            event.set_extra(
                "provider_request",
                event.request_llm(
                    prompt=prompt,
                    conversation=conv,
                ),
            )
            queue.put_nowait(event)
            return True
        except Exception as e:
            logger.warning("[body_monitor] 事件入队失败: %s", e)
            return False

    async def _get_contexts(self, session: str):
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return []
        try:
            cid = await cm.get_curr_conversation_id(session)
            if not cid:
                return []
            conv = await cm.get_conversation(session, cid)
            if conv is None:
                return []
            hist = getattr(conv, "history", None) or []
            if isinstance(hist, str):
                hist = json.loads(hist)
            if not isinstance(hist, list):
                return []
        except Exception as e:
            logger.warning("[body_monitor] 读取会话历史失败: %s", e)
            return []
        out = []
        for m in hist:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict):
                        parts.append(str(c.get("text", "") or ""))
                    elif isinstance(c, str):
                        parts.append(c)
                content = "".join(parts)
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                out.append({"role": role, "content": content})
        return out[-20:]

    def _pick_provider(self):
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _find_provider_by_name(self, name: str):
        if not name or name in ("默认（跟随会话）", "跟随回复模型"):
            return None
        try:
            p = self.context.get_provider_by_id(name)
            if p is not None:
                return p
        except Exception:
            pass
        try:
            for p in self.context.get_all_providers():
                cfg = getattr(p, "provider_config", {}) or {}
                if name in (cfg.get("id"), cfg.get("type")):
                    return p
        except Exception:
            pass
        return None

    def _pick_state_provider(self):
        name = str(self.config.get("state_provider", "") or "").strip()
        if name and name != "跟随回复模型":
            provider = self._find_provider_by_name(name)
            if provider is not None:
                return provider
            logger.warning("[body_monitor] 状态更新模型 %s 未找到，回退到回复模型", name)
        return self._pick_provider()

    async def _get_persona(self, session: str):
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        p = getattr(pm, "selected_default_persona", None)
        if p is not None:
            sp = getattr(p, "system_prompt", "") or ""
            if sp:
                return sp
        p3 = getattr(pm, "selected_default_persona_v3", None)
        if isinstance(p3, dict):
            sp = p3.get("prompt", "") or ""
            if sp:
                return sp
        fn = getattr(pm, "get_default_persona_v3", None)
        if fn is not None:
            try:
                res = fn()
                if inspect.isawaitable(res):
                    res = await res
                if isinstance(res, dict):
                    return res.get("prompt", "") or ""
                return getattr(res, "system_prompt", "") or ""
            except Exception:
                pass
        return ""

    def _load_session(self):
        try:
            if SESSION_FILE.exists():
                d = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                self.last_session = d.get("umo") or self.last_session
                self.last_user = d.get("user") or self.last_user
        except Exception:
            logger.warning("[body_monitor] 读取通道失败")

    def _save_session(self):
        _ensure_data_dir()
        try:
            SESSION_FILE.write_text(
                json.dumps({"umo": self.last_session, "user": self.last_user}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def get_session(self):
        umo = str(self.config.get("umo", "") or "").strip() or (self.last_session or "")
        return {"status": "ok", "data": {"umo": umo, "user": self.last_user or ""}}

    async def set_session(self):
        from astrbot.api.web import request as req

        body = await req.json(default={}) or {}
        umo = decode_param((body or {}).get("umo") or "").strip()
        if not umo or umo.count(":") < 2:
            return {"status": "error", "message": "格式是 平台:消息类型:会话id，冒号不能少"}
        self.last_session = umo
        self._save_session()
        await self._apply_config_session(umo)
        return {"status": "ok", "data": {"umo": umo}}

    async def put_session(self, umo: str = ""):
        umo = decode_param(umo).strip()
        if not umo or umo.count(":") < 2:
            return {"status": "error", "message": "格式是 平台:消息类型:会话id，冒号不能少"}
        self.last_session = umo
        self._save_session()
        await self._apply_config_session(umo)
        return {"status": "ok", "data": {"umo": umo}}

    async def _apply_config_session(self, umo: str):
        """把会话写进插件配置，保证「配置里的 UMO」和页面显示的会话是同一个。"""
        try:
            self.config["umo"] = umo
            await self._save_config()
        except Exception as exc:
            logger.warning("[body_monitor] 保存会话配置失败: %s", exc)

    async def _save_config(self) -> bool:
        try:
            saver = getattr(self.config, "save_config_async", None)
            if saver is not None:
                await saver()
            else:
                saver = getattr(self.config, "save_config", None)
                if saver is not None:
                    saver()
            return True
        except Exception as exc:
            logger.warning("[body_monitor] 保存插件配置失败: %s", exc)
            return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event):
        # 只记用户真实发来的消息；插件自己塞进流水线的触碰事件不要覆盖会话
        if not event.get_extra("_body_monitor_synthetic"):
            session = event.unified_msg_origin
            self.last_session = session
            self.last_user = event.get_sender_id()
            if self.last_user:
                self._session_users.pop(session, None)
                self._session_users[session] = self.last_user
                if len(self._session_users) > 200:
                    # 只保留最近活跃的一批会话，避免长时间运行后无限增长
                    for key in list(self._session_users)[:100]:
                        self._session_users.pop(key, None)
            self._save_session()
        if self.pending:
            items, self.pending = self.pending, []
            for s, txt in items:
                try:
                    sent = await self.context.send_message(s, MessageChain().message(txt))
                    if sent is False:
                        logger.warning("[body_monitor] 补发失败：找不到平台 %s", s)
                except Exception:
                    logger.warning("[body_monitor] 补发失败")
        return
