"""Reserve new core-image outputs after replanning, without bypassing retries."""
import hashlib
import json
import uuid

from . import openai_image_transport as transport


def storyboard_output(path, record, shot):
    suffix = str(shot.get('image_output_round') or record.get('image_generation_round') or '')
    return path / 'assets' / 'storyboards' / (str(shot['id']) + (f'_{suffix}' if suffix else '') + '.jpg')


def reserve_replanned_outputs(path, record):
    """Caller holds the project lock and saves reservations before starting work.

    Only untouched new shots backed by completed archived assets may rotate.
    Failed/running attempts retain their existing target and transport receipt.
    Validate the whole batch before modifying even the in-memory record.
    """
    reservations = []
    for shot in record['shots']:
        if (shot.get('image_status') not in (None, '', 'pending')
                or any(shot.get(key) for key in ('image', 'image_error', 'image_version',
                                                'image_attempt_output', 'image_output_round'))):
            continue
        output = storyboard_output(path, record, shot)
        receipt = transport.ledger_path(output)
        if not output.exists() and not receipt.exists():
            continue
        relative = output.relative_to(path).as_posix()
        archived = [old for entry in record.get('edit_history', [])
                    for old in (entry.get('shots') or [])
                    if old.get('id') == shot['id'] and old.get('image') == relative
                    and old.get('image_status') == 'completed' and old.get('image_version')]
        if receipt.exists():
            try:
                previous = json.loads(transport._read_bounded(receipt, transport.MAX_LEDGER_BYTES, 'LEDGER'))
                digest = previous.get('request_sha256') if isinstance(previous, dict) else None
            except (ValueError, OSError):
                raise ValueError(f'{shot["id"]}：原图片生成记录无法核实，未提交新请求。') from None
            # This checks receipt integrity, status and actual saved bytes. In
            # particular unknown/dispatching never gains a new output path.
            completed = transport._existing(output, receipt, digest)
            if completed is None:
                continue  # Explicit provider rejection can retry the same path.
        if not archived or not output.is_file():
            raise ValueError(f'{shot["id"]}：已有图片缺少已完成的分镜归档，不能自动创建新版本。')
        actual = hashlib.sha256(transport._read_bounded(output, transport.MAX_IMAGE_BYTES, 'SAVED_OUTPUT')).hexdigest()
        if not any(old['image_version'] == actual for old in archived):
            raise ValueError(f'{shot["id"]}：原图片与归档版本不一致，未提交新请求。')
        version = uuid.uuid4().hex[:12]
        candidate = storyboard_output(path, record, {**shot, 'image_output_round': version})
        if candidate.exists() or transport.ledger_path(candidate).exists():
            raise ValueError('新图片版本已存在，请重新检查生成记录。')
        reservations.append((shot, version))
    for shot, version in reservations:
        shot['image_output_round'] = version
    if reservations:
        record['logs'].append(f'重新规划的 {len(reservations)} 镜已分配新图片版本；原图与生成记录保留。')
