# 给 OCV 作者的接入说明

本仓库交付附加功能，用户主程序仍使用官方 OCV。基线是官方 2026.10.12.1，提交 `fdb4ba1b880b60d3019f4bf31db2a767fdb897d8`。现阶段没有完整通用插件执行接口，以下是不能仅靠插件目录完成的部分，以及具体需要接入的位置。

## 官方已具备与尚缺能力

官方 `plugins/README.md` 说明只读取清单，不执行任意插件代码；通用 TTS、Agent、提示词处理和后处理扩展点仍属后续计划。

- `backend/app/main.py::_plugin_manifest_items` 支持元数据和开关，没有通用后端路由或网页扩展面板加载器。
- `backend/app/director_extensions.py::read_profile` 只接受 `medical_paper` ID，题材规则无法直接注册新的导演模式。
- `backend/app/codex_bridge.py::plugin_valid` 只识别 `codex_bridge`，它是宿主内置的受控制作桥，不能用改 ID 的办法加载本包。

本包清单因此保持 `placeholder`，不让用户误以为启用即完成接入。下文接口是建议，不是当前官方已经实现的功能。

## 优先接入点

| 增强 | 附加源码 | 宿主改动位置 | 建议提供的稳定接口 |
|---|---|---|---|
| Qwen 音色、声音资产 | `qwen3_local.py`、`qwen3_runner.py`、`voice_design*.py` | `main.py` 的 GenerateRequest、路由及预检查；`module1_agent_director.py` 的配音调度；`tts_editor.py` | 可选 TTS provider 注册、健康检查、设计/克隆/合成、任务取消、经授权的声音资产存取 |
| 角色与声音绑定 | `video_project_cast.py`、`video_cast_planning.py`、`video_dialogue.py` | `main.py` 请求字段；`video_studio.py` 项目保存、创建、修订；`VideoStudio.vue` | 角色 ID、项目修订号、声音绑定及引用资产读写；使用当前登录用户，不另建账号体系 |
| 故事与题材导演 | `story_*.py`、`video_native_story.py` | `director_extensions.py` 模式注册；`video_agents.py`、`video_director_contracts.py`、`story_agents.py` | 自定义导演 ID、按阶段策略注册、语言模型调用、采用稿与规划结果契约；通用兜底保留 |
| 一键阶段与恢复 | `one_click.py`、`video_review_policy.py` | `main.py` 启停生命周期；`pipeline.py`、`video_generation.py`、`video_studio.py` | 读取阶段、受控提交、暂停/恢复/取消、任务回执、修订冲突；禁用插件不丢正在运行任务 |
| H3 输入与对白 | `h3_dialogue.py`、`video_inputs.py`、`video_native_preparation.py` | `h3_prompt_agent.py`、`comfyui_bridge.py`、`video_generation.py` | 提示词编译、实际输入映射、提交冻结、共享队列身份及取消恢复；不能靠插件直接改记录 |
| 旁白与对白后期 | `video_external_*.py`、`video_environment_audio.py`、`video_manual_dub.py` | `video_export.py`、`tts_segmentation.py`、`tts_editor.py` | 独立音轨、说话者/时间段、导出处理器；旁白不作为角色参考音频进入 H3 |
| 网页面板 | `VoiceDesignPanel.vue`、`ProjectCastPanel.vue`、`StoryWorkspace.vue`、`DubTimeline.vue` 等 | `Studio.vue`、`VideoStudio.vue`、`useStudio.js`、`useWorkspace.js` | 编译时或正式动态网页插槽、菜单/面板注册、请求与项目事件；本包没有绕过官方构建器加载 Vue 源码 |
| 画风预览 | `visualStyleCatalog.js`、`VisualStyleGallery.vue`、`frontend/public/style-previews` | 网页静态资源挂载与构建 | 为插件提供资源 URL；当前源码引用 `/style-previews/`，未接入时不可假定这些资源已有宿主路由 |

表中后端源码均位于 `plugins/ocv_community/extension_src/backend/app/`，网页源码位于同目录的 `frontend/src/`。

## 准确文件清单与补丁

[host-changes.tsv](host-changes.tsv) 列出 33 个官方文件的实际差异，`host_changes/` 包含各文件独立补丁；[file-map.json](file-map.json) 给出完整来源、哈希与导入依赖。完整历史差异在 [official-2026.10.12.1.patch](official-2026.10.12.1.patch)。所有差异只针对上述固定基线，不意味着以后官方版本能无冲突应用。

`auth.py` 的差异仅是可选 cookie 名称隔离，与插件功能接入无关，可以不采用。官方账号登录、计费、权限体系无需改写。其余差异是原微调版的衔接参考，作者应提取所需稳定扩展点；不用照抄全部核心修改。

普通安装器不应用这些补丁。作者可在独立开发 checkout 核对：

```bash
git checkout fdb4ba1b880b60d3019f4bf31db2a767fdb897d8
git apply --check /path/to/plugin-repo/integration/official-2026.10.12.1.patch
```

这只检查已有文件差异。若复现原微调实现，还要按 `addon-files.txt` 将附加模块放回对应原位置、安装 pypinyin==0.55.0 并重新构建网页；这属于宿主开发验证，会修改主程序，不能称为纯插件安装。

## 需要作者处理的路径与依赖

当前抽取模块仍使用 `.auth`、`.video_studio` 等相对导入，`Path(__file__).parents[2]` 及 `PROJECT_ROOT` 定位，也会调用宿主的任务锁、原子保存和 GPU 守锁。它们不能简单搬到另一个目录后直接运行。

作者提供插件 API 后，应把这些依赖改为明确注入的宿主服务：当前用户、项目目录与修订、资产存取、语言模型请求、任务队列/GPU租约、取消和导出。不要用 sys.path 覆盖官方模块或 monkey patch 改写已加载宿主函数。

额外依赖包括可选 Qwen 独立环境及相关模型，配音校验使用的 pypinyin；声音分离还依赖对应外部运行环境。缺依赖时应只禁用对应能力并提示安装，官方原制作路线仍然可用。环境、模型和宿主配置不得随插件分发。

## 推荐落地顺序与验收

先接入 Qwen provider 与声音面板，再开放导演模式和项目扩展，最后接一键调度与后期。每个阶段验证官方原有制作入口、插件启停、当前用户权限、项目切换、完成素材保留及失败恢复。

插件禁用不得改变官方默认路线；已有插件项目保留并提示所需能力，不静默换音色或规划。只有新装官方 OCV 能加载插件并完成实际操作后，才标记对应能力为可用。当前包的检查成功只表示登记或声明存在，不代表已完成实片验收。
