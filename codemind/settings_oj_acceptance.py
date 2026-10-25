"""仅供 scripts/oj_queue_acceptance.py 使用；隔离参数缺失时拒绝启动。"""
import json
import tempfile
from copy import deepcopy

from .settings import *  # noqa: F403

_acceptanceRoot = Path(tempfile.gettempdir()).resolve()
_acceptanceRun = os.environ.get('OJ_ACCEPTANCE_RUN', '')
_acceptancePath = os.environ.get('OJ_ACCEPTANCE_DB', '')
if not _acceptanceRun or not _acceptancePath:
    raise RuntimeError('队列验收必须显式指定独立临时数据库和运行标识')
_acceptanceDb = Path(_acceptancePath).resolve()
_acceptanceDir = _acceptanceDb.parent
if (_acceptanceDir.parent != _acceptanceRoot
        or not _acceptanceDir.name.startswith('codemind-oj-')
        or _acceptanceDb.name != 'acceptance.sqlite3'
        or _acceptanceDb == (BASE_DIR / 'db.sqlite3').resolve()):
    raise RuntimeError('拒绝使用不属于本次临时目录的数据库')
_marker = json.loads((_acceptanceDir / 'acceptance.json').read_text(encoding='utf-8'))
if _marker != {'run': _acceptanceRun, 'database': str(_acceptanceDb)}:
    raise RuntimeError('队列验收数据库标记不匹配')
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': str(_acceptanceDb),
    'OPTIONS': {'timeout': 20},
}}
AGENT_ENABLED = False
Q_CLUSTER = deepcopy(Q_CLUSTER)
Q_CLUSTER['name'] = 'oj-acceptance-' + _acceptanceRun
Q_CLUSTER['workers'] = 1
Q_CLUSTER['ALT_CLUSTERS']['long']['workers'] = 1
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                      'LOCATION': Q_CLUSTER['name']}}
