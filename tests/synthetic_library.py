"""Test helpers around migratarr.demo_library (the fake library, shared with `migratarr demo`)."""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from migratarr.demo_library import *  # noqa: F401,F403 (tests import NOW, MOVIES, SERIES, route, ...)
from migratarr.demo_library import fake_docker, fake_urlopen
from runtime_config import load_config

ROOT = Path(__file__).resolve().parents[1]


def offline_world(network=True):
    """Patches that let a planner run for real against this fake world.

    network=False keeps the example config but makes any HTTP or Docker call fail.
    """
    stack = ExitStack()
    config = load_config(ROOT / 'config/examples/runtime.v1.example.json', environ={})
    stack.enter_context(patch('runtime_config.get_config', return_value=config))
    stack.enter_context(patch.dict('os.environ', {
        'TMDB_TOKEN': 'fake', 'MIGRATARR_PLANNER_CONFIG': str(ROOT / 'config/planner.example.json'),
        'MIGRATARR_STORAGE_TARGETS': str(ROOT / 'config/storage-targets.example.json')}))
    if network:
        stack.enter_context(patch('urllib.request.urlopen', fake_urlopen))
        stack.enter_context(patch('subprocess.check_output', fake_docker))
    else:
        for target in ('urllib.request.urlopen', 'subprocess.check_output', 'subprocess.run'):
            stack.enter_context(patch(target, side_effect=AssertionError('I/O during replay: ' + target)))
    stack.enter_context(patch('time.sleep'))
    return stack
