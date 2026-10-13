# PrimeVul 线上实验包：我们的方法 vs 同模型基线 vs VulTrial

## 这个包是什么

三个条件，**全部使用同一个大模型**（默认 `qwen3.8-max`），在 PrimeVul 官方配对测试集上运行（435 对，共 870 个函数）：

| 条件 | 内容 | 每个函数调用次数 |
|---|---|---:|
| `graph_diff`（我们的方法） | 用 Anchor GraphRAG 从 3789 个历史修复对里检索相关修复，以修复前后的代码 diff 形式附在目标函数后面；和目标同 CVE 或同 commit 的修复已经排除 | 1 |
| `direct`（消融对照） | 同一个模型，只给目标函数源码，不给任何检索证据 | 1 |
| `vultrial`（对比方法） | 复现 VulTrial（ICSE 2026，模拟法庭的多智能体方法）：安全研究员 → 代码作者 → 主持人 → 评审团 | 4 |

- `direct` 和 `graph_diff` 的提示词已经**预先生成并冻结**在 `prepared/` 目录下，和服务器上本地实验发出的内容逐字相同。请不要修改 `prepared/` 里的文件，它们的哈希记录在 `manifest.json` 里。
- `vultrial` 使用和另外两个条件**完全相同的 870 个函数源码**，提示词在 `online/vultrial_prompts.json`（见文末"VulTrial 复现说明"）。

## 环境

- Python 3.8 或以上，**不需要安装任何第三方库**。
- 一个阿里云百炼（DashScope）的 API Key。

设置 API Key（只在当前终端窗口有效）：

```powershell
# Windows PowerShell
$env:DASHSCOPE_API_KEY = "你的 API Key"
```

```bash
# macOS / Linux
export DASHSCOPE_API_KEY="你的 API Key"
```

**接口地址**：脚本默认用 `https://dashscope.aliyuncs.com/compatible-mode/v1`。如果你的账号要求使用带业务空间 ID 的新地址（例如北京地域 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`），就在每条 `run` 命令后面加上 `--api-base <你的地址>`。注意不同地域的 API Key 不能混用，同一组实验的所有命令必须用同一个地址。

下面的命令都在包的根目录（解压后的文件夹，或克隆下来的仓库）里执行。

```bash
git clone https://github.com/hhcyb/-.git primevul-online-eval
cd primevul-online-eval
```

## 第 1 步：小规模试跑（约 5 分钟）

```bash
python online/run_online.py run --condition direct --model qwen3.8-max --limit 4 --tag smoke
python online/run_online.py run --condition vultrial --model qwen3.8-max --limit 2 --tag smoke
```

正常的话会显示 `4/4 {'ok': 4}` 和 `2/2 {'ok': 2}`。结果在 `results/` 目录，每一行的 `status` 应该是 `ok`，`reasoning_chars` 应该大于 0（说明思考模式已经开启）。

常见报错的处理（同一组实验的所有命令都要加上同样的参数）：

| 报错 | 处理 |
|---|---|
| `HTTP 400`，提到 `seed` | 加 `--seed -1` |
| `HTTP 400`，提到 `enable_thinking` | 加 `--thinking default` |
| `HTTP 400`，只出现在 `vultrial`，提到消息里必须有 user | 加 `--vultrial-message-role user` |
| `HTTP 429`（请求太频繁） | 降低并发，例如 `--workers 4` |

## 第 2 步：开发集试跑（direct 和 graph_diff，各 200 次调用）
可选，可以不做。
先在 100 对开发集上确认我们的方法在这个模型上有提升：

```bash
python online/run_online.py run --condition graph_diff --model qwen3.8-max --subset dev_a
python online/run_online.py run --condition direct --model qwen3.8-max --subset dev_a
python online/run_online.py score --baseline direct_qwen3.8-max
```

看打分结果里 **dev-A subset** 那一部分（`all 435 pairs` 部分这时还不完整，大部分会显示为 fail，是正常的）。把输出发回来，确认之后再跑全量。

## 第 3 步：全量运行

```bash
python online/run_online.py run --condition graph_diff --model qwen3.8-max
python online/run_online.py run --condition direct --model qwen3.8-max
python online/run_online.py run --condition vultrial --model qwen3.8-max
```

- 第 2 步跑过的函数会**自动跳过**，不会重复调用。
- 默认 8 路并发，每 20 个函数显示一次进度和预计剩余时间。
- **中途断了，用同一条命令重新运行即可**：已完成的会跳过，失败的会重试。
- 如果改了任何参数，程序会拒绝在原来的结果文件上续跑，这时需要加 `--tag` 另起名字。

**用量估算**（`qwen3.8-max` 默认推理强度较高，思考过程较长，以下只是粗略估计）：

| 条件 | 调用次数 | 输入 token | 输出 token（含思考） |
|---|---:|---:|---:|
| direct | 870 | 约 150 万 | 约 300–500 万 |
| graph_diff | 870 | 约 230 万 | 约 300–500 万 |
| vultrial | 3,480 | 约 1,100 万 | 约 1,000–1,500 万 |

具体费用请按百炼控制台当前的价格计算。

## 第 4 步：打分

```bash
python online/run_online.py score --baseline direct_qwen3.8-max
```

会分别显示全部 435 对和开发集 100 对的结果：

| 列 | 含义 |
|---|---|
| P-C | 漏洞版判为有漏洞、修复版判为安全，两侧都对，**主要指标** |
| P-V / P-B | 两侧都判有漏洞 / 都判安全 |
| P-R | 两侧都判反 |
| fail | 至少一侧没有拿到有效回答 |
| C-R | P-C 减 P-R，衡量真正的区分能力 |
| vs baseline | 相对 direct 改对和改错的 pair 数，以及 McNemar 检验的 p 值 |

**事先约定的判定标准**：在 435 对上，`graph_diff` 的 P-C 高于 `direct`，并且 McNemar p < 0.05。`graph_diff` 和 `vultrial` 的比较作为次要结果报告；需要时可以用 `--baseline vultrial_qwen3.8-max` 再打一次分。

跑完后把整个 `results/` 文件夹打包发回来。

## VulTrial 复现说明

- **来源**：VulTrial 作者公开的复现包（figshare `1514bc9a7aa64b46d94e`，CC BY 4.0），实现基于 AgentVerse，主设置为 `vultrial_base`。
- **提示词**：四个角色的提示词和通用模板，从复现包里实际运行用的 868 份配置文件中**原样提取**（所有配置完全一致），包括原配置里几处被错误编码的撇号（`â€™`）。`online/vultrial_prompts.json` 的 SHA-256 是 `8a57ba64fdafbb9fb6f2512d387106511840c063c54d1587012725eb1391e188`。
- **流程**（与原实现一致）：`max_turns = 4`，即一轮：安全研究员 → 代码作者 → 主持人 → 评审团。每次调用只发一条 `system` 消息，内容是"通用开头 + 角色说明 + `<code>` + 前面角色的回复"；对话历史按原实现的规则拼接：研究员的回复作为开头，之后的回复用空行接在后面，不带角色名。
- **判定规则**（与原评测脚本一致）：评审团的判决里，只要有一条同时满足 `decision = valid`、`severity = high`、`recommended_action = fix immediately`，就判为有漏洞；JSON 解析失败时，最后一行含 "mitigate" 判为有漏洞，否则判为无漏洞。
- **复现核对**：用这套判定规则，在复现包自带的"指令微调主持人的 GPT-4o"记录上重算，得到 P-C / P-V / P-B / P-R = 96 / 154 / 134 / 51，与论文报告的数字完全一致。复现包里的 868 个函数与本包的测试函数源码逐字一致，标签也一致。
- **与原文的差异**（三个条件都相同，保证公平）：模型换成 `qwen3.8-max`（原文是 GPT-4o）；开启思考模式；输出上限 8192（原文 2048，为了容纳思考过程）；temperature 同为 0。
