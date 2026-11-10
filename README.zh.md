# evalctl

[English](README.md)

一个给 AI agent 产出把关的小工具。agent 在交付前运行 `evalctl check`，按失败项修改，再检查。全部通过时 evalctl 写一个签名回执。没有有效回执，说明要么没跑检查，要么通过之后又改过产出。

- 任何能跑 shell 命令的 agent 都能用（Claude Code、Codex、Hermes、自建 harness）。
- 检查规则放在 agent 工作目录之外，agent 改不到。
- Claude Code 可以用 Stop hook 把"要求"变成"强制"。

v0 范围：`check`、`verify`、`stats`、一个通用 skill、一个 Stop hook、一个示例工作流（论坛抓取）。不做代理网关、文件监听、GUI。

## 安装

需要 Python 3.9+，唯一依赖是 PyYAML。

```bash
cd ~/evalctl
uv tool install --editable .        # 或 pipx install -e .  或 pip install -e .
evalctl --version
```

状态都在 `~/.evalctl/`：

| 路径 | 内容 |
| --- | --- |
| `checks/<name>.yaml` | 检查配置 |
| `judge.yaml` | `llm_rubric` 用的评分模型 |
| `key` | HMAC 签名密钥，第一次通过时生成，权限 600 |
| `runs.sqlite` | 每次 check 的运行记录 |

## 先跑一下

```bash
mkdir -p ~/.evalctl/checks
cp checks/forum_scrape.yaml ~/.evalctl/checks/
cp judge.example.yaml ~/.evalctl/judge.yaml      # 然后按你能用的评分模型修改

evalctl check --dir fixtures/forum_scrape/duplicate_url --config forum_scrape
```

```text
evalctl check: FAIL  (4/6 checks passed, 1 failed, 1 skipped)
config: forum_scrape   dir: .../fixtures/forum_scrape/duplicate_url   round: 1/3

[FAIL] unique_urls (schema): 2 problem(s)
    - posts.jsonl:9: duplicate url "https://forum.homelab.example/t/1054" (first seen at posts.jsonl:2); keep one, remove or replace the other
    - posts.jsonl:17: duplicate url "https://forum.homelab.example/t/1075" (first seen at posts.jsonl:5); keep one, remove or replace the other
    how to fix: Each post may appear once. Remove the repeated line, or fix the url if two different posts got the same one.
[SKIP] body_is_post_content (llm_rubric): not run: runs only after all other checks pass
[PASS] posts_file (file): 1 file(s) OK
...
Round 1 of 3. Fix only the FAIL items above, leave everything that passed untouched, then run `evalctl check` again.
```

其他样例：`good`（合格）、`missing_field`（缺字段）、`too_few`（条数不够）、`nav_body`（结构没问题，但正文是导航栏和广告，只有评分模型能发现）。

输出是英文的，因为它直接给 agent 当修改指令用。

## 命令

```text
evalctl check  [--dir DIR] [--config NAME] [--json] [--worker FAMILY]
evalctl verify [--dir DIR] [--config NAME] [--json]
evalctl stats  [--config NAME] [--json]
```

**check。** 配置的选择顺序：`--config`，否则在目录及其父目录里找最近的 `.evalctl.yaml`。不带 `--dir` 时，检查的是 `.evalctl.yaml` 所在的目录，所以在子目录里运行也可以。硬检查先跑，全部通过后才跑 `llm_rubric`。默认输出文字清单，`--json` 输出 JSON（里面也带那份文字）。

| 退出码 | 含义 |
| --- | --- |
| 0 | 全部通过，已写回执 |
| 1 | 至少一项失败 |
| 2 | 配置或环境错误：没有配置、配置写错、没有可用的评分模型、某项检查无法运行 |

**verify。** 只有同时满足以下条件才返回 0：`.eval/receipt.json` 签名有效；回执覆盖的每个文件哈希都没变（没改、没删、没新增）；检查配置（包括它引用的 Python 检查模块）没变。`.evalctl.yaml` 改指到别的配置，或者回执是从别的目录拷来的，也会失败。

**stats。** 按配置统计：任务数、通过率、首轮通过率、平均轮数、最常失败的检查项、服从率。

- **轮**：产出有变化后再跑一次 check 才算新的一轮。文件没变重复跑，轮数不变。
- **任务**：同一个目录、同一个配置下，从第一次 check 到第一次通过的这一串运行。失败后 6 小时没动，算放弃，下次从新任务开始。
- **服从率** = 有有效回执的任务数 / 总任务数。通过之后又改了产出却没重新检查的任务，不算有效。
- 盲区：agent 从头到尾没跑过 check 的任务，在这里不留痕迹，除非被 Stop hook 抓到。对没有 hook 的 agent，验收前自己跑一下 `evalctl verify --dir <产出目录>`。

## 给新工作流写检查配置

1. 新建 `~/.evalctl/checks/<name>.yaml`，可以从 `checks/forum_scrape.yaml` 改。
2. 在这个工作流的产出目录里放一个 `.evalctl.yaml`，只写一行：`config: <name>`。
3. 手工准备一份好的产出和几份坏的，分别跑 `evalctl check`，直到失败信息读起来就是你会对 agent 说的修改指令。

```yaml
name: weekly_report
artifacts: [report.md, "charts/*.png"]   # 回执覆盖哪些文件；不写就是目录里的所有文件
max_rounds: 3                            # 默认 3

checks:
  - id: report_exists          # id 会出现在输出和统计里
    type: file
    path: report.md
    min_size: 500
    hint: 失败时显示在下面的 "how to fix" 提示。   # 每种检查都可以加，可选

  - id: no_placeholders
    type: contains
    file: report.md
    must: ["## Summary", "## Sources"]
    must_not: ["TODO", "lorem ipsum"]
    ignore_case: true
```

### 检查类型

| 类型 | 选项 | 通过条件 |
| --- | --- | --- |
| `shell` | `command`、`timeout`（120 秒）、`expect_exit`（0） | 在目录里执行命令，退出码符合预期。失败时显示最后 30 行输出。命令里可以用 `$EVALCTL_DIR`。 |
| `schema` | `file`、`format`（json/jsonl/csv，按扩展名推断）、`record_schema`、`schema`、`columns`、`min_records`、`max_records`、`unique` | 每条记录符合 `record_schema`（JSONL 的一行、CSV 的一行、JSON 顶层数组的一个元素）；整个 JSON 文档符合 `schema`；CSV 表头包含 `columns`；记录数在范围内；`unique` 指定的字段没有重复值。 |
| `contains` | `file`（glob）、`must`、`must_not`、`ignore_case` | 每个匹配的文件里都出现了所有 `must`，且没有任何 `must_not`。按字面匹配。 |
| `regex` | 同 `contains` | 同上，用 Python 正则（多行模式）。 |
| `file` | `path`（glob）、`exists`（true）、`min_count`、`max_count`、`name_regex`、`min_size`、`max_size` | 文件存在（`exists: false` 则是不得存在），匹配数量在范围内，文件名完整匹配 `name_regex`，大小在范围内。大小可以写字节数或 `10KB`、`2MB`。 |
| `python` | `module`、`function`、`args` | `function(dir, **args)` 返回 `(True, detail)`。`module` 是放在配置旁边的 `.py` 文件。函数抛异常或返回值形状不对，算环境错误（退出码 2），不算判定结果。 |
| `llm_rubric` | `file`、`rubric`、`sample`、`fields`、`min_pass_rate`（1.0）、`max_chars`（6000）、`judge` | 评分模型对每个被评条目的每条 rubric 都判通过。 |

schema 语法是 JSON Schema 的子集：`type`、`required`、`properties`、`additionalProperties: false`、`items`、`enum`、`const`、`minLength`、`maxLength`、`pattern`、`minimum`、`maximum`、`minItems`、`maxItems`、`format`（`uri`、`date-time`、`date`、`email`）。写了其他关键字，加载配置时直接报错，不会出现"写了规则但没生效"。CSV 的空单元格算缺失，单元格会按 schema 声明的数字或布尔类型转换。

自定义 Python 检查：

```python
# ~/.evalctl/checks/report_checks.py
import os

def charts_referenced(dir, min_charts=1):
    text = open(os.path.join(dir, "report.md")).read()
    missing = [f for f in os.listdir(os.path.join(dir, "charts")) if f not in text]
    if missing:
        return False, "\n".join(f"report.md: chart {f} is never referenced" for f in missing)
    return True, "all charts referenced"
```

```yaml
  - id: charts_referenced
    type: python
    module: report_checks.py
    function: charts_referenced
    args: {min_charts: 2}
```

返回的 detail 每一行会成为一条失败项。

### llm_rubric 和评分模型

```yaml
  - id: body_is_post_content
    type: llm_rubric
    file: posts.jsonl
    sample: 10                  # 不写就全部评
    fields: [title, url, body]  # 给评分模型看哪些字段
    rubric:
      - id: real_post_body
        text: The body is the actual text of the post, not navigation, banners or ads.
```

- JSONL、CSV、JSON 数组文件按记录评，其他文件整份当一个条目评。
- 抽样是随机的，但只取决于文件内容：同一份文件每次抽到的一样，文件变了抽样就变。
- 每个条目发一次请求，评分模型必须对每条 rubric 给出通过或不通过和理由。拿不准时判不通过。评分模型连不上，或者回答没法解析，算环境错误（退出码 2），绝不会当成通过。
- 条目超过 `max_chars` 时从中间截短，并告诉评分模型被截过。

评分模型的查找顺序：检查项上的 `judge:`，然后是配置顶层的 `judge:`，最后是 `~/.evalctl/judge.yaml`。每一处都可以写一个或一个列表。示例见 `judge.example.yaml`。

| provider | 需要 | 说明 |
| --- | --- | --- |
| `anthropic` | `model`、`ANTHROPIC_API_KEY` | Messages API |
| `openai` | `model`、`OPENAI_API_KEY` | Chat Completions |
| `deepseek` | `model`、`DEEPSEEK_API_KEY` | OpenAI 兼容，`https://api.deepseek.com` |
| `openai_compatible` | `model`、`base_url`、`api_key_env`、`family` | 其他 `/chat/completions` 接口 |
| `command` | `command`、`family` | 提示词走 stdin，答案读 stdout，在空的临时目录里执行。可以直接用已登录的 agent CLI 评分，不需要 API key。 |

**评分模型必须和干活的模型不同家。** evalctl 取列表里第一个"不同家且可用（key 已导出或命令已安装）"的评分模型。列表里放两家，你用的每个 agent 就都有人评。干活模型的家族依次取自：`--worker`、环境变量 `EVALCTL_WORKER`、自动识别（Claude Code 会设置 `CLAUDECODE`，对应 `anthropic`）、配置里的 `worker_family:`。Claude Code 以外的 agent，请在它执行命令的环境里导出 `EVALCTL_WORKER`（`openai`、`deepseek` 等）。家族未知时仍然会评分，并在输出里注明这条规则没有被强制。想对某个评分模型放开同家限制，在它上面写 `allow_same_family: true`。

## Skill

`skill/evalctl/SKILL.md` 对所有工作流、所有 agent 通用。内容是：交付前必须跑 `evalctl check`；只改失败项；最多 3 轮；仍失败就原样报告剩余失败项；不许碰 `.eval/` 和检查配置。

| Agent | 安装方法 |
| --- | --- |
| Claude Code | `mkdir -p ~/.claude/skills && cp -r skill/evalctl ~/.claude/skills/`（所有项目），或者放进 `<项目>/.claude/skills/` |
| Codex | `cp -r skill/evalctl ~/.codex/skills/`。如果你的 Codex 版本不加载 skill，就把 `SKILL.md` 正文追加到 `~/.codex/AGENTS.md` 或项目的 `AGENTS.md`。另外导出 `EVALCTL_WORKER=openai`。 |
| Hermes | `cp -r skill/evalctl ~/.hermes/skills/` |
| 其他 | 把 `SKILL.md` 正文贴进系统提示词或 agent 的指令文件。唯一要求是 agent 能跑 shell 命令。没有 shell 的 GUI agent，你自己跑 `evalctl check`，把输出贴回去当修改清单。 |

skill 只能要求，不能强制。让"跳过"变得可见的是回执：没有 hook 的 agent 交活时，先跑 `evalctl verify --dir <产出目录>` 再验收。

## Claude Code Stop hook

`hooks/evalctl_stop_hook.py` 在 Claude 每次想结束回合时运行。

| 情况 | 结果 |
| --- | --- |
| 会话目录及其父目录里没有 `.evalctl.yaml` | 正常结束。和检查无关的会话完全不受影响。 |
| `evalctl verify` 通过 | 正常结束。 |
| 没有有效回执，但 `evalctl check` 通过 | hook 跑的这次 check 写了回执，正常结束。 |
| `evalctl check` 失败 | 阻止结束，把失败清单作为原因交给 Claude。 |
| 到了轮数上限仍失败，或 evalctl 本身跑不起来 | 再阻止一次，让 Claude 把情况报告给你，然后放行。你会看到一条"没有回执"的警告。 |

把 `hooks/settings.example.json` 里的内容加进 `~/.claude/settings.json`（所有项目）或 `<项目>/.claude/settings.json`，路径改成本仓库的实际位置：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/evalctl/hooks/evalctl_stop_hook.py\"",
            "timeout": 600
          }
        ]
      }
    ]
  }
}
```

hook 脚本只用标准库，会在 `PATH`、`~/.local/bin` 或 `$EVALCTL_BIN` 找 `evalctl`。想临时关掉，用 `EVALCTL_HOOK=off claude` 启动。

需要知道的几点：

- 在受管目录里，**每一个**回合都会被检查，包括你只是问个问题的回合。Claude 会收到失败清单并开始修。所以等目录准备好接受检查时再放 `.evalctl.yaml`，或者用 `EVALCTL_HOOK=off`。
- Claude Code 自身的规则：Stop hook 连续阻止 8 次后，它会强制结束回合。
- hook 阻止时，Claude Code 界面会显示 "Stop hook error"。这是"阻止"的显示方式，不是故障。
- hook 的输入输出按[官方 hooks 文档](https://code.claude.com/docs/en/hooks)的 Stop 约定实现：stdin 读 `cwd` 和 `stop_hook_active`，stdout 输出 `{"decision": "block", "reason": ...}`，退出码 0。

## 回执能证明什么，不能证明什么

`.eval/receipt.json` 记录：每个产出文件的路径、大小、SHA-256；配置名、路径、哈希；通过时间；第几轮；每项检查的一行摘要。用 `~/.evalctl/key` 做 HMAC-SHA256 签名。

能发现：没跑检查；通过后改了产出；改了或换了检查配置；改了回执；从别处拷回执；换一个 `EVALCTL_HOME` 跑 evalctl。

防不住：agent 故意去读 `~/.evalctl/key` 伪造签名；agent 把 `.evalctl.yaml` 改指到一个更宽松的现有配置再跑检查（回执和 `stats` 里会显示那个配置名）。agent 和你是同一个系统用户，所以 v0 做到的是让作弊必须是故意的、而且看得出来，做不到让作弊不可能。需要更强的保证时，把 `.evalctl.yaml` 放在 agent 不工作的父目录里，或者让 agent 用单独的系统用户运行。

另外，检查通过不代表规则没测的事实是对的。抓取器编一个格式正确的 URL，URL 格式检查照样通过。

## 测试

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest
```

73 个测试，覆盖每种检查类型、配置选择、退出码、回执和各种篡改、轮数、统计，以及用 Stop hook 的 JSON 驱动 hook 脚本。模型评分的检查用一个本地假服务器测试（它同时实现 Anthropic 和 OpenAI 两种协议格式），所以整套测试可以离线跑。想看真实模型怎么判 rubric，配好评分模型后对 `fixtures/forum_scrape/nav_body` 和 `fixtures/forum_scrape/good` 运行 `evalctl check`。

`python3 fixtures/forum_scrape/build.py` 可以重新生成样例。
