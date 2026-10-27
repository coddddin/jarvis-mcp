"""Make JARVIS's own copy of a working CosyVoice 3 install with NTFS hard links (no extra disk, no download).

Source: the CosyVoice checkout that VoiceStudio installs (%APPDATA%\\OmniVoice\\engines\\cosyvoice\\CosyVoice), its
.venv and its Python 3.10. Target (default %LOCALAPPDATA%\\Jarvis\\voice\\cosyvoice):
    python/   the base interpreter            venv/   a venv on it, site-packages linked from the source .venv
    src/      the checkout without .venv and models
    models/Fun-CosyVoice3-0.5B/              the weights
Hard links survive deleting or updating VoiceStudio. Python replaces files rather than editing them in place, so
the two installs never change each other. This script only links and creates the venv; it runs no CosyVoice code.
Afterwards pin the transformers that upstream CosyVoice requires (see docs/guides/voice.md):
    uv pip install --python <target>/venv/Scripts/python.exe transformers==4.51.3

Usage: python scripts/voice/link_cosyvoice.py [--source DIR] [--target DIR]
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

MODEL = 'Fun-CosyVoice3-0.5B'
MARKER = '.jarvis-cosyvoice'
SKIP_IN_SOURCE = {'.venv', 'pretrained_models', '.git', '__pycache__'}


def default_source():
    return Path(os.environ.get('APPDATA', '')) / 'OmniVoice' / 'engines' / 'cosyvoice' / 'CosyVoice'


def default_target():
    return Path(os.environ.get('LOCALAPPDATA', '')) / 'Jarvis' / 'voice' / 'cosyvoice'


def plain_dir(path):
    return path.is_dir() and not path.is_symlink() and not path.is_junction()


def base_python(venv):
    """The interpreter folder a venv was made from, read from its pyvenv.cfg."""
    for line in (venv / 'pyvenv.cfg').read_text(encoding='utf-8').splitlines():
        key, _, value = line.partition('=')
        if key.strip() == 'home':
            return Path(value.strip())
    raise SystemExit('pyvenv.cfg has no home entry')


def link_tree(source, target, skip=frozenset()):
    """Hard-link every regular file under source into target. Returns the number of files linked."""
    count = 0
    for root, dirs, files in os.walk(source):
        here = Path(root)
        dirs[:] = [d for d in dirs if d not in skip and plain_dir(here / d)]
        destination = target / here.relative_to(source)
        destination.mkdir(parents=True, exist_ok=True)
        for name in files:
            file = here / name
            if file.is_symlink() or name in skip:
                continue
            link = destination / name
            if not link.exists():
                os.link(file, link)
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--source', type=Path, default=default_source())
    parser.add_argument('--target', type=Path, default=default_target())
    args = parser.parse_args()
    source, target = args.source.resolve(), args.target.resolve()
    model = source / 'pretrained_models' / MODEL
    for needed in (source / 'cosyvoice', source / 'third_party' / 'Matcha-TTS', source / '.venv', model):
        if not plain_dir(needed):
            raise SystemExit(f'Missing or linked folder in the source install: {needed}')
    python = base_python(source / '.venv')
    if not (python / 'python.exe').is_file():
        raise SystemExit(f'Base Python not found: {python}')
    if source.drive.lower() != target.drive.lower() or python.drive.lower() != target.drive.lower():
        raise SystemExit('Hard links need the source, its Python and the target on one volume.')
    if target.exists() and not (target / MARKER).is_file():
        raise SystemExit(f'Target exists and is not a JARVIS CosyVoice folder: {target}')
    target.mkdir(parents=True, exist_ok=True)
    (target / MARKER).write_text('JARVIS CosyVoice copy (hard links). Safe to delete as a whole.\n', encoding='utf-8')

    counts = {'python': link_tree(python, target / 'python', {'__pycache__'}),
              'src': link_tree(source, target / 'src', SKIP_IN_SOURCE),
              'models': link_tree(model, target / 'models' / MODEL, {'.cache'})}
    venv = target / 'venv'
    if not (venv / 'pyvenv.cfg').is_file():
        subprocess.run([str(target / 'python' / 'python.exe'), '-I', '-m', 'venv', '--without-pip', str(venv)],
                       check=True)
    counts['site-packages'] = link_tree(source / '.venv' / 'Lib' / 'site-packages', venv / 'Lib' / 'site-packages',
                                        {'__pycache__'})
    for name, count in counts.items():
        print(f'{name}: {count} files linked')
    print(f'Done: {target}')


if __name__ == '__main__':
    sys.exit(main())
