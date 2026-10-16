#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cmdvideo —— 在 cmd / Windows 终端窗口里播放视频

两种画法
--------
block（色块）: 每个字符格用 U+2580（上半块 ▀）表示。前景色涂满格子的上半部、
              背景色涂满下半部，于是一个字符格承载「2 个纵向像素」，最细腻。
chars（字符）: 每个字符格是一个真正的字符，按亮度从阶梯里挑一个字符、
              用前景色表现色彩，就是经典的黑客风彩色字符画。

解码交给 ffmpeg（-f rawvideo -pix_fmt rgb24 直接吐原始像素），
所以不依赖 numpy / OpenCV，纯标准库即可。
"""

import argparse
import ctypes
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

APP = "cmdvideo"
# 版本号：改这里，然后 README 的变更记录和 cmdvideo_setup.iss 里的
# MyAppVersion 跟着一起改（发 Release 时对得上号）。
__version__ = "1.8.1"

# 字符画模式的明暗阶梯（从最暗到最亮）。
# 亮度算出来后按比例取其中一个字符，字符本身的"疏密"就在表现明暗。
RAMP_PRESETS = {
    # 经典 10 级，通用
    "ascii": " .:-=+*#%@",
    # 更细腻的 70 级长阶梯，细节多但吃字体
    "dense": " .'`^\",:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
    # 用方块阴影字符，颗粒感强、最"实"
    "blocks": " \u2591\u2592\u2593\u2588",
    # 只有 8 级，干净利落
    "simple": " .:-=+*#",
    # 点阵风格
    "dots": " \u00b7\u2022\u25cf",
}

MODE_BLOCK = "block"
MODE_CHARS = "chars"
MODE_GRAY = "gray"

# ---------------------------------------------------------------- ffmpeg


def find_ffmpeg():
    """按优先级找一个可用的 ffmpeg。

    为什么找这么多地方：ffmpeg 是个几十 MB 的大家伙，没有被塞进 exe 里
    （EXE 里带的是「投屏」那套 GDI 代码，播放才需要 ffmpeg）。安装包是按
    「cmdvideo.exe + ffmpeg.exe 放同一个目录」装的，所以第一优先永远
    「程序旁边」。但只要有人把 exe 单独拷出来（比如拷到桌面），旁边就没有
    ffmpeg 了 —— 表现就是「点播放没反应」，因为子进程拿不到 ffmpeg 直接
    以退出码 2 结束，界面上什么提示都没有。所以下面多兜几个常见位置。
    """
    # 1) 环境变量
    env = os.environ.get("CMDVIDEO_FFMPEG")
    if env and os.path.exists(env):
        return env
    # 2) 打包成单文件 exe 时，ffmpeg 会被解压到 _MEIPASS
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = os.path.join(base, "ffmpeg.exe")
        if os.path.exists(p):
            return p
    # 3) 程序旁边的 ffmpeg.exe（安装包的标准布局）
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    cands = [os.path.join(here, "ffmpeg.exe"), os.path.join(here, "ffmpeg"),
             os.path.join(here, "bin", "ffmpeg.exe")]
    # 4) 再兜几个：当前工作目录、exe 的上级目录、以及源码目录
    #    （双击桌面快捷方式时 sys.argv[0] 是桌面，ffmpeg 常常还在原来的
    #     源码/解压目录里）
    more = []
    try:
        cwd = os.getcwd()
        if cwd and cwd != here:
            more.append(os.path.join(cwd, "ffmpeg.exe"))
    except Exception:
        pass
    try:
        if not getattr(sys, "frozen", False):
            more.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "ffmpeg.exe"))
    except Exception:
        pass
    for p in cands + more:
        if os.path.exists(p):
            return p
    # 5) PATH 里的
    p = shutil.which("ffmpeg")
    if p:
        return p
    # 6) imageio-ffmpeg 自带的（开发机常见）
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 终端控制

STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
ENABLE_PROCESSED_OUTPUT = 0x0001


def enable_ansi():
    """打开 Windows 控制台的 ANSI 转义支持"""
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(
            h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT))
    except Exception:
        return False


def set_title(text):
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass



def _invalid(h):
    """句柄是否无效。

    声明了 restype = c_void_p 之后，INVALID_HANDLE_VALUE 会以
    0xFFFFFFFFFFFFFFFF（无符号）的形式回来，不再等于 -1，
    所以不能再用 `h != -1` 判断。
    """
    return (not h) or h == ctypes.c_void_p(-1).value

_conout_cache = [None]


def _conout():
    """拿到一个「真的连着当前控制台」的输出句柄。

    被 CREATE_NEW_CONSOLE 拉起来的子进程，标准句柄可能还指向父进程继承来的
    管道/空设备，此时 GetConsoleScreenBufferInfo 会失败或返回垃圾数据。
    所以这里先试标准句柄，不行就打开 CONOUT$。

    结果会缓存：CreateFileW 每调一次就多一个内核句柄，播放循环里会反复
    检查窗口尺寸，不缓存的话会一直泄漏句柄。
    """
    if _conout_cache[0]:
        return _conout_cache[0]
    k = ctypes.windll.kernel32
    k.SetLastError(0)
    h = k.GetStdHandle(-11)
    if not _invalid(h):
        if k.GetConsoleScreenBufferInfo(h, ctypes.create_string_buffer(22)):
            _conout_cache[0] = h
            return h
    try:
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        h2 = k.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE,
                           FILE_SHARE_READ | FILE_SHARE_WRITE,
                           None, OPEN_EXISTING, 0, None)
        if not _invalid(h2):
            k.SetStdHandle(-11, h2)
            _conout_cache[0] = h2
            return h2
    except Exception:
        pass
    return None


def _console_metrics(h):
    """返回 (缓冲区列数, 缓冲区行数, 窗口列数, 窗口行数)

    CONSOLE_SCREEN_BUFFER_INFO 布局：
      COORD dwSize @0            (Left@0, Top@2)
      COORD dwCursorPosition @4
      WORD  wAttributes @8
      SMALL_RECT srWindow @10    (Left@10, Top@12, Right@14, Bottom@16)
      COORD dwMaximumWindowSize @18
    """
    csbi = ctypes.create_string_buffer(22)
    if not ctypes.windll.kernel32.GetConsoleScreenBufferInfo(h, csbi):
        return None
    buf_w, buf_h = struct.unpack_from("<hh", csbi.raw, 0)
    win_left, win_top = struct.unpack_from("<hh", csbi.raw, 10)
    win_right, win_bottom = struct.unpack_from("<hh", csbi.raw, 14)
    return (buf_w, buf_h,
            max(0, win_right - win_left + 1),
            max(0, win_bottom - win_top + 1))


def term_size():
    """读取控制台的真实字符行列数（窗口可见区域，不是缓冲区）"""
    try:
        h = _conout()
        if h:
            m = _console_metrics(h)
            if m:
                buf_w, buf_h, win_cols, win_rows = m
                if win_cols > 0 and win_rows > 0:
                    return win_cols, win_rows
                if buf_w > 0 and buf_h > 0:
                    return buf_w, buf_h
    except Exception:
        pass
    s = shutil.get_terminal_size((80, 24))
    return s.columns, s.lines


# 播放时窗口占屏幕的比例，以及给标题栏/taskbar 留的余量
_console_ready = False


def maximize_console(font_size=18):
    """把控制台窗口放大到接近满屏，并同步放大缓冲区、调大字体。

    新建的控制台默认只有 80x25、字体也偏小，不放大的话画面会非常小。
    步骤：先设字体 → 量出每格的像素尺寸 → 反推需要多少行列 → 撑大窗口和缓冲区。
    """
    global _console_ready
    if _console_ready:
        return
    _console_ready = True

    try:
        k = ctypes.windll.kernel32
        u = ctypes.windll.user32

        h = _conout()
        if not h:
            return

        hwnd = k.GetConsoleWindow()
        if not hwnd:
            return

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        # 1) 先把字体调大，这样同样的窗口能容纳的字符更少、看着更大更清楚
        set_console_font_size(font_size)
        time.sleep(0.05)

        # 2) 量出「当前字体下每个字符格的像素尺寸」
        m = _console_metrics(h)
        if not m:
            return
        win_cols, win_rows = max(1, m[2]), max(1, m[3])

        rc = RECT()
        u.GetClientRect(hwnd, ctypes.byref(rc))
        cw = max(1, rc.right - rc.left)
        ch = max(1, rc.bottom - rc.top)

        cell_w = cw / float(win_cols)
        cell_h = ch / float(win_rows)
        # 字符格宽高应该落在合理范围（6~60px），否则说明量出来的行列不可信
        if not (6.0 <= cell_w <= 60.0 and 6.0 <= cell_h <= 60.0):
            # 退回到经验值：Consolas 18px 高时，格宽约 9px
            cell_w = max(8.0, font_size * 0.5)
            cell_h = float(font_size)

        # 3) 目标尺寸：屏幕的 95% 宽、去掉任务栏后的 95% 高
        sw = u.GetSystemMetrics(0)
        sh = u.GetSystemMetrics(1)
        target_px_w = int(sw * 0.95)
        target_px_h = int((sh - 64) * 0.95)

        want_cols = max(40, min(600, int(target_px_w / cell_w) - 1))
        want_rows = max(12, min(300, int(target_px_h / cell_h) - 1))

        # 4) 关键顺序：
        #    a. 先把缓冲区撑到目标行列（缓冲区必须 >= 窗口可见区）
        #    b. 再用 SetConsoleWindowInfo 把「可见窗口」撑开
        #       —— 只靠 MoveWindow 改像素尺寸是没用的，
        #          控制台宿主的字符网格不会跟着变（实测窗口 1800px 宽仍只有 120 列）
        #    c. 最后才用 MoveWindow 挪到左上角
        coord = (want_cols & 0xFFFF) | ((want_rows & 0xFFFF) << 16)
        k.SetConsoleScreenBufferSize(h, ctypes.c_ulong(coord))

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

        r = SMALL_RECT(0, 0, want_cols - 1, want_rows - 1)
        if not k.SetConsoleWindowInfo(h, True, ctypes.byref(r)):
            # 失败说明目标超过宿主允许的最大窗口，逐步回退再试
            for factor in (0.9, 0.8, 0.7, 0.6, 0.5):
                c2 = max(40, int(want_cols * factor))
                r2 = max(12, int(want_rows * factor))
                coord = (c2 & 0xFFFF) | ((r2 & 0xFFFF) << 16)
                k.SetConsoleScreenBufferSize(h, ctypes.c_ulong(coord))
                rr = SMALL_RECT(0, 0, c2 - 1, r2 - 1)
                if k.SetConsoleWindowInfo(h, True, ctypes.byref(rr)):
                    want_cols, want_rows = c2, r2
                    break
        time.sleep(0.05)

        # 5) 把窗口挪到 (0,0) 并微调像素尺寸，尽量贴满屏幕
        m2 = _console_metrics(h)
        if m2:
            real_cols, real_rows = max(1, m2[2]), max(1, m2[3])
            rc2 = RECT()
            u.GetClientRect(hwnd, ctypes.byref(rc2))
            cw2 = max(1, rc2.right - rc2.left)
            ch2 = max(1, rc2.bottom - rc2.top)
            aw = cw2 / float(real_cols)
            ah = ch2 / float(real_rows)
            if 6.0 <= aw <= 60.0 and 6.0 <= ah <= 60.0:
                cell_w, cell_h = aw, ah

            wr = RECT()
            u.GetWindowRect(hwnd, ctypes.byref(wr))
            frame_w = max(0, (wr.right - wr.left) - cw2)
            frame_h = max(0, (wr.bottom - wr.top) - ch2)

            wpx = min(int(real_cols * cell_w) + frame_w, sw)
            hpx = min(int(real_rows * cell_h) + frame_h, sh)
            u.MoveWindow(hwnd, 0, 0, wpx, hpx, True)
    except Exception:
        pass


def set_console_font_size(size=18):
    """把控制台字体调大，让画面占的面积更大、更清楚"""
    try:
        k = ctypes.windll.kernel32
        h = _conout()
        if not h:
            return
        # CONSOLE_FONT_INFOEX
        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong),
                        ("nFont", ctypes.c_ulong),
                        ("dwFontSize", COORD),
                        ("FontFamily", ctypes.c_uint),
                        ("FontWeight", ctypes.c_uint),
                        ("FaceName", ctypes.c_wchar * 32)]

        fi = CONSOLE_FONT_INFOEX()
        fi.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        if not k.GetCurrentConsoleFontEx(h, False, ctypes.byref(fi)):
            return
        fi.dwFontSize.Y = size
        fi.dwFontSize.X = 0
        k.SetCurrentConsoleFontEx(h, False, ctypes.byref(fi))
    except Exception:
        pass


def setup_output():
    """把输出稳定成 UTF-8，并确保 stdout 真的连着一个控制台。

    --windowed 打包出来的 exe 没有控制台，sys.stdout 会是 None，
    直接 `out.write(...)` 就会抛 AttributeError。
    这个进程是被父进程用 CREATE_NEW_CONSOLE 拉起来的，系统已经给它分配了
    一个新控制台；只是 Python 解释器启动时标准句柄还没接上，
    所以这里主动把 stdout/stderr 重新绑到 CONOUT$。
    """
    k = ctypes.windll.kernel32

    if sys.stdout is None or not _has_console():
        try:
            if not k.GetConsoleWindow():
                k.AllocConsole()

            GENERIC_READ = 0x80000000
            GENERIC_WRITE = 0x40000000
            FILE_SHARE_READ = 0x00000001
            FILE_SHARE_WRITE = 0x00000002
            OPEN_EXISTING = 3

            h_out = k.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE,
                                  None, OPEN_EXISTING, 0, None)
            h_in = k.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE,
                                 FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 None, OPEN_EXISTING, 0, None)
            if not _invalid(h_out):
                k.SetStdHandle(-11, h_out)     # STD_OUTPUT_HANDLE
                k.SetStdHandle(-12, h_out)     # STD_ERROR_HANDLE
            if not _invalid(h_in):
                k.SetStdHandle(-10, h_in)      # STD_INPUT_HANDLE

            # Python 层也要换掉，否则 sys.stdout 还是 None
            if sys.stdout is None:
                sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                                  errors="replace", buffering=1)
            if sys.stderr is None:
                sys.stderr = sys.stdout
        except Exception:
            pass

    # 切到 UTF-8 代码页，否则块字符 ▀ 在 GBK 下编不出来
    try:
        k.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _has_console():
    """当前进程是否已经连着一个能写的控制台"""
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        if _invalid(h):
            return False
        return bool(k.GetConsoleWindow())
    except Exception:
        return False


# ---------------------------------------------------------------- 探测视频


def probe(ffmpeg, path):
    """返回 (宽, 高, 帧率, 时长秒)；读不出来返回 None"""
    import re
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                           capture_output=True, timeout=30)
    except Exception:
        return None
    text = (p.stderr or b"").decode("utf-8", "ignore")
    w = h = None
    fps = 25.0
    dur = None
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if m:
        fps = float(m.group(1))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if w is None:
        return None
    return w, h, fps, dur


def fit_size(video_w, video_h, cols, rows, px_per_cell=2, cell_aspect=2.0):
    """按视频宽高比，算出实际该用多少列多少行。

    关键：**「一个字符格在屏幕上占多大」和「一个字符格吃几个视频像素」是两回事**，
    早期版本把这两个概念混在一起，导致字符模式画面被压扁一半。

      cell_aspect  —— 字符格在屏幕上的**物理**高宽比（等宽字体通常 ≈ 2.0，
                      即一格的高度是宽度的两倍）。这个决定了「显示出来像不像原片」。
      px_per_cell  —— 渲染时一个格子在**纵向**上采样几个视频像素
                      （▀ 半块画法 = 2，一个字符一格 = 1）。这个只影响清晰度。

    所以：
      屏幕上的显示宽高比 = (cols * 1) / (rows * cell_aspect)
      要和视频宽高比 want 相等  =>  cols / rows = want * cell_aspect
    """
    cols = max(4, cols)
    rows = max(2, rows)
    want = video_w / float(video_h)
    if cell_aspect <= 0:
        cell_aspect = 2.0

    # 显示宽高比 = cols / (rows * cell_aspect)
    have = cols / (rows * cell_aspect)
    if have > want:
        # 太宽了 -> 收窄列数
        cols = max(4, int(round(rows * cell_aspect * want)))
    else:
        # 太高了 -> 压矮行数
        rows = max(2, int(round(cols / (cell_aspect * want))))
    return cols, rows


def guess_cell_aspect(hwnd=None):
    """量出当前控制台字符格的真实高宽比。

    理想情况从「客户区像素 ÷ 窗口字符行列」算出来；量不到就退回 2.0
    （等宽字体 Consolas / 新宋体 的实际比例就非常接近 2）。
    """
    try:
        k = ctypes.windll.kernel32
        u = ctypes.windll.user32
        h = _conout()
        if not h:
            return 2.0
        m = _console_metrics(h)
        if not m:
            return 2.0
        win_cols, win_rows = max(1, m[2]), max(1, m[3])

        hwnd = hwnd or k.GetConsoleWindow()
        if not hwnd:
            return 2.0

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rc = RECT()
        if not u.GetClientRect(hwnd, ctypes.byref(rc)):
            return 2.0
        cw = rc.right - rc.left
        ch = rc.bottom - rc.top
        if cw <= 0 or ch <= 0:
            return 2.0

        cell_w = cw / float(win_cols)
        cell_h = ch / float(win_rows)
        if cell_w <= 0:
            return 2.0
        asp = cell_h / cell_w
        # 等宽字体合理区间大约 1.2 ~ 3.0，超出说明量得不准
        if 1.2 <= asp <= 3.0:
            return asp
    except Exception:
        pass
    return 2.0


# ---------------------------------------------------------------- 渲染


class Renderer:
    """把 RGB 字节流渲染成 ANSI 字符串。

    两种画法：
      block —— 每格 ▀（上半块），前景色画上半、背景色画下半，一格 2 个纵向像素，最细腻
      chars —— 每格一个字符，用字符的疏密表现明暗、用前景色上色，经典"彩色字符画"

    性能（这几条是把播放从"卡"救回来的关键，实测 185x52 一帧从 ~14ms 降到 ~3ms）
    --------------------------------------------------------------------------
    1) 转义串预生成成查表：颜色按 5bit/通道 打包成 15bit 整数当键，
       用 list 懒填充，逐格只做一次索引，不再每次拼字符串。
       老实现按 6 元组做 dict 缓存，长时间播放会攒到十几万条，越播越慢。
    2) **只有颜色真的变了才发转义序列**。真实视频里相邻格大量同色，
       老实现每格都重发「前景+背景」两串转义，一帧要吐 300KB+，
       这一条直接砍到 60KB 量级 —— 控制台解析转义是最大的耗时项。
    3) 行级 diff：整行像素与上一帧逐字节相同就整行跳过（配合绝对光标定位
       \\x1b[{row};1H）。桌面镜像、字幕、暂停画面这类静态内容几乎零开销。
    4) 逐行绝对定位，不再靠换行推进光标，也就不怕最后一列自动换行把画面顶上去。
    """

    def __init__(self, cols, rows, mode=MODE_BLOCK, ramp="ascii", use_256=False,
                 pixel_stride=3, r_off=0, g_off=1, b_off=2):
        self.cols = cols
        self.rows = rows
        self.mode = mode
        self.use_256 = use_256
        # 每格纵向吃几个像素：只有 chars 是 1，block / gray 都是半块画法吃 2
        self.px_per_cell = 1 if mode == MODE_CHARS else 2
        # 输入缓冲的排布：ffmpeg 给的是 RGB24（stride=3），GDI 抓屏给的是
        # BGRA（stride=4，B 在前）。与其在 Python 里逐像素换序（很慢），
        # 不如直接告诉渲染器「哪个字节是红/绿/蓝」。
        #
        # 这里只存「相对红通道的偏移」：行首地址整体加上 r_off，逐格就只剩
        # data[i] / data[i+d1] / data[i+d2] 三次下标，和写死 +1/+2 一样快。
        # （如果每格都去加 r_off/g_off/b_off，实测每帧要多花 2~3ms。）
        self.pixel_stride = pixel_stride
        self.r_off = r_off
        self._d1 = g_off - r_off
        self._d2 = b_off - r_off
        self.frame_bytes = cols * rows * self.px_per_cell * pixel_stride
        self._prev = None                 # 上一帧原始数据，供行级 diff
        self._fg = [None] * 32768         # 15bit 颜色键 -> 前景转义串
        self._bg = [None] * 32768         # 15bit 颜色键 -> 背景转义串
        self.rows_drawn = 0               # 上一帧实际重画了多少行（诊断用）

        if mode == MODE_CHARS:
            self.ramp = RAMP_PRESETS.get(ramp, ramp)
            if not self.ramp or len(self.ramp) < 2:
                self.ramp = RAMP_PRESETS["ascii"]

    def _make_esc(self, key, is_fg, gray):
        """按 15bit 颜色键生成转义串，并写回查表"""
        if gray:
            v = (key << 3) & 0xFF
            r = g = b = v
        else:
            r = ((key >> 10) & 31) << 3
            g = ((key >> 5) & 31) << 3
            b = (key & 31) << 3
        if self.use_256:
            lv = ((r * 5) // 255) * 36 + ((g * 5) // 255) * 6 + ((b * 5) // 255) + 16
            s = "\x1b[%d;5;%dm" % (38 if is_fg else 48, lv)
        else:
            s = "\x1b[%d;2;%d;%d;%dm" % (38 if is_fg else 48, r, g, b)
        (self._fg if is_fg else self._bg)[key] = s
        return s

    def reset(self):
        """丢掉上一帧缓存，强制下一帧整屏重画（窗口尺寸变了之后要调）"""
        self._prev = None

    def render(self, data):
        if self.mode == MODE_CHARS:
            return self._render_chars(data)
        return self._render_half(data, self.mode == MODE_GRAY)

    # ---------------- 半块模式（block / gray）----------------

    def _render_half(self, data, gray):
        cols, rows = self.cols, self.rows
        stride = cols * self.pixel_stride
        ps = self.pixel_stride
        o0 = self.r_off
        o1 = self.r_off + self._d1
        o2 = self.r_off + self._d2
        prev = self._prev
        fgt = self._fg
        bgt = self._bg
        out = []
        ap = out.append
        drawn = 0
        for y in range(rows):
            r0 = y * 2 * stride
            r1 = r0 + stride * 2
            if prev is not None and data[r0:r1] == prev[r0:r1]:
                continue                      # 整行没变 -> 一个字都不发
            # 把上半行/下半行各自切出 R/G/B 三条通道（C 级切片，很快），
            # 再用 zip 直接迭代 —— 这样每格省掉两次乘法和两次下标加法。
            # 实测比「逐格算下标」快 1.2ms/帧。
            top = data[r0:r0 + stride]
            bot = data[r0 + stride:r1]
            tr = top[o0::ps]
            tg = top[o1::ps]
            tb = top[o2::ps]
            br = bot[o0::ps]
            bgc = bot[o1::ps]
            bbc = bot[o2::ps]
            row = []
            rap = row.append
            lf = lb = -1
            if gray:
                for r, g, b, r2, g2, b2 in zip(tr, tg, tb, br, bgc, bbc):
                    fk = (r * 77 + g * 151 + b * 28) >> 11
                    if fk != lf:
                        s = fgt[fk]
                        if s is None:
                            s = self._make_esc(fk, True, True)
                        rap(s)
                        lf = fk
                    bk = (r2 * 77 + g2 * 151 + b2 * 28) >> 11
                    if bk != lb:
                        s = bgt[bk]
                        if s is None:
                            s = self._make_esc(bk, False, True)
                        rap(s)
                        lb = bk
                    rap("\u2580")
            else:
                for r, g, b, r2, g2, b2 in zip(tr, tg, tb, br, bgc, bbc):
                    fk = ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)
                    if fk != lf:
                        s = fgt[fk]
                        if s is None:
                            s = self._make_esc(fk, True, False)
                        rap(s)
                        lf = fk
                    bk = ((r2 >> 3) << 10) | ((g2 >> 3) << 5) | (b2 >> 3)
                    if bk != lb:
                        s = bgt[bk]
                        if s is None:
                            s = self._make_esc(bk, False, False)
                        rap(s)
                        lb = bk
                    rap("\u2580")
            ap("\x1b[%d;1H" % (y + 1))
            ap("".join(row))
            drawn += 1
        self._prev = data
        self.rows_drawn = drawn
        if not out:
            return ""
        out.append("\x1b[0m")
        return "".join(out)

    # ---------------- 彩色字符模式 ----------------

    def _render_chars(self, data):
        cols, rows = self.cols, self.rows
        stride = cols * self.pixel_stride
        ps = self.pixel_stride
        o0 = self.r_off
        o1 = self.r_off + self._d1
        o2 = self.r_off + self._d2
        ramp = self.ramp
        n = len(ramp) - 1
        prev = self._prev
        fgt = self._fg
        out = []
        ap = out.append
        drawn = 0
        for y in range(rows):
            r0 = y * stride
            r1 = r0 + stride
            if prev is not None and data[r0:r1] == prev[r0:r1]:
                continue
            line = data[r0:r1]
            row = []
            rap = row.append
            lf = -1
            for r, g, b in zip(line[o0::ps], line[o1::ps], line[o2::ps]):
                fk = ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)
                if fk != lf:
                    s = fgt[fk]
                    if s is None:
                        s = self._make_esc(fk, True, False)
                    rap(s)
                    lf = fk
                # 感知亮度（人眼对绿最敏感），按亮度挑阶梯里的字符
                rap(ramp[((r * 77 + g * 151 + b * 28) >> 8) * n // 255])
            ap("\x1b[%d;1H" % (y + 1))
            ap("".join(row))
            drawn += 1
        self._prev = data
        self.rows_drawn = drawn
        if not out:
            return ""
        out.append("\x1b[0m")
        return "".join(out)


# ---------------------------------------------------------------- 音频


def prepare_audio(ffmpeg, path):
    """把音轨抽成临时 WAV（winsound 只能播 WAV），没有音轨就返回 None"""
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "cmdvideo_audio.wav")
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", path,
             "-vn", "-acodec", "pcm_s16le", "-ar", "22050", "-ac", "1", tmp],
            capture_output=True, timeout=180)
    except Exception:
        return None
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
        return tmp
    return None


def audio_start(wav):
    try:
        import winsound
        winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return True
    except Exception:
        return False


def audio_stop():
    try:
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass


# ---------------------------------------------------------------- 播放


def play(path, cols=None, rows=None, fps_override=None, loop=False,
         use_256=False, mode=MODE_BLOCK, ramp="ascii", stats=False,
         ffmpeg=None, audio=True):
    # 先把 stdout 接上控制台，后面所有 print / write 才安全
    setup_output()

    if not os.path.exists(path):
        print("找不到文件：%s" % path)
        return 1

    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        print("没有找到 ffmpeg。")
        print("请把 ffmpeg.exe 放到本程序同目录，或设置环境变量 CMDVIDEO_FFMPEG 指向它。")
        return 2

    info = probe(ffmpeg, path)
    if not info:
        print("读不出视频信息，可能不是视频文件，或者 ffmpeg 不可用。")
        return 1
    vw, vh, vfps, vdur = info

    px_per_cell = 1 if mode == MODE_CHARS else 2

    enable_ansi()

    # 兜底：万一 stdout 还是 None（某些打包/重定向组合），也不能让整个程序崩掉
    out = sys.stdout
    if out is None or not hasattr(out, "write"):
        try:
            out = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                       buffering=1)
            sys.stdout = out
        except Exception:
            out = open(os.devnull, "w", encoding="utf-8")

    # 先把控制台窗口放大再量尺寸 —— 新建的控制台默认只有 80x25，
    # 不放大画面会非常小。放大的动作要放在 term_size() 之前。
    maximize_console()

    term_cols, term_rows = term_size()
    if cols is None:
        cols = term_cols
    if rows is None:
        rows = max(2, term_rows - 1)
    # 用「字符格的真实物理高宽比」来算，这样各种画法的显示比例才都正确
    cell_aspect = guess_cell_aspect()
    cols, rows = fit_size(vw, vh, cols, rows, px_per_cell, cell_aspect)
    fps = fps_override or vfps

    # 关掉自动换行：我们用「绝对光标定位」逐行刷新，如果最后一列写字符时
    # 触发自动换行，整个画面会被顶上去一行、越播越歪。
    out.write("\x1b[?25l")      # 隐藏光标
    out.write("\x1b[?7l")       # 关闭自动换行(DECAWM)
    out.write("\x1b[2J")        # 清屏
    out.flush()
    set_title("%s - %s" % (APP, os.path.basename(path)))

    # ffmpeg 直接吐目标尺寸的 RGB24，省掉自己缩放的活儿。
    # 不加 -re：由我们自己按 fps 控速，这样 --fps 才有效。
    def build_cmd(loop_it):
        c = [ffmpeg, "-hide_banner", "-loglevel", "error"]
        if loop_it:
            c += ["-stream_loop", "-1"]
        c += ["-i", path,
              "-vf", "scale=%d:%d:flags=bilinear" % (cols, rows * px_per_cell),
              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        return c

    cmd = build_cmd(loop)

    renderer = Renderer(cols, rows, mode=mode, ramp=ramp, use_256=use_256)
    nbytes = renderer.frame_bytes

    # 有音轨就先把声音放起来（异步），画面自己按 fps 走
    wav = None
    if audio and not loop:
        wav = prepare_audio(ffmpeg, path)
        if wav:
            audio_start(wav)

    rendered = 0        # 真正画到屏幕上的帧
    consumed = 0        # 从 ffmpeg 读进来的帧（含被丢掉的）
    dropped = 0
    # 起跑时间**不能**在 Popen 之前定：ffmpeg 起来 + 解出第一帧要两三百毫秒，
    # 这段启动延迟会被算成"已经落后"，一上来就白丢好几帧。
    # 所以第一帧真正到手时才把时钟对齐，让首帧立刻显示。
    t_start = None
    interval = 1.0 / fps if fps > 0 else 0.04

    def run_once():
        nonlocal rendered, consumed, dropped, t_start
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=nbytes * 2)
        try:
            read = proc.stdout.read
            while True:
                buf = read(nbytes)
                if not buf or len(buf) < nbytes:
                    break
                consumed += 1
                now = time.perf_counter()
                if t_start is None:
                    t_start = now - interval
                # 这一帧「本来该在什么时候显示」
                due = t_start + consumed * interval
                # 已经落后超过一帧半，就干脆丢掉不画。
                # 宁可掉帧也不能让画面比声音慢 —— 那才是用户感觉到的"卡"。
                if now - due > interval * 1.5:
                    dropped += 1
                    continue
                s = renderer.render(buf)
                if s:
                    out.write(s)
                    out.flush()
                    rendered += 1
                # 隔一阵看一眼窗口有没有被拖动改变大小。变大了的话
                # 之前画的内容和新的字符网格对不上，会花屏，索性清屏重画。
                if consumed % 60 == 0:
                    m = _console_metrics(_conout())
                    if m and (m[2], m[3]) != (cols, rows):
                        out.write("\x1b[2J")
                        out.flush()
                        renderer.reset()
                # 睡到下一帧该显示的时刻
                gap = t_start + (consumed + 1) * interval - time.perf_counter()
                if gap > 0.0005:
                    time.sleep(gap)
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    try:
        run_once()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass
    finally:
        audio_stop()
        try:
            out.write("\x1b[0m\x1b[?7h\x1b[?25h\n")
            out.flush()
        except Exception:
            pass
        if wav:
            try:
                os.remove(wav)
            except Exception:
                pass

    el = (time.perf_counter() - t_start) if t_start else 0.0
    if stats:
        print("播放 %d 帧（读入 %d，为跟声音丢掉 %d）/ 用时 %.1fs / 平均 %.1f fps"
              % (rendered, consumed, dropped, el,
                 rendered / el if el > 0 else 0))
    return 0


# ---------------------------------------------------------------- 投屏 + 操控
#
# 把系统里另一个正在运行的窗口「搬」到控制台里显示，并且能操控它。
#
# 画面怎么来
#   PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)
#   让目标窗口把自己画到一块内存位图上 —— 好处是窗口被别的窗口挡住、
#   甚至不在最前面，照样能抓到完整画面（普通 BitBlt 抓屏只能抓到露出来的部分）。
#   抓到的原图再用 StretchBlt 一步缩放到字符网格需要的大小，全程走 GDI，
#   Python 这边只碰一次内存块。
#   有些窗口（硬件加速的、UWP）PrintWindow 会返回全黑，这时自动退回
#   「从屏幕 DC 直接 BitBlt」，所以有个 --capture 选项可以强制指定。
#
# 操控怎么做
#   控制台本身就能上报鼠标和键盘事件（ReadConsoleInput）。
#   鼠标事件直接带**字符格坐标**，正好拿来做映射：
#       目标客户区坐标 = 字符格坐标 / 字符网格大小 * 目标客户区像素大小
#   然后 PostMessage 把 WM_LBUTTONDOWN / WM_KEYDOWN 之类发进目标窗口的
#   消息队列。用 PostMessage 而不是 SendInput，是为了不抢走用户真实的
#   鼠标键盘 —— 你可以一边让程序自己动，一边用真鼠标干别的。
#   碰到不认 PostMessage 的程序（部分浏览器/游戏），可以开 --real —— 它和默认
#   的区别是「输入直接投给目标窗口，而不是塞给控制台自己」，但**不动系统光标、
#   也不切前台**：光标留在你手底下，投屏窗口一直待在最前面。
#   真需要「搬光标 + 切前台」的老行为，用 --real-takeover（代价见 WindowControl）。
#
#   注意：曾经 --real 是每动一下鼠标就把系统光标搬到目标窗口上，结果是光标
#   和控制台的鼠标事件形成正反馈，光标本屏乱飞、还再也出不了投屏窗口
#   （「鼠标进不了窗口」就是它）。所以现在默认形态一个系统 API 都不碰。

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_VSCROLL = 0x0115        # 经典滚动消息：投递 WM_MOUSEWHEEL 没人理时的兜底
SB_LINEUP = 0
SB_LINEDOWN = 1
SB_VERT = 1
SIF_ALL = 0x0017
EM_GETSCROLLPOS = 0x04DD   # RichEdit/编辑类控件读滚动位置用

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 控制台输入模式位
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080

# 抓帧方式
CAPTURE_AUTO = "auto"
CAPTURE_PRINT = "print"      # PrintWindow
CAPTURE_SCREEN = "screen"    # 从屏幕 DC BitBlt（要求窗口露在最前面）


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _POINT(ctypes.Structure):
    """屏幕坐标点。

    必须和 _COORD 分开：`ClientToScreen` / `WindowFromPoint` 要的是 **POINT**
    —— 两个 4 字节的 LONG。_COORD 是控制台 API 用的（两个 2 字节的 SHORT），
    总共只有 4 字节，喂给 ClientToScreen 会**只写回 x、y 永远是 0**
    （Windows 按 LONG 写 8 字节，越过结构体末尾），于是所有屏幕坐标的 y 全错：

        实测 ClientToScreen(0,0) 用 _COORD 得到 (214, 0)，用 _POINT 才是 (214, 208)

    y 错了之后滚轮会投给「屏幕上另一个位置的窗口」，而不是鼠标底下那个 ——
    表现就是「滚轮没反应」，而且 PostMessage 还返回成功，极难看出来。
    """

    _fields_ = [("X", ctypes.c_long), ("Y", ctypes.c_long)]


class _SCROLLINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("fMask", ctypes.c_uint),
                ("nMin", ctypes.c_int), ("nMax", ctypes.c_int),
                ("nPage", ctypes.c_uint), ("nPos", ctypes.c_int),
                ("nTrackPos", ctypes.c_int)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", ctypes.c_int),
                ("wRepeatCount", ctypes.c_ushort),
                ("wVirtualKeyCode", ctypes.c_ushort),
                ("wVirtualScanCode", ctypes.c_ushort),
                ("uChar", ctypes.c_wchar),
                ("dwControlKeyState", ctypes.c_uint)]


class _MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwMousePosition", _COORD),
                ("dwButtonState", ctypes.c_uint),
                ("dwControlKeyState", ctypes.c_uint),
                ("dwEventFlags", ctypes.c_uint)]


class _EVENT_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", _KEY_EVENT_RECORD),
                ("MouseEvent", _MOUSE_EVENT_RECORD),
                ("_pad", ctypes.c_byte * 16)]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", ctypes.c_ushort),
                ("_pad", ctypes.c_ushort),
                ("Event", _EVENT_UNION)]


KEY_EVENT = 0x0001
MOUSE_EVENT = 0x0002
WINDOW_BUFFER_SIZE_EVENT = 0x0004


def _declare_winapi():
    """把用到的 Win32 API 的 restype / argtypes 显式声明清楚。

    为什么必须做这件事：ctypes 默认把「整数」参数当成 32 位 C int、
    把返回值也当成 int。64 位 Windows 上句柄是个指针，一旦某个 API
    返回了大于 2^31 的句柄值，再把它传给下一个 API 就会
    `OverflowError: int too long to convert`。
    这个坑不声明就一定会踩（实测 CreateCompatibleDC 的返回值喂给
    CreateCompatibleBitmap 时直接炸）。
    """
    V = ctypes.c_void_p
    I = ctypes.c_int
    U = ctypes.c_uint
    U32 = ctypes.c_uint32
    U8 = ctypes.c_ubyte
    W = ctypes.c_wchar_p

    k = ctypes.windll.kernel32
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32

    # ---- kernel32 ----
    for name, res, args in (
        ("GetStdHandle", V, [U32]),
        ("CreateFileW", V, [W, U32, U32, V, U32, U32, V]),
        ("OpenProcess", V, [U32, I, U32]),
        ("GetConsoleWindow", V, []),
        ("AllocConsole", I, []),
        ("SetStdHandle", I, [U32, V]),
        ("CloseHandle", I, [V]),
        ("GetConsoleScreenBufferInfo", I, [V, V]),
        ("SetConsoleScreenBufferSize", I, [V, ctypes.c_ulong]),
        ("SetConsoleWindowInfo", I, [V, I, V]),
        ("GetConsoleMode", I, [V, V]),
        ("SetConsoleMode", I, [V, U32]),
        ("GetCurrentConsoleFontEx", I, [V, I, V]),
        ("SetCurrentConsoleFontEx", I, [V, I, V]),
        ("SetConsoleOutputCP", I, [U32]),
        ("SetConsoleTitleW", I, [W]),
        ("ReadConsoleInputW", I, [V, V, U32, V]),
        ("QueryFullProcessImageNameW", I, [V, U32, W, V]),
        ("GetCurrentProcessId", U32, []),
    ):
        f = getattr(k, name, None)
        if f is not None:
            f.restype = res
            f.argtypes = args

    # ---- user32 ----
    for name, res, args in (
        ("GetDC", V, [V]),
        ("GetWindowDC", V, [V]),
        ("ReleaseDC", I, [V, V]),
        ("GetForegroundWindow", V, []),
        ("WindowFromPoint", V, [V]),
        ("GetParent", V, [V]),
        ("ChildWindowFromPointEx", V, [V, V, U]),
        ("GetParent", V, [V]),
        ("IsWindow", I, [V]),
        ("IsWindowVisible", I, [V]),
        ("IsIconic", I, [V]),
        ("EnumWindows", I, [V, V]),
        ("GetWindowTextLengthW", I, [V]),
        ("GetWindowTextW", I, [V, W, I]),
        ("GetWindowLongW", I, [V, I]),
        ("GetWindowThreadProcessId", U32, [V, V]),
        ("AttachThreadInput", I, [U32, U32, I]),
        ("BringWindowToTop", I, [V]),
        ("GetClientRect", I, [V, V]),
        ("GetWindowRect", I, [V, V]),
        ("MoveWindow", I, [V, I, I, I, I, I]),
        ("ClientToScreen", I, [V, V]),
        ("PrintWindow", I, [V, V, U]),
        ("PostMessageW", I, [V, U, V, V]),
        ("SetForegroundWindow", I, [V]),
        ("GetSystemMetrics", I, [I]),
        ("SetCursorPos", I, [I, I]),
        ("mouse_event", None, [U32, I, I, U32, V]),
        ("keybd_event", None, [U8, U8, U32, V]),
        ("MessageBoxW", I, [V, W, W, U]),
    ):
        f = getattr(u, name, None)
        if f is not None:
            if res is not None:
                f.restype = res
            f.argtypes = args

    # ---- gdi32 ----
    for name, res, args in (
        ("CreateCompatibleDC", V, [V]),
        ("CreateCompatibleBitmap", V, [V, I, I]),
        ("SelectObject", V, [V, V]),
        ("DeleteDC", I, [V]),
        ("DeleteObject", I, [V]),
        ("CreateDIBSection", V, [V, V, U32, V, V, U32]),
        ("SetStretchBltMode", I, [V, I]),
        ("StretchBlt", I, [V, I, I, I, I, V, I, I, I, I, U32]),
        ("BitBlt", I, [V, I, I, I, I, V, I, I, U32]),
        ("GetDIBits", I, [V, V, U32, U32, V, V, U32]),
    ):
        f = getattr(g, name, None)
        if f is not None:
            f.restype = res
            f.argtypes = args


_declare_winapi()

# 结构体/句柄参数的补充声明。
# ctypes 的默认推断很坑：没写 argtypes 时「整数」按 32 位 C int 处理，而 64 位
# Windows 的 HWND 是 64 位指针值 —— 传进去会被**悄悄截断**，于是
# GetParent / WindowFromPoint 拿到的句柄是错的、返回乱值或 NULL，
# Python 这边看不出任何异常。滚轮「找鼠标底下那个子窗口」就栽在这上面。
ctypes.windll.user32.WindowFromPoint.argtypes = [_POINT]     # 按值传 POINT
ctypes.windll.user32.WindowFromPoint.restype = ctypes.c_void_p
ctypes.windll.user32.GetParent.argtypes = [ctypes.c_void_p]
ctypes.windll.user32.GetParent.restype = ctypes.c_void_p
ctypes.windll.user32.IsWindow.argtypes = [ctypes.c_void_p]
ctypes.windll.user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
ctypes.windll.user32.ChildWindowFromPointEx.argtypes = [
    ctypes.c_void_p, _POINT, ctypes.c_uint]
ctypes.windll.user32.ChildWindowFromPointEx.restype = ctypes.c_void_p
ctypes.windll.user32.ScreenToClient.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(_POINT)]
ctypes.windll.user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
ctypes.windll.user32.ClientToScreen.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(_POINT)]
ctypes.windll.user32.GetScrollInfo.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                               ctypes.POINTER(_SCROLLINFO)]
ctypes.windll.user32.GetScrollInfo.restype = ctypes.c_int
ctypes.windll.user32.SendMessageW.restype = ctypes.c_long
ctypes.windll.user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_void_p]


def _process_name(pid):
    """按 pid 拿进程可执行文件名（拿不到就返回空串）"""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        size = ctypes.c_ulong(512)
        buf = ctypes.create_unicode_buffer(512)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    except Exception:
        pass
    finally:
        try:
            k.CloseHandle(h)
        except Exception:
            pass
    return ""


def list_windows(include_minimized=False):
    """列出所有「像应用窗口」的顶层窗口。

    返回 [(hwnd, 标题, 进程名, 宽, 高)]，按标题排序。
    过滤掉：不可见的、没标题的、工具窗口、太小的、以及自己。
    """
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    own_pid = k.GetCurrentProcessId()
    own_console = k.GetConsoleWindow()
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        if u.IsIconic(hwnd) and not include_minimized:
            return True
        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW:
            return True
        rc = _RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(rc)):
            return True
        w = rc.right - rc.left
        h = rc.bottom - rc.top
        if w < 120 or h < 90:
            return True
        pid = ctypes.c_ulong(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        if own_console and hwnd == own_console:
            return True
        found.append((hwnd, title, _process_name(pid.value), w, h))
        return True

    try:
        u.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception:
        pass
    found.sort(key=lambda t: t[1].lower())
    return found


def find_window(keyword):
    """按标题/进程名关键字找一个窗口。

    关键字是纯数字时当成 hwnd 直接校验。找不到返回 None，
    找到多个时返回第一个（标题完全相等的优先）。
    """
    if keyword is None:
        return None
    s = str(keyword).strip()
    if not s:
        return None
    if s.isdigit():
        hwnd = int(s)
        if ctypes.windll.user32.IsWindow(hwnd):
            return hwnd
        return None
    wins = list_windows(include_minimized=True)
    low = s.lower()
    exact = [w for w in wins if w[1].lower() == low]
    if exact:
        return exact[0][0]
    for w in wins:
        if low in w[1].lower() or low in (w[2] or "").lower():
            return w[0]
    return None


def window_title(hwnd):
    try:
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


class WindowCapture:
    """把目标窗口的画面抓下来、缩放到字符网格需要的尺寸。

    一次分配、反复复用：源位图装原始画面，目标位图是 CreateDIBSection
    建的 32 位自上而下 DIB，拿到的就是一块 BGRA 内存，直接喂给 Renderer
    （Renderer 支持指定通道顺序，所以不用在 Python 里逐像素换序）。
    """

    def __init__(self, hwnd, cols, rows, px_per_cell=2, method=CAPTURE_AUTO):
        self.hwnd = hwnd
        self.cols = cols
        self.rows = rows
        self.out_h = rows * px_per_cell
        self.px_per_cell = px_per_cell
        self.method = method
        self.u = ctypes.windll.user32
        self.g = ctypes.windll.gdi32
        self.cw = 0
        self.ch = 0
        self.hdc_src = None
        self.bmp_src = None
        self.old_src = None
        self.hdc_dst = None
        self.bmp_dst = None
        self.old_dst = None
        self._ptr = ctypes.c_void_p()
        self.nbytes = cols * self.out_h * 4
        self._build()

    # ---- 资源 ----
    def _build(self):
        cw, ch = self.client_size()
        self.cw, self.ch = max(1, cw), max(1, ch)

        # 彩色位图必须用「屏幕 DC」来建。
        # CreateCompatibleBitmap 的规则是：传进去的 DC 里当前选着什么格式的位图，
        # 新建的就是什么格式。而 CreateCompatibleDC(NULL) 出来的内存 DC 里
        # 默认选的是一张 1x1 的**单色**位图 —— 于是抓出来的画面全是黑白
        # 全白（实测就是这个现象：整个窗口一片白，怎么调都不对）。
        hdc_screen = self.u.GetDC(None)
        try:
            self.hdc_src = self.g.CreateCompatibleDC(hdc_screen)
            self.bmp_src = self.g.CreateCompatibleBitmap(hdc_screen, self.cw, self.ch)
            self.hdc_dst = self.g.CreateCompatibleDC(hdc_screen)
        finally:
            self.u.ReleaseDC(None, hdc_screen)
        self.old_src = self.g.SelectObject(self.hdc_src, self.bmp_src)

        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = self.cols
        bi.bmiHeader.biHeight = -self.out_h        # 负高度 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0             # BI_RGB
        self.bmp_dst = self.g.CreateDIBSection(
            self.hdc_dst, ctypes.byref(bi), 0, ctypes.byref(self._ptr), None, 0)
        self.old_dst = self.g.SelectObject(self.hdc_dst, self.bmp_dst)
        self.g.SetStretchBltMode(self.hdc_dst, 4)  # HALFTONE：缩小时不糊不闪

    def release(self):
        try:
            if self.hdc_src:
                self.g.SelectObject(self.hdc_src, self.old_src)
                self.g.DeleteDC(self.hdc_src)
            if self.bmp_src:
                self.g.DeleteObject(self.bmp_src)
            if self.hdc_dst:
                self.g.SelectObject(self.hdc_dst, self.old_dst)
                self.g.DeleteDC(self.hdc_dst)
            if self.bmp_dst:
                self.g.DeleteObject(self.bmp_dst)
        except Exception:
            pass
        self.hdc_src = self.bmp_src = self.hdc_dst = self.bmp_dst = None

    # ---- 尺寸 ----
    def client_size(self):
        rc = _RECT()
        if self.u.GetClientRect(self.hwnd, ctypes.byref(rc)):
            return rc.right - rc.left, rc.bottom - rc.top
        return 0, 0

    def alive(self):
        return bool(self.u.IsWindow(self.hwnd)) and self.client_size() != (0, 0)

    # ---- 抓帧 ----
    def _grab_print(self):
        return bool(self.u.PrintWindow(self.hwnd, self.hdc_src, 2))

    def _grab_screen(self):
        """从屏幕 DC 直接 BitBlt —— 要求窗口露在最前面"""
        rc = _RECT()
        if not self.u.GetWindowRect(self.hwnd, ctypes.byref(rc)):
            return False
        pt = _POINT(0, 0)
        self.u.ClientToScreen(self.hwnd, ctypes.byref(pt))
        hdc_screen = self.u.GetDC(None)
        try:
            ok = self.g.BitBlt(self.hdc_src, 0, 0, self.cw, self.ch,
                               hdc_screen, pt.X, pt.Y, 0x00CC0020)  # SRCCOPY
        finally:
            self.u.ReleaseDC(None, hdc_screen)
        return bool(ok)

    @staticmethod
    def _looks_blank(buf):
        """抽样看是不是全黑。PrintWindow 抓硬件加速窗口时常返回全黑，
        这种情况要退回抓屏，否则用户只会看到一块黑。"""
        n = len(buf)
        if n < 4:
            return True
        step = max(4, (n // 400) // 4 * 4)     # 取 ~400 个采样点
        lit = 0
        total = 0
        for i in range(0, n - 3, step):
            total += 1
            if buf[i] > 12 or buf[i + 1] > 12 or buf[i + 2] > 12:
                lit += 1
        return total == 0 or lit * 100 // total < 2

    def grab(self):
        """抓一帧并缩放，返回 BGRA 字节（每帧都是新的 bytes 对象）。

        必须每帧返回新对象：Renderer 的行级 diff 会拿上一帧的 buffer
        做比较，如果复用同一块内存，diff 永远相等，画面就再也不更新了。
        """
        if self.client_size() != (self.cw, self.ch):
            # 目标窗口被改过大小，重建位图
            self.release()
            self._build()
        method = self.method
        if method == CAPTURE_SCREEN:
            self._grab_screen()
        elif method == CAPTURE_PRINT:
            self._grab_print()
        else:
            if not self._grab_print() or self._looks_blank(
                    ctypes.string_at(self._ptr, self.nbytes)):
                self._grab_screen()
        self.g.StretchBlt(self.hdc_dst, 0, 0, self.cols, self.out_h,
                          self.hdc_src, 0, 0, self.cw, self.ch, 0x00CC0020)
        return ctypes.string_at(self._ptr, self.nbytes)


class ConsoleInput:
    """读控制台的鼠标/键盘事件，转成对目标窗口的操作。

    控制台报的鼠标事件直接带字符格坐标，正好省掉反推像素的麻烦。
    用一个后台线程阻塞读 ReadConsoleInputW，主循环每帧来取一次队列，
    这样渲染不会被输入阻塞。
    """

    def __init__(self, forward, console_hwnd=None):
        self.forward = forward
        # 投屏窗口自己的 hwnd（也就是这个进程的控制台窗口）。
        # 必须由调用方传进来，不能在这里调 GetConsoleWindow() 现取：
        # 那个 API 返回的是「本进程当前挂着的控制台」，一旦进程被别的程序
        # 拉起来、或挂到了别的控制台上，拿到的就是**别人的**窗口句柄 ——
        # 实测踩过：抢前台抢到了调用方的控制台上，滚轮反而更不灵。
        self.console_hwnd = console_hwnd
        self.queue = []
        self.stop = threading.Event()
        self.h = None
        self.old_mode = None
        self.quit = False
        self._thread = None
        self._last_pos = (-1, -1)
        self._buttons = 0
        # 滚轮余数：攒够一格才转发一次，见 _on_mouse 里的说明
        self._wheel_acc = 0
        # 移动限速用：上次投移动的时间、被合并掉的位置（见 _emit_move）
        self._move_at = 0.0
        self._move_pending = None
        # 投屏帧率，由 project() 设进来，用来定「一拍」有多长
        self.fps = 0.0
        # 上次把投屏窗口抢回前台的时间（滚动时要用，做节流）
        self._focus_at = 0.0

    def start(self):
        k = ctypes.windll.kernel32
        self.h = k.GetStdHandle(-10)          # STD_INPUT_HANDLE
        if _invalid(self.h):
            return False
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(self.h, ctypes.byref(mode)):
            return False
        self.old_mode = mode.value
        # 打开鼠标/窗口事件上报；必须关掉快速编辑，否则鼠标点击会被
        # 控制台自己吃掉去做选区，根本不会变成事件给我们。
        new_mode = (mode.value
                    | ENABLE_MOUSE_INPUT | ENABLE_WINDOW_INPUT | ENABLE_EXTENDED_FLAGS)
        new_mode &= ~(ENABLE_QUICK_EDIT_MODE | ENABLE_LINE_INPUT
                      | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT)
        k.SetConsoleMode(self.h, new_mode)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def restore(self):
        self.stop.set()
        try:
            if self.h and self.old_mode is not None:
                ctypes.windll.kernel32.SetConsoleMode(self.h, self.old_mode)
        except Exception:
            pass

    def _loop(self):
        k = ctypes.windll.kernel32
        rec = _INPUT_RECORD()
        got = ctypes.c_ulong(0)
        while not self.stop.is_set():
            try:
                if not k.ReadConsoleInputW(self.h, ctypes.byref(rec), 1,
                                           ctypes.byref(got)):
                    time.sleep(0.05)
                    continue
            except Exception:
                time.sleep(0.05)
                continue
            if got.value:
                # 复制一份再入队：rec 会被下一次读取覆盖
                self.queue.append(_INPUT_RECORD.from_buffer_copy(rec))
    def pump(self):
        """把积压的事件处理掉。返回 True 表示用户要求退出。"""
        q = self.queue
        while q:
            rec = q.pop(0)
            self._handle(rec)
        # 一拍处理完，把被合并掉的那个移动补上（见 _emit_move）
        self._flush_move()
        return self.quit

    # ---- 事件处理 ----
    def _handle(self, rec):
        if rec.EventType == MOUSE_EVENT:
            self._on_mouse(rec.Event.MouseEvent)
        elif rec.EventType == KEY_EVENT:
            self._on_key(rec.Event.KeyEvent)

    def _keep_focus(self):
        """滚动前确认投屏窗口还在前台。

        为什么要做这件事：控制台的鼠标事件（**包括滚轮**）只有它是前台窗口时
        才会上报 —— 你先去点了别的窗口，再回来滚投屏，控制台根本收不到滚轮，
        表现就是「滚轮没法用」。所以真要滚的时候先把窗口抢回前台。

        代价是那一刻别的窗口会失去焦点，所以只在「滚轮真的要转发」时才做
        （攒够一格才到这里），并做节流，避免连续滚动时反复抢。
        """
        try:
            u = ctypes.windll.user32
            con = self.console_hwnd
            if not con or u.GetForegroundWindow() == con:
                return
            now = time.perf_counter()
            if now - self._focus_at < 1.0:
                return
            self._focus_at = now
            # 抢前台可能要几十毫秒，扔后台线程，别卡住这一帧的输入处理
            threading.Thread(target=self.forward.focus_console,
                             args=(self.console_hwnd,), daemon=True).start()
        except Exception:
            pass

    def _on_mouse(self, m):
        pos = (m.dwMousePosition.X, m.dwMousePosition.Y)
        btn = m.dwButtonState
        flags = m.dwEventFlags
        if flags & 0x0004:            # MOUSE_WHEELED
            delta = (btn >> 16) & 0xFFFF
            if delta >= 0x8000:
                delta -= 0x10000
            # 不能写成 delta // 120 就完事：微软的「平滑滚动」和高精度滚轮
            # 会发比一格(120)小的增量（常见 ±30/±60），整数除直接变成 0
            # —— 表现就是「投屏时滚轮完全没反应」。这里把余数攒起来，
            # 攒够一格才发一次（正负分开攒，换方向就清零，免得回头时多蹦一格）。
            if delta and (delta > 0) != (self._wheel_acc > 0):
                self._wheel_acc = 0
            self._wheel_acc += delta
            notches = int(self._wheel_acc / 120)      # 向零取整
            if notches:
                self._wheel_acc -= notches * 120
                self._keep_focus()
                # 日志由 WindowControl 记（它才知道投给了哪个窗口、有没有兜底）
                self.forward.wheel(pos, notches)
            return
        changed = btn ^ self._buttons
        self._buttons = btn
        if changed & MK_LBUTTON:
            if btn & MK_LBUTTON:
                self.forward.button(pos, "left", True)
            else:
                self.forward.button(pos, "left", False)
        if changed & MK_RBUTTON:
            if btn & MK_RBUTTON:
                self.forward.button(pos, "right", True)
            else:
                self.forward.button(pos, "right", False)
        if changed & MK_MBUTTON:
            if btn & MK_MBUTTON:
                self.forward.button(pos, "middle", True)
            else:
                self.forward.button(pos, "middle", False)
        if pos != self._last_pos:
            self._last_pos = pos
            self._emit_move(pos)

    def _emit_move(self, pos):
        """把「鼠标移动」投给目标窗口，但**不许把它的消息队列灌满**。

        为什么必须限速（实测出来的坑）：控制台在鼠标快速移动时会一口气上报几百
        个移动事件，一拍全投出去，目标窗口的队列瞬间堆到两千条。Windows 的窗口
        消息队列有上限，**超了就直接把新消息丢掉，而且 PostMessage 照样返回成功**
        —— 后果是别的事件被静默吞掉：滑动一下鼠标，紧接着的滚轮就没了，表现就是
        「投屏时滚轮没法用」（实验里 1500 条以上开始丢，滚轮首当其冲）。

        所以这里做两件事：
          1. **合并同一拍里的多次移动**：每拍最多投一条，位置取这拍最后一个。
             反正中间那些格子目标窗口根本来不及看（一拍 50ms，人眼也看不出差别）。
          2. **位置没变就别投**：悬停不动时一条都不发，队列始终是空的，
             点击和滚轮随到随处理。
        """
        now = time.perf_counter()
        # 拍长跟投屏帧率走：20fps ≈ 50ms；没有帧率信息时退回 30ms
        budget = 1.0 / max(1.0, getattr(self, "fps", 0.0) or 30.0)
        if now - self._move_at < budget:
            self._move_pending = pos          # 攒着，等下一拍
            return
        self._move_at = now
        self._move_pending = None
        self.forward.move(pos)

    def _flush_move(self):
        """一拍结束时补投那个被合并掉的移动（目标窗口才看得到最终位置）。"""
        if self._move_pending is not None:
            pos, self._move_pending = self._move_pending, None
            self._move_at = time.perf_counter()
            self.forward.move(pos)

    def _on_key(self, e):
        vk = e.wVirtualKeyCode
        ctrl = bool(e.dwControlKeyState & 0x000C)     # LEFT|RIGHT_CTRL
        # Ctrl+Q 退出。Esc 之类留给目标程序用，所以退出键特意选 Ctrl+Q。
        if e.bKeyDown and ctrl and vk == 0x51:
            self.quit = True
            return
        if e.bKeyDown and ctrl and vk == 0x43:        # Ctrl+C 也当退出
            self.quit = True
            return
        self.forward.key(vk, e.wVirtualScanCode, bool(e.bKeyDown),
                         e.uChar if e.bKeyDown else "", bool(ctrl))


class WindowControl:
    """把控制台里的鼠标键盘映射到目标窗口上。

    坐标映射：控制台的鼠标事件给的是「第几行第几列字符」，
    乘一下比例就得到目标窗口客户区里的像素坐标 —— 因为字符网格
    是按目标窗口的宽高比算出来的，所以这个线性映射是对得上的。

    真实操控（real=True）到底动不动系统光标
    ----------------------------------------
    分两种形态，靠 real_quiet 选：

    **安静形态（默认，real_quiet=True）—— 系统光标和前台窗口都不碰**
        鼠标、键盘全部当消息投给目标窗口。光标留在你手底下不动，前台
        也不换，投屏窗口一直待在最前面看得到画面。这是默认形态：
        「点一下画面，光标和窗口就跳到别处去」本身就是 bug，点完那一下
        人就找不着北了。

    **接管形态（real_quiet=False，命令行 --real-takeover）—— 真动系统**
        按下 / 拖拽 / 滚轮会把系统光标搬到目标窗口对应的点上，并按下去
        补一次真实点击，键盘也走 keybd_event。留给那些不认消息、非要
        真实输入的程序（部分游戏）。
        代价说清楚：光标会被搬走、目标窗口会被提到前台、投屏窗口退到后面。

    为什么连「跟着鼠标搬光标」都不能做（踩过的大坑）：
        移动就 SetCursorPos 的话，光标被搬走 → 控制台在新位置又报一个
        MOUSE_MOVE → 又搬一次 …… 同一个窗口里形成正反馈的伺服环，几个
        像素的鼠标抖动会被放大成满屏乱飞，而且这之后光标再也出不了投屏窗口
        （每个新格都把它拽回去）。现象就是「鼠标根本进不了窗口」。
    """

    def __init__(self, hwnd, cols, rows, real=False, real_quiet=True):
        self.hwnd = hwnd
        self.cols = cols
        self.rows = rows
        self.real = real
        self.real_quiet = real_quiet
        self.u = ctypes.windll.user32
        self.k = ctypes.windll.kernel32
        self.sent = 0
        self.rc = _RECT()
        # 按着的键。拖拽时 WM_MOUSEMOVE 要带上按键状态，目标窗口才知道在拖。
        self._down_buttons = 0
        # 接管形态专用：有没有键按着（只有拖拽时才跟着鼠标搬光标）。
        self._down = False
        # 滚轮兜底：投递 WM_MOUSEWHEEL 没人理时，改用 WM_VSCROLL
        self._scroll_prev = {}
        self._last_wheel_hwnd = None
        self._scroll_fix = False

    def refresh(self):
        self.u.GetClientRect(self.hwnd, ctypes.byref(self.rc))

    def _to_client(self, pos):
        mx, my = pos
        w = max(1, self.rc.right - self.rc.left)
        h = max(1, self.rc.bottom - self.rc.top)
        x = int(mx * w / max(1, self.cols))
        y = int(my * h / max(1, self.rows))
        return min(max(x, 0), w - 1), min(max(y, 0), h - 1)

    @staticmethod
    def _lparam(x, y):
        return ((y & 0xFFFF) << 16) | (x & 0xFFFF)

    def _post(self, msg, wparam, lparam):
        return self._post_to(self.hwnd, msg, wparam, lparam)

    def _post_to(self, hwnd, msg, wparam, lparam):
        ok = self.u.PostMessageW(hwnd, msg, wparam, lparam)
        self.sent += 1
        if not ok and os.environ.get("CMDVIDEO_DEBUG"):
            try:
                with open(os.path.join(tempfile.gettempdir(), "cmdvideo_events.log"),
                          "a", encoding="utf-8") as f:
                    f.write("POST FAILED msg=0x%x hwnd=%s err=%s\n"
                            % (msg, hwnd, ctypes.get_last_error()))
            except Exception:
                pass
        return ok

    def _to_screen(self, x, y):
        # 一定要用 _POINT（8 字节）而不是 _COORD（4 字节），见 _POINT 的注释
        pt = _POINT(x, y)
        self.u.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.X, pt.Y

    def _wheel_target(self, x, y):
        """滚轮该投给谁：**目标窗口里鼠标位置最深的那个子窗口**。

        为什么要往下找（两个实测出来的坑）：

        1. `WM_MOUSEWHEEL` 和 `WM_LBUTTONDOWN` 不一样，**不会沿着窗口层级
           往下转发**。记事本这类程序真正处理滚动的是一层子窗口（编辑区），
           顶层窗口收到滚动消息就那么晾着 —— 现象是「点击有反应、滚轮纹丝不动」，
           而日志里显示「消息已成功投递」，特别容易误判成没发出去。
        2. **不能靠 WindowFromPoint 找**：投屏窗口是最大化的、还常在最前面，
           那个 API 返回的是「屏幕上最顶上那个窗口」，也就是投屏窗口自己，
           永远指不到目标的子窗口。所以必须沿着**目标窗口自己的层级**往下走
           （ChildWindowFromPointEx），跟谁盖在上面无关。

        爬不到子窗口（目标就是个没有子窗口的壳）就退回顶层窗口。
        """
        try:
            CWP_SKIPINVISIBLE = 0x0001
            CWP_SKIPDISABLED = 0x0002
            pt = _POINT(x, y)                    # 客户区坐标，逐层往下换算
            node = self.hwnd
            for _ in range(16):
                child = self.u.ChildWindowFromPointEx(
                    node, pt, CWP_SKIPINVISIBLE | CWP_SKIPDISABLED)
                if not child or child == node:
                    break
                # 换成子窗口自己的客户区坐标，继续往下钻
                scr = _POINT(pt.X, pt.Y)
                self.u.ClientToScreen(node, ctypes.byref(scr))
                self.u.ScreenToClient(child, ctypes.byref(scr))
                pt = _POINT(scr.X, scr.Y)
                node = child
            return node
        except Exception:
            return self.hwnd

    def _wheel_log(self, delta, notches, pos, extra=""):
        """每次滚轮都记一笔，供排查「投屏时滚轮不灵」用。

        故意**不**放在 CMDVIDEO_DEBUG 后面：滚轮问题只在真机上复现得出来，
        等用户配好环境变量再让他重试一轮太折腾。日志很小（一次滚动几十行），
        出问题直接看 %TEMP%\\cmdvideo_wheel.log 就够了。

        两类行：
          - raw     每个原始事件（delta 可能不足一格）→ 看出是不是平滑滚轮的
                    高精度增量（±30/±60），以及控制台到底有没有上报滚轮
          - FORWARD 攒够一格真正转发的那次 → 看出格数、投给了哪个窗口、
                    PostMessage 的返回值
        """
        try:
            u = ctypes.windll.user32
            with open(os.path.join(tempfile.gettempdir(), "cmdvideo_wheel.log"),
                      "a", encoding="utf-8") as f:
                f.write("%.3f raw   delta=%-6d cell=%-9s fg_target=%s hwnd=%s\n"
                        % (time.time(), delta, pos,
                           u.GetForegroundWindow() == self.hwnd, self.hwnd))
                if notches:
                    f.write("%.3f FORWARD notches=%-4d cell=%-9s %s\n"
                            % (time.time(), notches, pos, extra))
        except Exception:
            pass

    def _trace(self, text):
        """CMDVIDEO_DEBUG=1 时把真实操控的关键步骤写到临时文件里，方便排障。"""
        if not os.environ.get("CMDVIDEO_DEBUG"):
            return
        try:
            p = os.path.join(tempfile.gettempdir(), "cmdvideo_real.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write("%.3f %s\n" % (time.time(), text))
        except Exception:
            pass

    def focus(self):
        """把目标窗口弄到前台，成功返回 True。

        为什么要这么绕：Windows 有「前台锁定」—— 不是前台进程的程序调
        SetForegroundWindow 只会闪一下任务栏、什么也不会发生。所以这里按
        成功率从高到低试几种办法：
          1. 直接 SetForegroundWindow（本来就被允许时一次就成）
          2. AttachThreadInput 到当前前台线程，借到前台权限再设
          3. 模拟按一下 Alt —— Windows 认为「用户刚按过键」就会放行
          4. SetWindowPos 置顶（不改变焦点，但至少能让它露在最前面）
        没成功也不影响 PostMessage 那条路（消息是直接塞进目标窗口队列的，
        跟谁在前台无关），所以这里只静默返回 False。

        这个函数是**阻塞式**的：调用方要放在后台线程里，否则那几十毫秒的
        前台切换会卡住渲染。
        """
        def is_fg():
            return self.u.GetForegroundWindow() == self.hwnd

        try:
            if is_fg():
                return True
        except Exception:
            pass

        def once():
            try:
                # 用 ctypes.get_last_error()（自带 use_last_error=True 的 windll），
                # kernel32.GetLastError 在 ctypes 里拿到的不一定是刚出错那次的值
                # —— 而且它不带 restype 声明时返回 None，直接格式化就抛 TypeError。
                ok = self.u.SetForegroundWindow(self.hwnd)
                self._trace("SetForegroundWindow -> %s err=%s fg=%d target=%d"
                            % (ok, ctypes.get_last_error(),
                               self.u.GetForegroundWindow(), self.hwnd))
            except Exception as e:
                self._trace("SetForegroundWindow raised %r" % (e,))
            return is_fg()

        try:
            if once():
                return True

            # 2) 借前台权限
            fg = self.u.GetForegroundWindow()
            tid_me = self.k.GetCurrentThreadId()
            tid_fg = ctypes.c_ulong(0)
            self.u.GetWindowThreadProcessId(fg, ctypes.byref(tid_fg))
            if fg and tid_fg.value and tid_fg.value != tid_me:
                attached = bool(self.u.AttachThreadInput(
                    tid_me, tid_fg.value, True))
                self._trace("AttachThreadInput fg=%d tid_fg=%d -> %s"
                            % (fg, tid_fg.value, attached))
                try:
                    self.u.BringWindowToTop(self.hwnd)
                    if once():
                        return True
                finally:
                    if attached:
                        self.u.AttachThreadInput(tid_me, tid_fg.value, False)

            # 3) 模拟一次 Alt：给当前线程挣一个「刚刚有用户输入」的资格
            VK_MENU = 0x12
            self.u.keybd_event(VK_MENU, 0, 0, 0)
            self.u.keybd_event(VK_MENU, 0, 0x0002, 0)
            if once():
                return True

            # 4) 至少让它露在最前面（不改焦点）
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            self.u.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE)
            self._trace("fallback SetWindowPos TOPMOST, is_fg=%s" % is_fg())
            return is_fg()
        except Exception as e:
            self._trace("focus raised %r" % (e,))
            return False

    def focus_console(self, con=None):
        """把**投屏窗口自己**（控制台）抢回前台，成功返回 True。

        用途：控制台的鼠标事件（尤其滚轮）只有它是前台窗口时才上报。用户先
        去点了别的窗口，再回来滚投屏，滚轮就会被系统丢掉 —— 所以真正要滚
        的时候先把窗口抢回来。手法和 focus() 一样（借前台线程的输入队列 +
        模拟一次 Alt），只是目标换成我们自己的控制台窗口。

        con 必须由调用方给出：不能用 GetConsoleWindow() 现取 —— 那个 API 返回
        「本进程当前挂着的控制台」，进程被别的程序拉起来时会拿到调用方的窗口，
        抢前台就抢错了对象。
        """
        try:
            k = ctypes.windll.kernel32
            if con is None:
                con = k.GetConsoleWindow()
            if not con:
                return False
            if self.u.GetForegroundWindow() == con:
                return True
            fg = self.u.GetForegroundWindow()
            tid_me = k.GetCurrentThreadId()
            tid_fg = ctypes.c_ulong(0)
            self.u.GetWindowThreadProcessId(fg, ctypes.byref(tid_fg))
            attached = False
            if fg and tid_fg.value and tid_fg.value != tid_me:
                attached = bool(self.u.AttachThreadInput(tid_me, tid_fg.value, True))
            try:
                self.u.BringWindowToTop(con)
                self.u.SetForegroundWindow(con)
            finally:
                if attached:
                    self.u.AttachThreadInput(tid_me, tid_fg.value, False)
            if self.u.GetForegroundWindow() == con:
                return True
            # 前台锁定挡掉了：先模拟一次 Alt（系统会认为"用户刚按过键"）再试
            VK_MENU = 0x12
            self.u.keybd_event(VK_MENU, 0, 0, 0)
            self.u.keybd_event(VK_MENU, 0, 0x0002, 0)
            self.u.SetForegroundWindow(con)
            return self.u.GetForegroundWindow() == con
        except Exception:
            return False

    def move(self, pos):
        x, y = self._to_client(pos)
        if self.real and not self.real_quiet and self._down:
            # 只有接管形态、而且按住不放（拖拽）时才搬光标：拖窗口/拉选区时
            # 目标窗口需要真实的光标位置才对得上。没按键时绝不搬，否则就是
            # 类注释里说的那个伺服环。
            sx, sy = self._to_screen(x, y)
            self.u.SetCursorPos(sx, sy)
        # 不管哪种形态，移动都要让目标窗口知道 —— 画面里的悬停效果靠它。
        self._post(WM_MOUSEMOVE, self._down_buttons, self._lparam(x, y))

    def button(self, pos, which, down):
        x, y = self._to_client(pos)
        msg = {"left": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
               "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
               "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON)}[which]
        if self.real and not self.real_quiet:
            # MOUSEEVENTF: 按下是 LEFT/RIGHT/MIDDLE_DOWN，抬起是 *_UP
            down_flag = {"left": 0x0002, "right": 0x0008, "middle": 0x0020}[which]
            up_flag = {"left": 0x0004, "right": 0x0010, "middle": 0x0040}[which]
            if down:
                # 按下去的这一刻：把光标真的放到目标窗口上的那个点，再把目标
                # 窗口提到前台，这一下才是「真实操控」——不是的话点击只会落在
                # 盖在目标上面的投屏窗口自己身上。
                sx, sy = self._to_screen(x, y)
                self.u.SetCursorPos(sx, sy)
                # focus() 内部要试着切前台，可能有几十毫秒的阻塞，扔到后台线程里
                # 去，别卡住渲染循环（投屏掉帧就是这么来的）。_down 先置上，
                # 拖拽时的 move() 才有资格跟着搬光标。
                self._down = True
                threading.Thread(target=self.focus, daemon=True).start()
                self.u.mouse_event(down_flag, 0, 0, 0, 0)
            else:
                self.u.mouse_event(up_flag, 0, 0, 0, 0)
                self._down = False
            self.sent += 1
            return
        # 安静模式（也是 real 的默认形态）：整条点击都当消息发过去。
        # 光标不动、前台不切换，目标窗口照样在按下的那一刻收到 WM_*BUTTONDOWN。
        if down:
            self._down_buttons |= msg[2]
        else:
            self._down_buttons &= ~msg[2]
        self._post(msg[0] if down else msg[1],
                   self._down_buttons if down else 0, self._lparam(x, y))

    def wheel(self, pos, notches):
        if not notches:
            notches = 1 if notches >= 0 else -1
        x, y = self._to_client(pos)
        delta = int(notches) * 120
        if self.real and not self.real_quiet:
            # 滚轮也吃光标位置：Windows 把滚轮消息发给**光标底下**的窗口，
            # 不先搬过去的话滚的是压在上面的投屏窗口自己。
            # 和点击不一样：这里搬完光标**就留在那儿**，只是为了让这一格滚轮
            # 落到目标窗口上，没有伺服环的问题（滚轮不会因为光标动了再来一条）。
            sx, sy = self._to_screen(x, y)
            self.u.SetCursorPos(sx, sy)
            self.u.mouse_event(0x0800, 0, 0, delta & 0xFFFFFFFF, 0)   # MOUSEEVENTF_WHEEL
            self.sent += 1
            return
        # WM_MOUSEWHEEL 的 lParam 用的是**屏幕坐标**，和别的鼠标消息不一样。
        # 目标也不是顶层窗口，而是鼠标底下那个子窗口 —— 滚动消息不会往下转发，
        # 投错窗口就成了「点击有反应、滚轮没反应」。
        sx, sy = self._to_screen(x, y)
        tgt = self._wheel_target(x, y)
        ok = self._post_to(tgt, WM_MOUSEWHEEL,
                           (delta & 0xFFFF) << 16, self._lparam(sx, sy))
        # 有些现代控件（记事本的 RichEditD2DPT、部分 WinUI 控件）**完全不认
        # 投递过来的 WM_MOUSEWHEEL**：post 返回 1、日志一切正常，画面纹丝不动
        # （实测确认）。判定办法是看滚动位置有没有动；没动就补发 WM_VSCROLL
        # —— 那个是经典滚动消息，真正的滚动条控件都认，而且**不用碰系统光标**。
        moved = self._scroll_moved(tgt)
        if moved is False and tgt == self._last_wheel_hwnd and not self._scroll_fix:
            self._scroll_fix = True
        if self._scroll_fix:
            self._post_to(tgt, WM_VSCROLL,
                          (SB_LINEUP if delta > 0 else SB_LINEDOWN), 0)
        self._last_wheel_hwnd = tgt
        self._wheel_log(delta, notches, pos,
                        extra="target=%d%s post=%s scroll_moved=%s vscroll_fix=%s"
                        % (tgt, "" if tgt == self.hwnd else " (child)", ok,
                           moved, self._scroll_fix))

    def _scroll_pos(self, hwnd):
        """读窗口的垂直滚动位置（读不到返回 None）。"""
        try:
            si = _SCROLLINFO()
            si.cbSize = ctypes.sizeof(_SCROLLINFO)
            si.fMask = SIF_ALL
            if self.u.GetScrollInfo(hwnd, SB_VERT, ctypes.byref(si)):
                return si.nPos
            # 编辑类控件（记事本那种）走这个
            p = _POINT(0, 0)
            r = self.u.SendMessageW(hwnd, EM_GETSCROLLPOS, 0, ctypes.byref(p))
            if r:
                return (p.X, p.Y)
        except Exception:
            pass
        return None

    def _scroll_moved(self, hwnd):
        """和这个窗口上次记录的滚动位置比一比。

        返回 True=动了 / False=没动 / None=读不到（读不到就别乱判，
        当成「可能有效」，免得对浏览器这类程序平白多做动作）。
        """
        cur = self._scroll_pos(hwnd)
        prev = self._scroll_prev.get(hwnd)
        self._scroll_prev[hwnd] = cur
        if cur is None or prev is None:
            return None
        return cur != prev

    def key(self, vk, scan, down, ch, ctrl):
        if self.real and not self.real_quiet:
            if down:
                self.u.keybd_event(vk, scan, 0, 0)
            else:
                self.u.keybd_event(vk, scan, 0x0002, 0)     # KEYEVENTF_KEYUP
            self.sent += 1
            return
        lp = (1 & 0xFFFF) | ((scan & 0xFF) << 16)
        if ctrl:
            lp |= 1 << 29          # 上下文位，配合 Ctrl 的快捷键
        if down:
            self._post(WM_KEYDOWN, vk, lp)
            # 可打印字符再补一条 WM_CHAR —— 很多程序只认 WM_CHAR 来收文本
            if ch and ord(ch) >= 32:
                self._post(WM_CHAR, ord(ch), lp)
        else:
            self._post(WM_KEYUP, vk, lp | (1 << 30) | (1 << 31))


def project(target, mode=MODE_BLOCK, ramp="ascii", use_256=False, real=False,
            capture=CAPTURE_AUTO, fps=20.0, hint=True, stats=False,
            real_quiet=True):
    """把另一个窗口投到控制台里显示，并且可以操控它。

    target —— hwnd，或者窗口标题/进程名的关键字
    """
    setup_output()
    enable_ansi()
    out = sys.stdout
    if out is None or not hasattr(out, "write"):
        try:
            out = open("CONOUT$", "w", encoding="utf-8", errors="replace",
                       buffering=1)
            sys.stdout = out
        except Exception:
            out = open(os.devnull, "w", encoding="utf-8")

    hwnd = target if isinstance(target, int) else find_window(target)
    if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
        print("找不到目标窗口。用 --list-windows 看看有哪些窗口可选。")
        return 1
    title = window_title(hwnd)

    maximize_console()
    term_cols, term_rows = term_size()
    rows = max(4, term_rows - (1 if hint else 0))

    # 先按目标窗口的宽高比算网格，再按实际尺寸微调
    rc = _RECT()
    ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rc))
    tw = max(1, rc.right - rc.left)
    th = max(1, rc.bottom - rc.top)
    px_per_cell = 1 if mode == MODE_CHARS else 2
    cell_aspect = guess_cell_aspect()
    cols, rows = fit_size(tw, th, term_cols, rows, px_per_cell, cell_aspect)

    renderer = Renderer(cols, rows, mode=mode, ramp=ramp, use_256=use_256,
                        pixel_stride=4, r_off=2, g_off=1, b_off=0)
    cap = WindowCapture(hwnd, cols, rows, px_per_cell, capture)
    ctl = WindowControl(hwnd, cols, rows, real=real, real_quiet=real_quiet)
    ctl.refresh()
    # 把自己这个控制台窗口的句柄交给输入层：滚轮要用它把窗口抢回前台，
    # 这里取最可靠（本进程就是这个控制台的主人）。
    inp = ConsoleInput(ctl, console_hwnd=ctypes.windll.kernel32.GetConsoleWindow())
    # 移动事件的限速跟着投屏帧率走（见 ConsoleInput._emit_move）
    inp.fps = fps
    input_ok = inp.start()

    # 输入线程一起步就该把控制台设成鼠标上报模式；这中间有个几十毫秒的窗口期，
    # 那时候哪怕用户的手已经在窗口上了，控制台也不会报事件 —— 等它两帧再开画面。
    time.sleep(0.12)

    out.write("\x1b[?25l\x1b[?7l\x1b[2J")
    out.flush()
    set_title("%s - 投屏：%s" % (APP, title))

    if real and not real_quiet:
        # 接管形态才需要目标窗口在前台（真实按键要有人收）。直接
        # SetForegroundWindow 会被前台锁定挡掉（自己的窗口反而是前台），
        # focus() 里用 AttachThreadInput 把前台权限借过来再设。
        # 安静形态**故意不切前台** —— 用户要的就是「点画面的时候窗口别跳走」。
        # 目标窗口如果被最小化了，挪到前台等于把它从任务栏拽出来，投屏就成了
        # 「看着任务栏画窗口」—— 这种就保持最小化，只走消息那条路。
        if not ctypes.windll.user32.IsIconic(hwnd):
            ctl.focus()

    interval = 1.0 / fps if fps > 0 else 0.05
    t_start = time.perf_counter()
    drawn = 0
    bad_frames = 0
    hint_text = "Ctrl+Q 退出 ｜ 鼠标可直接点 ｜ 键盘会转发 ｜ %s" % title
    try:
        while True:
            t0 = time.perf_counter()
            if not ctypes.windll.user32.IsWindow(hwnd):
                break
            buf = cap.grab()
            s = renderer.render(buf)
            if s:
                out.write(s)
                out.flush()
                drawn += 1
            if hint:
                out.write("\x1b[%d;1H\x1b[0m\x1b[7m %s \x1b[0m"
                          % (rows + 1, hint_text[:max(0, cols - 2)]))
                out.flush()
            # 处理输入（不阻塞）
            if input_ok and inp.pump():
                break
            gap = t_start + (drawn + 1) * interval - time.perf_counter()
            if gap > 0.0005:
                time.sleep(gap)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass
    finally:
        inp.restore()
        cap.release()
        try:
            out.write("\x1b[0m\x1b[?7h\x1b[?25h\n")
            out.flush()
        except Exception:
            pass

    if stats:
        print("投屏 %s：画了 %d 帧，转发输入 %d 次"
              % (title, drawn, ctl.sent))
    return 0


# ---------------------------------------------------------------- 图形界面


MODES = [
    ("block", "色块（最细腻）", "每格 2 个纵向像素，画面最接近原片"),
    ("chars", "彩色字符", "字符画风格，输出量只有色块的三分之一"),
    ("gray", "黑白", "不带颜色，速度最快"),
]

RAMP_DESC = {
    "ascii": " .:-=+*#%@",
    "dense": "70 级长阶梯，细节最多",
    "blocks": " ░▒▓█  方块阴影，颗粒感最强",
    "simple": " .:-=+*#  8 级，干净",
    "dots": " ·•●  点阵风",
}


def run_launcher(argv):
    """图形界面：选文件、选画法，分辨率自动适配窗口"""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    initial = argv[1] if len(argv) > 1 and os.path.exists(argv[1]) else None
    state = {"path": initial, "mode": "block", "ramp": "ascii",
             "loop": False, "audio": True, "fps": 0, "target": None}

    # ── 为什么长文本标签必须限宽 ──────────────────────────────────────
    # ttk.Label 的「请求宽度」= 整段文字的宽度。视频路径和窗口标题都是
    # 用户给的长文本，一旦选中一个长路径，整块内容就被撑宽；
    # 而窗口是按启动时的尺寸定死的（那时还没选文件、文本很短），
    # 于是内容比窗口宽 → 右边一截被裁掉，正好是「浏览…」和「开始播放」。
    # 现象就是：选了文件之后，「开始播放」不见了。
    # 所以凡是显示用户长文本的标签，一律给 wraplength 把宽度钉死在
    # WRAP 像素以内 —— 放不下就换行，完整路径照样看得到，
    # 不需要额外的悬停提示（弹窗很烦）。
    WRAP = 440

    root = tk.Tk()
    root.title("cmdvideo —— 在 cmd 窗口里放视频")

    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)

    # 整个窗口分两块：上面是「真正可滚动」的内容区，下面是「永远可见」的按钮栏。
    # 内容放进 Canvas，所以内容再高也只会出竖向滚动条、再宽也只会出横向滚动条，
    # 绝不会把底下的「开始播放」挤出窗口（这是真踩过的坑，见下面 WRAP）。
    outer = ttk.Frame(root)
    outer.grid(row=0, column=0, sticky="nsew")
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(0, weight=1)   # 只有内容区吸收多出来/不够的高度
    outer.rowconfigure(2, weight=0)   # 按钮栏永远保持自己的自然高度

    canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
    vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    hbar = ttk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vbar.grid(row=0, column=1, sticky="ns")

    content = ttk.Frame(canvas, padding=(16, 16, 16, 6))
    content.columnconfigure(0, weight=1)
    cwin = canvas.create_window((0, 0), window=content, anchor="nw")
    hbar_shown = {"on": False}

    def sync_scroll(_=None):
        """内容尺寸一变就重算滚动范围；内容比可视区窄就拉满、宽就出横向条。"""
        canvas.configure(scrollregion=canvas.bbox("all"))
        want = max(content.winfo_reqwidth(), canvas.winfo_width())
        try:
            cur = float(canvas.itemcget(cwin, "width"))
        except (TypeError, ValueError):
            cur = -1.0
        if abs(cur - want) > 1:
            canvas.itemconfigure(cwin, width=want)
        need_h = content.winfo_reqwidth() > canvas.winfo_width() + 1
        if need_h and not hbar_shown["on"]:
            hbar.grid(row=1, column=0, sticky="ew")
            hbar_shown["on"] = True
        elif not need_h and hbar_shown["on"]:
            hbar.grid_remove()
            hbar_shown["on"] = False

    content.bind("<Configure>", sync_scroll)
    canvas.bind("<Configure>", sync_scroll)

    def on_wheel(e):
        """滚轮只在内容真的超出可视高度时才滚，且不抢下拉框/表格自己的滚轮。"""
        w = root.winfo_containing(e.x_root, e.y_root)
        if w is not None and w.winfo_class() in (
                "Treeview", "Text", "Listbox", "Scrollbar", "TScrollbar",
                "TCombobox", "Combobox"):
            return
        if content.winfo_reqheight() <= canvas.winfo_height():
            return
        canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    root.bind_all("<MouseWheel>", on_wheel)

    frm = ttk.Frame(content)
    frm.grid(row=0, column=0, sticky="nsew")
    frm.columnconfigure(1, weight=1)

    # ---- 文件 ----
    ttk.Label(frm, text="视频文件", font=("Microsoft YaHei UI", 10, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w")

    file_box = ttk.Frame(frm)
    file_box.grid(row=1, column=0, columnspan=3, sticky="we", pady=(4, 0))
    file_box.columnconfigure(0, weight=1)
    path_var = tk.StringVar(value=state["path"] or "")
    ttk.Label(file_box, textvariable=path_var, anchor="w", wraplength=WRAP,
              justify="left", foreground="#444", background="#f4f4f4",
              relief="solid", borderwidth=1, padding=(6, 4)).grid(
        row=0, column=0, sticky="we")

    def pick():
        p = filedialog.askopenfilename(
            title="选择一个视频",
            filetypes=[("视频文件", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.wmv *.m4v"),
                       ("所有文件", "*.*")])
        if p:
            state["path"] = p
            path_var.set(p)

    ttk.Button(file_box, text="浏览...", command=pick).grid(
        row=0, column=1, sticky="e", padx=(8, 0))

    ttk.Separator(frm, orient="horizontal").grid(
        row=2, column=0, columnspan=3, sticky="we", pady=12)

    # ---- 画法 ----
    ttk.Label(frm, text="播放画法", font=("Microsoft YaHei UI", 10, "bold")).grid(
        row=3, column=0, columnspan=3, sticky="w")
    mode_var = tk.StringVar(value="block")
    for i, (val, label, hint) in enumerate(MODES):
        ttk.Radiobutton(frm, text=label, value=val, variable=mode_var).grid(
            row=4 + i, column=0, sticky="w", pady=1)
        ttk.Label(frm, text=hint, foreground="#777",
                  font=("Microsoft YaHei UI", 8)).grid(
            row=4 + i, column=1, columnspan=2, sticky="w", padx=(10, 0))

    ramp_row = 4 + len(MODES)
    ramp_hdr = ttk.Frame(frm)
    ramp_hdr.grid(row=ramp_row, column=0, columnspan=3, sticky="we", pady=(12, 0))
    ttk.Label(ramp_hdr, text="字符阶梯",
              font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w")
    ttk.Label(ramp_hdr, text="（只在「彩色字符」画法下生效）",
              foreground="#777", font=("Microsoft YaHei UI", 8)).grid(
        row=0, column=1, sticky="w", padx=(8, 0))

    ramp_box_row = ttk.Frame(frm)
    ramp_box_row.grid(row=ramp_row + 1, column=0, columnspan=3, sticky="we", pady=(4, 0))
    ramp_var = tk.StringVar(value="ascii")

    ramp_labels = {
        "ascii": "ascii —— 10 级通用",
        "dense": "dense —— 70 级，细节最多",
        "blocks": "blocks —— 方块阴影",
        "simple": "simple —— 8 级，干净",
        "dots": "dots —— 点阵风",
        "custom": "自定义 —— 自己输入一串字符",
    }
    ramp_display = {k: ramp_labels[k] for k in RAMP_PRESETS}
    ramp_display["custom"] = ramp_labels["custom"]

    ramp_box = ttk.Combobox(ramp_box_row, width=24, state="readonly",
                            values=[ramp_display[k] for k in
                                    list(RAMP_PRESETS.keys()) + ["custom"]])
    ramp_box.grid(row=0, column=0, sticky="w")
    ramp_box.current(0)

    # 预览固定宽度 + 超长截断：dense 是 70 级长阶梯，
    # 不限制的话标签会横向撑爆整个窗口，把下面的按钮挤出屏幕外。
    PREVIEW_CHARS = 34

    def fit_preview(chars):
        chars = chars or ""
        if len(chars) > PREVIEW_CHARS:
            return chars[:PREVIEW_CHARS - 3] + "..."
        return chars

    ramp_preview = ttk.Label(ramp_box_row, text="", foreground="#0a6",
                             font=("Consolas", 11), width=PREVIEW_CHARS + 8,
                             anchor="w")
    ramp_preview.grid(row=0, column=1, sticky="w", padx=(10, 0))

    custom_var = tk.StringVar(value="")
    custom_entry = ttk.Entry(ramp_box_row, textvariable=custom_var, width=16,
                             font=("Consolas", 10))
    custom_entry.grid(row=0, column=2, sticky="w", padx=(10, 0))

    # 反查：下拉框显示的中文 -> 内部的预设名
    label_to_key = {v: k for k, v in ramp_display.items()}

    def current_ramp_key():
        return label_to_key.get(ramp_box.get(), "ascii")

    # ---- 选项 ----
    opt_row = ramp_row + 2
    ttk.Separator(frm, orient="horizontal").grid(
        row=opt_row, column=0, columnspan=3, sticky="we", pady=12)

    opt_box = ttk.Frame(frm)
    opt_box.grid(row=opt_row + 1, column=0, columnspan=3, sticky="we")
    loop_var = tk.BooleanVar(value=False)
    audio_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(opt_box, text="循环播放", variable=loop_var).grid(
        row=0, column=0, sticky="w")
    ttk.Checkbutton(opt_box, text="播放声音", variable=audio_var).grid(
        row=0, column=1, sticky="w", padx=(16, 0))
    ttk.Label(opt_box, text="最大帧率",
              font=("Microsoft YaHei UI", 9)).grid(
        row=0, column=2, sticky="w", padx=(16, 0))
    fps_var = tk.IntVar(value=0)

    def on_fps(_=None):
        try:
            v = int(fps_var.get())
        except Exception:
            return
        if v > 120:
            fps_var.set(120)

    fps_spin = ttk.Spinbox(opt_box, from_=0, to=120, textvariable=fps_var,
                           width=6, command=on_fps)
    fps_spin.grid(row=0, column=3, sticky="w", padx=(6, 0))
    fps_spin.bind("<KeyRelease>", on_fps)
    ttk.Label(opt_box, text="0 = 跟源视频", foreground="#777",
              font=("Microsoft YaHei UI", 8)).grid(
        row=0, column=4, sticky="w", padx=(6, 0))

    ttk.Label(frm, text="分辨率会自动适配窗口大小，并保持视频原始宽高比，无需手动设置。",
              foreground="#777", font=("Microsoft YaHei UI", 8), wraplength=400,
              justify="left").grid(row=opt_row + 2, column=0, columnspan=3,
                                   sticky="w", pady=(10, 0))

    # ---- 投屏另一个窗口 ----
    proj_row = opt_row + 3
    ttk.Separator(frm, orient="horizontal").grid(
        row=proj_row, column=0, columnspan=3, sticky="we", pady=12)
    ttk.Label(frm, text="投屏另一个窗口（看得到画面，也能操控它）",
              font=("Microsoft YaHei UI", 10, "bold")).grid(
        row=proj_row + 1, column=0, columnspan=3, sticky="w")
    ttk.Label(frm, text="把系统里任意一个程序窗口搬到 cmd 窗口里用彩色字符画显示，"
                        "鼠标点击、拖动、滚轮、键盘都会转发给它。",
              foreground="#777", font=("Microsoft YaHei UI", 8), wraplength=430,
              justify="left").grid(row=proj_row + 2, column=0, columnspan=3,
                                   sticky="w", pady=(2, 0))

    proj_box = ttk.Frame(frm)
    proj_box.grid(row=proj_row + 3, column=0, columnspan=3, sticky="we", pady=(6, 0))
    proj_box.columnconfigure(0, weight=1)
    target_var = tk.StringVar(value="（还没选窗口）")
    ttk.Label(proj_box, textvariable=target_var, anchor="w", wraplength=WRAP,
              justify="left", foreground="#444", background="#f4f4f4",
              relief="solid", borderwidth=1, padding=(6, 4)).grid(
        row=0, column=0, sticky="we")

    def pick_window():
        wins = list_windows()
        if not wins:
            messagebox.showinfo("cmdvideo", "没找到可以投屏的窗口。")
            return
        dlg = tk.Toplevel(root)
        dlg.title("选择要投屏的窗口")
        dlg.transient(root)
        dlg.grab_set()
        ttk.Label(dlg, text="挑一个窗口。投屏后在新窗口里按 Ctrl+Q 退出。",
                  font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=12,
                                                       pady=(12, 6))
        box = ttk.Frame(dlg)
        box.pack(fill="both", expand=True, padx=12)
        tv = ttk.Treeview(box, columns=("title", "exe", "size"),
                          show="headings", height=12, selectmode="browse")
        for key, text, width in (("title", "窗口标题", 300), ("exe", "进程", 150),
                                 ("size", "尺寸", 90)):
            tv.heading(key, text=text)
            tv.column(key, width=width, anchor="w" if key != "size" else "center")
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for hwnd, title, exe, w, h in wins:
            tv.insert("", "end", iid=str(hwnd),
                      values=(title, exe, "%dx%d" % (w, h)))
        kids = tv.get_children()
        if kids:
            tv.selection_set(kids[0])
            tv.focus(kids[0])

        def ok(_=None):
            sel = tv.selection()
            if not sel:
                return
            hwnd = int(sel[0])
            state["target"] = hwnd
            target_var.set("%s  (hwnd=%d)" % (window_title(hwnd) or "?", hwnd))
            dlg.destroy()

        bar = ttk.Frame(dlg)
        bar.pack(fill="x", padx=12, pady=12)
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right")
        ttk.Button(bar, text="确定", command=ok).pack(side="right", padx=(0, 8))
        tv.bind("<Double-1>", ok)

    ttk.Button(proj_box, text="选择窗口...", command=pick_window).grid(
        row=0, column=1, sticky="e", padx=(8, 0))

    proj_opt = ttk.Frame(frm)
    proj_opt.grid(row=proj_row + 4, column=0, columnspan=3, sticky="we", pady=(6, 0))
    real_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(proj_opt, text="真实操控（输入直接投给目标窗口，光标/前台都不动）",
                    variable=real_var).grid(row=0, column=0, sticky="w")
    # 「接管鼠标」那个勾选框故意不放进界面：它会真的搬系统光标、切前台，
    # 表现就是「点一下画面光标闪到目标窗口那边去」，正是用户报的那个 bug。
    # 需要老行为的走命令行 --real-takeover，不摆在界面上等人误点。
    ttk.Label(proj_opt, text="抓帧方式", font=("Microsoft YaHei UI", 9)).grid(
        row=0, column=1, sticky="w", padx=(16, 0))
    cap_names = ["自动（推荐）", "PrintWindow", "抓屏 BitBlt"]
    cap_box = ttk.Combobox(proj_opt, width=15, state="readonly", values=cap_names)
    cap_box.current(0)
    cap_box.grid(row=0, column=2, sticky="w", padx=(6, 0))

    def project_go():
        if not state.get("target"):
            messagebox.showwarning("cmdvideo", "请先点「选择窗口...」挑一个要投屏的窗口。")
            return
        m = {"gray": MODE_GRAY, "chars": MODE_CHARS}.get(mode_var.get(), MODE_BLOCK)
        key = current_ramp_key()
        ramp = custom_var.get() if key == "custom" else key
        cap = {cap_names[0]: CAPTURE_AUTO, cap_names[1]: CAPTURE_PRINT,
               cap_names[2]: CAPTURE_SCREEN}[cap_box.get()]
        try:
            pfps = int(fps_var.get())
        except Exception:
            pfps = 0
        rc = _project_in_new_console(state["target"], mode=m, ramp=ramp,
                                     real=bool(real_var.get()), capture=cap,
                                     fps=pfps or None)
        if rc == 0:
            status_var.set("已开始投屏 —— 在投屏窗口里按 Ctrl+Q 退出。")
        else:
            status_var.set("启动投屏失败。")

    def sync_ramp(*_):
        key = current_ramp_key()
        if key == "custom":
            custom_entry.configure(state="normal")
            ramp_preview.configure(text="")
        else:
            custom_entry.configure(state="disabled")
            ramp_preview.configure(
                text="预览: %s" % fit_preview(RAMP_PRESETS.get(key, "")))

    ramp_box.bind("<<ComboboxSelected>>", sync_ramp)

    def on_custom(*_):
        if current_ramp_key() == "custom":
            ramp_preview.configure(text="预览: %s" % fit_preview(custom_var.get()))

    custom_var.trace_add("write", on_custom)
    sync_ramp()

    def go():
        if not state["path"] or not os.path.exists(state["path"]):
            messagebox.showwarning("cmdvideo", "请先选择一个视频文件。")
            return
        # 先检查 ffmpeg：播放器是在**另一个控制台**里跑的，它找不到 ffmpeg 会
        # 立刻以退出码 2 结束 —— 那个窗口一闪就没了，用户只看到「点了没反应」。
        # 所以这里先自己找一遍，找不到就当面说清楚，别让人对着空气猜。
        if not find_ffmpeg():
            messagebox.showerror(
                "cmdvideo",
                "找不到 ffmpeg.exe，放不了视频。\n\n"
                "把 ffmpeg.exe 放到 cmdvideo.exe 同一个目录就行"
                "（安装包本来就会放到一起；如果 exe 是单独拷出来的，"
                "就会缺这个文件）。\n\n"
                "也可以设环境变量 CMDVIDEO_FFMPEG 指向 ffmpeg.exe。")
            status_var.set("缺少 ffmpeg.exe —— 放到 cmdvideo.exe 同目录即可。")
            return
        state["mode"] = mode_var.get()
        key = current_ramp_key()
        state["ramp"] = custom_var.get() if key == "custom" else key
        state["loop"] = bool(loop_var.get())
        state["audio"] = bool(audio_var.get())
        try:
            state["fps"] = int(fps_var.get())
        except Exception:
            state["fps"] = 0

        # 主窗口**保持开着**：用户关了播放窗口还能再点一次开始播放。
        # 早期版本这里直接 root.destroy()，导致一按播放主窗口就消失了。
        mode = {"gray": MODE_GRAY, "chars": MODE_CHARS}.get(
            state["mode"], MODE_BLOCK)
        rc = _play_in_new_console(state["path"], mode=mode, ramp=state["ramp"],
                                  loop=state["loop"], audio=state["audio"],
                                  fps=state["fps"] or None)
        if rc == 0:
            status_var.set("已开始播放 —— 可以在播放窗口里按 Ctrl+C 停止。")
        else:
            status_var.set("启动播放器失败，请检查 ffmpeg 是否在程序目录里。")

    # ---- 固定按钮栏（永远贴在窗口底部，不会被内容挤出可见区域）----
    footer = ttk.Frame(outer, padding=(16, 6, 16, 14))
    footer.grid(row=2, column=0, columnspan=2, sticky="ew")
    footer.columnconfigure(0, weight=1)

    status_var = tk.StringVar(
        value="选好视频和画法，点「开始播放」会另开一个窗口播放。")
    ttk.Label(footer, textvariable=status_var, foreground="#666",
              font=("Microsoft YaHei UI", 8), wraplength=360,
              justify="left").grid(row=0, column=0, sticky="w", pady=(0, 6))

    btns = ttk.Frame(footer)
    btns.grid(row=1, column=0, sticky="e")
    ttk.Button(btns, text="取消", command=root.destroy).grid(row=0, column=0, padx=(0, 8))
    proj_btn = ttk.Button(btns, text="开始投屏", command=project_go)
    proj_btn.grid(row=0, column=1, padx=(0, 8))
    go_btn = ttk.Button(btns, text="开始播放", command=go)
    go_btn.grid(row=0, column=2)

    root.bind("<Return>", lambda e: go())
    root.bind("<Escape>", lambda e: root.destroy())
    root.update_idletasks()

    # 内容在 Canvas 里，所以窗口尺寸要分别拿「内容」和「按钮栏」来算：
    # 宽度取两者较大者，高度只给内容设上限 —— 按钮栏永远保留自己的高度，
    # 不会被内容顶出窗口。
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    cw = content.winfo_reqwidth()
    ch = content.winfo_reqheight()
    fh = footer.winfo_reqheight()
    # 竖滚动条一直占着一列，算宽度时要把它让出来，
    # 否则内容永远差那十几像素，横向滚动条会一直挂着。
    w = max(cw + vbar.winfo_reqwidth(), footer.winfo_reqwidth(), 560)
    w = min(w, sw - 40)                 # 屏幕放不下就贴边，靠横向滚动条看
    h = min(ch, sh - 120) + fh
    h = min(h, sh - 60)
    canvas.configure(height=max(120, h - fh))
    root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                   max(0, (sh - h) // 3)))
    # 允许放大（字大/屏幕大时可以拉宽看清完整路径），但不允许缩到装不下
    root.minsize(min(w, 560), min(h, 420))
    root.resizable(True, True)
    root.deiconify()
    root.lift()
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    root.focus_force()

    # 主窗口一直活着，播放交给另开的控制台窗口。
    # 用户关掉播放窗口后，这里依然开着一个窗口可以再次播放。
    root.mainloop()
    return 0


def _play_in_new_console(path, mode=MODE_BLOCK, ramp="ascii", loop=False,
                         audio=True, fps=None):
    """另开一个控制台窗口播放，让画面有独立的窗口"""
    # 打包后重启自己；脚本模式则通过当前 .py 文件重启
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--play", path]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--play", path]
    cmd += ["--mode", mode, "--ramp", ramp]
    if loop:
        cmd.append("--loop")
    if not audio:
        cmd.append("--mute")
    if fps:
        cmd += ["--fps", str(fps)]

    # CREATE_NEW_CONSOLE 让子进程拥有自己的窗口。
    # DETACHED_PROCESS 不能和它同时用（Windows 会报 87 参数错误），
    # 所以这里只加 CREATE_NEW_CONSOLE，界面进程自己立刻退出即可。
    CREATE_NEW_CONSOLE = 0x00000010
    try:
        env = os.environ.copy()
        # 让这个窗口走「经典控制台宿主(conhost)」而不是 Windows Terminal。
        # Windows Terminal 是标签式宿主，它把真正的字符窗口藏在里面，
        # SetConsoleWindowInfo / MoveWindow 改的是那个隐藏窗口，用户看不到效果。
        # 这两个环境变量是 Windows 官方用来控制终端委派的开关。
        env["WT_SESSION"] = ""
        env["WT_PROFILE_ID"] = ""
        subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE, close_fds=True,
                         env=env)
        return 0
    except Exception as e:
        print("启动播放器失败：%s" % e)
        return 3


def _project_in_new_console(hwnd, mode=MODE_BLOCK, ramp="ascii", real=False,
                            capture=CAPTURE_AUTO, fps=None, takeover=False):
    """另开一个控制台窗口，把指定窗口投屏进去"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--project", str(hwnd)]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--project", str(hwnd)]
    cmd += ["--mode", mode, "--ramp", ramp, "--capture", capture]
    if real:
        cmd.append("--real")
    if takeover:
        # 接管形态：真的搬光标、真的切前台（老行为），只有明确要才开
        cmd.append("--real-takeover")
    if fps:
        cmd += ["--fps", str(fps)]

    CREATE_NEW_CONSOLE = 0x00000010
    try:
        env = os.environ.copy()
        # 和播放一样，强制走经典 conhost，不然 SetConsoleWindowInfo 改的是
        # Windows Terminal 里那个看不见的真窗口，用户看不到效果。
        env["WT_SESSION"] = ""
        env["WT_PROFILE_ID"] = ""
        subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE, close_fds=True,
                         env=env)
        return 0
    except Exception as e:
        print("启动投屏失败：%s" % e)
        return 3


# ---------------------------------------------------------------- 入口


def main():
    argv = sys.argv

    # 内部模式：在当前（已打开的）控制台里真正渲染画面
    if len(argv) > 2 and argv[1] == "--play":
        ap = argparse.ArgumentParser(prog=APP)
        ap.add_argument("--play", action="store_true")
        ap.add_argument("video")
        ap.add_argument("--mode", choices=[MODE_BLOCK, MODE_CHARS, MODE_GRAY],
                        default=MODE_BLOCK)
        ap.add_argument("--ramp", default="ascii")
        ap.add_argument("--fps", type=float, default=None)
        ap.add_argument("--loop", action="store_true")
        ap.add_argument("--mute", action="store_true")
        ap.add_argument("--color", choices=["true", "256"], default="true")
        ap.add_argument("--stats", action="store_true")
        a = ap.parse_args(argv[1:])
        return play(a.video, fps_override=a.fps, loop=a.loop,
                    use_256=(a.color == "256"), mode=a.mode, ramp=a.ramp,
                    stats=a.stats, audio=not a.mute)

    # 内部模式：在当前控制台里投屏另一个窗口
    if len(argv) > 2 and argv[1] == "--project":
        ap = argparse.ArgumentParser(prog=APP)
        ap.add_argument("--project", action="store_true")
        ap.add_argument("target", help="窗口标题关键字，或窗口句柄数字")
        ap.add_argument("--mode", choices=[MODE_BLOCK, MODE_CHARS, MODE_GRAY],
                        default=MODE_BLOCK)
        ap.add_argument("--ramp", default="ascii")
        ap.add_argument("--fps", type=float, default=None)
        ap.add_argument("--real", action="store_true",
                        help="真实操控：输入直接投给目标窗口，但不动系统光标、不切前台")
        ap.add_argument("--real-takeover", action="store_true",
                        help="接管形态的真实操控：真的搬系统光标、真的切前台")
        ap.add_argument("--capture",
                        choices=[CAPTURE_AUTO, CAPTURE_PRINT, CAPTURE_SCREEN],
                        default=CAPTURE_AUTO)
        ap.add_argument("--no-hint", action="store_true")
        ap.add_argument("--stats", action="store_true")
        a = ap.parse_args(argv[1:])
        return project(a.target, mode=a.mode, ramp=a.ramp,
                       real=a.real or a.real_takeover,
                       real_quiet=not a.real_takeover,
                       capture=a.capture, fps=a.fps or 20.0,
                       hint=not a.no_hint, stats=a.stats)

    # 普通命令行：给个视频路径（或加参数、或不给参数）都支持
    ap = argparse.ArgumentParser(
        prog=APP,
        description="在 cmd 窗口里播放视频，或者把另一个窗口投屏过来（色块 / 彩色字符）")
    ap.add_argument("video", nargs="?", help="视频文件路径")
    ap.add_argument("--gui", action="store_true", help="强制打开选择窗口")
    ap.add_argument("--cols", type=int, default=None, help="横向字符数（默认铺满窗口）")
    ap.add_argument("--rows", type=int, default=None, help="纵向字符行数（默认铺满窗口）")
    ap.add_argument("--fps", type=float, default=None, help="强制帧率")
    ap.add_argument("--loop", action="store_true", help="循环播放")
    ap.add_argument("--color", choices=["true", "256"], default="true",
                    help="true=24 位真彩色（推荐），256=兼容老终端")
    ap.add_argument("--mode", choices=[MODE_BLOCK, MODE_CHARS, MODE_GRAY],
                    default=None,
                    help="block=色块（默认，最细腻）；chars=彩色字符；gray=黑白")
    ap.add_argument("--ramp", default="ascii",
                    help="chars 模式用的明暗字符阶梯，可以填预设名（%s），"
                         "也可以直接给一串自定义字符" % "/".join(RAMP_PRESETS))
    ap.add_argument("--gray", action="store_true", help="等同 --mode gray")
    ap.add_argument("--mute", action="store_true", help="不播声音")
    ap.add_argument("--stats", action="store_true", help="结束时打印统计")
    ap.add_argument("--check", action="store_true", help="只检查 ffmpeg 是否可用")
    ap.add_argument("--version", action="store_true", help="打印版本号后退出")
    ap.add_argument("--list-windows", action="store_true",
                    help="列出所有可以投屏的窗口，然后退出")
    ap.add_argument("--window", default=None, metavar="关键字",
                    help="投屏指定的窗口（标题/进程名关键字，或窗口句柄数字）")
    ap.add_argument("--real", action="store_true",
                    help="投屏时真实操控：输入直接投给目标窗口，但不动系统光标、不切前台")
    ap.add_argument("--real-takeover", action="store_true",
                    help="接管形态的真实操控：真的搬系统光标、真的切前台（老行为）")
    ap.add_argument("--capture", choices=[CAPTURE_AUTO, CAPTURE_PRINT, CAPTURE_SCREEN],
                    default=CAPTURE_AUTO,
                    help="投屏抓帧方式：auto=自动（默认），print=PrintWindow，screen=抓屏")
    ap.add_argument("--project-fps", type=float, default=None,
                    help="投屏的刷新帧率，默认 20")
    args, _unknown = ap.parse_known_args(argv[1:])

    if args.version:
        # exe 是 --windowed 打包的，不先接个控制台的话这里 print 出去谁也看不见
        setup_output()
        print("%s %s" % (APP, __version__))
        return 0

    if args.list_windows:
        # 打包成 --windowed 的 exe 默认没有控制台，不先接一个的话
        # 这里 print 出来的东西用户根本看不见（双击 exe 用命令行参数时会踩到）。
        setup_output()
        wins = list_windows()
        if not wins:
            print("没找到可以投屏的窗口。")
            return 0
        print("可以投屏的窗口（共 %d 个）：" % len(wins))
        print("  %-12s %-9s %-22s %s" % ("句柄", "尺寸", "进程", "标题"))
        for hwnd, title, exe, w, h in wins:
            print("  %-12d %-9s %-22s %s" % (hwnd, "%dx%d" % (w, h), exe[:20], title))
        print()
        print("投屏示例：cmdvideo --window \"关键字\"")
        return 0

    if args.window:
        return project(args.window, mode=args.mode or MODE_BLOCK, ramp=args.ramp,
                       real=args.real or args.real_takeover,
                       real_quiet=not args.real_takeover,
                       capture=args.capture,
                       fps=args.project_fps or 20.0, stats=args.stats)

    # 一个参数都没有 -> 打开图形界面（双击 exe 的情况）
    if len(argv) <= 1:
        return run_launcher(argv)

    # 带了参数 -> 走普通命令行播放
    if args.mode or args.cols or args.rows or args.fps or args.loop or args.gray \
            or args.mute or args.stats or args.check or args.color != "true":
        if args.check:
            setup_output()
            ff = find_ffmpeg()
            print("ffmpeg: %s" % (ff or "未找到"))
            return 0 if ff else 2
        if not args.video:
            run_launcher(argv)
            return 0
        mode = MODE_GRAY if args.gray else (args.mode or MODE_BLOCK)
        return play(args.video, cols=args.cols, rows=args.rows,
                    fps_override=args.fps, loop=args.loop,
                    use_256=(args.color == "256"), mode=mode, ramp=args.ramp,
                    stats=args.stats, audio=not args.mute)

    # 只给了一个视频路径 -> 相当于双击了视频文件，问用户想怎么播
    if args.video and os.path.exists(args.video):
        return run_launcher(argv)

    ap.print_help()
    return 1


def _crash_report(exc_text):
    """把异常写到日志，并弹个框 —— --windowed 的程序没有控制台，
    不这样做用户只会看到「双击没反应」。"""
    import tempfile
    log = os.path.join(tempfile.gettempdir(), "cmdvideo_error.log")
    try:
        with open(log, "w", encoding="utf-8") as f:
            f.write(exc_text)
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(
            None, "cmdvideo 出错了：\n\n%s\n\n详细信息已写到：\n%s"
            % (exc_text.strip().splitlines()[-1], log),
            "cmdvideo 错误", 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        _crash_report(traceback.format_exc())
        sys.exit(1)
