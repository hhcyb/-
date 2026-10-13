# PrimeVul 线上实验包：我们的方法（graph_diff）对比同模型基线（direct）

## 这个包是什么

- **`graph_diff`（我们的方法）**：先用 Anchor GraphRAG 从 3789 个历史修复对里检索相关修复，以修复前后的代码 diff 形式附在目标函数后面，再交给大模型判断。和目标同 CVE 或同 commit 的修复已经排除。
- **`direct`（基线）**：同一个模型，只给目标函数的源码，不给任何检索证据。
- 两个条件的全部提示词已经**预先生成并冻结**在 `prepared/` 目录下，和服务器上本地实验发出的内容逐字相同，所以线上和本地的结果可以直接对比。请不要修改 `prepared/` 里的文件，每个文件的哈希都记录在 `manifest.json` 里。
- 测试集是 PrimeVul 官方配对测试集：435 对，共 870 个函数。每个条件需要调用 870 次。

本地 qwen3:32b 在开发集 A（100 对，标签文件里标为 `dev_a`）上的结果是：direct 的 P-C 为 11，graph_diff 为 26（改对 19 对、改错 4 对，McNemar p = 0.003）。这次线上实验要在全部 435 对上确认这个结果。

## 环境

- Python 3.8 或以上，**不需要安装任何第三方库**。
- 能访问 `dashscope.aliyuncs.com`，并有一个百炼（DashScope）的 API Key。

先设置 API Key，只在当前终端窗口里有效：

```powershell
# Windows PowerShell
$env:DASHSCOPE_API_KEY = "你的 API Key"
```

```bash
# macOS / Linux
export DASHSCOPE_API_KEY="你的 API Key"
```

下面的命令都在**解压后的包目录**里执行。

## 第 1 步：小规模试跑（4 个函数，约 2 分钟）

```bash
python online/run_online.py run --condition direct --model qwen3-32b --limit 4 --tag smoke
```

正常的话会显示 `4/4 {'ok': 4}`。结果写在 `results/predictions_direct_qwen3-32b_smoke.jsonl`，每一行的 `status` 应该是 `ok`，`reasoning_chars` 应该大于 0（说明思考模式已经开启）。

- **模型名**默认用 `qwen3-32b`，也就是本地实验用的那个模型。请先在百炼控制台的模型列表里确认这个名字可以使用。
- 如果报错 `HTTP 400` 并提到 `seed`，就加上 `--seed -1`；提到 `enable_thinking`，就加上 `--thinking default`。这两项参数的改动都会写进运行配置，两个条件必须用同样的参数。
- 如果报错 `HTTP 429`（请求太频繁），就把并发数调小，比如 `--workers 4`。

## 第 2 步：正式运行（两个条件，各 870 次调用）

先跑我们的方法，再跑基线：

```bash
python online/run_online.py run --condition graph_diff --model qwen3-32b
python online/run_online.py run --condition direct --model qwen3-32b
```

- 默认 8 路并发，屏幕上会每 20 个函数显示一次进度和预计剩余时间。
- **中途断了，用同一条命令重新运行即可**：已经完成的函数会自动跳过，失败的调用会重试。
- 如果改了任何参数（模型、并发以外的设置），程序会拒绝在原来的结果文件上续跑，这时需要加 `--tag` 另起一个名字。
- **用量估算**（每个条件）：输入大约 150 万到 230 万 token（graph_diff 的提示词更长）；输出加上思考过程大约 300 万到 400 万 token。具体费用请按百炼当前的价格计算。

## 第 3 步：打分

```bash
python online/run_online.py score --baseline direct_qwen3-32b
```

会分别显示**全部 435 对**和**开发集 A 子集**的结果：

| 列 | 含义 |
|---|---|
| P-C | 漏洞版判为有漏洞、修复版判为安全，两侧都对，**主要指标** |
| P-V / P-B | 两侧都判有漏洞 / 都判安全 |
| P-R | 两侧都判反 |
| fail | 至少一侧没有拿到有效回答 |
| C-R | P-C 减 P-R，衡量真正的区分能力 |
| vs baseline | 相对 direct 改对和改错的 pair 数，以及 McNemar 检验的 p 值 |

**事先约定的判定标准**：在 435 对上，graph_diff 的 P-C 高于 direct，并且 McNemar p < 0.05。

## 跑完后

把整个 `results/` 文件夹打包发回来。里面有每次调用的预测、思考长度、token 用量和运行配置。
