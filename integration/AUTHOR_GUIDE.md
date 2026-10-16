# 增强功能接入说明

本包只提供附加模块，不交付官方核心补丁。官方已有功能继续由官方维护，参考图数量及模型限制由宿主与当前模型决定，插件不写死替代值。

## 当前宿主接口缺口

核对官方 `cb8edf14b13c246797b6905dee5afaafce788d6f` 的插件说明：通用插件默认只展示清单，导演配置与受控制作桥有特定接入槽；这不等于开放任意 TTS、故事工作台或网页代码。本包因此保持 `placeholder / requires_host_api`，没有借用其他插件 ID 或覆盖宿主函数。

下面是实现附加功能所需的接口，不是要求作者照抄私人版本或修改全部列出的文件。

| 增强功能 | 本包模块 | 相关官方宿主位置 | 所需接口 |
|---|---|---|---|
| Qwen 音色设计、复用、合成 | `qwen3_local.py`、`qwen3_runner.py`、`voice_design*.py` | `backend/app/main.py`、配音调度及 `tts_editor.py` | 可选 TTS provider 注册、健康检查、任务取消、声音资产存取 |
| 角色声音管理 | `video_project_cast.py`、`video_cast_planning.py`、`video_dialogue.py` | 项目与角色资产 API、`VideoStudio.vue` | 当前用户下的角色 ID、声音绑定、修订检查、授权资产读写 |
| 故事工作台与题材规则 | `story_*.py`、`video_native_story.py` | 故事 API、`director_extensions.py`、规划 Agent 调度 | 故事与导演阶段注册、采用稿契约、无上下文语言模型调用服务 |
| 一键协调与恢复 | `one_click.py`、`video_review_policy.py` | 任务生命周期、项目阶段 API | 查询阶段、受控提交、暂停/恢复/取消、任务回执和修订冲突 |
| 对白、旁白及 H3 控制 | `h3_dialogue.py`、`video_inputs.py`、`video_native_preparation.py` | H3 提交、现有图像与视频服务 | 实际输入映射、冻结与回执、模型能力预检；图像容量完全沿用宿主 |
| 后期音轨 | `video_external_*.py`、`video_environment_audio.py`、`video_manual_dub.py` | 导出与字幕 API | 独立音轨、说话者与时间段、受控后处理注册 |
| 网页面板 | `VoiceDesignPanel.vue`、`ProjectCastPanel.vue`、`StoryWorkspace.vue`、`DubTimeline.vue` 等 | 官方页面插件插槽 | 面板与菜单注册、当前用户请求服务、项目事件和资源 URL |

后端模块位于 `plugins/ocv_community/extension_src/backend/app/`，网页模块位于同目录 `frontend/src/`。[file-map.json](file-map.json)记录附加文件及来源，[addon-files.txt](addon-files.txt)列出路径。没有附带宿主文件、官方差异补丁或自动覆盖脚本。

## 接入约定

将现有宿主相对导入、项目目录定位、任务锁、原子保存和 GPU 锁改为明确注入的宿主服务。不得通过 `sys.path` 替换官方模块、monkey patch 已加载函数，或把本包源码覆盖回官方目录。

参考图全部交给官方现有服务：按实际顺序传递，不截断、不改上限、不更换模型；超出当前模型能力时使用官方预检错误。附加修复模块不自行定义参考图总数。旧版专用图像传输和输出回执实现已移除；其能力应通过宿主服务提供。

继续使用官方登录与当前用户权限，不另建账号体系。插件禁用不改变官方默认路线，不自动删除项目或素材；涉及插件能力的旧任务应明确提示缺少能力，不能悄悄换音色或重新规划。

额外模型、依赖和配置按需准备，不随源码发布。缺少依赖时只禁用对应增强功能。当前 `plugin_api.example.json` 是接口建议，不是已实现的加载协议，不应复制为“接口已经完成”的证明。

## 验收顺序

先接入并实际验证 Qwen provider 与声音面板，再接故事工作台和项目扩展，最后验证一键调度及后期。逐项检查启停、用户权限、项目切换、素材保留、失败恢复及官方原入口仍可用。只有在未改写官方功能的宿主中实际完成操作，才将对应增强标记为可用。
