"""
AI 桥 · 冒烟测试
================
覆盖接口可用性、鉴权、动作白名单、任务闭环、中文、并发、端口自动切换。

运行：
    python tests/smoke_test.py

说明：
    · 端口随机（20000-30000）。用固定端口时，上一轮没退干净的实例会占着端口，
      新实例静默切到 +1，测试却仍连原端口——会打到旧进程跑旧代码，
      表现为「修好的 bug 又复现了」。
    · 数据目录用临时目录，不污染项目。
"""

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SRC = Path(__file__).resolve().parent.parent
PY = sys.executable
KEY = "test-key-" + os.urandom(4).hex()
PORT = random.randint(20000, 30000)

# 与 README、简历上写的数字保持一致。
# 改动断言数量后若忘记同步文档，测试会在结尾报警并返回失败，
# 避免「文档写 20、实际跑 19」这种一点开就露馅的不一致。
EXPECTED_TOTAL = 20

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def call(path, data=None, key=KEY, port=PORT, raw_body=None, method=None):
    url = f"http://127.0.0.1:{port}/{path}"
    if raw_body is not None:
        body = raw_body
    else:
        body = json.dumps(data or {}, ensure_ascii=False).encode("utf-8") if data is not None else b""
    req = urllib.request.Request(
        url, data=body,
        method=method or ("POST" if data is not None or raw_body is not None else "GET"),
    )
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except OSError:
            raw = ""  # 服务端按协议关闭连接时 body 可能读不到，状态码才是断言对象
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def start_server(port, data_dir, tries=3):
    env = {**os.environ,
           "AI_BRIDGE_KEY": KEY,
           "AI_BRIDGE_DATA": data_dir,
           "AI_BRIDGE_PORT": str(port),
           "AI_BRIDGE_PORT_TRIES": str(tries),
           "PYTHONIOENCODING": "utf-8"}
    return subprocess.Popen([PY, str(SRC / "bridge_server.py")], cwd=str(SRC), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")


def wait_up(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            call("healthz", port=port)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    global passed, failed
    data1 = tempfile.mkdtemp(prefix="aibridge-t1-")
    proc1 = start_server(PORT, data1)

    print("== 启动 ==")
    up = wait_up(PORT)
    check("服务在 15 秒内启动", up)
    if not up:
        print(proc1.stdout.read() if proc1.stdout else "")
        proc1.kill()
        return 1

    print("\n== 接口与鉴权 ==")
    code, r = call("healthz", key=None)
    check("GET /healthz 免鉴权可访问", code == 200 and r.get("ok") is True, f"{code} {r}")
    code, r = call("status", {}, key="wrong-key")
    check("错误密钥访问业务接口被拒 401", code == 401, f"{code} {r}")
    code, r = call("status", {}, key=None)
    check("无 Authorization 头被拒 401", code == 401, f"{code} {r}")
    code, r = call("no_such_action", {})
    check("未知接口返回 404 并列出可用动作", code == 404 and isinstance(r.get("available"), list), f"{code} {r}")
    code, r = call("ping", {"device_id": "test-phone",
                            "info": {"brand": "Test", "model": "M1", "ip": "127.0.0.1", "port": 18789}})
    check("POST /ping 设备注册成功", code == 200 and r.get("ok") is True, f"{code} {r}")
    check("ping 返回在线状态", r.get("online") is True, str(r))

    print("\n== 动作白名单 ==")
    code, r = call("task/push", {"device_id": "test-phone", "device_action": "__rm_rf__"})
    check("非法 action 被白名单拦截", r.get("ok") is False and "白名单" in str(r.get("err", "")), f"{code} {r}")
    code, r = call("task/push", {"device_id": "test-phone", "device_action": "toast",
                                 "params": {"msg": "你好，中文测试"}})
    check("合法 action (toast) 入队成功", r.get("ok") is True, f"{code} {r}")
    tid = (r.get("task") or {}).get("task_id")
    check("任务分配了 task_id", bool(tid), str(r))

    print("\n== 任务闭环 ==")
    code, r = call("task/pull", {"device_id": "test-phone", "limit": 5})
    check("pull 取到待执行指令", r.get("count", 0) >= 1, f"{code} {r}")
    check("pull 后状态转 running", (r.get("tasks") or [{}])[0].get("status") == "running", str(r))
    code, r = call("task/report", {"task_id": tid, "ok": True, "result": {"toast_shown": True}})
    check("report 回传结果成功", r.get("ok") is True, f"{code} {r}")
    check("任务状态转 done", (r.get("task") or {}).get("status") == "done", str(r))
    code, r = call("task/report", {"task_id": "not-exist-xxx", "ok": True})
    check("未知 task_id 返回错误而非崩溃", r.get("ok") is False, f"{code} {r}")

    print("\n== 中文与边界 ==")
    code, r = call("notify", {"device_id": "test-phone", "text": "中文消息测试：端口占用已处理"})
    check("notify 中文消息上报成功", r.get("ok") is True and r.get("len", 0) > 0, f"{code} {r}")
    code, r = call("notify", raw_body=("x" * (2 * 1024 * 1024)).encode("utf-8"), method="POST")
    check("超大请求体被拦截 413", code == 413, f"{code} {r}")

    print("\n== 并发 ==")
    errors = []

    def one(i):
        try:
            c, _ = call("notify", {"device_id": f"dev-{i}", "text": f"并发消息 {i}"})
            if c != 200:
                errors.append(f"#{i}: HTTP {c}")
            return c == 200
        except Exception as e:
            errors.append(f"#{i}: {type(e).__name__}: {e}")
            return False

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(one, range(24)))
    check("24 个并发请求全部成功", all(results), f"成功 {sum(results)}/24 {errors[:3]}")

    print("\n== 端口占用自动切换 ==")
    data2 = tempfile.mkdtemp(prefix="aibridge-t2-")
    proc2 = start_server(PORT, data2, tries=5)
    time.sleep(3)
    check("同端口再起实例自动切换而非崩溃", wait_up(PORT + 1, timeout=8), f"端口 {PORT + 1} 未起来")
    proc2.kill()
    try:
        proc2.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass

    print("\n== 关闭 ==")
    proc1.kill()
    try:
        proc1.wait(timeout=6)
    except subprocess.TimeoutExpired:
        pass
    check("主实例关闭", proc1.poll() is not None)

    total = passed + failed
    print("\n" + "=" * 40)
    print(f"结果: {passed} 通过 / {failed} 失败（共 {total} 项）")
    if total != EXPECTED_TOTAL:
        print(f"警告: 断言总数 {total} 与声明的 {EXPECTED_TOTAL} 不一致")
        print("      README 与简历上写的数字需要同步修改")
        print("=" * 40)
        return 1
    print("=" * 40)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
