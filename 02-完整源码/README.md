# 影库 Windows v2.8.2

完整使用说明在上级目录的 `先读这里.md`；架构与后续开发说明在 `03-开发文档`。

开发快捷入口：`开发环境准备.bat` → `源码启动.bat` → `运行测试.bat` → `生成Windows运行版.bat`。

需要 Windows 10 / 11 x64 和 Python 3.12 x64，完整交付包自带离线开发安装材料。运行版无需安装 Python。

源码与运行版默认使用 `%LOCALAPPDATA%\YingKu`，普通分区使用旁边的 `YingKu-Public`。请通过环境变量使用隔离资料库做实验；测试脚本会自动隔离。

Windows 打包仅使用 `YingKu-Windows.spec`。不选择 `影库-Mac.spec`，不携带 Mac 原生执行文件，不提交本机 `.venv`。更多开发规则见 `AGENTS.md`。
