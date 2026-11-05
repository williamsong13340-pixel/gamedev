# 冰原列车：给新会话的交接说明

新会话请先读完本文件，再按"下一步"做。做完一步就更新本文件的"当前进度"和"已知情况"。

## 项目和用户

- 手机游戏：饥寒末日里一列永不停歇的列车。玩家升级、加挂车厢，点开车厢能看到里面的人生活和工作。
- 用户不会画画，全部美术由 AI 生成，要求**精美统一**。用户希望 Claude 全包。
- **用中文和用户交流**，说话简单，少用术语。
- 美术规则：`docs/art-bible.md`。工具和命令：`README.md`。

## 必须遵守

- **图像模型固定用 `gpt-image-2.5-sunburst`**（用户明确要求）。不要换成 Flare 或别的模型。如果服务商不认这个模型名，先停下来问用户。
- **API key**：只从环境变量读取；绝不打印、写进文件或提交到 git；绝不让用户把 key 发到聊天里。
- **花钱之前**先 `--dry-run`；遵守 `tools/imagegen.config.json` 里的数量上限。Sora 视频比较贵，**只有用户同意才跑**。
- **审美由用户决定**：每批结果都做成编号总览图（`tools/sprites.py contact`），用 SendUserFile（display: render）发给用户，请用户报编号。
- **每一步都提交并推送**生成的文件（`art/raw/…`、`art/ledger.jsonl`、`art/preview/…`、选定的锚点图和角色图）。容器是临时的，不提交就会丢。

## 当前进度（2026-11-05）

- 已完成：出图和视频脚本、模板图、动作骨架图（走路、拧螺栓）、提示词库、风格规范。全部用模拟接口测试通过。
- 还没做：从未真正调用过 API，一张图都还没生成。
- 用户说改不了上一个会话的云环境设置，但已经自己配置好了 key（可能新建了环境）。

## 下一步

### 第 0 步：检查环境（免费，不生成图）

```bash
python3 tools/imagegen.py --check
```

| 结果 | 怎么办 |
|---|---|
| `RESULT: OK` 且 `model gpt-image-2.5-sunburst: available` | 进入第 1 步 |
| `OK`，但提示用 `--proxy-auth` | 环境自己注入了 key。之后**每条** imagegen 命令都加 `--proxy-auth` |
| `BLOCKED` | 网络规则拦着显示的域名。请用户确认新会话用的是他配置好的那个环境，并在该环境的 Network access 里放行这个域名，然后开新会话 |
| `key is NOT set`，且请求被拒 | 用 `env \| cut -d= -f1` **只看变量名**，找用户是否用了别的名字（比如带 OPENAI 的）。找不到就请用户检查环境设置 |
| `model … NOT in the provider's list` | 把列出的图像模型告诉用户，问用户怎么办，不要自己换 |

如果 `OPENAI_BASE_URL` 指向的服务不兼容 OpenAI 的 Images API（`/images/generations`、`/images/edits`），需要先给 `tools/imagegen.py` 写适配，动手前和用户确认。

### 第 1 步：定画风（9 张图）

1. `python3 tools/imagegen.py prompts/batch-001-style-explore.json`
   - 如果 API 拒绝某个参数（size、quality、background 等），按报错改批次文件里的参数再跑，并把结论写进下面的"已知情况"。
2. `python3 tools/sprites.py contact art/raw/001-style-explore --out art/preview/001-contact.png`，发给用户，请用户报编号。
3. 用户选定后：
   - 把选中的图复制为 `art/anchor/style_anchor.png`；
   - 把对应的风格 key（`storybook` / `papercraft` / `silhouette`）写进 `prompts/batch-002-rolling-stock.json` 和 `prompts/batch-003-anim-test.json` 的 `defaults.style`；
   - 提交并推送。

### 第 2 步：测试角色（2 张）

1. `python3 tools/imagegen.py prompts/batch-003-anim-test.json --only char-`
2. 做总览图给用户选，存为 `art/chars/mechanic.png`（需要透明背景；不是的话先去背景），然后提交。

### 第 3 步：人物动画测试（核心）

- **C 骨架引导（首选）**：`--only posed-`，2 张（走路、拧螺栓）。动作来自 `templates/poses/`，由 `tools/poses.py` 生成。
- **A 自由序列图（对照组）**：`--only sheet-`，2 张。
- 处理结果：`python3 tools/sprites.py sheet <png> --grid 4x2 --name posed-walk --label "骨架引导：走路"`，其余三张同理（posed-work、sheet-walk、sheet-work）。
- 对比页：`python3 tools/sprites.py preview posed-walk posed-work sheet-walk sheet-work --out art/preview/anim-test.html`，用 SendUserFile（render）发给用户。
- 如实告诉用户：AI 有没有照着骨架画、每帧角色是否一致、看起来闪不闪。不满意的话，可以只重画有问题的那张、调提示词，或在 `tools/poses.py` 里调动作。
- **Sora（方法 B）**：只有用户同意才跑 `python3 tools/videogen.py prompts/video-001-anim-test.json`。

### 第 4 步：车头和车厢（10 张）

`python3 tools/imagegen.py prompts/batch-002-rolling-stock.json`，做总览图给用户看，并叠在 `templates/` 的模板上检查轮廓有没有跑。

### 之后

- 问清平台（手机 App 还是微信/抖音小游戏）和横竖屏，搭可玩的原型（用户没意见就默认 Godot 4 + 横屏）。
- 量产角色和动作、场景物件套件、UI。

## 已知情况（发现新情况就更新这里）

- 全部工作在分支 `ccr-12c7c891-s3zgy9` 上（它也是仓库默认分支）。如果新会话的分支上没有这些文件，先 `git fetch origin ccr-12c7c891-s3zgy9` 再合并。
- 尚未验证：Sunburst 是否支持透明背景（`background: transparent`）、`1536x1024` 尺寸、多张输入图（`image[]`）。
- 待机呼吸、眨眼这类细微动作不要逐帧画，用一张图加代码轻微缩放和摆动来做（见 art-bible 第 6 节）。
