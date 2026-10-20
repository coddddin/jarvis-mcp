"""AST/file-layout guard for the feature package architecture; no imports executed."""
import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT_ENTRIES = {'server.py', 'google_auth_setup.py'}
LEGACY_MODULES = {'runtime', 'guardrails', 'knowledge', 'google_briefing', 'snapshots',
                  'file_organizer', 'workflow_launcher', 'debug_assistant', 'web_information', 'web_transport', 'tools'}
REQUIRED = ('jarvis/core/paths.py', 'jarvis/core/guardrails.py', 'jarvis/core/redaction.py',
            'jarvis/app/server.py', 'jarvis/app/runtime.py', 'jarvis/app/plugins.py',
            'jarvis/workers/web_https.py', 'jarvis/workers/workflow_status.py',
            'jarvis/cli/google_auth.py', 'jarvis/__main__.py')


def check_architecture(root):
    root = Path(root).resolve()
    errors = []
    for path in root.glob('*.py'):
        if path.name not in ROOT_ENTRIES:
            errors.append(f'Python implementation belongs inside jarvis/: {path.name}')
    for name in (*ROOT_ENTRIES, *REQUIRED):
        if not (root / name).is_file():
            errors.append(f'Missing architecture file: {name}')
    if (root / 'tools').exists():
        errors.append('Legacy tools/ directory must not be reintroduced; use jarvis/features/<feature>/tools/.')
    package = root / 'jarvis'
    paths = list(package.rglob('*.py')) + list((root / 'tests').rglob('*.py'))
    paths += [root / name for name in ROOT_ENTRIES if (root / name).is_file()]
    for path in paths:
        relative = path.relative_to(root)
        parts = relative.parts
        if path.is_symlink() or path.is_junction() or path.stat().st_nlink != 1:
            errors.append(f'Linked code file: {relative}')
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        except (SyntaxError, UnicodeError):
            errors.append(f'Invalid Python source: {relative}')
            continue
        if parts[0] == 'jarvis':
            if len(parts) == 2 and path.name not in {'__init__.py', '__main__.py'}:
                errors.append(f'Classify package implementation by responsibility: {relative}')
            if path.name == '__init__.py' and any(not (isinstance(n, ast.Expr) and
                    isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)) for n in tree.body):
                errors.append(f'Package initializer must not run code: {relative}')
            if len(parts) >= 3 and parts[1] == 'features':
                if len(parts) == 4 and path.name not in {'service.py', '__init__.py'}:
                    errors.append(f'Feature implementation must use service.py: {relative}')
                if len(parts) >= 5 and (len(parts) != 5 or parts[3] != 'tools'
                        or path.name != '__init__.py' and not path.name.endswith('_tool.py')):
                    errors.append(f'Feature adapters belong in tools/*_tool.py: {relative}')
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                name = node.module or ''
                if node.level:
                    try:
                        name = resolve_name('.' * node.level + name, '.'.join(parts[:-1]))
                    except (ValueError, ImportError):
                        errors.append(f'Invalid relative import in {relative}')
                        continue
                names = [name] + [name + '.' + alias.name for alias in node.names if alias.name != '*']
            for name in names:
                if name.split('.')[0] in LEGACY_MODULES:
                    errors.append(f'Legacy root import in {relative}: {name}')
                if parts[:2] == ('jarvis', 'core') and name.startswith(('jarvis.app', 'jarvis.features', 'jarvis.cli', 'jarvis.workers', 'jarvis.agents')):
                    errors.append(f'Core reverse dependency in {relative}: {name}')
                if parts[:2] == ('jarvis', 'agents') and name.startswith(('jarvis.app', 'jarvis.cli', 'jarvis.features', 'jarvis.workers')):
                    errors.append(f'Agent control must depend only on core/contracts in {relative}: {name}')
                if parts[:2] == ('jarvis', 'workers') and name.startswith('jarvis'):
                    errors.append(f'Isolated worker imports project code in {relative}: {name}')
                if parts[:2] == ('jarvis', 'features') and name.startswith(('jarvis.app', 'jarvis.cli')):
                    errors.append(f'Feature reverse dependency in {relative}: {name}')
                if (parts[:2] == ('jarvis', 'features') and len(parts) == 4 and path.name == 'service.py'
                        and name.startswith('jarvis.features.') and '.tools' in name):
                    errors.append(f'Service imports a tool adapter in {relative}: {name}')
    return errors


def main():
    errors = check_architecture(Path(__file__).resolve().parents[1])
    for error in errors:
        print('ERROR:', error)
    if not errors:
        print('Architecture checks passed: entry points, feature layout, import boundaries and isolated workers.')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
