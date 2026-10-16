# 验证范围

v0.1.3 的验证只针对附加源码、包清单、安装保护和发布范围，不应用宿主补丁，不修改官方目录，不请求模型。

```powershell
python -m unittest discover -s tests
python tools/verify_source_bundle.py
python tools/verify_source_bundle.py --official-root "C:\official-ocv-source"
python tools/check_release_scope.py
python tools/check_release_privacy.py
```

第三条是可选的官方源码对照：检查附加模块不与官方文件重名，并使用官方清单读取函数确认本包可显示、可停用且仍为 framework_only；不会启动官方应用。后两条检查 Git 暂存索引，发布前应明确暂存审查过的文件。

回归包含安装不覆盖宿主/配置/项目、拒绝已有插件目录、包完整性与路径检查、Windows 换行兼容、缺接口时不声称可用、隐私扫描不回显凭据、拒绝宿主补丁和图像服务副本、附加修复保留全部身份参考（含当前图共八张及更多的宿主兼容输入）。输入组装测试不代表任意图像服务都支持这些数量。

**尚未验收：增强模块在未修改的官方宿主中执行，以及实际成片质量。** 相应功能仍待宿主接口，不以源码测试或历史私人项目的成片代替。接入缺口见[作者说明](../integration/AUTHOR_GUIDE.md)。
