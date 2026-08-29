# AI 桥 · 跨设备 AI 能力调度系统

把 AI 能力封装成 RESTful API，让手机端执行体通过局域网 HTTP 直接调用——**算力在电脑，执行在手机**。

```
        ┌────────────────────────┐          ┌────────────────────────┐
        │      电脑端（大脑）      │          │      手机端（手脚）      │
        │   bridge_server.py     │  ◄────►  │       hand.js          │
        │     默认 :8787         │  局域网   │         :8789          │
        ├────────────────────────┤   HTTP   ├────────────────────────┤
        │ · 持有模型与决策能力     │          │ · 屏幕 / 触摸 / 无障碍   │
        │ · 任务队列（落盘持久化） │          │ · 应用启动 / 包名定位    │
        │ · 设备注册表与在线状态   │          │ · Shell / 截图 / 输入    │
        └────────────────────────┘          └────────────────────────┘
```

**完整闭环**：模型决策 → `task/push` 下发 → 设备执行 → `task/report` 回传

两条投递通道都在：设备在线时服务端主动推送（快通道），离线时指令留在队列里，设备上线的 `task/pull` 兜底领取——**在线走快通道，离线不丢指令**。

---

## 快速开始

```bash
# 1. 启动电脑端（仅依赖 Python 标准库）
AI_BRIDGE_KEY=你的密钥 python bridge_server.py

# 2. 手机端：AutoX.js 里运行 hand.js，改 PORT / KEY 与服务端对齐
# 3. 验证链路
curl http://127.0.0.1:8787/healthz
```

启动后控制台会打印局域网入口与本次密钥：

```
[INFO] 监听地址 : 0.0.0.0:8787
[INFO] 局域网入口: http://192.168.31.79:8787/
[INFO] 可用动作  : 6 个接口 / 18 个设备指令
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AI_BRIDGE_KEY` | 随机生成 | 鉴权密钥。**未设置时自动生成临时密钥并打印**，不会以占位符裸奔 |
| `AI_BRIDGE_HOST` | `0.0.0.0` | 监听地址 |
| `AI_BRIDGE_PORT` | `8787` | 起始端口，被占用自动 +1 |
| `AI_BRIDGE_PORT_TRIES` | `10` | 端口重试次数 |
| `AI_BRIDGE_DATA` | `./data` | 任务队列、设备表、日志目录 |
| `AI_BRIDGE_MAX_BODY` | `1048576` | 请求体上限（1 MB） |

---

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 服务探活，**免鉴权**，供局域网扫描与保活检测 |
| POST | `/ping` | 设备心跳：注册设备、上报状态、双向链路验证 |
| POST | `/notify` | 设备消息上报：落盘 + 写入事件流 |
| POST | `/task/push` | 下发指令：白名单校验 → 入队 → 设备在线则主动推送 |
| POST | `/task/pull` | 设备拉取待执行指令（离线兜底通道） |
| POST | `/task/report` | 设备回传执行结果，闭环收口 |

鉴权：`Authorization: Bearer <KEY>`，除 `/healthz` 外全部需要。

### 下发指令示例

```bash
curl -X POST http://127.0.0.1:8787/task/push \
  -H "Authorization: Bearer 你的密钥" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"my-phone","device_action":"toast","params":{"msg":"你好"}}'
```

`device_action` 走**白名单校验**，与 `hand.js` 的 `doAction` 分支一一对应，白名单外一律拒绝：

```
ping  toast  launch  launchPackage  clickText  press  swipe  shell  shot
screenshot  rawdump  nodes  back  home  setClip  input  clickEdit  current
```

---

## 目录结构

```
ai-bridge/
├── bridge_server.py      电脑端服务端（大脑侧），仅标准库
├── hand.js               手机端执行体（AutoX.js），已脱敏
├── tests/
│   └── smoke_test.py     20 项冒烟测试，覆盖接口/鉴权/并发/端口切换
└── data/                 运行时生成：任务队列、设备表、事件日志
```

---

## 核心实现（面试官可快速验证）

20 项冒烟测试不是凑数，背后是两段真实工程处理。摘录自 `bridge_server.py`：

**1. 并发安全落盘（原子替换）**——多线程同时写任务队列，临时文件名带线程 ID + 随机串，写完 `replace()` 原子覆盖，避免 Windows 上写到一半被占用抛 `OSError [Errno 22]` 的偶发 500：

```python
def _save_json(path: Path, data: Any) -> None:
    tmp = path.with_name(
        f"{path.name}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        tmp.replace(path)          # 原子替换，避免写到一半断电留下坏文件
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
```

**2. 端口占用自动切换**——v1 写死端口，被占用直接崩；实际「上次实例没退干净」是最常见的启动失败，这个重试把它变成一行日志：

```python
def bind_with_fallback(start_port: int, tries: int):
    last_err = None
    for offset in range(tries):
        port = start_port + offset
        try:
            return BridgeServer((HOST, port), BridgeHandler), port
        except OSError as e:
            last_err = e
            log(f"端口 {port} 被占用，尝试 {port + 1}...", "WARN")
    raise RuntimeError(f"端口 {start_port} 起连续 {tries} 个都被占用: {last_err}")
```

---

## 验证

```bash
python tests/smoke_test.py
```

覆盖 20 项：接口可用性、鉴权（含错误密钥与无密钥）、未知接口返回 404 并列出可用动作、动作白名单拦截、任务完整闭环、未知 task_id 容错、中文消息、请求体超限、24 并发、端口占用自动切换、进程清理。

测试自带一项自证：断言总数与文件里声明的 `EXPECTED_TOTAL` 不符时会报警并返回失败——避免文档写 20、实际跑 19 这类不一致。

---

## 踩过的坑（真实环境，不是教程里能查到的）

| 问题 | 现象 | 处理 |
|---|---|---|
| Android 执行体保活 | AutoX.js 在国产 ROM 上被杀后台 | 前台服务 + 电池优化白名单 |
| 端口占用 | 上次实例没退干净，重启直接崩 | 绑定失败自动 +1 重试，并打印实际端口 |
| Windows 的 `SO_REUSEADDR` | 语义与 Unix 相反：允许两个进程绑同一端口，后启动者静默抢流量，冲突检测完全失效 | 按平台区分，Windows 改走 `SO_EXCLUSIVEADDRUSE` |
| 中文编码 | 指令里的中文发出去变乱码 | 全链路统一 UTF-8，响应 `ensure_ascii=False` |
| 并发落盘 | 多线程写同一个临时文件再 `replace()`，Windows 抛 `OSError [Errno 22]`，偶发 500 | 临时文件名带线程 ID + 随机串 |
| backlog 过小 | `HTTPServer` 默认队列只有 5，多设备并发时内核直接丢连接 | 调到 64 |
| 大请求体 | 无上限读取会撑爆内存；超限回 413 却读不到状态码 | 设上限 + 明确回 `Connection: close` |
| 应用包名定位 | 应用名与包名对不上 | 服务端同时支持 `launch`（应用名）与 `launchPackage`（包名） |
| 自更新路径 | 中文路径在编码转换中损坏，新代码写到了读不到的位置 | 修复文件编码，路径统一 UTF-8 |

---

## 安全说明

- 密钥只从环境变量读取，**不硬编码进仓库**；未设置时自动生成临时密钥并告警，而不是回落成占位符
- 鉴权用 `secrets.compare_digest` 常量时间比较，避免时序侧信道
- 设备指令白名单机制，避免服务端被当成任意命令执行的跳板
- 请求体有体积上限，超限请求在读之前就拒绝
- `hand.js` 中的口令与更新源地址已脱敏为占位符，部署前需替换

---

## 已知限制

- 任务队列用 JSON 文件持久化，适合单机多设备场景；规模上去后应换 SQLite
- 设备在线判定依赖心跳，默认 5 分钟无心跳视为离线
- `hand.js` 的 `shell` 通道依赖 Shizuku 或 Root，普通环境下该能力不可用
