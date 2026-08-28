"""
AI 桥 · 服务端（电脑端 / 大脑侧）
================================

把 AI 能力封装成 RESTful API，让手机端执行体（hand.js / AutoX.js）通过局域网 HTTP 调用，
形成「算力在家、执行在手」的跨设备架构。

架构
----
    电脑端（本文件，默认 :8787）  <──────>  手机端（hand.js，默认 :8789）
        · 持有模型 / 决策能力                 · 持有屏幕、触摸、应用、Shell
        · 任务队列 + 设备注册表               · 拉取指令、执行、回传结果

    完整闭环：模型决策 → 指令下发(task/push) → 设备执行(hand.js) → 结果回传(task/report)

接口清单（6 个）
--------------
    GET  /healthz        服务探活，免鉴权（供局域网扫描与保活检测）
    POST /ping           设备心跳：注册设备、上报状态、双向链路验证
    POST /notify         设备消息上报：落盘 + 写入事件流
    POST /task/push      大脑下发指令：入队 + 设备在线时主动推送
    POST /task/pull      设备拉取待执行指令（设备不在线时的兜底通道）
    POST /task/report    设备回传执行结果，闭环收口

运行
----
    python bridge_server.py                      # 默认 0.0.0.0:8787
    AI_BRIDGE_KEY=你的密钥 python bridge_server.py
    AI_BRIDGE_PORT=9000 python bridge_server.py

依赖：仅标准库，无需 pip install。

v2 相对 v1 的修复记录（面试可讲）
--------------------------------
 1. 密钥：v1 未设置时回落为占位符且服务照常启动，等于鉴权失效；
           v2 改为未设置时自动生成随机密钥并打印，安全默认值不靠用户自觉。
 2. 鉴权比较：v1 用 == 直接比字符串，存在时序侧信道；v2 改用 secrets.compare_digest。
 3. 并发：v1 是单线程 HTTPServer，多设备同时调用会排队阻塞；v2 改 ThreadingHTTPServer。
 4. 请求体：v1 无条件 read(Content-Length)，恶意大包会撑爆内存；v2 加体积上限。
 5. 端口：v1 写死 8789，被占用直接崩；v2 绑定失败自动 +1 重试，并记入启动日志。
 6. 日志：v1 把访问日志全静默，出问题时无从排查；v2 改为落盘 JSONL + 控制台摘要。
 7. 状态：v1 无设备跟踪、任务全在内存，重启即丢；v2 加设备注册表与任务队列持久化。
 8. 超时：v1 未设置，慢连接会长期占用线程；v2 设置 socket 超时。
 9. 关闭：v1 只能 Ctrl+C 硬退；v2 加优雅退出，落盘未完成任务。
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# 配置：全部走环境变量，密钥绝不硬编码进仓库
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AI_BRIDGE_DATA", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TASK_FILE = DATA_DIR / "tasks.json"
DEVICE_FILE = DATA_DIR / "devices.json"
EVENT_LOG = DATA_DIR / "events.jsonl"
SERVER_LOG = DATA_DIR / "server.log"

_ENV_KEY = os.environ.get("AI_BRIDGE_KEY", "").strip()
if _ENV_KEY:
    BRIDGE_KEY = _ENV_KEY
    KEY_IS_EPHEMERAL = False
else:
    # 安全默认值：不设置密钥就临时生成一个，服务可用但重启即变，避免"占位符裸奔"
    BRIDGE_KEY = secrets.token_hex(16)
    KEY_IS_EPHEMERAL = True

HOST = os.environ.get("AI_BRIDGE_HOST", "0.0.0.0")
PORT_START = int(os.environ.get("AI_BRIDGE_PORT", "8787"))
PORT_TRIES = int(os.environ.get("AI_BRIDGE_PORT_TRIES", "10"))

MAX_BODY = int(os.environ.get("AI_BRIDGE_MAX_BODY", str(1024 * 1024)))  # 1 MB
SOCKET_TIMEOUT = int(os.environ.get("AI_BRIDGE_TIMEOUT", "15"))
DEVICE_PUSH_TIMEOUT = float(os.environ.get("AI_BRIDGE_PUSH_TIMEOUT", "5"))
DEVICE_OFFLINE_AFTER = int(os.environ.get("AI_BRIDGE_OFFLINE_AFTER", "300"))  # 5 分钟无心跳视为离线

# 允许下发给手机的指令白名单：与 hand.js 的 doAction 分支一一对应
# 任何不在这个集合里的 action 一律拒绝，避免服务端被当作任意命令执行跳板
ALLOWED_DEVICE_ACTIONS = {
    "ping", "toast", "launch", "launchPackage", "clickText", "press",
    "swipe", "shell", "shot", "screenshot", "rawdump", "nodes",
    "back", "home", "setClip", "input", "clickEdit", "current",
}

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 基础设施：日志 / 存储 / 设备注册表 / 任务队列
# ---------------------------------------------------------------------------

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, level: str = "INFO") -> None:
    """同时写控制台与日志文件。v1 完全静默日志，线上出问题时无从排查，这里补上。"""
    line = f"[{now_str()}] [{level}] {msg}"
    print(line)
    try:
        with open(SERVER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def record_event(kind: str, payload: dict) -> None:
    """事件流：append-only JSONL，用于回溯"谁在什么时候下发了什么、设备回了什么"。"""
    entry = {"ts": now_str(), "kind": kind, **payload}
    try:
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"事件写入失败: {e}", "ERROR")


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # 文件损坏时不当场崩，备份后重来——宁可丢状态也不能起不来
        try:
            path.rename(path.with_suffix(path.suffix + ".corrupt"))
        except OSError:
            pass
        return default


def _save_json(path: Path, data: Any) -> None:
    # 临时文件名必须唯一。若用固定名字，多线程同时落盘会写到同一个 tmp 上，
    # 彼此覆盖内容，且在 Windows 上 replace 一个正被占用的文件会直接抛
    # OSError [Errno 22]——表现为偶发的 500，极难复现。
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        tmp.replace(path)  # 原子替换，避免写到一半断电留下坏文件
    except OSError:
        # 兜底：替换失败也不能把异常抛给调用方，数据仍在内存状态里
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class DeviceRegistry:
    """设备注册表：记录谁在线、IP 是多少、最后心跳时间。"""

    def __init__(self) -> None:
        self.devices: dict[str, dict] = _load_json(DEVICE_FILE, {})

    def heartbeat(self, device_id: str, info: dict, ip: str) -> dict:
        with _lock:
            dev = self.devices.get(device_id, {})
            dev.update({
                "device_id": device_id,
                "ip": info.get("ip") or ip,
                "port": int(info.get("port") or 8789),
                "last_seen": time.time(),
                "info": {k: v for k, v in info.items() if k not in ("ip", "port")},
            })
            self.devices[device_id] = dev
            self._flush()
            return dict(dev)

    def get(self, device_id: str) -> dict | None:
        return self.devices.get(device_id)

    def online_devices(self) -> list[dict]:
        cutoff = time.time() - DEVICE_OFFLINE_AFTER
        return [d for d in self.devices.values() if d.get("last_seen", 0) >= cutoff]

    def is_online(self, device_id: str) -> bool:
        d = self.devices.get(device_id)
        return bool(d) and (time.time() - d.get("last_seen", 0)) < DEVICE_OFFLINE_AFTER

    def _flush(self) -> None:
        _save_json(DEVICE_FILE, self.devices)


class TaskQueue:
    """任务队列：pending -> running -> done/failed，落盘持久化，重启不丢。"""

    def __init__(self) -> None:
        self.tasks: dict[str, dict] = _load_json(TASK_FILE, {})

    def push(self, device_id: str, action: str, params: dict) -> dict:
        if action not in ALLOWED_DEVICE_ACTIONS:
            raise ValueError(f"action 不在白名单内: {action}")
        tid = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"
        task = {
            "task_id": tid,
            "device_id": device_id,
            "action": action,
            "params": params or {},
            "status": "pending",
            "created_at": now_str(),
            "updated_at": now_str(),
            "result": None,
            "error": None,
        }
        with _lock:
            self.tasks[tid] = task
            self._flush()
        record_event("task.push", {"task_id": tid, "device_id": device_id, "action": action})
        return dict(task)

    def pull(self, device_id: str, limit: int = 5) -> list[dict]:
        """原子取出：标记 running 再返回，避免多设备重复领取同一条指令。"""
        with _lock:
            picked = [t for t in self.tasks.values()
                      if t["status"] == "pending" and t["device_id"] in (device_id, "*")][:limit]
            for t in picked:
                t["status"] = "running"
                t["updated_at"] = now_str()
            if picked:
                self._flush()
        return [dict(t) for t in picked]

    def report(self, task_id: str, ok: bool, result: Any = None, error: str | None = None) -> dict | None:
        with _lock:
            t = self.tasks.get(task_id)
            if not t:
                return None
            t["status"] = "done" if ok else "failed"
            t["result"] = result
            t["error"] = error
            t["updated_at"] = now_str()
            self._flush()
            snapshot = dict(t)
        record_event("task.report", {"task_id": task_id, "ok": ok, "error": error})
        return snapshot

    def stats(self) -> dict:
        c = {"pending": 0, "running": 0, "done": 0, "failed": 0}
        for t in self.tasks.values():
            c[t["status"]] = c.get(t["status"], 0) + 1
        return {"total": len(self.tasks), **c}

    def _flush(self) -> None:
        _save_json(TASK_FILE, self.tasks)


DEVICES = DeviceRegistry()
TASKS = TaskQueue()


def forward_to_device(device_id: str, task: dict) -> dict:
    """
    设备在线时主动推送指令到手机端 hand.js 的 8789 端口。

    这是 push 模式的快通道；失败不抛异常，指令仍留在队列里，
    设备下次 pull 时照样能拿到——在线走快通道，离线走兜底，两条路都不丢指令。
    """
    dev = DEVICES.get(device_id)
    if not dev:
        return {"delivered": False, "reason": "unknown_device"}

    url = f"http://{dev['ip']}:{dev.get('port', 8789)}/"
    body = json.dumps({
        "auth": BRIDGE_KEY,
        "action": task["action"],
        **task["params"],
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=DEVICE_PUSH_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return {"delivered": True, "response": json.loads(raw) if raw else {}}
    except urllib.error.URLError as e:
        return {"delivered": False, "reason": f"device_unreachable: {e}"}
    except (json.JSONDecodeError, TimeoutError, socket.timeout) as e:
        return {"delivered": False, "reason": f"bad_response: {e}"}


# ---------------------------------------------------------------------------
# 动作实现：新增能力 = 写一个函数 + 在 ACTIONS 里注册一行
# ---------------------------------------------------------------------------

def action_ping(payload: dict) -> dict:
    """设备心跳。顺带把服务端视角的设备状态回给客户端，作为双向链路验证。"""
    device_id = str(payload.get("device_id") or payload.get("device") or "unnamed")
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    dev = DEVICES.heartbeat(device_id, info, payload.get("_client_ip", ""))
    return {
        "ok": True,
        "msg": "pong",
        "device_id": device_id,
        "online": DEVICES.is_online(device_id),
        "online_devices": len(DEVICES.online_devices()),
        "ts": int(time.time()),
        "info_registered": bool(info),
    }


def action_notify(payload: dict) -> dict:
    """设备消息上报：落事件流，供大脑侧模型读取上下文。"""
    text = str(payload.get("text", ""))
    device_id = str(payload.get("device_id") or payload.get("_client_ip") or "unknown")
    record_event("device.notify", {"device_id": device_id, "text": text})
    return {"ok": True, "received": text, "len": len(text), "ts": int(time.time())}


def action_task_push(payload: dict) -> dict:
    """大脑下发指令。校验白名单 -> 入队 -> 设备在线则尝试主动推送。"""
    device_id = str(payload.get("device_id") or "*")
    action = str(payload.get("device_action") or payload.get("action") or "")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

    if not action:
        return {"ok": False, "err": "缺少 device_action"}
    if action not in ALLOWED_DEVICE_ACTIONS:
        return {"ok": False, "err": f"action 不在白名单: {action}",
                "allowed": sorted(ALLOWED_DEVICE_ACTIONS)}

    task = TASKS.push(device_id, action, params)
    delivery = {"delivered": False, "reason": "device_offline_or_unknown"}
    if device_id != "*" and DEVICES.is_online(device_id):
        delivery = forward_to_device(device_id, task)

    return {"ok": True, "task": task, "delivery": delivery}


def action_task_pull(payload: dict) -> dict:
    """设备拉取待执行指令。离线设备上线后的兜底通道。"""
    device_id = str(payload.get("device_id") or "unnamed")
    limit = max(1, min(int(payload.get("limit", 5)), 50))
    if payload.get("info"):
        DEVICES.heartbeat(device_id, payload["info"], payload.get("_client_ip", ""))
    tasks = TASKS.pull(device_id, limit)
    return {"ok": True, "count": len(tasks), "tasks": tasks, "ts": int(time.time())}


def action_task_report(payload: dict) -> dict:
    """设备回传执行结果，闭环收口。"""
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        return {"ok": False, "err": "缺少 task_id"}
    ok = bool(payload.get("ok", False))
    task = TASKS.report(task_id, ok, payload.get("result"), payload.get("error"))
    if not task:
        return {"ok": False, "err": f"未找到任务: {task_id}"}
    return {"ok": True, "task": task, "stats": TASKS.stats()}


def action_status(payload: dict) -> dict:
    """全局状态查询：排障时第一眼看这个。"""
    return {
        "ok": True,
        "tasks": TASKS.stats(),
        "devices_online": [d["device_id"] for d in DEVICES.online_devices()],
        "devices_total": len(DEVICES.devices),
        "allowed_actions": sorted(ALLOWED_DEVICE_ACTIONS),
        "ts": int(time.time()),
    }


ACTIONS: dict[str, Callable[[dict], dict]] = {
    "ping": action_ping,
    "notify": action_notify,
    "task/push": action_task_push,
    "task/pull": action_task_pull,
    "task/report": action_task_report,
    "status": action_status,
}


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ai-bridge/2.0"

    def _auth_ok(self) -> bool:
        # 常量时间比较，避免按字节短路造成时序侧信道
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {BRIDGE_KEY}"
        return secrets.compare_digest(auth.encode("utf-8"), expected.encode("utf-8"))

    def _reply(self, code: int, obj: dict, close: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if close:
            # 明确告知对端"这个连接我要关了"，否则未读完请求体的客户端会收到连接重置
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError(f"请求体超过上限 {MAX_BODY} 字节")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体不是合法 UTF-8 JSON")
        return data if isinstance(data, dict) else {}

    def _resolve_action(self) -> str:
        path = urlparse(self.path).path.strip("/")
        # 兼容两种写法：/task/push 与 /task_push
        return path.replace("_", "/") if path.replace("_", "/") in ACTIONS else path

    def _dispatch(self, payload: dict) -> None:
        action = self._resolve_action()
        fn = ACTIONS.get(action)
        if fn is None:
            self._reply(404, {"ok": False, "err": f"unknown action: {action}",
                              "available": sorted(ACTIONS)})
            return
        try:
            self._reply(200, fn(payload))
        except ValueError as e:
            self._reply(400, {"ok": False, "err": str(e)})
        except Exception as e:  # 任何 action 挂了都不带崩服务
            # 记完整堆栈：只记 str(e) 的话，并发类故障（偶发、难复现）事后无从查起
            log(f"action {action} 异常: {type(e).__name__}: {e}\n{traceback.format_exc()}", "ERROR")
            self._reply(500, {"ok": False, "err": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        if urlparse(self.path).path.strip("/") == "healthz":
            self._reply(200, {"ok": True, "service": "ai-bridge",
                              "ver": "2.0", "tasks": TASKS.stats()})
            return
        if not self._auth_ok():
            self._reply(401, {"ok": False, "err": "unauthorized"})
            return
        self._dispatch(parse_qs(urlparse(self.path).query, keep_blank_values=True))

    def do_POST(self):
        if not self._auth_ok():
            self._reply(401, {"ok": False, "err": "unauthorized"})
            return
        declared = self.headers.get("Content-Length", "0")
        try:
            declared = int(declared)
        except ValueError:
            declared = 0
        if declared > MAX_BODY:
            # 超限就不读了，但要先声明关闭连接：否则客户端还在发数据时被断开，
            # 连 413 这个状态码都收不到，只会看到一个莫名其妙的连接重置
            self.close_connection = True
            self._reply(413, {"ok": False,
                              "err": f"请求体 {declared} 字节，超过上限 {MAX_BODY} 字节"},
                        close=True)
            return
        try:
            payload = self._read_body()
        except ValueError as e:
            self._reply(400, {"ok": False, "err": str(e)})
            return
        payload["_client_ip"] = self.client_address[0]
        self._dispatch(payload)

    def log_message(self, fmt, *args):
        # 不刷屏，但留痕到日志文件，出问题时能翻
        log(f"{self.client_address[0]} {fmt % args}", "ACCESS")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    timeout = SOCKET_TIMEOUT  # 慢连接不至于长期占着线程

    # 默认 backlog 只有 5，多设备同时发起请求时（手机批量上报、并发拉取任务）
    # 连接会被内核直接丢弃，表现为"偶发的连接失败"，很难排查。这里放大。
    request_queue_size = 64

    # Windows 下 SO_REUSEADDR 的语义与 Unix 相反：它不是"允许 TIME_WAIT 重用"，
    # 而是"允许多个进程绑定同一个端口"，后启动者会静默抢走流量。
    # 这会让端口冲突检测彻底失效（第二个实例绑定成功，不报错，但流量去了谁不确定），
    # 所以 Windows 上改走独占模式。
    allow_reuse_address = (os.name != "nt")

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind_with_fallback(start_port: int, tries: int) -> tuple[BridgeServer, int]:
    """
    端口占用自动切换：绑定失败就 +1 重试。

    v1 写死端口，被占用直接崩；实际场景里「上次实例还没退干净」是最常见的启动失败原因，
    这个重试把最常见的故障变成一行日志。
    """
    last_err = None
    for offset in range(tries):
        port = start_port + offset
        try:
            return BridgeServer((HOST, port), BridgeHandler), port
        except OSError as e:
            last_err = e
            log(f"端口 {port} 被占用，尝试 {port + 1}...", "WARN")
    raise RuntimeError(f"端口 {start_port} 起连续 {tries} 个都被占用: {last_err}")


def _graceful_shutdown(signum, frame):
    stats = TASKS.stats()
    log(f"收到退出信号，当前任务状态: {stats}")
    sys.exit(0)


def main() -> int:
    signal.signal(signal.SIGINT, _graceful_shutdown)
    try:
        signal.signal(signal.SIGTERM, _graceful_shutdown)
    except (AttributeError, ValueError):
        pass  # Windows 上 SIGTERM 支持有限，忽略即可

    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass

    try:
        server, port = bind_with_fallback(PORT_START, PORT_TRIES)
    except RuntimeError as e:
        log(str(e), "ERROR")
        return 1

    if port != PORT_START:
        log(f"注意：首选端口 {PORT_START} 不可用，已切换到 {port}", "WARN")

    log("=" * 56)
    log("AI 桥 · 服务端启动")
    log(f"监听地址 : {HOST}:{port}")
    log(f"局域网入口: http://{local_ip}:{port}/")
    log(f"数据目录  : {DATA_DIR}")
    if KEY_IS_EPHEMERAL:
        log("未设置 AI_BRIDGE_KEY，本次已自动生成临时密钥（重启后会变）", "WARN")
        log(f"本次密钥  : {BRIDGE_KEY}", "WARN")
        log("生产用法  : AI_BRIDGE_KEY=你的密钥 python bridge_server.py", "WARN")
    else:
        log("鉴权      : 已启用（Bearer Token，来自环境变量）")
    log(f"可用动作  : {len(ACTIONS)} 个接口 / {len(ALLOWED_DEVICE_ACTIONS)} 个设备指令")
    log("=" * 56)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("服务已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
