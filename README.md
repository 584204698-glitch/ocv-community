# OCV Community · 一键成片社区微调版

基于 [One-Click VidGen](https://github.com/IFRIT-Zhou/One-Click-VidGen) 的非官方增强版，保留原作者署名、许可证、账号登录及原有制作入口。维护仓库：[584204698-glitch/ocv-community](https://github.com/584204698-glitch/ocv-community)。

提供故事创作与一键制作、角色与音色管理、Qwen3-TTS、旁白/对白/混合制作、动态镜头局部恢复、后期音轨编辑及 ComfyUI 接入。见 [升级说明](UPGRADE.md)、[使用说明](docs/community-user-guide.md)、[开发修改地图](docs/community-development.md)。

这是 **2026-10-12 源码预发布版**，不是官方整合包。不含私人配置、账号数据、声音资产、成片、模型权重或运行时。已有环境实片验证不等于所有机器均完成安装验收；生成质量仍需检查，第三方服务异常可能暂停制作。

## 源码安装

准备 Python 3.10/3.11、Node.js 和加入 PATH 的 FFmpeg，在 PowerShell 执行：

```powershell
git clone https://github.com/584204698-glitch/ocv-community.git
cd ocv-community
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm ci
npm --prefix frontend ci
npm --prefix frontend run build
.\.venv\Scripts\python.exe scripts/start_public.py
```

打开 `http://127.0.0.1:8010` 注册并登录本地账号，再配置自己的模型接口。云端账号/充值属于原 OCV 服务，是另一个入口。首次启动创建本机 `.env` 和独立会话密钥，后续沿用，不提交 Git。可信局域网可增加 `--host 0.0.0.0`。

原整合包启动脚本保留，但源码仓库没有便携运行时；请按上面安装。本仓库尚未提供完整整合包。

## 可选 Qwen 声音模型

声音面板提供安装说明；缺模型时仍可使用原配音路线。以下命令需显式执行，在独立环境安装依赖和约 9.1 GB 模型：

```powershell
.\.venv\Scripts\python.exe scripts/setup_qwen3.py --install --download --cuda cu128
```

按命令输出配置 `.env` 并重启。CUDA 版本应匹配驱动，详见 [模型说明](docs/optional-local-models.md)。Windows Qwen 推理尚未独立实机验收，首次试听需检查环境。

## 来源与许可证

上游基线：`97cc34c58366357759f0eb74553143f6c08bace7`。沿用 [AGPL-3.0-only](LICENSE)、[附加条款](ADDITIONAL_TERMS.md)、[署名](AUTHORS.md)、[商标规则](TRADEMARKS.md)、[第三方声明](THIRD_PARTY_NOTICES.md)。社区版与官方发行和服务不应混淆。
