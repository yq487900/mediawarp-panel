#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""忘记密码时用：生成一个新的「初始密码」，立即生效（登录后仍会被要求设置新密码）
   用法（在容器里跑）：docker exec mediawarp-av python3 /opt/ui/reset_pw.py
   ⚠️ 会先备份现有状态到 ui_state.json.prewreset.<时间>，方便误重置后回退"""
import hashlib
import json
import os
import secrets
import shutil
import time

STATE = os.environ.get('MW_STATE', '/app/ui_state.json')
ROUNDS = 120000

d = {}
try:
    d = json.load(open(STATE, encoding='utf-8'))
except Exception:
    pass

# 备份（这是「误重置」唯一的后悔药）
try:
    shutil.copy2(STATE, '%s.prereset.%s' % (STATE, time.strftime('%Y%m%d-%H%M%S')))
    for old in sorted([p for p in os.listdir(os.path.dirname(STATE) or '.') if '.prereset.' in p])[:-5]:
        try:
            os.remove(os.path.join(os.path.dirname(STATE) or '.', old))
        except Exception:
            pass
except Exception:
    pass

pw = 'mw-' + secrets.token_urlsafe(6)
salt = secrets.token_hex(8)
d['pw_salt'] = salt
d['pw_hash'] = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), ROUNDS).hex()
d['initialized'] = False
d['changed'] = 'reset'
tmp = STATE + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
os.replace(tmp, STATE)
try:
    os.chmod(STATE, 0o600)
except Exception:
    pass
print('=' * 56)
print('新的初始密码（立即生效）：%s' % pw)
print('用它登录面板后，会被强制要求设置你自己的新密码。')
print('（原状态已备份到 %s.prereset.*）' % os.path.basename(STATE))
print('=' * 56)
