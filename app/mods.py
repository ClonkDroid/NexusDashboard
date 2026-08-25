import os
import re
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


def _mod_root():
    root = Path(os.getenv('MODS_LOCATION', '/app/mods')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _manifest(path):
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
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


@mods_blueprint.route('/', methods=['GET'])
@login_required
@gm_level(8)
def index():
    root = _mod_root()
    return render_template('mods/index.html.j2', mods=_entries(root))


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
    if len(payload) > max_size:
        flash(f'Mod exceeds the {max_size // 1024} KiB upload limit.', 'danger')
        return redirect(url_for('main.mods.index'))

    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        flash('Mod must be UTF-8 text.', 'danger')
        return redirect(url_for('main.mods.index'))

    staging = root / f'.{filename}.upload'
    target = root / filename
    staging.write_text(text, encoding='utf-8')
    manifest = _manifest(staging)
    if manifest is None:
        staging.unlink(missing_ok=True)
        flash('Mod rejected: no valid dlu.mod manifest was found.', 'danger')
        return redirect(url_for('main.mods.index'))
    if manifest['api'] != 1:
        staging.unlink(missing_ok=True)
        flash(f"Mod requests unsupported API {manifest['api']}; this server provides API 1.", 'danger')
        return redirect(url_for('main.mods.index'))

    installed_ids = {
        item['id'] for item in _entries(root)
        if item['id'] and item['filename'] != filename
    }
    if manifest['id'] in installed_ids:
        staging.unlink(missing_ok=True)
        flash(f"A mod with id '{manifest['id']}' is already installed.", 'danger')
        return redirect(url_for('main.mods.index'))

    os.replace(staging, target)
    log_audit(f"MODS::INSTALL {manifest['id']} {manifest['version']} ({filename})")
    flash(f"Installed {manifest['name']} {manifest['version']}. Run /modreload in-game to activate it.", 'success')
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
