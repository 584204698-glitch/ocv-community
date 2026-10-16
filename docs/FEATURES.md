# 附加功能地图

这里列出的是本包的增强源码，不是宣称启用清单后已经运行。除独立 Qwen 安装辅助外，均仍需宿主执行接口。

| 用户能力 | 主要源码 |
|---|---|
| 设计角色音色、复用已采用声音 | `voice_design.py`、`voice_design_agent.py`、`qwen3_local.py`、`qwen3_runner.py`、`VoiceDesignPanel.vue` |
| 在项目中管理角色与声音 | `video_project_cast.py`、`video_dialogue.py`、`ProjectCastPanel.vue`、`ProjectVoicePanel.vue` |
| 创意展开、改稿、采用稿追踪 | `story_workspace.py`、`story_development.py`、`story_writer.py`、`story_genre_profiles.py`、`StoryWorkspace.vue` |
| 多角色对白、独立旁白与对口型输入 | `h3_dialogue.py`、`video_native_preparation.py`、`video_external_dialogue.py`、`DialogueEditor.vue` |
| 一键阶段协调与局部恢复 | `one_click.py`、`productionRecovery.js`、`ProductionRecovery.vue`、`OneClickStatus.vue` |
| 自定义 H3 提示与动态节奏 | `ManualH3Prompt.vue`、`DynamicPacingSelector.vue`、`dynamicPacing.js` |
| 后期手工配音与音轨编辑 | `video_manual_dub.py`、`video_environment_audio.py`、`DubTimeline.vue`、`AfterAudioSettings.vue` |
| 画风选择预览 | `visualStyleCatalog.js`、`VisualStyleGallery.vue`、`frontend/public/style-previews/` |

后端源码目录：`plugins/ocv_community/extension_src/backend/app/`。网页源码目录：`plugins/ocv_community/extension_src/frontend/src/`。具体接口缺口见[接入说明](../integration/AUTHOR_GUIDE.md)。

官方参考图容量、选择器、模型 API、队列与生产桥不列为本包增强，不在本包重新实现。私人账号栏目及其提示词不公开。
