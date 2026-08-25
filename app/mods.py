import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_user import login_required
from werkzeug.utils import secure_filename

from app import gm_level, log_audit

mods_blueprint = Blueprint('mods', __name__)

_MANIFEST_RE = re.compile(r"dlu\.mod\s*\{(?P<body>.*?)\}", re.IGNORECASE | re.DOTALL)
_FIELD_RE = re.compile(
    r"\b(?P<key>id|name|version|api)\s*=\s*(?:(?P<quote>['\"])(?P<str>.*?)(?P=quote)|(?P<num>\d+))",
    re.IGNORECASE | re.DOTALL,
)

_CATALOG = (
    {
        'id': 'debug-panel',
        'name': 'Debug Panel',
        'filename': 'DebugPanel.dlumod',
        'description': 'Graphical developer toolbox: zones, item browser with previews, progression, currencies, missions, cheats and diagnostics.',
    },
    {
        'id': 'debug-world',
        'name': 'Debug World',
        'filename': 'DebugWorld.dlumod',
        'description': 'Developer command set and private Avant Gardens debug instance helpers.',
    },
)


def _mod_root():
    root = Path(os.getenv('MODS_LOCATION', '/app/mods')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _manifest_text(text):
    match = _MANIFEST_RE.search(text)
    if not match:
        return None
    data = {}
    for field in _FIELD_RE.finditer(match.group('body')):
        value = field.group('str') if field.group('str') is not None else field.group('num')
        data[field.group('key').lower()] = value.strip()
    if not data.get('id') or not data.get('name') or not data.get('version'):
        return None
    try:
        data['api'] = int(data.get('api', '1'))
    except ValueError:
        return None
    return data


def _manifest(path):
    try:
        return _manifest_text(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError):
        return None


def _entries(root):
    if not root.is_dir():
        return []
    result = []
    for path in sorted(root.glob('*.dlumod'), key=lambda item: item.name.lower()):
        manifest = _manifest(path)
        result.append({
            'filename': path.name,
            'valid': manifest is not None,
            'id': manifest.get('id') if manifest else None,
            'name': manifest.get('name') if manifest else path.stem,
            'version': manifest.get('version') if manifest else None,
            'api': manifest.get('api') if manifest else None,
            'size': path.stat().st_size,
        })
    return result


def _catalog_entries(installed):
    installed_by_id = {entry['id']: entry for entry in installed if entry['id']}
    result = []
    for entry in _CATALOG:
        item = dict(entry)
        item['installed'] = installed_by_id.get(entry['id'])
        result.append(item)
    return result


def _catalog_url(filename):
    base = os.getenv(
        'MOD_CATALOG_BASE_URL',
        'https://raw.githubusercontent.com/ClonkDroid/DarkflameServer/feature/lua-mod-framework/mods',
    ).rstrip('/')
    return f'{base}/{filename}'


def _install_payload(root, filename, payload, expected_id=None):
    max_size = int(os.getenv('MOD_UPLOAD_MAX_BYTES', str(1024 * 1024)))
    if len(payload) > max_size:
        return None, f'Mod exceeds the {max_size // 1024} KiB upload limit.'

    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        return None, 'Mod must be UTF-8 text.'

    manifest = _manifest_text(text)
    if manifest is None:
        return None, 'Mod rejected: no valid dlu.mod manifest was found.'
    if manifest['api'] != 1:
        return None, f"Mod requests unsupported API {manifest['api']}; this server provides API 1."
    if expected_id is not None and manifest['id'] != expected_id:
        return None, f"Catalog integrity check failed: expected mod id '{expected_id}', got '{manifest['id']}'."

    installed = _entries(root)
    conflicting = [
        item for item in installed
        if item['id'] == manifest['id'] and item['filename'] != filename
    ]
    if conflicting:
        return None, f"A mod with id '{manifest['id']}' is already installed as {conflicting[0]['filename']}."

    staging = root / f'.{filename}.upload'
    target = root / filename
    staging.write_text(text, encoding='utf-8')
    os.replace(staging, target)
    return manifest, None


@mods_blueprint.route('/', methods=['GET'])
@login_required
@gm_level(8)
def index():
    root = _mod_root()
    installed = _entries(root)
    return render_template(
        'mods/index.html.j2',
        mods=installed,
        catalog=_catalog_entries(installed),
    )


@mods_blueprint.route('/install', methods=['POST'])
@login_required
@gm_level(8)
def install():
    root = _mod_root()
    upload = request.files.get('mod_file')
    if upload is None or not upload.filename:
        flash('Choose a .dlumod file to install.', 'warning')
        return redirect(url_for('main.mods.index'))

    filename = secure_filename(upload.filename)
    if not filename.lower().endswith('.dlumod'):
        flash('Only .dlumod files can be installed.', 'danger')
        return redirect(url_for('main.mods.index'))

    max_size = int(os.getenv('MOD_UPLOAD_MAX_BYTES', str(1024 * 1024)))
    payload = upload.read(max_size + 1)
    manifest, error = _install_payload(root, filename, payload)
    if error:
        flash(error, 'danger')
        return redirect(url_for('main.mods.index'))

    log_audit(f"MODS::INSTALL {manifest['id']} {manifest['version']} ({filename})")
    flash(f"Installed {manifest['name']} {manifest['version']}. Run /modreload in-game to activate it.", 'success')
    return redirect(url_for('main.mods.index'))


@mods_blueprint.route('/catalog/<mod_id>/install', methods=['POST'])
@login_required
@gm_level(8)
def install_catalog(mod_id):
    catalog = next((item for item in _CATALOG if item['id'] == mod_id), None)
    if catalog is None:
        flash('Unknown catalog mod.', 'danger')
        return redirect(url_for('main.mods.index'))

    max_size = int(os.getenv('MOD_UPLOAD_MAX_BYTES', str(1024 * 1024)))
    request_url = _catalog_url(catalog['filename'])
    try:
        with urllib.request.urlopen(request_url, timeout=8) as response:
            payload = response.read(max_size + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        flash(f'Unable to download {catalog["name"]} from the mod catalog: {exc}', 'danger')
        return redirect(url_for('main.mods.index'))

    manifest, error = _install_payload(_mod_root(), catalog['filename'], payload, expected_id=catalog['id'])
    if error:
        flash(error, 'danger')
        return redirect(url_for('main.mods.index'))

    log_audit(f"MODS::CATALOG_INSTALL {manifest['id']} {manifest['version']} ({catalog['filename']})")
    flash(f"Installed {manifest['name']} {manifest['version']} from the catalog. Run /modreload in-game to activate it.", 'success')
    return redirect(url_for('main.mods.index'))


@mods_blueprint.route('/uninstall/<path:filename>', methods=['POST'])
@login_required
@gm_level(8)
def uninstall(filename):
    root = _mod_root()
    safe_name = secure_filename(filename)
    if safe_name != filename or not safe_name.lower().endswith('.dlumod'):
        flash('Invalid mod filename.', 'danger')
        return redirect(url_for('main.mods.index'))

    target = (root / safe_name).resolve()
    if target.parent != root or not target.is_file():
        flash('Mod not found.', 'warning')
        return redirect(url_for('main.mods.index'))

    manifest = _manifest(target)
    target.unlink()
    mod_id = manifest['id'] if manifest else safe_name
    log_audit(f"MODS::UNINSTALL {mod_id} ({safe_name})")
    flash(f"Uninstalled {mod_id}. Run /modreload in-game to apply the change.", 'success')
    return redirect(url_for('main.mods.index'))
