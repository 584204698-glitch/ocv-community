"""Read declared integration capabilities. No host imports, requests or GPU use."""
import argparse
import json
from pathlib import Path

CAPABILITIES = {
    'tts_provider': 'Qwen design/clone/synthesis registration',
    'project_assets': 'Authorized project, cast and voice asset operations',
    'director_strategy': 'Custom genre and director stage registration',
    'task_lifecycle': 'Queue, revision, cancellation and recovery operations',
    'frontend_panels': 'Official frontend extension panel registration',
    'export_tracks': 'Dialogue/narration track and export operations',
}

def check(root):
    root = Path(root).expanduser().resolve(strict=True)
    if not (root / 'backend/app/main.py').is_file():
        raise ValueError('Not an OCV source installation')
    # Proposed declaration location; stock 2026.10.12.1 does not provide it.
    declaration = root / 'backend/app/plugin_api.json'
    capabilities = {}
    declared = False
    if declaration.exists():
        if declaration.is_symlink() or declaration.stat().st_size > 65536:
            raise ValueError('Invalid host integration declaration')
        data = json.loads(declaration.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('api_version') != 1:
            raise ValueError('Unsupported host integration declaration version')
        capabilities = data.get('capabilities', {})
        if not isinstance(capabilities, dict) or not all(type(value) is bool for value in capabilities.values()):
            raise ValueError('Host capability values must be explicit booleans')
        declared = True
    missing = [key for key in CAPABILITIES if capabilities.get(key) is not True]
    return {'declaration_found': declared, 'declared_capabilities': capabilities,
            'missing_interfaces': {key: CAPABILITIES[key] for key in missing},
            'status': 'requires_host_api' if missing else 'declared_only_not_runtime_verified',
            'runtime_verified': False, 'model_calls': 0,
            'message': 'The declaration is a proposed integration contract, not proof of working features.'}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ocv-root', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = check(args.ocv_root)
    except (OSError, ValueError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Missing or merely declared APIs must not be reported as usable.
    return 2

if __name__ == '__main__':
    raise SystemExit(main())
