"""One local retake of a completed, speech-mismatched native clip.

ASR is fallible: neither detection nor a later match certifies voice/lips.
This never retries missing ASR, empty recognition or an uncertain GPU receipt.
The normal generation worker keeps the original clip/request in its history.
"""
import copy
import hashlib
from fastapi import HTTPException


def retake(identity, revision, user, failure):
    from . import video_studio as studio, video_generation as generation, one_click
    from .video_export import _source

    report = failure.report
    review = report.get('review') or {}
    if (not review.get('recognized', '').strip() or review.get('text_matches') is not False
            or [r.get('code') for r in review.get('issues', [])] != ['speech_mismatch']):
        raise failure
    with studio.LOCK:
        path = studio.directory(user['id'], identity)
        record = studio.read(path)
        studio.editable(record, revision)
        if (not record.get('native_story') or record.get('native_owner_id') != int(user['id'])
                or record.get('creation_parameters', {}).get('video_generation_backend') != 'comfyui'):
            raise failure
        if one_click._executing.get():
            state = one_click.load(user['id'], identity)
            if not state or state['state'] != 'running':return record
        if (record['status'] not in {'video_generation_ready', 'video_review'}
                or str(path) in studio.ACTIVE or studio.project_has_image_edits(identity)):
            raise HTTPException(409, '当前阶段仍在处理，未另发对白补拍')
        shot = studio._find_shot(record, failure.shot_id)
        if (shot.get('video_status') != 'completed' or not shot.get('video_task_id')
                or shot.get('native_speech_repairs')):
            raise failure
        evidence = report.get('evidence') or {}
        frozen = (shot.get('video_request') or {}).get('dialogue', {}).get('plan')
        if (not frozen or frozen != evidence.get('plan')
                or hashlib.sha256(_source(path, shot).read_bytes()).hexdigest() != evidence.get('clip_sha256')):
            raise ValueError('对白检查与当前原片不一致，未另发补拍')
        # Durable attempt marker is written before dispatch. A restart or failed
        # dispatch cannot silently create a retry loop. Manual retry stays usable.
        shot['native_speech_repairs'] = [dict(previous_task=shot['video_task_id'],
            review_fingerprint=report.get('fingerprint'), clip_sha256=evidence['clip_sha256'],
            recognized=review['recognized'], expected=copy.deepcopy(review.get('expected', [])),
            strategy='same-approved-inputs-new-seed', quality_accepted=False)]
        record['revision'] += 1
        record['logs'].append(f'{shot["id"]}：原声识别与台词不符，自动补拍一次；原片保留，仍需重新检查。')
        studio.save(path, record)
        return generation.generate_for_user(identity, generation.GenerateClips(
            revision=record['revision'], shot_ids=[shot['id']], regenerate_completed=True), user)
