# rsync-tui

一个基于 Python 和 prompt_toolkit 的跨平台远程文件管理器，支持 SSH 目录浏览、批量 rsync 下载、断点续传、实时进度输出。

## 特性
- 远程 SSH 目录浏览与文件选择
- 批量下载，支持断点续传（rsync --partial）
- 实时显示 rsync 原始输出和进度
- 支持 Linux/macOS/WSL
- 纯命令行 TUI，键盘友好

## 快速开始

1. 安装依赖：
   ```bash
   pip install prompt_toolkit
   ```

2. 运行：
   ```bash
   python rsync-tui.py <远程主机IP或域名> --user <用户名> --port <端口>
   ```
   例如：
   ```bash
   python rsync-tui.py 192.168.1.100 --user root --port 22
   ```

3. 操作说明：
   - 上下方向键：移动光标
   - 空格：选择/取消文件
   - 回车：进入目录/返回上级
   - D：批量下载选中文件/文件夹
   - Q：退出并强制终止所有传输
   - 右侧窗口实时显示 rsync 输出

## 依赖
- Python 3.7+
- prompt_toolkit
- rsync (本地和远端均需安装)
- ssh

## 开源协议
MIT License
