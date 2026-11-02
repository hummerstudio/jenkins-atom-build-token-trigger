# build-token-trigger atom

用 Jenkins 的 **build token** 触发一次构建，等价于插件
[build-token-trigger](https://plugins.jenkins.io/build-token-trigger/) 的 `buildTokenTrigger` 步骤。

原子只做一件事：向目标 Jenkins 发一次同步 POST，把 `Location` 当作队列地址返回。没有 job 模型、不轮询、不碰
凭据插件——这正是它能被一个单次进程包住的原因。

## 从插件抄来的规则（11.v5e2ee5cdb_32a_，`TriggerStep.Execution`）

- 端点：`<jenkinsUrl>/buildByToken` + 参数为空时 `/build`，否则 `/buildWithParameters`
- 报文：`job=<job>&token=<token>[&delay=<秒>][&<k>=<v>...]`，`application/x-www-form-urlencoded`，
  `URLEncoder`（UTF-8，空格变 `+`），参数按 key 升序（插件的 `TreeMap`）
- `job` 在报文里是**原样的一整段**（`folder/job` 编成 `folder%2Fjob`）；插件日志里的 job 页地址才展开成
  `/job/folder/job/job`
- `delay` 为空、为负或不是数字 → 不发送（`newInstance` 的行为）
- URL 归一化（`TriggerCredentialsImpl.normalizeUrl`）：host 小写、省掉 scheme 默认端口、去掉一个结尾斜杠
- 应答：`404` → 失败（`ignoreMissing` 时 `about:missing`）；`302` → 未进队列（`ignoreUnqueued` 时
  `about:unqueued`）；其余读 `Location`，以 `/` 开头时用 `jenkinsUrl` 补全

## 与插件不同的地方（`tools/smoke.py` 逐条断言）

| 场景 | 插件 | 本原子 |
|---|---|---|
| 401 / 403 | 当成功，随后读 `Location` 时 NPE | `40006`，消息点明 token 被拒 |
| 其他非 2xx | 同上 | `40005`，消息带状态码与报文首段 |
| 2xx 但没有 `Location` | NPE | `40007` |
| 301/303/307/308 | 不跟随，却把 `Location` 当队列地址 | `40008`，明确不跨重定向重发 token |
| 超时 | 只设连接超时，无读超时 | `40009`，`timeoutSeconds` 同时管连接与读取 |
| 代理 | 读 `Jenkins.proxy` | 不支持（原子没有 Jenkins 可问），文档写明 |

另外：token 只从入参或 `x-jenkins.secrets.token` 取，任何日志与 `output.json` 里都不出现；本原子不跟随任何
重定向（`HttpClient.Redirect.NEVER`）。

## 入参 / 出参

入参：`jenkinsUrl`、`job`、`token`（或绑定的同名 secret）、`parameters`（每行 `KEY=VALUE`）、`delay`、
`ignoreMissing`、`ignoreUnqueued`、`timeoutSeconds`。

出参：`outcome`（`queued`/`missing`/`unqueued`）、`queued`、`queueUrl`、`httpCode`、`jobUrl`、`triggerUrl`、
`location`。

## 错误码

| 码 | 含义 |
|---|---|
| 40001 | 入参不合法（job/token 为空，参数行不是 KEY=VALUE） |
| 40002 | 连不上 / URL 不是 http(s) |
| 40003 | job 不存在（404） |
| 40004 | 未被队列接受（302） |
| 40005 | 其他非 2xx |
| 40006 | token 被拒（401/403） |
| 40007 | 2xx 但没有 Location |
| 40008 | 未跟随的重定向 |
| 40009 | 超时 |

## 验证

```bash
export JAVA_HOME=~/tools/jdk-21.0.12.1+1/Contents/Home
./build.sh          # 需要 ../jenkins-atom-sdk/target/*.jar
./tools/smoke.sh    # 真 HTTP 服务器 + 逐字断言报文/路径/失败码
python3 tools/mutations.py   # 12 处故意改坏，每处都必须让 smoke 失败
```

`tools/smoke.py` 里的期望值是用 `urllib.parse.urlencode` 从 job、token、参数**独立复算**出来的，不是从原子
回读的；`Content-Length` 也按字节数核对。假 Jenkins 记录每个请求的原始路径、头与报文，并且只按 `job` 字段
选择应答，所以"有没有跟随重定向"可以直接用请求条数证明。
