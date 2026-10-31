# 冰原列车（暂定名）

一款手机游戏：一列列车在饥寒末日的冰原上永不停歇地行驶。玩家升级车厢、加挂新车厢，点开任意一节车厢，就能看到里面的幸存者在生活、工作。

美术路线：**AI（GPT Image 2.5 Sunburst）只画静态零件，运动全部由引擎和代码完成**（视差滚动、车厢颠簸、车轮连杆、粒子、光影、纸偶骨骼动画）。详见 [`docs/art-bible.md`](docs/art-bible.md)。

## 目录

```
docs/art-bible.md          风格规范：视角、比例、色板、验收清单
prompts/style.json         所有提示词的唯一来源（风格词、色板、约束）
prompts/batch-*.json       每一批出图任务
templates/                 灰模模板图 + templates.json（锚点坐标，引擎直接读取）
tools/make_templates.py    生成模板图
tools/imagegen.py          调用图像 API 出图（带安全与预算保护）
tools/imagegen.config.json 模型名、预算上限
art/anchor/                选定的风格锚点图
art/raw/<批次>/            生成的原图 + 每张图的参数记录 (.json)
art/ledger.jsonl           出图账本（每张图一行，用于预算控制）
```

## API Key 安全配置

**Key 只放在云环境设置里，绝不写进代码、绝不提交到 Git、绝不发到聊天里。**

1. 在 OpenAI 后台为这个游戏单独建一个 Project，只在这个 Project 下建 API Key；权限尽量收窄到图像生成。
2. 只预充少量余额并关闭自动充值。这是最可靠的花费硬上限。
3. 在会话标题栏的云环境菜单 → Edit：
   - **Network secrets**（旧版 App 叫 API credentials）里添加，变量名 `OPENAI_API_KEY`；没有这一项就作为环境变量添加。
   - **Network access** 里把 `api.openai.com` 加进 Allowed domains（保持 "Allow package managers" 勾选）。
4. 新开一个会话，新会话才会读到 Key。

`tools/imagegen.py` 的保护措施：只从环境变量读 Key、从不打印或写盘（报错里出现也会打码）；只走 HTTPS；只上传仓库内的文件；单次与累计出图数量上限（`tools/imagegen.config.json`）；`--dry-run` 不联网、不需要 Key。

## 常用命令

```bash
pip install -r requirements.txt
python3 tools/make_templates.py                                        # 重新生成模板
python3 tools/imagegen.py prompts/batch-001-style-explore.json --dry-run  # 预览，不花钱
python3 tools/imagegen.py prompts/batch-001-style-explore.json            # 真正出图
python3 tools/imagegen.py prompts/batch-002-rolling-stock.json --only locomotive --n 1  # 只跑一项、一张
```

如果用的不是 OpenAI 官方接口（例如兼容 OpenAI 的其他服务商），额外设置环境变量 `OPENAI_BASE_URL`，并把对应域名加入 Allowed domains。
