"""Main-thread shared GPU supervision, independent of the HTTP worker lifetime."""
import argparse
import os
import time

from . import qwen3_local
from .voice_design import folder, read, save, process_identity


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner', type=int, required=True)
    parser.add_argument('--identity', required=True)
    args = parser.parse_args()
    path = folder(args.owner, args.identity)
    record = read(path)
    if record['status'] != 'generating':
        raise RuntimeError('Voice request is no longer generating')
    record['_supervisor'] = {'pid': os.getpid(), 'start_ticks': process_identity(os.getpid())}
    save(path, record)
    try:
        report = qwen3_local.run_batch(path / 'design', mode='design', texts=[record['reference_text']],
                                      description=record['description'], owner=args.owner,
                                      task_id='voice-design-' + args.identity)
        record.update(status='ready', audio=report['outputs'][0], timing=report, updated_at=time.time())
    except Exception as exc:
        record.update(status='failed', error='本次音色生成未完成，请检查计算队列或模型服务；不会自动重复提交。',
                      updated_at=time.time())
        print(type(exc).__name__ + ': ' + str(exc), flush=True)
    save(path, record)


if __name__ == '__main__':
    main()
