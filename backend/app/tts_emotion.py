"""Conservative per-line IndexTTS controls; no additional model inference."""
from __future__ import annotations

import math
import re
from typing import Literal

Emotion = Literal['auto', 'neutral', 'happy', 'angry', 'sad', 'afraid',
                  'disgusted', 'melancholic', 'surprised', 'calm']
DEFAULT_STRENGTH = .35
EMOTIONS = ('happy', 'angry', 'sad', 'afraid', 'disgusted', 'melancholic',
            'surprised', 'calm')

# Only legacy delivery instructions are considered, never dialogue content or
# character age. New planners supply the structured field in the same request.
CUES = {
    'happy': r'\b(?:happy|happily|joyful|joyfully|cheerful|cheerfully)\b',
    'angry': r'\b(?:angry|angrily|furious|furiously)\b',
    'sad': r'\b(?:sad|sadly|sorrowful|sorrowfully)\b',
    'afraid': r'\b(?:afraid|fearful|fearfully|scared|frightened)\b',
    'disgusted': r'\b(?:disgusted|disgust|revulsion)\b',
    'melancholic': r'\b(?:melancholic|melancholy|wistful|wistfully)\b',
    'surprised': r'\b(?:surprised|surprise|astonished|astonishment)\b',
    'calm': r'\b(?:calm|calmly|serene|serenely)\b',
}


def resolve(turn):
    """Return actual runner parameters, respecting manual neutral and zero."""
    emotion = turn.get('emotion', 'auto')
    if emotion not in ('auto', 'neutral', *EMOTIONS):
        raise ValueError('不支持的台词情绪')
    strength = float(turn.get('emotion_strength', DEFAULT_STRENGTH))
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('情绪强度必须在 0 到 1 之间')
    source = 'manual' if emotion != 'auto' else 'delivery'
    if emotion == 'auto':
        delivery = str(turn.get('delivery', '')).lower()
        # Negated or mixed cues are ambiguous; retain the adopted natural voice.
        if re.search(r'\b(?:not|no|without|avoid|never)\b', delivery):
            matches = []
        else:
            matches = [name for name, pattern in CUES.items() if re.search(pattern, delivery)]
        emotion = matches[0] if len(matches) == 1 else 'neutral'
    if emotion == 'neutral' or strength == 0:
        return {'emotion': 'neutral', 'strength': 0.0, 'source': source}
    return {'emotion': emotion, 'strength': strength, 'source': source}


def runner_arguments(control):
    """Neutral deliberately leaves the reference voice's expression intact."""
    if control['emotion'] == 'neutral' or control['strength'] == 0:
        return []
    return ['--tts-emotion', control['emotion'],
            '--tts-emotion-weight', format(control['strength'], '.6g')]
