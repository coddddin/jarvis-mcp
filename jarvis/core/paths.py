"""Stable project locations, independent of cwd and feature module placement."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = PROJECT_ROOT / 'config'
AUTH_ROOT = PROJECT_ROOT / '.jarvis-auth'
DEFAULT_WORKSPACE = PROJECT_ROOT / 'JarvisWorkspace'
FEATURES_ROOT = PROJECT_ROOT / 'jarvis' / 'features'
WORKERS_ROOT = PROJECT_ROOT / 'jarvis' / 'workers'
