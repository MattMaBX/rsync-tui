# rsync-tui: 远程文件管理与断点续传 TUI 工具
# Author: Matt (https://github.com/你的用户名)
# License: MIT
#
# 一个基于 prompt_toolkit 的跨平台远程文件管理器，支持 SSH 目录浏览、批量 rsync 下载、断点续传、实时进度输出。

import argparse
import asyncio
import os
import re
import signal
import subprocess
import sys
import threading
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

# 检查远端是否安装rsync，如无则自动安装

def check_and_install_rsync(user, host, port):
    """检查远端是否安装rsync，如无则自动尝试安装。"""
    check_cmd = [
        'ssh', '-p', str(port), f'{user}@{host}', 'command -v rsync'
    ]
    result = subprocess.run(check_cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return True
    # 尝试自动安装（支持apt/yum）
    for install in [
        'sudo apt-get update && sudo apt-get install -y rsync',
        'sudo yum install -y rsync'
    ]:
        install_cmd = [
            'ssh', '-p', str(port), f'{user}@{host}', install
        ]
        res = subprocess.run(install_cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True
    print('自动安装rsync失败，请手动安装')
    sys.exit(1)

def get_remote_home(user, host, port):
    """获取远程主机的家目录。"""
    ssh_cmd = [
        'ssh', '-p', str(port), f'{user}@{host}', 'echo $HOME'
    ]
    result = subprocess.run(ssh_cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return os.path.normpath(result.stdout.strip())
    return '/'

# 解析ls输出

def parse_ls_output(output):
    """解析ls -lA输出，返回文件条目列表。"""
    lines = output.strip().split('\n')
    entries = []
    for line in lines:
        if line.startswith('total'):
            continue
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        perms, links, user, group, size, date, time, name = parts
        ftype = perms[0]
        entries.append({
            'ftype': ftype,
            'perms': perms,
            'user': user,
            'group': group,
            'size': size,
            'date': date,
            'time': time,
            'name': name
        })
    return entries

# 获取远程目录内容

def get_entries(user, host, port, path):
    """获取远程目录内容，返回文件条目列表。"""
    ls_bins = ['/bin/ls', '/usr/bin/ls', 'ls']
    ls_cmds = []
    for bin in ls_bins:
        ls_cmds.append(f'{bin} -lA --time-style=long-iso {path}')
        ls_cmds.append(f'{bin} -lA {path}')
    result = None
    for ls_cmd in ls_cmds:
        ssh_cmd = [
            'ssh', '-p', str(port), f'{user}@{host}', ls_cmd
        ]
        try:
            result = subprocess.run(ssh_cmd, check=True, capture_output=True, text=True)
            break
        except subprocess.CalledProcessError:
            result = None
            continue
    if not result:
        return []
    entries = parse_ls_output(result.stdout)
    if path != '/':
        entries = [{
            'ftype': 'd', 'perms': 'drwxr-xr-x', 'user': '', 'group': '', 'size': '', 'date': '', 'time': '', 'name': '..'
        }] + entries
    return entries

async def rsync_pull(user, host, remote_path, local_path, port, follow_symlinks, set_message_threadsafe, active_procs=None, append_output=None):
    """
    使用rsync拉取远程文件/目录，支持断点续传和实时进度。
    """
    remote_path = os.path.normpath(remote_path)
    local_path = os.path.normpath(local_path)
    ssh_cmd = f"ssh -p {port} -o LogLevel=ERROR -o StreamLocalBindUnlink=yes -o ServerAliveInterval=30"
    rsync_cmd = [
        'stdbuf', '-oL', 'rsync', '-avz', '--progress', '--partial', '-e', ssh_cmd
    ]
    if follow_symlinks:
        rsync_cmd.append('-L')
    rsync_cmd.append(f'{user}@{host}:{remote_path}')
    rsync_cmd.append(local_path)
    def parse_file_progress(line):
        m = re.search(r'to-chk=(\d+)/(\d+)', line)
        if m:
            remain = int(m.group(1))
            total = int(m.group(2))
            done = total - remain
            return f'文件进度: {done}/{total}'
        return None
    proc = subprocess.Popen(rsync_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, preexec_fn=os.setsid)
    if active_procs is not None:
        active_procs.append(proc)
    def reader():
        for line in proc.stdout:
            if append_output:
                append_output(line.rstrip())
            msg = parse_file_progress(line)
            if msg:
                set_message_threadsafe(msg)
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    while t.is_alive() or proc.poll() is None:
        await asyncio.sleep(0.1)
    t.join()
    rc = proc.wait()
    if active_procs is not None:
        try:
            active_procs.remove(proc)
        except Exception:
            pass
    if rc == 0:
        set_message_threadsafe('传输完毕')
    else:
        set_message_threadsafe('同步失败')

# 交互式浏览与选择

async def interactive_browse(user, host, port, start_path):
    """
    主交互界面，支持远程目录浏览、文件选择、批量下载、实时进度输出。
    """
    import functools
    cwd = start_path
    selected = 0
    selected_files = set()
    message = ['']
    active_procs = []
    output_lines = []
    style = Style.from_dict({
        'selected': 'reverse',
        'directory': 'ansiblue',
        'symlink': 'ansimagenta',
        'file': '',
        'marked': 'bold underline',
        'message': 'bg:#444444 #ffffff',
        'output': 'bg:#222222 #00ff00',
    })
    page_size = 20
    page_start = 0
    def get_lines(entries, selected, marked):
        lines = []
        end = min(page_start + page_size, len(entries))
        for idx in range(page_start, end):
            entry = entries[idx]
            t = entry['ftype']
            n = entry['name']
            color = 'class:file'
            if t == 'd':
                color = 'class:directory'
            elif t == 'l':
                color = 'class:symlink'
            prefix = '➤ ' if idx == selected else '  '
            mark = '[*] ' if n in marked else '    '
            style_line = 'class:marked' if n in marked else color
            if idx == selected:
                lines.append(('class:selected', prefix + mark + n + '\n'))
            else:
                lines.append((style_line, prefix + mark + n + '\n'))
        return FormattedText(lines)
    entries = get_entries(user, host, port, cwd)
    kb = KeyBindings()
    body_control = FormattedTextControl(lambda: get_lines(entries, selected, selected_files))
    message_control = FormattedTextControl(lambda: message[0], style='class:message')
    class OutputControl(FormattedTextControl):
        def __init__(self, lines):
            super().__init__(self.get_text, style='class:output')
            self.lines = lines
        def get_text(self):
            return '\n'.join(self.lines)
    output_control = OutputControl(output_lines)
    body_window = Window(content=body_control, always_hide_cursor=False)
    message_window = Window(content=message_control, height=1, style='class:message')
    output_window = Window(content=output_control, width=60, style='class:output', wrap_lines=True)
    def refresh(entries, selected, marked):
        body_control.text = get_lines(entries, selected, marked)
        message_control.text = message[0]
    def set_message_threadsafe(msg):
        message[0] = msg
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(functools.partial(refresh, entries, selected, selected_files))
        except Exception:
            refresh(entries, selected, selected_files)
    def append_output(line):
        output_lines.append(line)
        if len(output_lines) > 30:
            del output_lines[0]
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(app.invalidate)
        except Exception:
            pass
    def update_entries():
        nonlocal entries, page_start, selected
        entries = get_entries(user, host, port, cwd)
        page_start = 0
        selected = 0
    @kb.add('up')
    def _(event):
        nonlocal selected, page_start
        if selected > 0:
            selected -= 1
            if selected < page_start:
                page_start = max(0, selected)
            refresh(entries, selected, selected_files)
    @kb.add('down')
    def _(event):
        nonlocal selected, page_start
        if selected < len(entries) - 1:
            selected += 1
            if selected >= page_start + page_size:
                page_start = selected - page_size + 1
            refresh(entries, selected, selected_files)
    @kb.add('pageup')
    def _(event):
        nonlocal selected, page_start
        if selected > 0:
            selected = max(0, selected - page_size)
            page_start = max(0, page_start - page_size)
            refresh(entries, selected, selected_files)
    @kb.add('pagedown')
    def _(event):
        nonlocal selected, page_start
        if selected < len(entries) - 1:
            selected = min(len(entries) - 1, selected + page_size)
            page_start = min(max(0, len(entries) - page_size), page_start + page_size)
            if selected >= page_start + page_size:
                page_start = selected - page_size + 1
            refresh(entries, selected, selected_files)
    @kb.add('space')
    def _(event):
        entry = entries[selected]
        n = entry['name']
        if n in selected_files:
            selected_files.remove(n)
        else:
            selected_files.add(n)
        refresh(entries, selected, selected_files)
    @kb.add('enter')
    def _(event):
        nonlocal cwd, entries, selected
        entry = entries[selected]
        if entry['name'] == '..':
            cwd = os.path.dirname(cwd.rstrip('/')) or '/'
            update_entries()
            selected = 0
            refresh(entries, selected, selected_files)
        elif entry['ftype'] == 'd':
            cwd = os.path.join(cwd, entry['name'])
            update_entries()
            selected = 0
            refresh(entries, selected, selected_files)
    @kb.add('d')
    async def _(event):
        for n in selected_files:
            remote_path = os.path.join(cwd, n)
            local_path = '.'
            follow_symlinks = False
            await rsync_pull(user, host, remote_path, local_path, port, follow_symlinks, set_message_threadsafe, active_procs, append_output)
        selected_files.clear()
        refresh(entries, selected, selected_files)
    @kb.add('<any>')
    def _(event):
        if message[0]:
            set_message_threadsafe('')
    @kb.add('q')
    def _(event):
        for proc in active_procs:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
        event.app.exit()
        import os as _os
        _os._exit(0)
    root_container = Frame(HSplit([
        VSplit([
            body_window,
            output_window
        ]),
        message_window
    ]), title=lambda: f'远程目录: {cwd} (空格选中, 回车进目录, D批量下载, Q退出)')
    layout = Layout(root_container)
    app = Application(layout=layout, key_bindings=kb, style=style, full_screen=True)
    refresh(entries, selected, selected_files)
    # 定时器强制刷新UI，防止output_control未被重绘
    import threading as _threading
    def periodic_refresh():
        try:
            app.invalidate()
        except Exception:
            pass
        _threading.Timer(0.2, periodic_refresh).start()
    periodic_refresh()
    await app.run_async()

def main():
    """命令行入口。"""
    parser = argparse.ArgumentParser(description='rsync-tui: 远程文件管理器')
    parser.add_argument('host', help='远程主机IP或域名')
    parser.add_argument('--user', default='root', help='ssh用户名，默认root')
    parser.add_argument('--port', type=int, default=22, help='ssh端口，默认22')
    args = parser.parse_args()
    check_and_install_rsync(args.user, args.host, args.port)
    home = get_remote_home(args.user, args.host, args.port)
    asyncio.run(interactive_browse(args.user, args.host, args.port, home))

if __name__ == '__main__':
    main()
