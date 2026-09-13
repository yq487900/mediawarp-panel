#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MediaWarp (emby-av) 设置面板 + 进程守护  ——  单上游版 + 密码模式
   · 9002 = MediaWarp 本体（一份配置：/app/config/config.yaml）
   · 9003 = 本面板：卡片式设置 / 原始 YAML / 日志；底部固定操作条
   · 密码模式：
       1) 首次启动在容器日志里找「初始密码」，用它登录
       2) 登录后强制设置新密码（两次输入、需一致、≥6 位）
       3) 之后一律以新密码为准；面板内也可再改密码
       4) 忘记密码：删掉 /app/ui_state.json 重启容器 → 日志里会给新的初始密码
"""
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from glob import glob
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

APP = os.environ.get('MW_HOME', '/app')
CFG = os.path.join(APP, 'config', 'config.yaml')
STATE = os.path.join(APP, 'ui_state.json')
SESSFILE = os.path.join(APP, 'sessions.json')   # 会话持久化：进程/容器重启后不必重新登录
MW = os.environ.get('MW_BIN') or os.path.join(APP, 'MediaWarp')
                                   # 二进制位置：默认跟着数据目录；镜像部署用 MW_BIN 指向 /opt/mediawarp/MediaWarp
MW_LOG = os.path.join(APP, 'logs', 'mediawarp.out')
UI_PORT = int(os.environ.get('UI_PORT', '9009'))
MW_PORT = os.environ.get('MW_PORT', '9000')

_proc = None
_lock = threading.Lock()
_sessions = {}                      # sid -> {'t': ts, 'must': bool}
PBKDF2_ROUNDS = 120000
MIN_PW = 6
SESSION_TTL = 24 * 3600             # 会话闲置 24 小时自动失效（每次操作自动续期）
DEFAULT_CFG_FMT = os.environ.get('MW_CFG_FMT', 'old')   # 首次生成配置用哪种格式（镜像里设成 new）
ASS_FORMAT_DEFAULT = ('Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,'
                      ' BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,'
                      ' BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding')
ASS_STYLE_DEFAULT = ('Style: Default,楷体,20,&H03FFFFFF,&H00FFFFFF,&H00000000,&H02000000,-1,0,0,0,'
                     '100,100,0,0,1,1,0,2,10,10,10,1')


# ---------------- 密码 / 状态 ----------------
def load_sessions():
    """启动/重启时从磁盘恢复未过期会话"""
    try:
        with open(SESSFILE, encoding='utf-8') as f:
            d = json.load(f) or {}
    except Exception:
        return {}
    now = time.time()
    return {k: v for k, v in d.items()
            if isinstance(v, dict) and (now - float(v.get('t') or 0)) <= SESSION_TTL}


def save_sessions():
    """把会话写盘（登录/登出/改密时调用；滑动续期只在内存里更新以免频繁写盘）"""
    try:
        tmp = SESSFILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_sessions, f)
        os.replace(tmp, SESSFILE)
        os.chmod(SESSFILE, 0o600)
    except Exception as ex:
        print('[UI] 会话持久化失败：%s' % ex, flush=True)


def load_state():
    try:
        with open(STATE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)
    try:
        os.chmod(STATE, 0o600)
    except Exception:
        pass


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), PBKDF2_ROUNDS).hex()
    return salt, h


def pw_ok(pw):
    st = load_state()
    if not st.get('pw_hash') or not st.get('pw_salt'):
        return False
    for cand in {pw, (pw or '').strip()}:
        if hash_pw(cand, st['pw_salt'])[1] == st['pw_hash']:
            return True
    return False


def set_password(pw, initialized=True):
    st = load_state()
    salt, h = hash_pw(pw)
    st['pw_salt'] = salt
    st['pw_hash'] = h
    st['initialized'] = initialized
    st['changed'] = time.strftime('%F %T')
    save_state(st)


def ensure_initial_password():
    """没有任何密码状态时：生成初始密码，写进容器日志，并标记为未初始化（登录后强制改密）"""
    st = load_state()
    if st.get('pw_hash'):
        return None
    init = 'mw-' + secrets.token_urlsafe(6)
    set_password(init, initialized=False)
    print('=' * 62, flush=True)
    print('[UI] MediaWarp 面板初始密码：%s' % init, flush=True)
    print('[UI] 首次登录后会强制要求设置你自己的新密码（两次输入确认）', flush=True)
    print('=' * 62, flush=True)
    return init


# ---------------- MediaWarp 进程 ----------------
def mw_running():
    return _proc is not None and _proc.poll() is None


def rotate_log(max_bytes=5 * 1024 * 1024, keep=3):
    """mediawarp.out 超过 max_bytes 就轮转：当前 → .1，旧的依次后移，最多保留 keep 份"""
    try:
        if not os.path.exists(MW_LOG) or os.path.getsize(MW_LOG) < max_bytes:
            return False
        for i in range(keep, 0, -1):
            src = MW_LOG if i == 1 else '%s.%d' % (MW_LOG, i - 1)
            if os.path.exists(src):
                os.replace(src, '%s.%d' % (MW_LOG, i))
        return True
    except Exception as ex:
        print('[UI] 日志轮转失败：%s' % ex, flush=True)
        return False


def start_mw():
    global _proc
    with _lock:
        if mw_running():
            return 'already'
        os.makedirs(os.path.dirname(MW_LOG), exist_ok=True)
        if rotate_log():
            print('[UI] mediawarp.out 超过 5MB，已轮转（保留最近 3 份 .1/.2/.3）', flush=True)
        f = open(MW_LOG, 'ab')
        f.write(('\n===== [UI] start %s =====\n' % time.strftime('%F %T')).encode())
        f.flush()
        _proc = subprocess.Popen([MW, '-config', CFG], cwd=APP, stdout=f, stderr=subprocess.STDOUT)
        return 'started'


def stop_mw():
    global _proc
    with _lock:
        if not mw_running():
            return 'not-running'
        _proc.terminate()
        try:
            _proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _proc.kill()
        return 'stopped'


def restart_mw():
    stop_mw()
    time.sleep(0.4)
    return start_mw()


def mw_tcp_ok(port=None, tmo=0.6):
    s = socket.socket()
    s.settimeout(tmo)
    try:
        s.connect(('127.0.0.1', int(port or MW_PORT)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def mw_wait_up(timeout=8.0):
    """等 MediaWarp 真的开始监听（不是「进程起来了」就算）"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if mw_tcp_ok():
            return time.time() - t0
        time.sleep(0.15)
    return 0.0


def mw_wait_free(timeout=6.0):
    """等旧进程把端口让出来，避免新进程 bind: address already in use"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not mw_tcp_ok():
            return True
        time.sleep(0.15)
    return False


def apply_config():
    """让配置立刻生效。
    MediaWarp 本身不支持热加载（实测改 web.crx 后 index 注入不变），只能换进程，
    所以这里做到：停 → 等端口释放 → 起 → 等端口监听（失败自动再试一次）。
    返回 (是否成功, 耗时秒, 说明)"""
    t0 = time.time()
    stop_mw()
    freed = mw_wait_free(5.0)
    start_mw()
    up = mw_wait_up(8.0)
    if not up:
        print('[UI] 重载后 %s 秒内没监听，自动重试一次' % MW_PORT, flush=True)
        stop_mw()
        mw_wait_free(4.0)
        start_mw()
        up = mw_wait_up(8.0)
    dt = time.time() - t0
    if up:
        print('[UI] 配置已生效：MediaWarp 重载完成，%s 已在监听（%.2f 秒，端口释放=%s）'
              % (MW_PORT, dt, freed), flush=True)
        return True, dt, '端口 %s 已确认监听' % MW_PORT
    print('[UI] ⚠️ MediaWarp 重载后仍没监听 %s，请看日志' % MW_PORT, flush=True)
    return False, dt, 'MediaWarp 没起来（端口 %s 无响应），请查看日志' % MW_PORT


def effect_check():
    """确认「网页美化」开关真的生效：拿 MediaWarp 实际吐出的首页与配置比对"""
    try:
        cfg = read_cfg()
    except Exception:
        return {}
    w = cfg.get('Web') or {}
    out = {}
    if not w.get('Enable', True):
        return {'总开关': False}          # 关掉总开关时本来就不注入
    try:
        with urllib.request.urlopen('http://127.0.0.1:%s/web/index.html' % MW_PORT, timeout=6) as r:
            h = r.read().decode('utf-8', 'ignore')
    except Exception:
        return {}
    for name, flag, mark in (('crx 剧照墙', w.get('Crx'), 'emby-crx'),
                             ('演员过滤', w.get('ActorPlus'), 'actorPlus.js'),
                             ('同人图 fanart', w.get('FanartShow'), 'fanart_show.js'),
                             ('外置播放器', w.get('ExternalPlayerUrl'), 'embyLaunchPotplayer')):
        out[name] = ((mark in h) == bool(flag))
    return out


# ---------------- 配置读写 ----------------
AUTH_MASK_YAML = '••••••••••••••••（已设置；改它请用「设置」页的「更改」按钮）'


def mask_auth_in_yaml(text):
    """原始 YAML 视图：把 AUTH 真值换成掩码（页面不下发明文）"""
    if not text:
        return text
    try:
        cur = str((read_cfg().get('MediaServer') or {}).get('AUTH') or '').strip()
    except Exception:
        cur = ''
    return text.replace(cur, AUTH_MASK_YAML) if (cur and cur in text) else text


def unmask_auth_in_yaml(text):
    """保存原始 YAML 时把掩码换回真值，避免把掩码写进配置"""
    if text and AUTH_MASK_YAML in text:
        try:
            cur = str((read_cfg().get('MediaServer') or {}).get('AUTH') or '').strip()
        except Exception:
            cur = ''
        text = text.replace(AUTH_MASK_YAML, cur)
    return text


AUTH_MIN_LEN = 8     # Emby 的 API 密钥是 32 位十六进制；过短的多半是手滑


def merge_auth_field(f, cfg):
    """API 密钥：只有点「更改」重填并提交时才更新；否则保留配置里的原值。
    页面永远不下发明文，所以「没改」时表单里是空串，不能拿它覆盖真值。"""
    if 'ms_auth' not in f:
        return 'no-field'
    changed = (f.get('ms_auth_changed') or ['0'])[0] == '1'
    new = (f.get('ms_auth') or [''])[0].strip()
    if not changed or not new:
        return 'kept'
    if len(new) < AUTH_MIN_LEN:
        return 'too-short'      # 防止误填（曾把 key 写成 "123" 导致上游连接失效）
    cfg.setdefault('MediaServer', {})['AUTH'] = new
    return 'updated'


def normalize_cfg(cfg):
    """YAML 会把纯数字的值（如 AUTH: 123）解析成 int，下游 .strip()/urlparse 会崩，
    统一把 MediaServer 的文本字段转成字符串。"""
    ms = cfg.get('MediaServer')
    if isinstance(ms, dict):
        for k in ('Type', 'ADDR', 'AUTH'):
            if ms.get(k) is not None and not isinstance(ms[k], str):
                ms[k] = str(ms[k])
    return cfg


def detect_fmt(raw):
    """识别配置格式：'new' = 0.2.x（小写 port/server/web），'old' = 0.1.x（大写）"""
    if not isinstance(raw, dict):
        return 'old'
    if 'MediaServer' in raw or 'ClientFilter' in raw or 'HTTPStrm' in raw:
        return 'old'
    if 'server' in raw or 'web' in raw or 'port' in raw or 'http_strm' in raw:
        return 'new'
    return 'old'


def _dd(v):
    return v if isinstance(v, dict) else {}


def _ll(v):
    return v if isinstance(v, list) else []


def to_internal(raw, fmt):
    """把任意格式的配置转成面板内部结构（沿用 0.1.x 风格 key，UI 无需改动）"""
    if fmt != 'new':
        return normalize_cfg(dict(raw))          # 旧格式：直通（只做类型兜底）

    srv, web, cli = _dd(raw.get('server')), _dd(raw.get('web')), _dd(raw.get('client'))
    hs, als, sub = _dd(raw.get('http_strm')), _dd(raw.get('alist_strm')), _dd(raw.get('subtitle'))
    cache = _dd(raw.get('cache'))
    log = _dd(raw.get('log'))
    la, ls = _dd(log.get('access')), _dd(log.get('service'))
    return {
        'Port': raw.get('port', MW_PORT),
        'MediaServer': {'Type': srv.get('type', 'Emby'),
                        'ADDR': srv.get('addr', ''),
                        'AUTH': srv.get('auth', '')},
        'Logger': {'AccessLogger': {'Console': bool(la.get('console', False)), 'File': bool(la.get('file', True))},
                   'ServiceLogger': {'Console': bool(ls.get('console', True)), 'File': bool(ls.get('file', True))}},
        'Cache': {'Enable': bool(cache.get('enable', True)),
                  'HTTPStrmTTL': cache.get('http_strm_ttl', '1m'),
                  'AlistAPITTL': cache.get('alist_api_ttl', '10m'),
                  'ImageTTL': cache.get('image_ttl', '10m'),
                  'SubtitleTTL': cache.get('subtitle_ttl', '2h')},
        'Web': {'Enable': bool(web.get('enable', False)),
                'Custom': bool(web.get('custom', False)),
                'Index': bool(web.get('index', False)),
                'Head': web.get('head') or '',
                'Robots': web.get('robots') or '',
                'Crx': bool(web.get('crx', False)),
                'ActorPlus': bool(web.get('actor_plus', False)),
                'FanartShow': bool(web.get('fanart_show', False)),
                'ExternalPlayerUrl': bool(web.get('external_player_url', False)),
                'Danmaku': bool(web.get('danmaku', False)),
                'VideoTogether': bool(web.get('video_together', False))},
        'ClientFilter': {'Enable': bool(cli.get('enable', False)),
                         'Mode': cli.get('mode', 'BlackList'),
                         'ClientList': [str(x) for x in _ll(cli.get('list'))]},
        'HTTPStrm': {'Enable': bool(hs.get('enable', False)),
                     'TransCode': bool(hs.get('proxy', False)),
                     'FinalURL': bool(hs.get('final_url', False)),
                     'CompatibilityMode': bool(hs.get('compatibility_mode', False)),
                     'PrefixList': [str(x) for x in _ll(hs.get('prefix_list'))]},
        'AlistStrm': {'Enable': bool(als.get('enable', False)),
                      'TransCode': bool(als.get('proxy', False)),
                      'RawURL': bool(als.get('raw_url', False)),
                      'List': [{'ADDR': _dd(i).get('addr', ''), 'Username': _dd(i).get('username', ''),
                                'Password': _dd(i).get('password', ''), 'Token': _dd(i).get('token', ''),
                                'PrefixList': [str(x) for x in _ll(_dd(i).get('prefix_list'))]}
                               for i in _ll(als.get('list'))]},
        'Subtitle': {'Enable': bool(sub.get('enable', True)),
                     'SRT2ASS': bool(sub.get('srt2ass', False)),
                     'ASSStyle': [str(x) for x in _ll(sub.get('ass_style'))]},
    }


def read_cfg():
    """读配置 → 内部统一结构；同时记住原格式（_fmt）与原始字典（_raw）"""
    try:
        with open(CFG, encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    fmt = detect_fmt(raw)
    out = to_internal(raw, fmt)
    out['_fmt'] = fmt
    out['_raw'] = raw
    return out


def render_cfg_old(cfg):
    web = cfg.get('Web') or {}
    ms = cfg.get('MediaServer') or {}
    cf = cfg.get('ClientFilter') or {}
    hs = cfg.get('HTTPStrm') or {}
    als = cfg.get('AlistStrm') or {}
    sub = cfg.get('Subtitle') or {}

    def b(v):
        return 'True' if v in (True, 'True', 'true', 'on', '1', 1) else 'False'
    p = []
    p.append("Port: '%s'                                # MediaWarp 监听端口\n" % cfg.get('Port', MW_PORT))
    p.append("\nMediaServer:                                # 上游媒体服务器\n")
    p.append("  Type: %s                                # Emby / Jellyfin\n" % ms.get('Type', 'Emby'))
    p.append("  ADDR: %s\n" % ms.get('ADDR', ''))
    p.append("  AUTH: '%s'\n" % ms.get('AUTH', ''))    # 加引号：纯数字 key 会被 YAML 解析成 int
    p.append("\nLogger:                                     # 日志设定\n")
    p.append("  AccessLogger:\n    Console: False\n    File: True\n")
    p.append("  ServiceLogger:\n    Console: True\n    File: True\n")
    p.append("\nWeb:                                        # Web 页面修改相关设置\n")
    p.append("  Enable: %s\n" % b(web.get('Enable', True)))
    p.append("  Custom: %s\n" % b(web.get('Custom', True)))
    p.append("  Index: %s\n" % b(web.get('Index', False)))
    p.append("  Head: |\n")
    p.append(''.join('    %s\n' % x for x in (web.get('Head') or '').splitlines()) or '    \n')
    for k, c in (('Crx', 'crx 美化（剧照墙/界面美化）'), ('ActorPlus', '过滤没有头像的演员和制作人员'),
                 ('FanartShow', '显示同人图（fanart 图）'), ('ExternalPlayerUrl', '外置播放器（仅 Emby）'),
                 ('Danmaku', 'Web 弹幕'), ('VideoTogether', '共同观影')):
        p.append("  %-19s %s   # %s\n" % (k + ':', b(web.get(k, False)), c))
    p.append("\nClientFilter:                               # 客户端过滤器\n")
    p.append("  Enable: %s\n" % b(cf.get('Enable', False)))
    p.append("  Mode: %s # WhileList / BlackList\n" % (cf.get('Mode') or 'BlackList'))
    p.append("  ClientList:\n")
    p.append(''.join('    - %s\n' % x for x in (cf.get('ClientList') or [])) or '    \n')
    p.append("\nHTTPStrm:                                   # HTTPStrm 重定向\n")
    p.append("  Enable: %s\n  TransCode: %s\n  FinalURL: %s\n  PrefixList:\n"
             % (b(hs.get('Enable', False)), b(hs.get('TransCode', False)), b(hs.get('FinalURL', False))))
    p.append(''.join('    - %s\n' % x for x in (hs.get('PrefixList') or [])) or '    \n')
    p.append("\nAlistStrm:                                  # AlistStrm 重定向\n")
    p.append("  Enable: %s\n  TransCode: %s\n  RawURL: %s\n  List:\n"
             % (b(als.get('Enable', False)), b(als.get('TransCode', False)), b(als.get('RawURL', False))))
    if not (als.get('List') or []):
        p.append('    \n')
    else:
        for it in als['List']:
            p.append('    - ADDR: %s\n' % (it.get('ADDR') or ''))
            for k in ('Username', 'Password', 'Token'):
                if it.get(k):
                    p.append('      %s: %s\n' % (k, it[k]))
            p.append('      PrefixList:\n')
            p.append(''.join('        - %s\n' % x for x in (it.get('PrefixList') or [])) or '        \n')
    p.append("\nSubtitle:                                   # 字体相关设置（仅 Emby）\n")
    p.append("  Enable: %s\n  SRT2ASS: %s\n  ASSStyle:\n" % (b(sub.get('Enable', True)), b(sub.get('SRT2ASS', False))))
    styles = sub.get('ASSStyle') or []
    if not styles:
        styles = ['Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,'
                  ' Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,'
                  ' Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
                  'Style: Default,楷体,20,&H03FFFFFF,&H00FFFFFF,&H00000000,&H02000000,-1,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1']
    for s in styles:
        p.append('    - "%s"\n' % str(s).replace('"', '\\"'))
    return ''.join(p)


def render_cfg_new(cfg):
    """生成 MediaWarp 0.2.x（小写字段）格式的配置"""
    ms = cfg.get('MediaServer') or {}
    web = cfg.get('Web') or {}
    cli = cfg.get('ClientFilter') or {}
    hs = cfg.get('HTTPStrm') or {}
    als = cfg.get('AlistStrm') or {}
    sub = cfg.get('Subtitle') or {}
    cache = cfg.get('Cache') or {}

    def b(v):
        return 'true' if v in (True, 'True', 'true', 'on', '1', 1) else 'false'

    p = []
    p.append("port: %s                                    # MediaWarp 监听端口\n" % cfg.get('Port', MW_PORT))
    p.append("\nserver:                                     # 媒体服务器相关设置\n")
    p.append("  type: %s                                  # 媒体服务器类型（Emby、Jellyfin、FNTV）\n" % (ms.get('Type') or 'Emby'))
    p.append("  addr: %s\n" % (ms.get('ADDR') or ''))
    p.append("  auth: '%s'                                # 媒体服务器认证方式（FNTV 不需要）\n" % (ms.get('AUTH') or ''))
    p.append("\nlog:                                        # 日志设定\n")
    p.append("  access:\n    console: false\n    file: true\n")
    p.append("  service:\n    console: true\n    file: true\n")
    p.append("\ncache:                                      # 缓存相关设置\n")
    p.append("  enable: %s\n" % b(cache.get('Enable', True)))
    p.append("  http_strm_ttl: %s\n" % (cache.get('HTTPStrmTTL') or '1m'))
    p.append("  alist_api_ttl: %s\n" % (cache.get('AlistAPITTL') or '10m'))
    p.append("  image_ttl: %s\n" % (cache.get('ImageTTL') or '10m'))
    p.append("  subtitle_ttl: %s\n" % (cache.get('SubtitleTTL') or '2h'))
    p.append("\nweb:                                        # Web 页面修改相关设置\n")
    p.append("  enable: %s\n" % b(web.get('Enable', False)))
    p.append("  custom: %s\n" % b(web.get('Custom', False)))
    p.append("  index: %s\n" % b(web.get('Index', False)))
    p.append("  head: |\n")
    p.append(''.join('    %s\n' % x for x in (web.get('Head') or '').splitlines()) or '    \n')
    p.append("  robots: |\n")
    p.append(''.join('    %s\n' % x for x in (web.get('Robots') or '').splitlines()) or '    \n')
    for nk, ok, c in (('crx', 'Crx', 'crx 美化（剧照墙/界面美化）'),
                      ('actor_plus', 'ActorPlus', '过滤没有头像的演员和制作人员'),
                      ('fanart_show', 'FanartShow', '显示同人图（fanart 图）'),
                      ('external_player_url', 'ExternalPlayerUrl', '外置播放器（仅 Emby）'),
                      ('danmaku', 'Danmaku', 'Web 弹幕'),
                      ('video_together', 'VideoTogether', '共同观影')):
        p.append("  %-21s %s   # %s\n" % (nk + ':', b(web.get(ok, False)), c))
    p.append("\nclient:                                     # 客户端过滤器\n")
    p.append("  enable: %s\n" % b(cli.get('Enable', False)))
    p.append("  mode: %s # WhileList / BlackList\n" % (cli.get('Mode') or 'BlackList'))
    p.append("  list:\n")
    p.append(''.join('    - %s\n' % x for x in (cli.get('ClientList') or [])) or '    \n')
    p.append("\nhttp_strm:                                  # HTTPStrm 相关配置\n")
    p.append("  enable: %s\n  proxy: %s\n  final_url: %s\n  compatibility_mode: %s\n  prefix_list:\n"
             % (b(hs.get('Enable', False)), b(hs.get('TransCode', False)),
                b(hs.get('FinalURL', False)), b(hs.get('CompatibilityMode', False))))
    p.append(''.join('    - %s\n' % x for x in (hs.get('PrefixList') or [])) or '    \n')
    p.append("\nalist_strm:                                 # AlistStrm 相关配置\n")
    p.append("  enable: %s\n  proxy: %s\n  raw_url: %s\n  list:\n"
             % (b(als.get('Enable', False)), b(als.get('TransCode', False)), b(als.get('RawURL', False))))
    if not (als.get('List') or []):
        p.append('    \n')
    else:
        for it in als['List']:
            p.append('    - addr: %s\n' % (it.get('ADDR') or ''))
            for k in ('Username', 'Password', 'Token'):
                if it.get(k):
                    p.append('      %s: %s\n' % (k.lower(), it[k]))
            p.append('      prefix_list:\n')
            p.append(''.join('        - %s\n' % x for x in (it.get('PrefixList') or [])) or '        \n')
    p.append("\nsubtitle:                                   # 字体相关设置（仅 Emby 支持）\n")
    p.append("  enable: %s\n  srt2ass: %s\n  ass_style:\n"
             % (b(sub.get('Enable', True)), b(sub.get('SRT2ASS', False))))
    styles = sub.get('ASSStyle') or [ASS_FORMAT_DEFAULT, ASS_STYLE_DEFAULT]
    for st in styles:
        p.append('    - "%s"\n' % str(st).replace('"', '\\"'))
    return ''.join(p)


def render_cfg(cfg):
    """按配置原本的格式写回（读时检测到的 _fmt 决定）；首次生成用 DEFAULT_CFG_FMT"""
    fmt = (cfg or {}).get('_fmt') or DEFAULT_CFG_FMT
    return render_cfg_new(cfg) if fmt == 'new' else render_cfg_old(cfg)


# ---------------- 「打开媒体服务器」按钮的对外端口（自动探测） ----------------
# 面板跑在容器里，它只知道容器内部的端口(9002)；映射到外面的端口（比如 19002）只能自己找：
#   从浏览器用的主机名 + 内部端口出发，逐个探测候选端口，用「上游 Emby 的服务器 Id」确认
#   这条路后面是不是我们这条 MediaWarp（主库那份 MediaWarp 在 9000，靠 Id 区分，不会认错）。
_up_cache = {'id': '', 'ts': 0.0}
_pub = {'host': '', 'port': 0, 'ts': 0.0, 'src': ''}


def _http_json(url, tmo=2.5):
    try:
        with urllib.request.urlopen(url, timeout=tmo) as r:
            return json.loads(r.read().decode('utf-8', 'ignore'))
    except Exception:
        return None


def upstream_server_id():
    now = time.time()
    if _up_cache['id'] and now - _up_cache['ts'] < 300:
        return _up_cache['id']
    addr = ((read_cfg().get('MediaServer') or {}).get('ADDR') or '').rstrip('/')
    sid = ''
    if addr:
        d = _http_json(addr + '/emby/System/Info/Public', 6.0)
        if isinstance(d, dict):
            sid = str(d.get('Id') or '')
    if sid:
        _up_cache.update({'id': sid, 'ts': now})
    return sid


def _tcp_open(host, port, tmo=0.7):
    s = socket.socket()
    s.settimeout(tmo)
    try:
        s.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _probe_pub(host, port, want_id):
    """这个端口后面是不是「我们这条」MediaWarp：比对上游服务器 Id"""
    if not _tcp_open(host, port):
        return 0
    d = _http_json('http://%s:%d/emby/System/Info/Public' % (host, int(port)), 3.0)
    if isinstance(d, dict) and (not want_id or str(d.get('Id') or '') == want_id):
        return int(port)
    return 0


def _pub_candidates(ext_port=None):
    """候选端口，越可能命中的越靠前：
       ① 端口镜像（面板在外是 19003 → MediaWarp 多半是 19002）
       ② 容器内部端口本身（映射成同号 / host 网络）
       ③ 内部端口 +10000（很常见的映射习惯：9002→19002）
       ④ 常见区段扫一遍"""
    out = []
    try:
        if ext_port:
            out.append(int(MW_PORT) + (int(ext_port) - int(UI_PORT)))
    except Exception:
        pass
    out += [int(MW_PORT), int(MW_PORT) + 10000]
    out += list(range(9000, 9011)) + list(range(19000, 19011))
    seen = []
    for p in out:
        if 0 < p < 65536 and p != int(UI_PORT) and p not in seen:
            seen.append(p)
    return seen


def detect_pub_port(host, ext_port=None, budget=5.0):
    """返回 (端口, 说明)；探测不到就返回 (0, 原因)"""
    host = (host or '').split(':')[0].strip() or '127.0.0.1'
    cands = _pub_candidates(ext_port)
    want = upstream_server_id()
    got = {}
    try:
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(_probe_pub, host, p, want): p for p in cands}
            try:
                for fu in as_completed(futs, timeout=budget):
                    p = futs[fu]
                    try:
                        got[p] = fu.result()
                    except Exception:
                        got[p] = 0
            except Exception:
                pass
    except Exception:
        pass
    for p in cands:                       # 按候选顺序取第一个命中，而不是「谁先跑完」
        if got.get(p):
            return p, '自动探测'
    if not want:
        return 0, '读不到上游服务器 Id（配置里的 ADDR 不通？）'
    return 0, '没探测到对外端口（容器可能未映射，或宿主不可从容器访问）'


def pub_port(host, ext_port=None):
    """面板要用的对外端口：环境变量 > 面板里手填 > 自动探测（缓存 60 秒）"""
    env = os.environ.get('MW_PUBLIC_PORT', '').strip()
    if env.isdigit() and 0 < int(env) < 65536:
        return int(env), '环境变量 MW_PUBLIC_PORT'
    man = str(load_state().get('pub_port') or '').strip()
    if man.isdigit() and 0 < int(man) < 65536:
        return int(man), '面板里手填'
    now = time.time()
    if _pub['host'] == host and now - _pub['ts'] < 60:
        return (_pub['port'] or int(MW_PORT)), (_pub['src'] or '容器内部端口')
    p, why = detect_pub_port(host, ext_port)
    _pub.update({'host': host, 'port': p, 'ts': now, 'src': why})
    if p:
        print('[UI] 「打开媒体服务器」对外端口探测到 %s:%d（%s）' % (host, p, why), flush=True)
    else:
        print('[UI] 对外端口自动探测失败（%s），按钮先用内部端口 %s' % (why, MW_PORT), flush=True)
    return (p or int(MW_PORT)), (why if p else '容器内部端口')


def write_cfg(text):
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    # 覆盖前先留备份（面板最怕的是「保存之后原配置没了」，只留最近 5 份）
    try:
        if os.path.exists(CFG):
            shutil.copy2(CFG, CFG + '.bak.' + time.strftime('%Y%m%d-%H%M%S'))
        # 注意：本文件是 `from glob import glob`，glob 是函数不是模块，只能用 glob(pat)
        for old in sorted(glob(CFG + '.bak.20*'), key=os.path.getmtime)[:-5]:  # 保留最近 5 份
            os.remove(old)
    except Exception as ex:
        print('[UI] 配置备份/清理失败：%s' % ex, flush=True)
    tmp = CFG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, CFG)


def tail_log(n=200):
    try:
        data = open(MW_LOG, 'rb').read()
        return '\n'.join(data.decode('utf-8', 'ignore').splitlines()[-n:])
    except Exception as ex:
        return '(读不到日志：%s)' % ex


# ---------------- 界面 ----------------
CSS = """
:root{--bg:#0a0c10;--card:#12151c;--line:#222836;--line2:#1b2130;--fg:#e9ecf2;--mut:#8a93a6;
--acc:#5b8cff;--acc2:#8b5cff;--ok:#34d399;--err:#f87171}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--fg);
font:14.5px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
-webkit-font-smoothing:antialiased}
body{background:radial-gradient(1100px 480px at 12% -12%,#1a2440 0%,transparent 60%),
radial-gradient(900px 420px at 96% 4%,#2a1c45 0%,transparent 55%),var(--bg);min-height:100vh}
a{color:var(--acc);text-decoration:none}
.wrap{max-width:900px;margin:0 auto;padding:0 16px 40px}
header.top{position:sticky;top:0;z-index:30;backdrop-filter:blur(14px);
background:linear-gradient(180deg,rgba(10,12,16,.94),rgba(10,12,16,.72));border-bottom:1px solid var(--line)}
.tin{max-width:900px;margin:0 auto;padding:14px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:17.5px;margin:0;font-weight:650;letter-spacing:.2px}
h1 .sub{color:var(--mut);font-weight:400;font-size:12.5px;margin-left:8px}
.spacer{flex:1}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:99px;font-size:12.5px;
border:1px solid var(--line);background:#10131a;color:var(--mut)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:var(--mut)}
.pill.on{color:#b9f3d8;border-color:#1d5f45;background:#0f2a20}
.pill.on .dot{background:var(--ok);box-shadow:0 0 0 3px #34d39922;animation:bp 2s infinite}
.pill.off{color:#ffc9c9;border-color:#6b2020;background:#2a1212}.pill.off .dot{background:var(--err)}
@keyframes bp{0%,100%{opacity:1}50%{opacity:.35}}
.tabs{display:flex;gap:6px;margin:16px 0 14px;background:#0e1117;border:1px solid var(--line);
border-radius:12px;padding:5px;width:fit-content;max-width:100%;flex-wrap:wrap}
.tabs button{border:0;background:transparent;color:var(--mut);padding:8px 15px;border-radius:9px;cursor:pointer;
font:600 13.5px/1 inherit;transition:.15s}
.tabs button:hover{color:var(--fg);background:#151a24}
.tabs button.act{color:#fff;background:linear-gradient(90deg,var(--acc),var(--acc2));box-shadow:0 4px 14px #5b8cff33}
.card{background:linear-gradient(180deg,#141822,#12151c);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;margin-bottom:14px;box-shadow:0 1px 0 #ffffff0a inset,0 12px 30px -22px #000}
.card h2{font-size:14.5px;margin:0 0 4px;font-weight:650;display:flex;align-items:center;gap:8px}
.card h2 .ic{width:22px;height:22px;border-radius:7px;display:grid;place-items:center;font-size:12px;
background:linear-gradient(135deg,#5b8cff33,#8b5cff33);border:1px solid #5b8cff33;color:#bcd0ff}
.card p.d{color:var(--mut);font-size:12.5px;margin:2px 0 6px}
label.f{display:block;color:#c8d0dc;font-size:13px;margin:12px 0 6px}
.in,textarea,select{width:100%;background:#0c0f15;border:1px solid var(--line);color:var(--fg);border-radius:10px;
padding:10px 12px;font:13.5px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;transition:.15s}
.in:focus,textarea:focus,select:focus{outline:0;border-color:#3f6bd6;box-shadow:0 0 0 3px #5b8cff22}
select,textarea{font-family:inherit}textarea{min-height:96px;resize:vertical}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
@media(max-width:680px){.grid2{grid-template-columns:1fr}.tin{padding:12px}}
.trow{display:flex;align-items:center;gap:14px;padding:11px 0;border-bottom:1px dashed var(--line2)}
.trow:last-child{border-bottom:0}.trow .tx{flex:1;min-width:0}
.trow .tt{font-size:13.8px}.trow .td{color:var(--mut);font-size:12.2px;margin-top:1px}
.sw{position:relative;flex:0 0 46px;width:46px;height:26px}
.sw input{position:absolute;inset:0;width:100%;height:100%;margin:0;opacity:0;cursor:pointer;z-index:2}
.sw i{position:absolute;inset:0;border-radius:99px;background:#232a37;border:1px solid var(--line);transition:.18s}
.sw i:after{content:"";position:absolute;left:3px;top:3px;width:18px;height:18px;border-radius:50%;
background:#818aa0;transition:.18s}
.sw input:checked+i{background:linear-gradient(90deg,var(--acc),var(--acc2));border-color:transparent}
.sw input:checked+i:after{left:23px;background:#fff}
.btn{font:inherit;font-size:13.5px;padding:9px 15px;border-radius:10px;border:1px solid var(--line);
background:#161b25;color:var(--fg);cursor:pointer;transition:.15s;white-space:nowrap}
.btn:hover{background:#1c2330;transform:translateY(-1px)}
.btn.p{background:linear-gradient(90deg,var(--acc),var(--acc2));border:0;color:#fff;font-weight:650;
box-shadow:0 8px 22px -10px #5b8cffcc}
.btn.sm{padding:6px 11px;font-size:12.5px;border-radius:9px}
.bar{position:sticky;bottom:0;z-index:15;margin-top:10px;padding:12px 0 14px;
background:linear-gradient(180deg,rgba(10,12,16,.5),rgba(10,12,16,.96) 45%);backdrop-filter:blur(10px);
border-top:1px solid var(--line);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.bar .note{color:var(--mut);font-size:12.5px;flex:1;min-width:170px}
.gap{margin-left:18px}
.toasts{position:fixed;top:14px;right:14px;z-index:99;display:flex;flex-direction:column;gap:8px;
max-width:min(430px,88vw);pointer-events:none}
.toast{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border-radius:12px;font-size:13px;
line-height:1.5;box-shadow:0 12px 32px -14px #000;animation:tin .22s ease;pointer-events:auto}
@keyframes tin{from{opacity:0;transform:translateX(16px)}to{opacity:1;transform:none}}
.toast.ok{color:#b9f3d8;background:#0f2a20f2;border:1px solid #1d5f45}
.toast.err{color:#ffc9c9;background:#2a1212f2;border:1px solid #6b2020}
.toast.warn{color:#ffe6a8;background:#2a2410f2;border:1px solid #6b5714}
.toast .tx{flex:1;word-break:break-word}
.toast .x{background:transparent;border:0;color:inherit;opacity:.65;cursor:pointer;font-size:15px;
line-height:1;padding:0 2px;font-family:inherit}
.toast .x:hover{opacity:1}
.msg{border-radius:12px;padding:11px 14px;margin:12px 0;font-size:13.5px;border:1px solid #1d5f45;
background:#0f2a20;color:#b9f3d8}
.msg.err{border-color:#6b2020;background:#2a1212;color:#ffc9c9}
.msg.warn{border-color:#6b5714;background:#2a2410;color:#ffe6a8}
pre{background:#0b0e13;border:1px solid var(--line);border-radius:12px;padding:13px;overflow:auto;
max-height:420px;font:12.3px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;color:#b7c1d1;margin:0;white-space:pre-wrap}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0}
.pane{display:none}.pane.act{display:block}
kbd{background:#171c26;border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:11.5px;color:#c3ccda}
.hint{color:var(--mut);font-size:12.3px;margin:8px 0 2px;line-height:1.7}
.center{min-height:100vh;display:grid;place-items:center;padding:24px}
.lcard{width:100%;max-width:400px;background:linear-gradient(180deg,#151a25,#11151d);border:1px solid var(--line);
border-radius:18px;padding:26px 24px;box-shadow:0 30px 70px -30px #000}
.lcard h1{font-size:19px;margin:0 0 4px}.lcard .d{color:var(--mut);font-size:12.8px;margin-bottom:14px}
.badge{display:inline-block;font-size:11.5px;color:#bcd0ff;background:#5b8cff1a;border:1px solid #5b8cff33;
border-radius:6px;padding:1px 7px;margin-left:8px}
.steps{color:var(--mut);font-size:12.4px;line-height:1.9;margin:6px 0 0}
.authrow{display:flex;gap:8px;align-items:stretch;margin-bottom:2px}
.authrow .in{flex:1;min-width:0}
"""

JS = """
function tab(n){
  document.querySelectorAll('.tabs button').forEach(function(b){b.classList.toggle('act',b.dataset.tab===n)});
  document.querySelectorAll('.pane').forEach(function(p){p.classList.toggle('act',p.dataset.pane===n)});
  if(n==='log')loadlog();
  try{location.hash=n}catch(e){}
}
function loadlog(){
  var p=document.getElementById('logtxt');if(!p)return;
  p.textContent='加载中…';
  fetch('/logs',{cache:'no-store'}).then(function(r){return r.text()}).then(function(t){
    p.textContent=t||'(空)';p.scrollTop=p.scrollHeight;}).catch(function(e){p.textContent='读取失败: '+e});
}
function clearlog(){fetch('/clearlog',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'x=1'}).then(loadlog)}
function showpw(on){
  ['n1','n2','old','pw'].forEach(function(i){var e=document.getElementById(i);
    if(e)e.type=on?'text':'password';});
}
function checkpw(){
  var a=document.getElementById('n1'),b=document.getElementById('n2'),t=document.getElementById('tip');
  if(!a||!b)return true;
  if(a.value.length<6){t.innerHTML='⛔ 新密码至少 6 位（当前 '+a.value.length+' 位）—— 还没有保存！';t.style.color='#ffb4b4';t.style.fontWeight='700';return false}
  if(a.value!==b.value){t.innerHTML='⛔ 两次输入不一致 —— 还没有保存！请重新输入两次';t.style.color='#ffb4b4';t.style.fontWeight='700';return false}
  t.innerHTML='✔ 两次输入一致，可以保存';t.style.color='#8ee0b3';t.style.fontWeight='600';return true;
}
function autohideMsgs(){
  var els=document.querySelectorAll('[data-autohide]');
  Array.prototype.forEach.call(els,function(el){
    var ms=parseInt(el.getAttribute('data-autohide'),10)||3000;
    setTimeout(function(){
      el.style.transition='opacity .4s ease, transform .4s ease';
      el.style.opacity='0';
      el.style.transform='translateX(16px)';
      setTimeout(function(){if(el.parentNode)el.parentNode.removeChild(el);},420);
    },ms);
  });
}
window.addEventListener('DOMContentLoaded',function(){
  var h=(location.hash||'').replace('#','');
  if(h&&document.querySelector('.tabs button[data-tab="'+h+'"]'))tab(h);
  ['n1','n2'].forEach(function(id){var e=document.getElementById(id);if(e)e.addEventListener('input',checkpw)});
  autohideMsgs();
});
var AUTH_PH_NEW='粘贴新的 API 密钥';
function authEdit(){
  var i=document.getElementById('ms_auth');if(!i)return;
  i.value='';i.readOnly=false;i.placeholder=AUTH_PH_NEW;i.focus();
  document.getElementById('ms_auth_changed').value='1';
  var b=document.getElementById('authbtn');if(b){b.textContent='确定';b.onclick=authSubmit;}
  document.getElementById('authcancel').style.display='';
}
function authCancel(){
  var i=document.getElementById('ms_auth');if(!i)return;
  i.value='';i.readOnly=(i.dataset.set==='1');
  i.placeholder=i.dataset.mask||'粘贴 API 密钥';
  document.getElementById('ms_auth_changed').value='0';
  var b=document.getElementById('authbtn');if(b){b.textContent='更改';b.onclick=authEdit;}
  document.getElementById('authcancel').style.display='none';
}
function authSubmit(){
  var i=document.getElementById('ms_auth');
  if(!i||!i.value.trim()){alert('请先填入新的 API 密钥');return;}
  i.readOnly=true;
  var f=document.querySelector('form[action="/save"]');
  if(f)f.submit();
}
"""


def esc(v):
    return html.escape('' if v is None else str(v))


def shell(content):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="dark"><title>MediaWarp 面板 · emby-av</title>'
            '<style>' + CSS + '</style></head><body>' + content +
            '<script>' + JS + '</script></body></html>')


def sw(name, on, title, desc=''):
    # 多一个 __posted 隐藏字段：这样 /save 能区分「用户关掉了」和「这次请求没带这一项」，
    # 避免部分提交（脚本/半截表单）把没提交的设置统统写成关闭
    return ('<div class="trow"><div class="tx"><div class="tt">%s</div>%s</div>'
            '<input type="hidden" name="%s__posted" value="1">'
            '<label class="sw"><input type="checkbox" name="%s"%s><i></i></label></div>'
            % (esc(title), ('<div class="td">%s</div>' % esc(desc)) if desc else '', name, name,
               ' checked' if on else ''))


def toasts(items):
    """右上角浮动提示。items=[(kind, text, autohide_ms)]，kind ∈ ok/warn/err；
    autohide_ms>0 时到点自动淡出，=0 则常驻（带 × 手动关闭）。"""
    its = [(k, t, a) for (k, t, a) in items if t]
    if not its:
        return ''
    out = ['<div class="toasts" id="toasts">']
    for k, t, a in its:
        out.append('<div class="toast %s"%s><span class="tx">%s</span>'
                   '<button class="x" type="button" onclick="this.parentNode.remove()"'
                   ' title="关闭">×</button></div>'
                   % (k, (' data-autohide="%d"' % a) if a else '', esc(t)))
    out.append('</div>')
    return ''.join(out)


def login_page(err='', warn='', host='127.0.0.1', init_hint=False):
    foot = (('<div class="hint">首次使用：密码在容器日志里<br><kbd>docker logs mediawarp-av | grep 初始密码</kbd>'
             '<div class="steps">首次登录后会被要求设置你自己的新密码。</div></div>') if init_hint else
            ('<div class="hint">忘记密码：<kbd>docker exec mediawarp-av python3 /opt/ui/reset_pw.py</kbd>'
             ' → 会打印一个新的初始密码</div>'))
    body = ('<div class="center"><div class="lcard"><h1>MediaWarp 面板</h1>'
            '<div class="d">emby-av 专用 &nbsp;<span class="badge">:9002 → :28096</span></div>'
            + toasts([('err', err, 0), ('warn', warn, 0)])
            + '<form method="post" action="/login"><label class="f">面板密码</label>'
              '<input class="in" type="password" name="pw" id="pw" autofocus autocomplete="current-password">'
              '<div class="tools"><label class="hint" style="margin:0;display:flex;align-items:center;gap:6px">'
              '<input type="checkbox" onchange="showpw(this.checked)"> 显示密码</label>'
              '<button class="btn p" type="submit" style="margin-left:auto">登录</button></div></form>'
            + foot + '</div></div></div>')
    return shell(body)


def setpass_page(err='', first=True, old=''):
    """old 非空时塞一个隐藏字段：浏览器不收 Cookie 时用它证明「我知道当前密码」"""
    hold = ('<input type="hidden" name="old" value="%s">' % esc(old)) if old else ''
    body = ('<div class="center"><div class="lcard"><h1>%s</h1>'
            '<div class="d">%s</div>' % ('设置新密码' if first else '修改密码',
                                         '首次登录必须设置你自己的新密码' if first else '输入当前密码与新密码')
            + toasts([('err', err, 0)])
            + '<form method="post" action="%s" onsubmit="return checkpw()">' % ('/setpass' if first else '/changepw')
            + hold
            + ('' if first else '<label class="f">当前密码</label><input class="in" type="password" id="old" name="old">')
            + '<label class="f">新密码（至少 6 位）</label><input class="in" type="password" id="n1" name="n1">'
              '<label class="f">再输一次</label><input class="in" type="password" id="n2" name="n2">'
              '<div class="hint" id="tip" style="min-height:18px">两次输入需一致（首尾空格会被自动忽略）</div>'
              '<div class="tools"><label class="hint" style="margin:0;display:flex;align-items:center;gap:6px">'
              '<input type="checkbox" onchange="showpw(this.checked)"> 显示密码</label>'
              '<button class="btn p" type="submit" style="margin-left:auto">保存新密码</button></div>'
              '</form>'
            + ('' if not first else '<div class="hint">设好后，以后登录都用这个新密码（初始密码作废）。</div>')
            + '</div></div>')
    return shell(body)


def panel_page(cfg, msg='', err='', warn='', host='127.0.0.1', ext_port=None,
               bar='', bar_kind='ok'):
    """msg/err/warn/bar 全部渲染成右上角浮动 toast；
    bar 表示操作结果（成功 3 秒后自动消失，失败/警告常驻可手动关）。"""
    web = cfg.get('Web') or {}
    ms = cfg.get('MediaServer') or {}
    cf = cfg.get('ClientFilter') or {}
    hs = cfg.get('HTTPStrm') or {}
    als = cfg.get('AlistStrm') or {}
    pub, pub_src = pub_port(host, ext_port)
    manual_pub = str(load_state().get('pub_port') or '')
    try:
        raw_cfg = open(CFG, encoding='utf-8').read()
    except Exception:
        raw_cfg = ''
    run = mw_running()
    stat = ('<span class="pill %s"><span class="dot"></span>MediaWarp %s</span>'
            % ('on' if run else 'off', '运行中' if run else '已停止'))
    mw_url = 'http://' + esc(host) + ':' + str(pub) + '/web/index.html'
    head = ('<header class="top"><div class="tin"><h1>MediaWarp 设置面板<span class="sub">emby-av</span></h1>'
            + stat
            + '<span class="pill">上游 ' + esc(ms.get('ADDR') or '(未设置)') + '</span>'
            + '<span class="pill">媒体服务器端口 ' + str(pub) + '（' + esc(pub_src) + '）</span>'
            + '<span class="spacer"></span>'
            + '<button class="btn" type="button" onclick="document.getElementById(\'rf\').submit()">重新加载配置</button>'
            + '<button class="btn p" type="submit" form="saveform">保存并立即生效</button>'
            + '<a class="btn gap" href="/changepw">修改密码</a>'
            + '<form method="post" action="/logout" class="gap" style="margin:0"><button class="btn">退出</button></form>'
            + '</div></header>')

    # API 密钥：页面不下发明文，只放掩码占位；改了才通过 merge_auth_field 写回
    auth_set = bool(str(ms.get('AUTH') or '').strip())
    auth_mask = '••••••••••••••••（已设置，点「更改」可重填）'
    auth_ph = auth_mask if auth_set else '粘贴 API 密钥'
    auth_ro = ' readonly' if auth_set else ''
    settings = ('<div class="card"><h2><span class="ic">⚙</span>上游媒体服务器</h2>'
                '<p class="d">MediaWarp 反代的目标 Emby / Jellyfin</p>'
                '<div class="grid2">'
                '<div><label class="f">类型</label><select name="ms_type">'
                '<option value="Emby"%s>Emby</option><option value="Jellyfin"%s>Jellyfin</option></select></div>'
                '<div><label class="f">服务器地址</label><input class="in" type="text" name="ms_addr" value="%s"></div>'
                '</div><label class="f">API 密钥 (AUTH)</label>'
                '<div class="authrow">'
                '<input class="in" type="text" name="ms_auth" id="ms_auth" value=""'
                ' placeholder="%s" data-set="%s" data-mask="%s"%s>'
                '<input type="hidden" name="ms_auth_changed" id="ms_auth_changed" value="0">'
                '<button class="btn" type="button" id="authbtn" onclick="authEdit()">更改</button>'
                '<button class="btn" type="button" id="authcancel" style="display:none"'
                ' onclick="authCancel()">取消</button>'
                '</div>'
                '<div class="hint">Emby 后台 → 高级 → API 密钥。已设置后不再显示明文；点「更改」重新填写，'
                '填好点「确定」立即应用。</div>'
                '<label class="f">「打开媒体服务器」按钮用的端口（留空 = 自动探测）</label>'
                '<div class="authrow">'
                '<input class="in" type="text" name="mw_pub_port" value="%s" placeholder="自动探测">'
                '<a class="btn" href="%s" target="_blank">打开媒体服务器</a>'
                '</div>'
                '<div class="hint">面板会自动探测这个容器映射到外面的端口（改映射后 1 分钟内自动跟上）；'
                '也可以在这里手填固定值。当前生效：<b>%s</b></div></div>'
                % (' selected' if (ms.get('Type') or 'Emby') == 'Emby' else '',
                   ' selected' if ms.get('Type') == 'Jellyfin' else '', esc(ms.get('ADDR')),
                   esc(auth_ph), ('1' if auth_set else '0'), esc(auth_mask), auth_ro,
                   esc(manual_pub), mw_url, str(pub)))

    beauty = ('<div class="card"><h2><span class="ic">✦</span>网页美化与功能</h2>'
              '<p class="d">决定用 :%s 访问媒体服务器时网页端长什么样</p>' % str(pub)
              + sw('web_enable', web.get('Enable', True), '网页修改总开关', '关掉就只是纯反代')
              + sw('crx', web.get('Crx', False), 'crx 美化（剧照墙就在这）', '注入 emby-crx：剧照墙、界面美化')
              + sw('actorplus', web.get('ActorPlus', False), '过滤没有头像的演员')
              + sw('fanartshow', web.get('FanartShow', False), '显示同人图 fanart')
              + sw('externalplayer', web.get('ExternalPlayerUrl', False), '外置播放器（仅 Emby）')
              + sw('danmaku', web.get('Danmaku', False), 'Web 弹幕')
              + sw('videotogether', web.get('VideoTogether', False), '共同观影')
              + sw('web_custom', web.get('Custom', True), '加载自定义静态资源')
              + sw('web_index', web.get('Index', False), '从 custom 目录读取 index.html')
              + '<label class="f">Head 注入脚本（每行一条）</label><textarea name="web_head">%s</textarea></div>'
              % esc(web.get('Head') or ''))

    filt = ('<div class="card"><h2><span class="ic">⌘</span>客户端过滤器</h2>'
            '<p class="d">按客户端名称决定是否套用美化</p>'
            + sw('cf_enable', cf.get('Enable', False), '启用过滤器')
            + '<div class="grid2"><div><label class="f">模式</label><select name="cf_mode">'
              '<option value="BlackList"%s>BlackList（名单内不套用）</option>'
              '<option value="WhileList"%s>WhileList（只对名单内套用）</option></select></div>'
              '<div><label class="f">客户端名单（一行一个）</label><textarea name="cf_list" style="min-height:76px">%s</textarea></div></div></div>'
            % (' selected' if (cf.get('Mode') or 'BlackList') == 'BlackList' else '',
               ' selected' if cf.get('Mode') == 'WhileList' else '', esc('\n'.join(cf.get('ClientList') or []))))

    strm = ('<div class="card"><h2><span class="ic">⇄</span>Strm 重定向</h2>'
            '<p class="d">上游是网盘 strm 时才需要；本地文件保持关闭</p>'
            + sw('hs_enable', hs.get('Enable', False), 'HTTPStrm 重定向')
            + sw('als_enable', als.get('Enable', False), 'AlistStrm 重定向', '多 Alist/Token 请用「原始 YAML」')
            + '</div>')

    logs = ('<div class="card"><h2><span class="ic">▤</span>运行日志</h2>'
            '<p class="d">%s</p><div class="tools">'
            '<button class="btn sm" type="button" onclick="loadlog()">刷新</button>'
            '<button class="btn sm" type="button" onclick="clearlog()">清空</button>'
            '<span class="hint" style="margin:0">访问日志在 logs/&lt;日期&gt;/access.log</span></div>'
            '<pre id="logtxt">%s</pre></div>' % (esc(MW_LOG), esc(tail_log())))

    # 所有提示统一汇成右上角 toast：成功 3 秒自动消失，失败/警告常驻（可点 × 关闭）
    _ti = []
    if msg:
        _ti.append(('ok', msg, 3000))
    if warn:
        _ti.append(('warn', warn, 0))
    if err:
        _ti.append(('err', err, 0))
    if bar:
        _bk = {'ok': 'ok', 'err': 'err', 'warn': 'warn'}.get(bar_kind, 'ok')
        _ti.append((_bk, bar, 3000 if _bk == 'ok' else 0))
    toasts_html = toasts(_ti)

    rawpane = ('<div class="pane" data-pane="raw"><form method="post" action="/raw">'
               '<div class="card"><h2><span class="ic">{ }</span>原始 config.yaml</h2>'
               '<p class="d">文件：%s（保存后自动生效；API 密钥以掩码显示，改它请用「设置」页的「更改」按钮）</p>'
               '<textarea name="raw" style="min-height:520px">%s</textarea>'
               '<div class="tools"><button class="btn p" type="submit">保存原始 YAML 并立即生效</button></div>'
               '</div></form></div>' % (esc(CFG), esc(mask_auth_in_yaml(raw_cfg))))

    body = (head + toasts_html + '<div class="wrap">' +
            '<div class="tabs"><button data-tab="set" class="act" onclick="tab(\'set\')">设置</button>'
            '<button data-tab="raw" onclick="tab(\'raw\')">原始 YAML</button>'
            '<button data-tab="log" onclick="tab(\'log\')">日志</button></div>'
            '<div class="pane act" data-pane="set"><form id="saveform" method="post" action="/save">'
            + settings + beauty + filt + strm +
            '<div class="hint" style="margin-top:16px">改完点右上角「保存并立即生效」即可 —— 面板会自动重载 '
            'MediaWarp 并确认生效（约 0.5 秒），无需手动重启。</div>'
            '</form></div>'
            '<div class="pane" data-pane="log">' + logs + '</div>' + rawpane + '</div>'
            '<form id="rf" method="post" action="/restart" style="display:none"></form>')
    return shell(body)


# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    server_version = 'MediaWarpUI/4.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='text/html; charset=utf-8', cookie=None):
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(b)

    def _sess(self, f=None):
        """会话来源优先级：Cookie → URL ?sid= → 表单 sid 字段
        （末尾两种是兜底：有些浏览器/隐私设置会把面板的 Cookie 丢掉）"""
        sid = None
        c = SimpleCookie(self.headers.get('Cookie') or '')
        if 'mwui' in c:
            sid = c['mwui'].value
        if not sid:
            q = parse_qs(urlparse(self.path).query)
            sid = (q.get('sid') or [None])[0]
        if not sid and f:
            sid = (f.get('sid') or [None])[0]
        now = time.time()
        for k, v in list(_sessions.items()):          # 顺手回收已过期会话
            if now - v.get('t', 0) > SESSION_TTL:
                _sessions.pop(k, None)
        s = _sessions.get(sid) if sid else None
        if s:
            s['t'] = now                              # 每次操作续期（滑动过期）
        return (sid, s) if s else (None, None)

    def _inject_sid(self, html, sid):
        """把会话 id 写进页面里的每个表单/链接，浏览器不收 Cookie 时照样能操作"""
        if not sid:
            return html
        html = re.sub(r'(<form\b[^>]*>)',
                      lambda m: m.group(1) + '<input type="hidden" name="sid" value="%s">' % sid, html)
        html = html.replace('href="/changepw"', 'href="/changepw?sid=%s"' % sid)
        html = html.replace("fetch('/logs'", "fetch('/logs?sid=%s'" % sid)
        html = html.replace("fetch('/clearlog'", "fetch('/clearlog?sid=%s'" % sid)
        return html

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        # keep_blank_values=True：空输入框也要算「提交了」（否则清空某项永远清不掉）
        return parse_qs(self.rfile.read(n).decode('utf-8'), keep_blank_values=True) if n else {}

    def _host_port(self):
        """浏览器访问面板用的 (主机, 端口)：用来推断「打开媒体服务器」该用哪个端口"""
        hp = (self.headers.get('Host') or '').strip()
        if hp.startswith('[') and ']' in hp:              # IPv6 字面量
            h, _, p = hp.partition(']')
            return (h + ']') or '127.0.0.1', (p.lstrip(':') or None)
        if ':' in hp:
            h, _, p = hp.rpartition(':')
            return (h or '127.0.0.1'), (p or None)
        return (hp or '127.0.0.1'), None

    def _host(self):
        return self._host_port()[0]

    def _panel(self, msg='', err='', warn='', sid=None, bar='', bar_kind='ok'):
        host, ext_port = self._host_port()
        try:
            return self._inject_sid(panel_page(read_cfg(), msg=msg, err=err, warn=warn,
                                               host=host, ext_port=ext_port,
                                               bar=bar, bar_kind=bar_kind), sid)
        except Exception as ex:
            return panel_page({}, err='读取配置失败：%s' % ex, host=host, ext_port=ext_port)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == '/health':
            return self._send(200, json.dumps({'ok': True, 'mw': mw_running(),
                                               'initialized': bool(load_state().get('initialized'))}),
                              'application/json')
        sid, s = self._sess()
        if not s:
            had = 'mwui' in SimpleCookie(self.headers.get('Cookie') or '')
            warn = '会话已失效（面板重启过），请用密码重新登录' if had else ''
            return self._send(200, login_page(warn=warn, init_hint=not load_state().get('initialized')))
        if s.get('must'):
            return self._send(200, self._inject_sid(setpass_page(), sid))
        if p == '/changepw':
            return self._send(200, self._inject_sid(setpass_page(first=False), sid))
        if p == '/logs':
            return self._send(200, tail_log(), 'text/plain; charset=utf-8')
        if p == '/raw':
            try:
                return self._send(200, self._panel(sid=sid))
            except Exception as ex:
                return self._send(500, self._panel(err=str(ex), sid=sid))
        return self._send(200, self._panel(sid=sid))

    def _setpass(self, f):
        """首次设置新密码：有会话最省事；浏览器把 Cookie 丢了的话，只要带上「当前密码」也放行"""
        sid, s = self._sess(f)
        old = (f.get('old') or [''])[0].strip()
        if not s and not pw_ok(old):
            print('[UI] 设置新密码被拒：无有效会话也未带正确的当前密码（Cookie:%s）'
                  % ('有' if self.headers.get('Cookie') else '无'), flush=True)
            return self._send(401, login_page(err='会话已失效：请重新登录（或刷新页面后重新输入初始密码）再设置新密码'))
        n1 = (f.get('n1') or [''])[0].strip()
        n2 = (f.get('n2') or [''])[0].strip()
        if len(n1) < MIN_PW:
            print('[UI] 设置新密码被拒：长度 %d 不足' % len(n1), flush=True)
            return self._send(400, self._inject_sid(setpass_page(err='新密码至少 %d 位 —— 还没有保存！' % MIN_PW, old=old), sid))
        if n1 != n2:
            print('[UI] 设置新密码被拒：两次输入不一致（%d vs %d 字符）' % (len(n1), len(n2)), flush=True)
            return self._send(400, self._inject_sid(setpass_page(err='两次输入不一致 —— 还没有保存！请重新输入两次', old=old), sid))
        set_password(n1, initialized=True)
        sid2 = sid or secrets.token_urlsafe(16)
        _sessions[sid2] = {'t': time.time(), 'must': False}
        save_sessions()
        print('[UI] 新密码已设置成功（%d 位），已自动登录面板' % len(n1), flush=True)
        return self._send(200, self._panel(msg='新密码已设置成功，已自动登录。以后请用这个新密码登录（初始密码作废）。', sid=sid2),
                          cookie='mwui=%s; Path=/; HttpOnly; SameSite=Lax' % sid2)

    def _changepw(self, f):
        """改密码：靠「当前密码」验证，不依赖 Cookie；成功后作废会话并要求重新登录"""
        self._sess(f)          # 只为顺带回收过期会话（不再需要其返回值）
        old = (f.get('old') or [''])[0].strip()
        n1 = (f.get('n1') or [''])[0].strip()
        n2 = (f.get('n2') or [''])[0].strip()
        if not pw_ok(old):
            print('[UI] 改密被拒：当前密码不对（%d 位）' % len(old), flush=True)
            return self._send(400, self._inject_sid(setpass_page(err='当前密码不对', first=False), sid))
        if len(n1) < MIN_PW:
            return self._send(400, self._inject_sid(setpass_page(err='新密码至少 %d 位 —— 还没有保存！' % MIN_PW, first=False), sid))
        if n1 != n2:
            return self._send(400, self._inject_sid(setpass_page(err='两次输入不一致 —— 还没有保存！请重新输入两次', first=False), sid))
        set_password(n1, initialized=True)
        # 安全：改密后作废**所有**会话（含其它已登录设备），强制用新密码重新登录
        _sessions.clear()
        save_sessions()
        print('[UI] 密码已修改（新密码 %d 位），已退出全部会话，需用新密码重新登录' % len(n1), flush=True)
        return self._send(200, login_page(warn='密码已修改成功，已自动退出登录。请使用新密码重新登录。'),
                          cookie='mwui=; Path=/; Max-Age=0')

    def do_POST(self):
        p = urlparse(self.path).path
        f = self._body()
        if p == '/login':
            pw = (f.get('pw') or [''])[0].strip()
            if pw and pw_ok(pw):
                sid = secrets.token_urlsafe(16)
                st = load_state()
                _sessions[sid] = {'t': time.time(), 'must': not st.get('initialized')}
                cookie = 'mwui=%s; Path=/; HttpOnly; SameSite=Lax' % sid
                save_sessions()
                print('[UI] 登录成功（%s），%s' % (self.client_address[0],
                      '需强制设置新密码' if _sessions[sid]['must'] else '进入面板'), flush=True)
                if _sessions[sid]['must']:
                    # 隐藏字段带上刚输入的密码：万一浏览器不收 Cookie，也能凭它完成设置
                    return self._send(200, self._inject_sid(setpass_page(old=pw), sid), cookie=cookie)
                return self._send(200, self._panel(msg='登录成功', sid=sid), cookie=cookie)
            print('[UI] 登录失败（%s，密码长度 %d）' % (self.client_address[0], len(pw)), flush=True)
            warn = ''
            if not load_state().get('initialized'):
                warn = '请使用容器日志里的「初始密码」登录（docker logs mediawarp-av | grep 初始密码）'
            return self._send(401, login_page(err='密码不对，请重试（注意：首尾空格会被忽略；密码区分大小写）', warn=warn,
                                              init_hint=not load_state().get('initialized')))
        if p == '/setpass':
            return self._setpass(f)
        if p == '/changepw':
            return self._changepw(f)
        sid, s = self._sess(f)
        if not s:
            print('[UI] 拒绝：POST %s 会话无效（Cookie:%s，页面 sid 字段:%s）'
                  % (p, '有' if self.headers.get('Cookie') else '无',
                     '有' if (f.get('sid') or [''])[0] else '无'), flush=True)
            return self._send(401, login_page(err='会话已失效（面板更新或重启过）。请重新登录，然后重试刚才的操作。'))
        if p == '/logout':
            _sessions.pop(sid, None)
            save_sessions()
            return self._send(200, login_page(warn='已退出，请重新登录'),
                              cookie='mwui=; Path=/; Max-Age=0')
        if p == '/clearlog':
            try:
                open(MW_LOG, 'w').close()
            except Exception:
                pass
            return self._send(200, 'ok', 'text/plain; charset=utf-8')
        if p == '/restart':
            ok, dt, note = apply_config()
            if not ok:
                return self._send(200, self._panel(bar='重载失败：%s' % note, bar_kind='err', sid=sid))
            return self._send(200, self._panel(bar='已重新加载配置并生效（%.1f 秒，%s）' % (dt, note),
                                               bar_kind='ok', sid=sid))
        if p == '/raw':
            txt = unmask_auth_in_yaml((f.get('raw') or [''])[0])
            try:
                yaml.safe_load(txt or '')
                write_cfg(txt)
                ok, dt, note = apply_config()
                if not ok:
                    return self._send(200, self._panel(bar='YAML 已写入，但 %s' % note, bar_kind='err', sid=sid))
                bad = [k for k, v in (effect_check() or {}).items() if not v]
                tip = '生效确认：' + ('全部正常' if not bad else '⚠ 这几项与预期不符 → ' + '、'.join(bad))
                return self._send(200, self._panel(bar='原始 YAML 已保存并立即生效（%.1f 秒）。%s' % (dt, tip),
                                                   bar_kind=('ok' if not bad else 'warn'), sid=sid))
            except Exception as ex:
                return self._send(400, self._panel(bar='保存失败：%s' % ex, bar_kind='err', sid=sid))
        if p == '/save':
            try:
                cfg = read_cfg()
                cfg.setdefault('MediaServer', {})
                cfg['MediaServer']['Type'] = (f.get('ms_type') or ['Emby'])[0]
                cfg['MediaServer']['ADDR'] = (f.get('ms_addr') or [''])[0].strip()
                auth_res = merge_auth_field(f, cfg)
                if auth_res == 'too-short':
                    _n = len((f.get('ms_auth') or [''])[0].strip())
                    print('[UI] API 密钥被拒：只有 %d 位（至少 %d 位）' % (_n, AUTH_MIN_LEN), flush=True)
                    return self._send(200, self._panel(
                        bar='API 密钥只有 %d 位，太短，已取消保存（Emby 的密钥通常是 32 位十六进制）。'
                            '重新点「更改」填入完整密钥再保存。' % _n,
                        bar_kind='err', sid=sid))
                print('[UI] API 密钥：%s' % {'updated': '已更新', 'kept': '未改动（保留原值）'}.get(auth_res, auth_res), flush=True)
                w = cfg.setdefault('Web', {})
                for key, nm in (('Enable', 'web_enable'), ('Custom', 'web_custom'), ('Index', 'web_index'),
                                ('Crx', 'crx'), ('ActorPlus', 'actorplus'), ('FanartShow', 'fanartshow'),
                                ('ExternalPlayerUrl', 'externalplayer'), ('Danmaku', 'danmaku'),
                                ('VideoTogether', 'videotogether')):
                    if nm in f or (nm + '__posted') in f:
                        w[key] = nm in f
                if 'web_head' in f or 'web_head__posted' in f:
                    w['Head'] = (f.get('web_head') or [''])[0]
                cf = cfg.setdefault('ClientFilter', {})
                if 'cf_enable' in f or 'cf_enable__posted' in f:
                    cf['Enable'] = 'cf_enable' in f
                if 'cf_mode' in f and (f.get('cf_mode') or [''])[0]:
                    cf['Mode'] = (f.get('cf_mode') or ['BlackList'])[0]
                if 'cf_list' in f:
                    cf['ClientList'] = [x.strip() for x in ((f.get('cf_list') or [''])[0]).splitlines() if x.strip()]
                if 'hs_enable' in f or 'hs_enable__posted' in f:
                    cfg.setdefault('HTTPStrm', {})['Enable'] = 'hs_enable' in f
                if 'als_enable' in f or 'als_enable__posted' in f:
                    cfg.setdefault('AlistStrm', {})['Enable'] = 'als_enable' in f
                if 'mw_pub_port' in f or 'mw_pub_port__posted' in f:
                    raw_pub = (f.get('mw_pub_port') or [''])[0].strip()
                    stt = load_state()
                    if raw_pub.isdigit() and 0 < int(raw_pub) < 65536:
                        stt['pub_port'] = int(raw_pub)
                    else:
                        stt.pop('pub_port', None)
                    save_state(stt)
                    _pub.update({'host': '', 'port': 0, 'ts': 0.0, 'src': ''})   # 下次重新探测
                    print('[UI] 「打开媒体服务器」端口：%s' % (raw_pub or '自动探测'), flush=True)
                write_cfg(render_cfg(cfg))
                print('[UI] 配置已保存（Web.Enable=%s Custom=%s Crx=%s ActorPlus=%s），立即生效中'
                      % (w.get('Enable'), w.get('Custom'), w.get('Crx'), w.get('ActorPlus')), flush=True)
                ok, dt, note = apply_config()
                if not ok:
                    return self._send(200, self._panel(bar='配置已写入，但 %s' % note, bar_kind='err', sid=sid))
                bad = [k for k, v in (effect_check() or {}).items() if not v]
                tip = '生效确认：' + ('全部正常' if not bad else '⚠ 这几项与预期不符 → ' + '、'.join(bad))
                return self._send(200, self._panel(bar='已保存并立即生效（%.1f 秒，%s）。%s' % (dt, note, tip),
                                                   bar_kind=('ok' if not bad else 'warn'), sid=sid))
            except Exception as ex:
                return self._send(500, self._panel(bar='保存失败：%s' % ex, bar_kind='err', sid=sid))


def main():
    first = ensure_initial_password()
    _sessions.update(load_sessions())          # 恢复上次未过期的会话（重启不掉登录）
    print('[UI] 已恢复 %d 个未过期会话' % len(_sessions), flush=True)
    os.makedirs(os.path.dirname(MW_LOG), exist_ok=True)
    if not os.path.exists(CFG):
        write_cfg(render_cfg({'_fmt': DEFAULT_CFG_FMT}))
        print('[UI] 已生成初始配置（格式：%s）' % DEFAULT_CFG_FMT, flush=True)
    start_mw()
    print('[UI] 设置面板: http://0.0.0.0:%d  (MediaWarp 已启动，端口 %s)' % (UI_PORT, MW_PORT), flush=True)
    if first:
        print('[UI] 请先用上面的初始密码登录，然后设置你自己的新密码。', flush=True)

    def _warm_pub():
        """先按上游地址猜一个主机名预热探测，用户打开面板时按钮端口就是对的"""
        try:
            time.sleep(2)
            h = urlparse(((read_cfg().get('MediaServer') or {}).get('ADDR') or '')).hostname
            if h:
                p, why = pub_port(h, None)
                print('[UI] 对外端口预热结果：%s:%s（%s）' % (h, p, why), flush=True)
        except Exception:
            pass
    threading.Thread(target=_warm_pub, daemon=True).start()

    def bye(signum, frame):
        stop_mw()
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    ThreadingHTTPServer(('0.0.0.0', UI_PORT), H).serve_forever()


if __name__ == '__main__':
    main()
