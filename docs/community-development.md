# 开发修改地图

| 行为 | 主要入口 |
|---|---|
| 故事与题材 | `backend/app/story_writer.py`、`story_development.py`、`story_genre_profiles.py`、`story_production.py` |
| 一键阶段/恢复 | `backend/app/one_click.py` |
| 角色和音色 | `backend/app/video_project_cast.py`、`video_cast_planning.py`、`voice_design.py`、`voice_design_agent.py` |
| Qwen/模型安装 | `backend/app/qwen3_local.py`、`qwen3_runner.py`、`scripts/setup_qwen3.py`、`prepare_qwen3_models.py` |
| 动态规划和时长 | `backend/app/video_native_story.py`、`video_agents.py`、`video_timing_policy.py`、`video_dynamic_review.py` |
| H3 提示词/输入 | `backend/app/h3_prompt_agent.py`、`h3_dialogue.py`、`video_native_preparation.py`、`video_inputs.py` |
| 配音和对齐 | `backend/app/video_external_dialogue.py`、`video_external_narration.py`、`video_external_alignment.py`、`video_environment_audio.py` |
| 后期/手工提交 | `frontend/src/components/DubTimeline.vue`、`backend/app/video_manual_dub.py`、`video_manual_h3.py` |
| 图片/回执/恢复 | `backend/app/openai_image_transport.py`、`video_native_image_repair.py`、`video_studio.py` |
| 网页操作 | `frontend/src/components/VideoStudio.vue`、`VoiceDesignPanel.vue`、`ProjectVoicePanel.vue` |
| 登录和启动 | `backend/app/auth.py`、`scripts/start_public.py`、`.env.example` |

保留项目修订号、稳定角色 ID、完成素材和任务身份。不要直接修改运行记录绕过流程，也不要加入某个故事的专用硬编码。

验证：后端 `python -m unittest tests.<模块>`；前端 `npm --prefix frontend test` 和 `npm --prefix frontend run build`。模型与付费接口另做实片验收，测试通过不代表口型合格。

这是独立源码快照，不包含私人 Git 历史。继续分发需保留许可证、署名和上游来源。
