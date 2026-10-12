# OCV 社区增强插件 · 接入预览

用户继续使用[官方 OCV](https://github.com/IFRIT-Zhou/One-Click-VidGen)。本仓库只提供附加功能源码、安装辅助和宿主接入说明，不再分发另一套 OCV 主程序。

**当前为开发者接入预览。** 官方 2026.10.12.1 能展示、启停插件清单，但尚不能执行通用 TTS、Agent 或网页插件。安装本包只登记插件，不会自动出现全部增强功能。需要作者接入的地方已列在[作者接入说明](integration/AUTHOR_GUIDE.md)，并提供精确逐文件补丁；普通用户无需自行改主程序。

## 附加内容

- 可选 Qwen3-TTS 音色设计与参考声音复用、按角色设计声音、声音资产管理。
- 故事工作台、固定题材导演规则及通用兜底。
- 多角色对白、独立旁白、一键制作与局部恢复、手工 H3 提示词和后期音轨编辑。
- Qwen 独立环境与模型安装辅助，模型权重不在仓库内。

这是从已有社区增强中抽出的 **107 个附加源码文件**。它们保留原有宿主相对导入，位于 `extension_src`，用于作者接入；不能作为独立 Python 包直接导入。33 份宿主差异中，32 份涉及增强衔接，账号 cookie 隔离的 1 份为可选历史差异。宿主依赖、接口和文件一一对应，不以添加清单冒充已运行插件。

## 安装与检查

需要 Python 3.10 或更高版本；登记和检查只用标准库，不加载模型。

```powershell
python tools/install_plugin.py --ocv-root "C:\OCV"
python tools/install_plugin.py --ocv-root "C:\OCV" --install
python tools/check_host.py --ocv-root "C:\OCV"
```

首条命令只预览。第二条只复制 `plugins/ocv_community`，不写配置、项目、模型或核心源码；同名目录已经存在时会停止，避免覆盖用户修改。登记后可在 OCV 插件面板看到本包。功能是否接入以检查报告和作者完成的接口为准。

需要单独准备 Qwen 环境时，见[模型安装说明](plugins/ocv_community/README.md)。安装模型不等于官方已接入配音入口。

## 目录

| 路径 | 内容 |
|---|---|
| `plugins/ocv_community` | 清单、附加源码、Qwen 安装工具 |
| `integration/AUTHOR_GUIDE.md` | 当前不能直接插件化的原因、作者需要改的位置 |
| `integration/host-changes.tsv` | 33 个宿主文件与逐文件补丁对应 |
| `integration/file-map.json` | 源文件、来源提交、哈希和 Python 导入依赖 |
| `integration/official-2026.10.12.1.patch` | 作者参考的完整历史衔接差异，不自动应用 |
| `tools`、`tests` | 插件登记、能力检查和安装保护验证 |

现有完整微调版保留在 `v2026.10.12-rc2` 标签，供追溯。此包不会更换正在运行的私人实例。没有模型权重、密钥、账号、音色资产或媒体。

## 验证与许可证

安装保护、宿主能力检查、附加源码语法、补丁与固定官方基线的对应关系另行验证。此前完整微调版的实片及测试结果不作为本插件已接入官方的证据。

来源官方 2026.10.12.1（`fdb4ba1`），抽取自社区源码 `4ded43a`。沿用 [AGPL-3.0-only](LICENSE)、[附加条款](ADDITIONAL_TERMS.md)、[原作者署名](AUTHORS.md)、[商标规则](TRADEMARKS.md) 和[第三方声明](THIRD_PARTY_NOTICES.md)。社区扩展不代表官方发行。
