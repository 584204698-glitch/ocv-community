# OCV 社区扩展源码

供[官方 OCV](https://github.com/IFRIT-Zhou/One-Click-VidGen)作者和开发 AI 复用的可选增强模块。仓库只放扩展源码、必要安装与检查工具；功能说明和接入位置统一在本页。

**当前为接入预览，不是安装即用的完整插件。** 已核对官方 `cb8edf14b13c246797b6905dee5afaafce788d6f`（2026.10.14.1）：通用清单支持展示和启停，相应故事、TTS、面板执行接口尚需接入。清单登记不等于下表能力已运行。官方新增的参考图容量、重绘选择器、模型适配和任务队列继续使用官方实现。

## 提供哪些增量

代码根目录：[`plugins/ocv_community/extension_src/`](plugins/ocv_community/extension_src/)。下列路径均相对该目录；这些是接入源码及预期能力，不是未经验证的质量或性能承诺。

| 增量能力 | 优先阅读的现成代码 | 官方接入位置 / 需要的服务 |
|---|---|---|
| 可选 Qwen3 音色设计、参考声音复用 | `backend/app/qwen3_local.py`、`qwen3_runner.py`、`voice_design_agent.py` | TTS provider 注册、健康检查、GPU任务取消；独立环境准备工具可单独使用 |
| 项目内角色与声音管理 | `backend/app/video_project_cast.py`、`video_dialogue.py`；`frontend/src/components/ProjectVoicePanel.vue` | 已登录用户的项目、角色、声音资产API与修订检查 |
| 故事创作、审稿、修订与采用稿追踪 | `backend/app/story_workspace.py`、`story_development.py`、`story_audience.py`、`story_genre_profiles.py` | 故事工作台路由、无上下文LLM调用、当前稿版本与采用凭据 |
| 故事结构的原文证据核对 | `backend/app/story_structure_evidence.py` | 可独立使用的纯函数；接收调用方标注，验证逐字引用、顺序和闭环，返回字符位置与计数，不预测留存或给质量分 |
| 多角色对白、独立旁白与音色参考 | `backend/app/h3_dialogue.py`、`video_native_preparation.py`、`video_external_dialogue.py`；`frontend/src/components/DialogueEditor.vue` | 官方H3/音频任务服务、输入回执、说话者及时序，不自行替换模型能力预检 |
| 一键阶段协调、素材保留与局部恢复 | `backend/app/one_click.py`、`video_review_policy.py`；`frontend/src/productionRecovery.js` | 官方项目阶段、任务生命周期、修订锁与提交回执 |
| H3手工提示、动态节奏、画风选择 | `frontend/src/components/ManualH3Prompt.vue`、`DynamicPacingSelector.vue`、`VisualStyleGallery.vue` | 官方前端面板插槽与设置服务；已有角色不随选择自动重绘 |
| 后期配音与音轨编辑 | `backend/app/video_manual_dub.py`、`video_environment_audio.py`；`frontend/src/components/DubTimeline.vue` | 导出、独立音轨、字幕时间轴及受控后处理接口 |

附加文件的机器清单与摘要在 [`integration/file-map.json`](integration/file-map.json)，安装完整性清单在插件目录 `package-files.json`；两者分别服务源码核验和安装，不另维护重复路径表。

## 给作者与开发 AI 的接入顺序

1. 按上表选择一个增强模块，先读代码和相对导入，再对照当前官方代码；官方已经吸收的能力直接复用，不重复接入旧副本。
2. 将宿主相对导入、工作目录、项目读写、任务锁、GPU锁、模型调用改为明确的宿主服务依赖。官方入口通常在 `backend/app/main.py`、`tts_editor.py`、导演阶段调度及 `frontend/src/components/VideoStudio.vue`；不要把本目录覆盖回这些位置。
3. 优先接入 Qwen provider 和声音面板，再接故事工作台、对白与一键调度。参考接口声明见 [`integration/plugin_api.example.json`](integration/plugin_api.example.json)，只是建议合同，不是现成加载协议。
4. 继续用官方登录、当前用户权限、资源URL、项目版本和任务回执。逐项验证启停、项目切换、失败恢复和原入口，再把对应功能标为可用。

官方尚缺的接口：可选TTS provider注册、授权资产服务、故事/导演阶段注册、任务生命周期、前端面板注册、导出音轨注册。通用清单不能代替这些执行钩子；当前保持 `placeholder / requires_host_api`，不使用 monkey patch 或其他插件身份绕过加载限制。

## 安装辅助

Python 3.10+。先预览，再选择登记到官方源码安装目录；已有同名插件时拒绝覆盖。此步骤只新增插件目录，不修改核心、模型环境或项目。

```powershell
python tools/install_plugin.py --ocv-root "C:\OCV"
python tools/install_plugin.py --ocv-root "C:\OCV" --install
python tools/check_host.py --ocv-root "C:\OCV"
```

独立Qwen环境准备（默认只预览；下载约9.1GB，按驱动选择CUDA版本）：

```powershell
python plugins/ocv_community/tools/setup_qwen3.py --root "C:\OCV-Qwen"
python plugins/ocv_community/tools/setup_qwen3.py --root "C:\OCV-Qwen" --install --download --cuda cu128
```

准备工具安装到独立venv，核对固定版本与文件摘要，不自动启动推理或改变OCV环境。

## 检查与发布范围

```powershell
python -m unittest discover -s tests
python tools/verify_source_bundle.py --official-root "C:\official-ocv-source"
python tools/check_release_scope.py
python tools/check_release_privacy.py
```

检查覆盖安装不覆盖宿主/配置/项目、包摘要和路径、Windows换行、源码不与指定官方版本重名、官方清单可展示与停用、隐私与发布范围。最后两项检查Git暂存索引；没有模型请求。结构证据模块测试核对引用和计数，不证明故事好看。**增强模块在未改的官方宿主执行及实际成片仍待逐项验收。**

不发布官方核心、宿主差异补丁、旧维护包、测试成片、私人栏目路线与专有提示词、私人项目或媒体、声音资产、权重、密钥和运行配置；不推送私人主程序历史。每个源码文件只保留一个版本，不附重复源码压缩包和过程报告。保留根目录与独立插件包必需的许可证及署名。

0.1.4整理：合并重复说明、删除重复路径表和私人试片预览素材，保留画风选项；新增独立故事证据核对函数。此前0.1.2的宿主维护补丁已撤下，不应套到新版OCV。当前包没有参考图数量替代上限，不发布官方功能的降级副本。

许可证：[AGPL-3.0-only](LICENSE)、[附加条款](ADDITIONAL_TERMS.md)、[署名](AUTHORS.md)、[商标规则](TRADEMARKS.md)、[第三方声明](THIRD_PARTY_NOTICES.md)。社区扩展不代表官方发行。
