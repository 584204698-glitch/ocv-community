"""Persist independent early opening results; no GPU or image generation."""
import copy


def check_shots(owner,path,record,*,shot_id=None):
    from .video_inputs import validate_shot_inputs
    from .video_native_preparation import prepare_native
    from .video_opening_repair import capture
    rows=[]
    for shot in record['shots']:
        if shot_id and shot['id']!=shot_id:continue
        if shot.get('kind')!='video' or not shot.get('native_dialogue'):continue
        if shot.get('video_status')=='completed':
            rows.append(dict(shot_id=shot['id'],status='completed',message='已有动态片段，保留'))
            continue
        candidate=copy.deepcopy(shot)
        try:
            selection,images,_=validate_shot_inputs(owner,path,record,candidate)
            prepare_native(owner,path,record,candidate,selection,images,audio=False)
        except ValueError as exc:
            message=str(exc)
            diagnostic={**shot,'video_error':message}
            capture(path,diagnostic)
            if diagnostic.get('opening_image_repair'):
                shot['opening_image_repair']=diagnostic['opening_image_repair']
            row=dict(shot_id=shot['id'],status='blocked',message=message)
        except Exception as exc:
            row=dict(shot_id=shot['id'],status='unavailable',message='检查服务未完成：'+str(exc)[:1200])
        else:
            for key in ('native_dialogue','native_visual_grounding'):
                if key in candidate:shot[key]=copy.deepcopy(candidate[key])
            row=dict(shot_id=shot['id'],status='ready',message='已按视频提交使用的同一开场规则准备；未生成视频')
        shot['opening_preflight']=row
        rows.append(row)
    return rows
