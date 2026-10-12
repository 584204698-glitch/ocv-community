"""Cancel one receipt-owned shared task; never clear a queue or interrupt globally."""
import json
import os
import signal
import threading
import time
from pathlib import Path
import requests
from . import comfyui_guard as guard

LOCK = threading.RLock()


def targeted_support(script):
    """Require evidence of conditional interruption in this installed runtime."""
    root=Path(script).parent
    try:
        runtime=(root/'active-runtime').read_text(encoding='utf8').strip()
        profiles=json.loads((root/'runtime_profiles.json').read_text(encoding='utf8'))
        source=(Path(profiles['profiles'][runtime]['comfy_root'])/'server.py').read_text(encoding='utf8')
        section=source.split('async def post_interrupt(request):',1)[1].split('@routes.',1)[0]
        return ("json_data.get('prompt_id')" in section or 'json_data.get("prompt_id")' in section) and 'if item[1] == prompt_id:' in section and 'if should_interrupt:' in section
    except (OSError,ValueError,KeyError,IndexError):
        return False


def cancel(connection, directory, *, timeout=30):
    directory=Path(directory).resolve()
    guard._validate_target(connection)
    deadline=time.monotonic()+timeout
    with LOCK:
        receipt=guard._read_receipt(directory)
        if not receipt:raise ValueError('缺少本镜提交凭据，不能取消共享队列任务')
        prompt_id=guard._response_id(directory,receipt)
        while not prompt_id and time.monotonic()<deadline:
            if not guard._alive(receipt,require_known=True):
                if receipt['state'] in {'refused','start_failed'} or guard._refusal(directory):
                    return dict(terminal=True,status='not_submitted',message='本镜未提交，已停止后续处理。')
                break
            time.sleep(.2)
            receipt=guard._read_receipt(directory)
            prompt_id=guard._response_id(directory,receipt)
        if not prompt_id:raise ValueError('本镜任务编号尚未确认，取消未完成；保留凭据，可重试停止')
        def get(route):
            response=requests.get(guard.TARGET+route,timeout=10);response.raise_for_status();return response.json()
        def post(route,payload):
            response=requests.post(guard.TARGET+route,json=payload,timeout=10);response.raise_for_status()
        def queue_ids(queue,key):return {row[1] for row in queue[key]}
        def own():
            state=json.loads((Path(receipt['script']).parent/'gpu-comfyui-state.json').read_text(encoding='utf8'))
            if not (state.get('status')=='HELD' and state.get('owner')=='ocv' and
                    state.get('task_id')==receipt['task_id'] and state.get('pid')==receipt['pid'] and
                    state.get('prompt_id')==prompt_id and guard._alive(receipt,require_known=True)):
                raise ValueError('守锁归属不匹配，未取消其他任务')
        prior=directory/'cancel.json'
        if prior.exists():
            saved=json.loads(prior.read_text(encoding='utf8'))
            if saved.get('prompt_id')==prompt_id and saved.get('terminal'):
                return saved
        queue=get('/queue')
        pending=prompt_id in queue_ids(queue,'queue_pending')
        running=prompt_id in queue_ids(queue,'queue_running')
        if not pending and not running:
            history=get('/history/'+prompt_id).get(prompt_id,{})
            state=history.get('status',{})
            if state.get('completed') or state.get('status_str') in {'success','error'}:
                return dict(terminal=True,status='already_finished',prompt_id=prompt_id,
                            message='本镜后台任务已经结束，队列中没有该任务；原结果仍可取回。')
            raise ValueError('原任务不在队列且无终态证据，不能声明取消成功')
        own()
        if pending:
            post('/queue',{'delete':[prompt_id]})
            queue=get('/queue')
            pending=prompt_id in queue_ids(queue,'queue_pending')
            running=prompt_id in queue_ids(queue,'queue_running')
            if pending:raise ValueError('本镜仍在等待队列，取消未完成')
        if running:
            own()
            if not targeted_support(receipt['script']):
                raise ValueError('当前服务未验证按任务ID终止能力，未使用全局终止；请升级ComfyUI后重试')
            post('/interrupt',{'prompt_id':prompt_id})
        while time.monotonic()<deadline:
            queue=get('/queue')
            running=prompt_id in queue_ids(queue,'queue_running')
            pending=prompt_id in queue_ids(queue,'queue_pending')
            if not running and not pending:
                history=get('/history/'+prompt_id).get(prompt_id,{})
                state=history.get('status',{})
                terminal=state.get('completed') or state.get('status_str') in {'success','error'}
                # Deleting a queued item creates no history. Stop only its own
                # waiting guard once absence and process identity are proven.
                if not terminal and not history and guard._alive(receipt,require_known=True):
                    own()
                    os.kill(receipt['pid'],signal.SIGTERM)
                if not guard._alive(receipt,require_known=True):
                    result=dict(terminal=True,status='already_finished' if state.get('status_str')=='success' else 'cancelled',
                                prompt_id=prompt_id,message='本镜任务已结束并退出队列，GPU守锁已释放；后续镜头不再提交。')
                    guard._write_json(prior,result)
                    return result
            time.sleep(.2)
        raise ValueError('已请求取消本镜，仍在等待后台退出；尚未声明停止完成，请稍后重试停止')
