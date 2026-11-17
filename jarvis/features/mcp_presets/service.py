"""Recommended MCP server presets (board 204bc125 part 4): a pinned program version and an allowlist of tool pins.

REA first: rea-agents 6.7.0 from the project's own lockfile install (.integrations/rea, paths in
config/integrations.json), started as `node rea.mjs --mcp` with Ghidra and Java given as fixed, non-secret settings.
Only static read tools are pinned, all family local, so their results never reach an external model. Everything
else REA offers stays hidden: debuggers (gdb, rizin, Frida), ADB devices, process and UI capture, browser/CDP and
Electron sessions, tools that write analysis notes or files (set_*, annotate, record_*, export_*, extract_*,
recover_*), and free-form commands (rizin, objdump). Pins name what the server must report; a missing one stops
the setup instead of approving a smaller set silently.
"""
import json
from pathlib import Path

from jarvis.features.integrations.service import IntegrationError, IntegrationHub
from jarvis.features.mcp_client.service import McpError, McpSettings, discover

REA_PACKAGE, REA_VERSION = 'rea-agents', '6.7.0'
REA_SERVER_ID, REA_LABEL = 'rea', 'REA'
REA_MAXMEM = '1G'                      # Ghidra headless heap, the same as the static analysis tool uses

# REA wraps each answer in an evidence record whose first ~3 KB are provenance metadata; keep the payload.
_KEEP = ['result', 'normalized_result']
_READ = {'family': 'local', 'effect': 'read', 'timeout_s': 60, 'max_chars': 8000, 'result_keys': _KEEP}
_LONG = {**_READ, 'timeout_s': 120, 'max_chars': 12000}
# A task session works on one opened binary, so 'document' (which open analysis to use) stays at its default.
_ONE = {**_READ, 'blocked_args': ['document']}
REA_PINS = {
    # Open and close one binary. REA detects PE/ELF/Mach-O itself; format, architecture, entry_point and provider
    # are for raw blobs and a small model fills them wrongly. snapshot_path would save or load an analysis file.
    # REA's own description explains those raw-blob options, so the approved description says only what applies.
    'open_binary': {**_LONG, 'path_args': [{'name': 'path', 'mode': 'read'}],
                    'blocked_args': ['format', 'architecture', 'entry_point', 'provider_id', 'snapshot_path'],
                    'description': 'Open one local program or app file (PE/EXE/DLL, ELF, Mach-O, archive, JavaScript) '
                                   'for static analysis. Give only its path; the format is detected from the file. '
                                   'Call this first: the other REA tools work on the opened file.'},
    'close_binary': {**_READ, 'blocked_args': ['snapshot_path', 'overwrite']},
    'binary_overview': _LONG,
    'list_documents': _READ,
    'current_document': _READ,
    'list_segments': _ONE,
    'list_procedures': _ONE,
    'search_procedures': _ONE,
    'procedure_info': _ONE,
    'procedure_pseudo_code': {**_LONG, 'blocked_args': ['document']},
    'procedure_assembly': _ONE,
    'procedure_callers': _ONE,
    'procedure_callees': _ONE,
    'procedure_references': _ONE,
    'batch_decompile': _LONG,
    'analyze_function': _LONG,
    'inspect_native_api': _READ,
    'get_call_graph': _READ,
    'list_strings': _ONE,
    'search_strings': _ONE,
    'list_names': _ONE,
    'xrefs': _ONE,
    'find_xrefs_to_name': _READ,
    'read_bytes': _ONE,
    'inspect_signature': _READ,
    # Single-file format readers take the file path directly. (inspect_binary_layout is left out: it is the pwntools
    # profile for Linux x64 ELF and refuses PE files, which misled the model in the eval.)
    'inspect_pe_resources': {**_READ, 'path_args': [{'name': 'path', 'mode': 'read'}],
                             'blocked_args': ['max_file_bytes', 'max_entries']},
    'inspect_go_binary': {**_READ, 'path_args': [{'name': 'path', 'mode': 'read'}]},
    'inspect_rust_binary': {**_READ, 'path_args': [{'name': 'path', 'mode': 'read'}]},
    'analyze_javascript_application': {**_LONG, 'path_args': [{'name': 'input_path', 'mode': 'read'},
                                                              {'name': 'source_paths', 'mode': 'read'}],
                                       'not_paths': ['max_semantic_nodes_per_file', 'max_semantic_unknowns_per_file']},
}


class PresetError(ValueError):
    """The preset cannot be set up; the message says what to do."""


def rea_launch(guard, hub=None):
    """(command, args, fixed_env) for the installed REA, checked: node, rea.mjs, Ghidra and Java come from
    config/integrations.json through IntegrationHub (regular files or folders outside the workspace, no links), and
    the installed package must be exactly REA_VERSION."""
    hub = hub or IntegrationHub(guard, None)
    try:
        paths = hub.program_paths(('node', 'rea_entry', 'ghidra', 'java_home'))
    except (IntegrationError, OSError, ValueError):
        raise PresetError('REA 설치 경로(config/integrations.json의 node, rea_entry, ghidra, java_home)를 확인하지 '
                          '못했습니다. REA·Ghidra 설치를 먼저 마쳐 주십시오.') from None
    package = Path(paths['rea_entry']).parents[1] / 'package.json'
    try:
        manifest = json.loads(package.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        manifest = {}
    if manifest.get('name') != REA_PACKAGE or manifest.get('version') != REA_VERSION:
        raise PresetError(f'설치된 REA가 {REA_PACKAGE} {REA_VERSION}이 아닙니다(찾은 버전: {manifest.get("version")}). '
                          f'.integrations/rea에서 `npm ci`로 잠금 파일 버전을 다시 설치해 주십시오.')
    fixed_env = {'GHIDRA_INSTALL_DIR': str(paths['ghidra']), 'JAVA_HOME': str(paths['java_home']),
                 'GHIDRA_HEADLESS_MAXMEM': REA_MAXMEM}
    return str(paths['node']), [str(paths['rea_entry']), '--mcp'], fixed_env


def setup_rea(guard, *, roots=('projects',), confirm=None, hub=None, discover_tools=discover):
    """Add (or replace) the REA server, list its tools once, and approve REA_PINS after confirm(found) returns
    True. roots: workspace folders open_binary and the file readers may use. Returns the approved server.
    A missing pinned tool, a failed start or a refused confirmation removes the entry again."""
    command, args, fixed_env = rea_launch(guard, hub)
    settings = McpSettings(guard)
    if any(server.id == REA_SERVER_ID for server in settings.load().servers):
        settings.remove(REA_SERVER_ID)
    settings.add_server(REA_SERVER_ID, REA_LABEL, command, args, fixed_env=fixed_env)
    try:
        found = discover_tools(guard, settings.load(), REA_SERVER_ID)
        if found['status'].get('state') != 'ready':
            raise PresetError('REA MCP 서버를 시작하지 못했습니다: ' + str(found['status'].get('reason', ''))[:160])
        missing = sorted(set(REA_PINS) - {tool['name'] for tool in found['tools']})
        if missing:
            raise PresetError(f'REA {REA_VERSION}이 이 도구를 알리지 않았습니다: ' + ', '.join(missing))
        if confirm is not None and not confirm(found):
            raise PresetError('승인하지 않아 REA 서버 항목을 지웠습니다.')
        settings.approve(REA_SERVER_ID, found['tools'], REA_PINS)
        if roots:
            settings.set_roots(REA_SERVER_ID, roots)
        return next(server for server in settings.load().servers if server.id == REA_SERVER_ID)
    except (PresetError, McpError):
        settings.remove(REA_SERVER_ID)
        raise
