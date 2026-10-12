# 可选本地模型：Qwen3音色设计与H3视频

这些增强功能按需启用。沿用官方配音、已有音频或原视频接口时，不需要安装Qwen3或H3。模型部署在运行OCV后端的工作站，操作电脑只需浏览器；浏览器不会直接使用操作电脑的显卡。

## Qwen3-TTS音色设计与固定声音复用

音色设计使用官方 `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign`；确认音色后，使用 `Qwen/Qwen3-TTS-12Hz-1.7B-Base` 和已保存的参考音频、原文来复用声音。每段重新设计会引入声音变化，因此制作阶段复用已采用的声音。

建议为Qwen单独建立Python环境，避免覆盖官方IndexTTS所用的依赖。当前接入在Linux、Python 3.11、PyTorch/torchaudio 2.10.0 CUDA 13.0、`qwen-tts==0.1.1`上完成实机验证；推理使用BF16和PyTorch SDPA。其他显卡、驱动和Windows模型推理仍需独立验证。PyTorch版本与CUDA轮子应按设备匹配，不复制其他机器的虚拟环境。

先依照 [Qwen3-TTS官方说明](https://github.com/QwenLM/Qwen3-TTS) 在独立环境安装运行库，再准备模型。目录结构：

```text
<QWEN3_TTS_ROOT>/
  venv/                       # Linux解释器位于bin/python，Windows位于Scripts/python.exe
  models/
    Qwen3-TTS-12Hz-1.7B-VoiceDesign/
    Qwen3-TTS-12Hz-1.7B-Base/
  download-verified.json      # 由下方校验脚本生成
```

从OCV项目根运行：

```sh
python scripts/prepare_qwen3_models.py --root "<QWEN3_TTS_ROOT>" --download
```

显式加`--download`才下载，文件来自ModelScope的官方Qwen命名空间，按源码清单固定修订、大小和SHA256校验；总文件大小约9.1GB，不包含Python环境。已经下载同版本时省略`--download`只校验本地文件。已存在但不匹配的文件不会被覆盖，中断的`.partial`文件需检查后再处理。清单适用于此接入验证的模型版本，不代表自动跟进官方最新版。

在后端`.env`中配置实际绝对路径：

```dotenv
QWEN3_TTS_ROOT=<模型与运行环境根目录>
QWEN3_TTS_PYTHON=<独立环境Python的绝对路径>
```

重新启动OCV后，在声音设计面板输入声音描述和参考句，试听后采用。网页健康检查不加载模型、不消耗显卡；点击设计或配音时才由独立进程推理，模型读取为离线模式。完整模型文件校验不等于驱动/运行库可用，第一次试听仍是必要验证。

## H3与共享显卡

在OCV的ComfyUI工作台填写后端工作站可访问的ComfyUI地址，导入适合自己的H3工作流，并映射提示词、图片、时长、分辨率和输出节点。首帧、首尾帧、构图与多参考是不同输入合同，不应只改下拉框名称而不改工作流连线。

使用现有共享GPU guard的部署，管理员可以配置：

```dotenv
OCV_COMFYUI_GUARD_SCRIPT=<已有H3 guard绝对路径>
INDEXTTS25_GPU_GUARD=<已有TTS guard绝对路径>
INDEXTTS25_GPU_GUARD_SHA256=<该TTS guard已核验的SHA256>
```

上述guard是部署环境提供的资源协调程序，不随OCV源码自动安装。Qwen与IndexTTS共用TTS适配器；H3已配置共享guard而TTS guard缺失时，会拒绝无保护的本地语音推理。普通独占GPU部署未配置guard时沿用原本地执行路径。

如果使用社区Sol加速节点，参见[长序列索引修复](patches/sol-attn-int64.md)。此补丁不是使用OCV的前置条件，也不会在启动时自动修改ComfyUI。8步加速预设仍需检查动作、角色、光照和声音；工作流执行成功不等于成片质量合格。

模型、密钥、账号库、声音参考和成片保存在部署环境，不包含在公开源码里。
