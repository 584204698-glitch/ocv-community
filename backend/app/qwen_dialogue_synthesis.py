"""Supervise the shared TTS guard on a process main thread, like IndexTTS."""
import argparse
from pathlib import Path
import uuid
from .qwen3_local import run_batch
from .video_external_dialogue import qwen_reference


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--owner',type=int,required=True)
    parser.add_argument('--voice-id',required=True)
    parser.add_argument('--voice-sha',required=True)
    parser.add_argument('--text-file',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    reference,text=qwen_reference(args.owner,dict(voice_id=args.voice_id,sha256=args.voice_sha))
    run_batch(Path(args.output),mode='clone',texts=[Path(args.text_file).read_text(encoding='utf8')],
              owner=args.owner,task_id='dialogue_'+uuid.uuid4().hex,
              reference=reference,reference_text=text)


if __name__=='__main__':main()
