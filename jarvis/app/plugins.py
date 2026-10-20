"""Discover trusted code plugins and validate every factory before registration."""
from jarvis.core.paths import FEATURES_ROOT
import importlib.util
import inspect
from pathlib import Path
import sys

from jarvis.app.runtime import guarded_tool

PLUGIN_DIR = FEATURES_ROOT


class PluginLoadError(RuntimeError):
    """Fail-closed plugin discovery or registration error."""


def _validate_path(path):
    relative = path.relative_to(PLUGIN_DIR)
    if (len(relative.parts) != 3 or relative.parts[1] != 'tools' or not path.stem.isidentifier()
            or any(not part.isidentifier() for part in relative.parts[:-1])
            or path.parent.is_symlink() or path.parent.is_junction()
            or path.parent.parent.is_symlink() or path.parent.parent.is_junction()
            or path.is_symlink() or path.is_junction()
            or not path.is_file() or path.stat().st_nlink > 1):
        raise PluginLoadError(f'Invalid plugin file: {path.name}')


def _import_plugin(path):
    name = 'jarvis.features.' + '.'.join(path.relative_to(PLUGIN_DIR).with_suffix('').parts)
    existing = sys.modules.get(name)
    if existing is not None:
        if Path(existing.__file__).resolve() != path.resolve():
            raise PluginLoadError(f'Cached plugin path mismatch: {path.name}')
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f'Cannot import plugin: {path.name}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def collect_tools(context):
    if (not PLUGIN_DIR.is_dir() or PLUGIN_DIR.is_symlink() or PLUGIN_DIR.is_junction()
            or PLUGIN_DIR.resolve().is_relative_to(context.guard.workspace)):
        raise PluginLoadError('Plugin directory must be trusted code outside the data workspace.')
    if any(PLUGIN_DIR.glob('*_tool.py')):
        raise PluginLoadError('Invalid plugin placement; use <feature>/tools/*_tool.py.')
    paths = []
    for category in sorted(PLUGIN_DIR.iterdir()):
        if category.name.startswith(('.', '_')):
            continue
        if category.is_symlink() or category.is_junction():
            raise PluginLoadError(f'Linked plugin category is forbidden: {category.name}')
        if category.is_dir():
            if not category.name.isidentifier():
                raise PluginLoadError(f'Invalid plugin category: {category.name}')
            if any(category.glob('*_tool.py')):
                raise PluginLoadError('Invalid plugin placement; use <feature>/tools/*_tool.py.')
            directory = category / 'tools'
            if directory.is_symlink() or directory.is_junction():
                raise PluginLoadError(f'Linked plugin directory is forbidden: {category.name}')
            if directory.exists() and not directory.is_dir():
                raise PluginLoadError(f'Invalid plugin directory: {category.name}')
            paths.extend(directory.glob('*_tool.py'))
    paths.sort(key=lambda path: path.relative_to(PLUGIN_DIR).as_posix())
    if not paths:
        raise PluginLoadError('No tool plugins found.')
    for path in paths:
        _validate_path(path)
    names, functions = set(), []
    for path in paths:
        try:
            module = _import_plugin(path)
            factory = getattr(module, 'build', None)
            if not inspect.isfunction(factory) or factory.__module__ != module.__name__:
                raise PluginLoadError(f'Plugin requires its own build(context): {path.name}')
            inspect.signature(factory).bind(context)
            function = factory(context)
            if (not inspect.isfunction(function) or inspect.iscoroutinefunction(function)
                    or function.__module__ != module.__name__
                    or not function.__name__.isidentifier() or function.__name__.startswith('_')):
                raise PluginLoadError(f'Plugin must return one local synchronous tool function: {path.name}')
            if function.__name__ in names:
                raise PluginLoadError(f'Duplicate tool name: {function.__name__}')
            names.add(function.__name__)
            functions.append(guarded_tool(function, context))
        except PluginLoadError:
            raise
        except Exception as exc:
            raise PluginLoadError(f'Plugin {path.name} failed ({type(exc).__name__}).') from exc
    return functions


def load_tools(mcp, context):
    functions = collect_tools(context)
    for function in functions:
        try:
            mcp.add_tool(function)
        except Exception as exc:
            raise PluginLoadError(f'Tool registration failed: {function.__name__} ({type(exc).__name__}).') from exc
    return tuple(function.__name__ for function in functions)
