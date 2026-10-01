# codex-websearch-bridge

让 **Codex（桌面版 / CLI）在第三方 OpenAI 兼容中转上也能用「原生联网搜索」** 的本地桥。

> 一句话原理：Codex 0.159+ 的联网搜索只认 `{base_url}/alpha/search`，这是 Codex 侧的私有
> 端点，需要中转自己实现。如果你的中转还没有实现它，Codex 就会收到 404、联网搜索用不了。
> 这个桥在本地把端点补上，内部改走中转一般都支持的 `/v1/responses` + `web_search` 工具，
> 再把结果翻译回 Codex 要的格式。

## 为什么需要它

Codex 0.159+ 的联网搜索（standalone web search）不走标准 Responses 的 `web_search` 工具，
而是自己往 **`{base_url}/alpha/search`** 发一个请求，期望拿回 `{"output": "<纯文本>"}`。

`/alpha/search` 是 Codex 侧的私有端点，需要中转自己实现。**如果你的中转还没有实现它**：

```
POST /v1/alpha/search -> 404 Invalid URL
```

表现就是：工具列表里没有联网搜索，或者模型一搜就报错。

## 它做了什么

```
Codex ──base_url=http://127.0.0.1:8787/v1──▶ bridge ──▶ 你的中转
                                              │
                                              ├─ /v1/alpha/search  → 本地实现：
                                              │     把 commands.search_query 拼成提示词，
                                              │     调中转自己的原生 web_search 工具，
                                              │     把结果包成 {"output": "..."} 返回
                                              └─ 其它所有请求 → 原样透传（含 SSE 流式）
```

## 快速开始

**1. 装依赖**

```bash
pip install -r requirements.txt
```

**2. 建配置**

```bash
cp bridge.config.example.json bridge.config.json     # Windows: copy ...
```

打开 `bridge.config.json`，只需要改 `upstream`：

```json
{
  "upstream": "https://你的中转地址"
}
```

**3. 启动桥**

```bash
python web_search_bridge.py
```

Windows 想静默后台跑，直接双击 `launch-hidden.vbs`（或跑 `run-bridge.cmd`）。

**4. 改 Codex 配置**（`~/.codex/config.toml`）

```toml
web_search = "live"          # 顶层，合法值 disabled / cached / indexed / live

[model_providers.relay]
base_url = "http://127.0.0.1:8787/v1"   # 指向桥
wire_api = "responses"
requires_openai_auth = true
supports_standalone_web_search = true   # ★ 关键开关，默认 false
```

`supports_standalone_web_search = true` 是**必须的**：自定义 provider 不打开它，
Codex 根本不会把联网工具塞给模型，跟 `web_search` 顶层配置怎么写都无关。

改完记得**新建对话**，旧线程的工具集是创建时锁死的。

## 配置项

`bridge.config.json` 里全部字段都是可选的，不写就用默认值：

| 字段 | 默认 | 说明 |
|---|---|---|
| `upstream` | `https://your-relay.example` | **必填**，你的中转地址 |
| `host` | `127.0.0.1` | 监听地址，别乱改，见下方「安全须知」 |
| `port` | `8787` | 监听端口，跟 Codex 的 `base_url` 保持一致 |
| `model` | `gpt-6.1-sol` | 兜底模型名，仅在 Codex 请求没带 `model` 时使用 |
| `verbose` | `true` | 是否同时打印到控制台 |
| `allow_remote` | `false` | 允许监听非本机地址，见「安全须知」 |
| `log` | `""` | 日志路径，留空 = 脚本目录下的 `bridge.log` |

环境变量优先级更高，临时改不用动文件：

| 变量 | 对应字段 |
|---|---|
| `BRIDGE_UPSTREAM` | `upstream` |
| `BRIDGE_HOST` | `host` |
| `BRIDGE_PORT` | `port` |
| `BRIDGE_MODEL` | `model` |
| `BRIDGE_VERBOSE` | `verbose` |
| `BRIDGE_ALLOW_REMOTE` | `allow_remote` |
| `BRIDGE_LOG` | `log` |

单个请求想临时换上游，请求头里带 `X-Bridge-Upstream: https://other-relay.example` 即可，
优先级最高。所以同一个桥可以同时服务多个中转，不用改配置重启。

## 安全须知

**这个桥会把你的 API Key 原样转发给上游，并且对任何能访问该端口的程序开放。**

具体说，它有这两个特点：

- **没有鉴权**。凡是能连上 `127.0.0.1:8787` 的进程，都能借你的 Key 发请求。
- **`X-Bridge-Upstream` 可以指定任意上游**，等于一个通用转发器。

所以：

1. **不要改 `host`**。默认的 `127.0.0.1` 只有本机能连，是安全的。绑到 `0.0.0.0`
   或者局域网 IP，等于把 Key 和开放代理一起送出去。
2. 确实需要远程访问时，必须在配置里显式写 `"allow_remote": true`，并自己另外加一层
   防护（防火墙 / 反代鉴权）。桥会在启动时打印警告。
3. `bridge.log` 只记录模型名、上游地址、返回长度，**不记录请求体、请求头和搜索结果**，
   可以放心排查。但它仍然会暴露你的上游地址，别提交到公开仓库（`.gitignore` 已经忽略）。

## 排查顺序（联网又不好使的时候按这个来）

1. 桥还在不在：`netstat -ano | Select-String ':8787.*LISTENING'`。不在就双击
   `launch-hidden.vbs`（或直接跑 `run-bridge.cmd`）。
2. 看 `bridge.log`：
   - 有 `[fatal] 启动失败` → 配置没填对，按提示改 `bridge.config.json`；
   - 有 `[search] ok: N web_search_call(s)` → 桥这侧正常，问题在模型 prompt / 上游；
   - 有 `upstream HTTP 4xx/5xx` → 中转那边挂了或者 key / 额度有问题；
   - 一条 `[search]` 都没有 → Codex 根本没发搜索请求，要么 `supports_standalone_web_search`
     没生效，要么你还在旧线程里（新建对话试试），要么 Codex 的契约又变了。
3. 桥健康检查：`Invoke-WebRequest http://127.0.0.1:8787/v1/models`，
   返回 401 是**正常**的（桥原样转发了中转的鉴权响应，说明通路是好的）。

## 已知的坑

- `[features] web_search_request = true` 和 `[tools] web_search = true` 都是**无效的旧写法**，
  Codex 0.159.2 会直接忽略，删掉即可。
- Codex 的联网走的是 standalone 模式，请求体里的字段名（`commands.search_query` /
  `response_length` 等）是**非公开契约**，Codex 升级后可能变。桥坏了先看 `bridge.log` 里
  还有没有 `[search]` 记录，一条都没有基本就是契约变了，对着 Codex 客户端调整 `build_prompt()`
  和 `_handle_search()` 即可。
- 搜索结果是由模型「转述」的，不是原始检索结果，所以会比原生 standalone 慢一点、
  也可能带上模型的总结口味。
- `run-bridge.cmd` 默认从 PATH 找 `pythonw`；找不到会退回 `python`。都没有就自己把
  `run-bridge.cmd` 里的路径改成你本机的完整路径。

## 兼容性

| Codex 版本 | 状态 |
|---|---|
| 0.159.x | 已验证可用 |
| 更低版本 | 没有 standalone web search，不需要这个桥 |

## 如果你的中转已经支持了 /alpha/search

把 `base_url` 改回 `https://你的中转地址/v1`，保留
`supports_standalone_web_search = true` 就行，这个桥可以停掉。

## 开机自启（Windows）

建一个计划任务 `CodexWebSearchBridge`，登录时触发，执行 `launch-hidden.vbs`（隐藏窗口）。

卸载：

```powershell
Unregister-ScheduledTask -TaskName CodexWebSearchBridge -Confirm:$false
```

## License

[MIT](LICENSE)