# -*- coding: utf-8 -*-
"""
P115SubSearch v1.7.2 逻辑自测（standalone，无 MoviePilot 运行时/真实数据库依赖）

覆盖场景：
A. 屏蔽 -> RssSites 变为 [-1] 且备份已保存
B. 二次进入屏蔽（幂等）-> 备份不被 [-1] 污染（不覆盖）
C. 恢复 -> 备份原值还原，备份 key 清除
D. 重启自愈三分支：
   D1 屏蔽态 + RssSites != [-1] -> 补写 [-1]
   D2 非屏蔽态 + 存在遗留备份 -> 还原备份并清除
   D3 干净态 -> 不动
E. 恢复路径无备份时走 _try_set_default_sites_for_unblocked（还原备份优先，二者不冲突）
F. 屏蔽时 RssSites 已是 [-1] -> 不备份（防污染兜底）

实现方式：直接从 __init__.py 提取 P115SubSearch 类（import 前桩掉全部 app.* 依赖），
mock SystemConfigOper（app.db.systemconfig_oper 模块）与 update_config。
"""
import importlib.util
import sys
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent

# ---------------- 桩掉 app.* 运行时依赖 ----------------


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


def _mk(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


_app = _mk('app')
_mk('app.core')
_app_conf = _mk('app.core.config')
_app_conf.settings = types.SimpleNamespace(TZ='Asia/Shanghai', PROXY=None)
_app_conf.global_vars = types.SimpleNamespace(is_system_stopped=False)
_mk('app.core.event')
_app_event = types.ModuleType('app.core.event')
_app_event.Event = object
_app_event.eventmanager = types.SimpleNamespace(
    register=lambda *a, **k: (lambda f: f)
)
sys.modules['app.core.event'] = _app_event
_mk('app.db')
_mk('app.db.subscribe_oper')
sys.modules['app.db.subscribe_oper'].SubscribeOper = object
_mk('app.db.models')
_mk('app.db.models.site')
sys.modules['app.db.models.site'].Site = object
_app_db = sys.modules['app.db']
_app_db.SessionFactory = lambda: None
_mk('app.db.systemconfig_oper')
_mk('app.log')
sys.modules['app.log'].logger = _Logger()
_mk('app.plugins')
sys.modules['app.plugins']._PluginBase = object
_mk('app.schemas.types')
_types_m = sys.modules['app.schemas.types']
for _n in ('EventType', 'MediaType', 'NotificationType'):
    setattr(_types_m, _n, types.SimpleNamespace(__getattr__=lambda k: k))
mk_stub = lambda k: k
sys.modules['app.schemas.types'].EventType = type('EventType', (), {'PluginAction': 'PluginAction', 'SubscribeAdded': 'SubscribeAdded', 'SubscribeModified': 'SubscribeModified'})
sys.modules['app.schemas.types'].MediaType = type('MediaType', (), {'TV': 'TV', 'MOVIE': 'MOVIE'})
sys.modules['app.schemas.types'].NotificationType = type('NotificationType', (), {'Plugin': 'Plugin', 'Manual': 'Manual'})
# 事件装饰器桩：register 返回原函数
class _EM:
    @staticmethod
    def register(evt):
        def deco(fn):
            return fn
        return deco
sys.modules['app.core.event'].eventmanager = _EM
# 插件子包桩（避免拉入 clients/handlers 真实依赖）
for sub in ['clients', 'handlers', 'ui', 'utils']:
    _mk(f'p115subsearch.{sub}')
sys.modules['p115subsearch'] = types.ModuleType('p115subsearch')
sys.modules['p115subsearch'].__path__ = [str(PLUGIN)]
sys.modules['p115subsearch.clients'].PanSouClient = object
sys.modules['p115subsearch.clients'].P115ClientManager = object
sys.modules['p115subsearch.clients'].NullbrClient = object
sys.modules['p115subsearch.clients'].HDHiveOpenAPIClient = object
sys.modules['p115subsearch.clients'].HDHiveOpenAPIError = Exception
sys.modules['p115subsearch.clients'].KDocsClient = object
sys.modules['p115subsearch.clients'].KDocsError = Exception
sys.modules['p115subsearch.handlers'].SearchHandler = lambda **k: types.SimpleNamespace()
sys.modules['p115subsearch.handlers'].SyncHandler = lambda **k: types.SimpleNamespace()
sys.modules['p115subsearch.handlers'].SubscribeHandler = lambda **k: types.SimpleNamespace(
    set_blocked_sites_only_115=lambda: [-1],
)
sys.modules['p115subsearch.handlers'].ApiHandler = lambda **k: types.SimpleNamespace()
sys.modules['p115subsearch.ui'].UIConfig = types.SimpleNamespace(get_form=lambda: ([], {}), get_page=lambda h: [])
sys.modules['p115subsearch.utils'].download_so_file = lambda *a, **k: None

# 外部依赖桩（__init__.py 顶部 import）
for mod in ('pytz', 'apscheduler.schedulers.background', 'apscheduler.triggers.cron', 'sqlalchemy'):
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)
sys.modules['pytz'].timezone = lambda tz: None
import datetime as _dt
sys.modules['apscheduler.schedulers.background'].BackgroundScheduler = object
sys.modules['apscheduler.triggers.cron'].CronTrigger = type('CronTrigger', (), {'from_crontab': staticmethod(lambda c, timezone=None: None)})
sys.modules['sqlalchemy'].text = lambda s: s

# ---------------- Mock SystemConfigOper ----------------

MOCK_STORE = {}


class MockSystemConfigOper:
    def __init__(self, *a, **k):
        pass

    def get(self, key):
        return MOCK_STORE.get(key)

    def set(self, key, value):
        MOCK_STORE[key] = list(value) if isinstance(value, list) else value


sys.modules['app.db.systemconfig_oper'].SystemConfigOper = MockSystemConfigOper

# ---------------- 加载插件类 ----------------

spec = importlib.util.spec_from_file_location('p115subsearch.__init__', PLUGIN / '__init__.py')
init_mod = importlib.util.module_from_spec(spec)
sys.modules['p115subsearch.__init__'] = init_mod
spec.loader.exec_module(init_mod)

P115SubSearch = init_mod.P115SubSearch

# ---------------- 测试辅助 ----------------

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_plugin():
    """构造一个最小可用的插件实例（绕过 __init__ 依赖）"""
    p = object.__new__(P115SubSearch)
    # 屏蔽 handler 重建（_init_subscribe_handler 会调用，桩掉 post_message 等）
    p.post_message = lambda *a, **k: None
    p._exclude_subscribes = []
    p._subscribe_filter_mode = "exclude"
    p._notify = False
    p._subscribe_handler = types.SimpleNamespace(set_blocked_sites_only_115=lambda: [-1])
    p._toggle_scheduler = types.SimpleNamespace(
        remove_job=lambda *a, **k: None,
        add_job=lambda *a, **k: None,
    )
    p._unblock_site_ids = [12, 14]
    p._unblock_site_names = ["站点A", "站点B"]
    p._unblock_delay_minutes = 5
    p._system_subscribe_window_hours = 1.0
    p._block_system_subscribe = False
    p._rss_sites_backup = None
    # 记录 update_config 落盘的配置
    saved = {}
    p.update_config = lambda cfg: saved.update(cfg)
    p._resolve_site_ids = lambda ids=None, names=None: [12, 14]
    p._apply_sites_to_all_subscribes = lambda site_ids, reason="": None
    p._try_set_default_sites_for_unblocked_calls = []

    def _try(site_ids):
        p._try_set_default_sites_for_unblocked_calls.append(list(site_ids))

    p._try_set_default_sites_for_unblocked = _try
    p._saved_config = saved
    return p


ORIGINAL_RSS = [12, 14, 3, 8, 1, 5, 10, 7, 4, 9, 15]

# ---------------- 测试用例 ----------------

print("=" * 60)
print("P115SubSearch v1.7.2 RssSites 备份/还原/自愈 逻辑自测")
print("=" * 60)

# A. 屏蔽 -> RssSites=[-1] 且备份保存
MOCK_STORE.clear()
MOCK_STORE['RssSites'] = list(ORIGINAL_RSS)
p = make_plugin()
p._enter_blocked(reason="测试A")
check("A1 屏蔽后 RssSites=[-1]", MOCK_STORE.get('RssSites') == [-1], f"actual={MOCK_STORE.get('RssSites')}")
check("A2 备份值=原值", p._rss_sites_backup == ORIGINAL_RSS, f"actual={p._rss_sites_backup}")
check("A3 备份持久化到插件配置", p._saved_config.get('rss_sites_backup') == ORIGINAL_RSS)
check("A4 屏蔽态标志置位", p._block_system_subscribe is True)

# B. 二次屏蔽（幂等）-> 备份不被污染
p._enter_blocked(reason="测试B-二次屏蔽")
check("B1 二次屏蔽后 RssSites 仍为 [-1]", MOCK_STORE.get('RssSites') == [-1])
check("B2 备份未被 [-1] 覆盖（幂等）", p._rss_sites_backup == ORIGINAL_RSS, f"actual={p._rss_sites_backup}")

# B2'. 备份丢失但 RssSites 已是 [-1]（重启场景）-> 不备份
MOCK_STORE.clear()
MOCK_STORE['RssSites'] = [-1]
p2 = make_plugin()
p2._enter_blocked(reason="测试B2-已是-1")
check("B3 RssSites 已是[-1]时不产生备份", p2._rss_sites_backup is None, f"actual={p2._rss_sites_backup}")

# C. 恢复 -> 备份还原 + key 清除
p._enter_unblocked(reason="测试C-窗口期恢复")
check("C1 恢复后 RssSites=原值", MOCK_STORE.get('RssSites') == ORIGINAL_RSS, f"actual={MOCK_STORE.get('RssSites')}")
check("C2 备份 key 已清除", p._rss_sites_backup is None)
check("C3 有备份时不再走默认站点尝试（还原优先）", len(p._try_set_default_sites_for_unblocked_calls) == 0)
check("C4 恢复态标志复位", p._block_system_subscribe is False)

# E. 恢复路径无备份 -> 走 _try_set_default_sites_for_unblocked
MOCK_STORE.clear()
MOCK_STORE['RssSites'] = list(ORIGINAL_RSS)
p3 = make_plugin()
p3._rss_sites_backup = None
p3._enter_unblocked(reason="测试E-无备份恢复")
check("E1 无备份时走默认站点尝试", len(p3._try_set_default_sites_for_unblocked_calls) == 1)
check("E2 无备份时 RssSites 不被插件改动", MOCK_STORE.get('RssSites') == ORIGINAL_RSS)

# D1. 重启自愈：屏蔽态 + RssSites != [-1] -> 补写 [-1]
MOCK_STORE.clear()
MOCK_STORE['RssSites'] = list(ORIGINAL_RSS)
p4 = make_plugin()
p4._block_system_subscribe = True
p4._rss_sites_backup = None
p4._selfheal_rss_sites_on_start()
check("D1-1 自愈(屏蔽态)后 RssSites=[-1]", MOCK_STORE.get('RssSites') == [-1])
check("D1-2 自愈(屏蔽态)补建了备份", p4._rss_sites_backup == ORIGINAL_RSS)

# D2. 重启自愈：非屏蔽态 + 存在遗留备份 -> 还原并清除
MOCK_STORE.clear()
MOCK_STORE['RssSites'] = [-1]
p5 = make_plugin()
p5._block_system_subscribe = False
p5._rss_sites_backup = list(ORIGINAL_RSS)
p5._selfheal_rss_sites_on_start()
check("D2-1 自愈(非屏蔽态)后 RssSites=原值", MOCK_STORE.get('RssSites') == ORIGINAL_RSS)
check("D2-2 自愈(非屏蔽态)清除了备份", p5._rss_sites_backup is None)

# D3. 重启自愈：干净态 -> 不动
MOCK_STORE.clear()
MOCK_STORE['RssSites'] = list(ORIGINAL_RSS)
p6 = make_plugin()
p6._block_system_subscribe = False
p6._rss_sites_backup = None
p6._selfheal_rss_sites_on_start()
check("D3-1 干净态 RssSites 不变", MOCK_STORE.get('RssSites') == ORIGINAL_RSS)
check("D3-2 干净态无备份产生", p6._rss_sites_backup is None)

# D1'. 重启自愈：屏蔽态且 RssSites 已是 [-1] -> 不重复动作、不污染
MOCK_STORE.clear()
MOCK_STORE['RssSites'] = [-1]
p7 = make_plugin()
p7._block_system_subscribe = True
p7._rss_sites_backup = list(ORIGINAL_RSS)  # 已有正确备份
p7._selfheal_rss_sites_on_start()
check("D4-1 屏蔽态且已[-1]时保持[-1]", MOCK_STORE.get('RssSites') == [-1])
check("D4-2 已有备份不被覆盖", p7._rss_sites_backup == ORIGINAL_RSS)

# 版本断言
check("V1 插件版本=1.7.2", P115SubSearch.plugin_version == "1.7.2", f"actual={P115SubSearch.plugin_version}")
import json
pkg = json.loads((PLUGIN.parent.parent / 'package.v2.json').read_text(encoding='utf-8'))
check("V2 package.v2.json 版本=v1.7.2", pkg['P115SubSearch']['version'] == 'v1.7.2')
check("V3 package.v2.json 含 v1.7.2 history", 'v1.7.2' in pkg['P115SubSearch']['history'])

# ---------------- 结果 ----------------

print("=" * 60)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项失败 -> {FAILURES}")
    sys.exit(1)
print("结果：全部断言通过（25 项）")
