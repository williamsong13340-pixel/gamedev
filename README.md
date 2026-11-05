# 冰原列车（暂定名）

一款手机游戏：一列列车在饥寒末日的冰原上永不停歇地行驶。玩家升级车厢、加挂新车厢，点开任意一节车厢，就能看到里面的幸存者在生活、工作。

美术路线：**列车和世界的运动由引擎和代码完成**（视差滚动、车厢颠簸、车轮连杆、粒子、光影）；**人物动作由 AI 直接画成动作帧**（我们设计动作骨架、GPT Image 2.5 Sunburst 照着画；或 Sora 图生视频后抽帧），不用骨骼拆件。详见 [`docs/art-bible.md`](docs/art-bible.md)。

## 目录

```
docs/art-bible.md          风格规范：视角、比例、色板、验收清单
prompts/style.json         所有提示词的唯一来源（风格词、色板、约束）
prompts/batch-*.json       每一批出图任务
templates/                 灰模模板图 + templates.json（锚点坐标，引擎直接读取）
tools/make_templates.py    生成模板图
tools/poses.py             生成动作骨架图（templates/poses/），AI 照着骨架画动作帧
tools/imagegen.py          调用图像 API 出图（带安全与预算保护）
tools/videogen.py          调用视频 API（Sora）生成人物动作短片（同样的安全与预算保护）
tools/sprites.py           序列图/视频 → 去背景、对齐、找循环的动画帧；生成动画对比页
tools/stabilize.py         走路序列图 → 稳定版：上半身每帧同一张、只有腿逐帧、程序加起伏（解决抽动和闪烁）
tools/imagegen.config.json 模型名、预算上限（图片按张、视频按秒）
art/anchor/                选定的风格锚点图
art/chars/                 选定的角色立绘（动画的起点）
art/raw/<批次>/            生成的原图/视频 + 每个文件的参数记录 (.json)
art/anim/<名字>/           处理好的动画：逐帧 PNG、合图、anim.json、预览 GIF
art/ledger.jsonl           出图账本（每张图一行）；art/video_ledger.jsonl 视频账本（按秒）
```

## API Key 安全配置

**Key 只放在云环境设置里，绝不写进代码、绝不提交到 Git、绝不发到聊天里。**

1. 在 OpenAI 后台为这个游戏单独建一个 Project，只在这个 Project 下建 API Key；权限尽量收窄到图像和视频生成（Sora 用同一个 Key、同一个域名）。
2. 只预充少量余额并关闭自动充值。这是最可靠的花费硬上限。
3. 在会话标题栏的云环境菜单 → Edit：
   - **Network secrets**（旧版 App 叫 API credentials）里添加，变量名 `OPENAI_API_KEY`；没有这一项就作为环境变量添加。
   - **Network access** 里把 `api.openai.com` 加进 Allowed domains（保持 "Allow package managers" 勾选）。
4. 新开一个会话，新会话才会读到 Key。

`tools/imagegen.py` 和 `tools/videogen.py` 的保护措施：只从环境变量读 Key、从不打印或写盘（报错里出现也会打码）；只走 HTTPS；只上传仓库内的文件；单次与累计上限（图片按张、视频按秒，见 `tools/imagegen.config.json`）；`--dry-run` 不联网、不需要 Key。

## 常用命令

```bash
pip install -r requirements.txt
python3 tools/imagegen.py --check                                      # 免费检查：key、网络、模型是否可用
python3 tools/make_templates.py                                        # 重新生成模板
python3 tools/imagegen.py prompts/batch-001-style-explore.json --dry-run  # 预览，不花钱
python3 tools/imagegen.py prompts/batch-001-style-explore.json            # 真正出图
python3 tools/imagegen.py prompts/batch-002-rolling-stock.json --only locomotive --n 1  # 只跑一项、一张

# 人物动画对比测试
python3 tools/imagegen.py prompts/batch-003-anim-test.json --only char-     # 1. 角色立绘，选一张存为 art/chars/mechanic.png
python3 tools/poses.py --preview <目录>                                    # 生成骨架图（+可选的动画预览）
python3 tools/imagegen.py prompts/batch-003-anim-test.json --only posed-    # 2a. 骨架引导：照骨架画（推荐）
python3 tools/imagegen.py prompts/batch-003-anim-test.json --only sheet-    # 2b. AI 自由发挥的序列图
python3 tools/videogen.py prompts/video-001-anim-test.json                  # 2c. Sora 视频
python3 tools/sprites.py sheet <序列图.png> --grid 4x2 --name sunburst-walk
python3 tools/sprites.py video <视频.mp4> --name sora-walk
python3 tools/sprites.py preview sunburst-walk sora-walk --out art/preview/anim-test.html
python3 tools/stabilize.py <序列图.png> --grid 4x2 --name xiaoxue-chibi-walk-steady --ref 2 --debug <目录>  # 稳定版走路
python3 tools/sprites.py contact art/raw/001-style-explore --out art/preview/001-contact.png  # 编号总览图，方便挑选
```

新会话的接手说明在 [`CLAUDE.md`](CLAUDE.md)（Claude Code 会自动读取）。

如果用的不是 OpenAI 官方接口（例如兼容 OpenAI 的其他服务商），额外设置环境变量 `OPENAI_BASE_URL`，并把对应域名加入 Allowed domains。
