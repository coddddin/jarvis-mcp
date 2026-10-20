"""Install the pinned official free Ghidra archive into project-local tools only."""
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1] / '.integrations'
URL = 'https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.4_build/ghidra_12.1.4_PUBLIC_20260921.zip'
SHA256 = 'ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db'


def main():
    ROOT.mkdir(exist_ok=True)
    destination = ROOT / 'ghidra_12.1.4_PUBLIC'
    if destination.exists():
        raise SystemExit('Existing installation preserved; verify it before replacing.')
    archive = ROOT / 'ghidra-12.1.4.zip'
    if not archive.exists():
        print('Downloading official Ghidra 12.1.4 archive...', flush=True)
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(URL, timeout=180) as response, archive.open('xb') as output:
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > 700_000_000:
                    raise ValueError('Archive size limit exceeded.')
                digest.update(block)
                output.write(block)
    else:
        digest = hashlib.sha256(archive.read_bytes())
    if digest.hexdigest() != SHA256:
        raise SystemExit('Archive digest mismatch; extraction refused. Preserve for investigation.')
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            target = (ROOT / info.filename).resolve()
            if not target.is_relative_to(destination) or info.filename.startswith(('/', '\\')):
                raise ValueError('Archive path outside the new installation.')
        bundle.extractall(ROOT)
    print(json.dumps({'version': '12.1.4', 'path': str(destination), 'sha256': SHA256}), flush=True)


if __name__ == '__main__':
    main()
