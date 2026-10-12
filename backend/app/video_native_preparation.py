"""One preparation path for early opening checks and actual H3 submission."""
import copy


def prepare_native(owner, path, record, shot, selection, images, *, audio=False):
    from .video_dialogue import prepare_dialogue
    from .video_native_staging import assert_staging_current, complete_performance
    from .video_native_grounding import ground_performance, reuse_asset_only_grounding
    assert_staging_current(shot)
    compiled=prepare_dialogue(owner,path,record,shot,selection,images)
    if shot.get('manual_h3_prompt'):
        from .video_manual_h3 import adopted
        if not compiled.get('external_dialogue'):
            raise ValueError('手动稿需要已有配音音轨，请先完成配音准备')
        compiled['prompt']=adopted(shot,compiled)
        return compiled,'user-manual-h3-v1'
    if ((shot.get('video_prompt_input_basis') or {}).get('source')=='asset_replacement'
            and not compiled['plan'].get('performance_beats')):
        raise ValueError('本镜尚无完整动作时间线，请主动更新提示词后继续')
    timed=complete_performance({**shot,'native_dialogue':compiled['plan']})
    shot['native_dialogue']=copy.deepcopy(compiled['plan'])
    shot['native_dialogue'].update(scene=timed['scene'],performance_beats=timed['performance_beats'])
    grounded,receipt=(reuse_asset_only_grounding(path,shot,images[0]['path'])
                      or ground_performance(path,shot,images[0]['path']))
    shot['native_dialogue']=grounded
    shot['native_visual_grounding']=receipt
    result=prepare_dialogue(owner,path,record,shot,selection,images,prepare_narration=audio)
    return result,'official-ref2va-native-dialogue-v1'
