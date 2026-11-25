"""플랫폼 어댑터 로더. `.ctfm/adapter.py`가 있으면 그것을, 없으면 내장 CTFd 어댑터를 쓴다."""
import importlib.util

from ..state import load_env


def load(store):
    """(http, adapter 모듈)을 돌려준다. 어댑터는 make_http/check/list_challenges/fetch를 정의한다."""
    custom = store.meta / "adapter.py"
    if custom.is_file():
        spec = importlib.util.spec_from_file_location("ctfm_custom_adapter", custom)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        from . import ctfd as module
    return module.make_http(store.config(), load_env(store.root)), module
