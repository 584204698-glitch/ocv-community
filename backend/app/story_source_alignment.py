"""Restore only unambiguous dropped repetition in source citations, never dialogue."""
from difflib import SequenceMatcher


def restore_repeated_source(candidate, original):
    rows = candidate.get('shots') if isinstance(candidate, dict) else None
    if not rows or any(not isinstance(r, dict) or not isinstance(r.get('source_text'), str) for r in rows):
        return []
    compact = lambda s: ''.join(s.split())
    source = compact(original)
    parts = [compact(r['source_text']) for r in rows]
    copied = ''.join(parts)
    if copied == source:
        return []
    edits = [op for op in SequenceMatcher(None, copied, source, autojunk=False).get_opcodes() if op[0] != 'equal']
    if len(edits) != 1:
        return []
    tag, a, b, c, d = edits[0]
    missing = source[c:d]
    if tag != 'insert' or len(missing) != 1 or a != b:
        return []
    # A one-character omission within a repeated two-character phrase, e.g.
    # ABAB -> ABA. Limit to a citation inside one shot, never move a boundary.
    if c < 3 or source[c-3:c-1] != source[c-1:c+1]:
        return []
    offset = 0
    for i, part in enumerate(parts):
        if offset < a < offset + len(part):
            local = a-offset
            text = rows[i]['source_text']
            positions = [n for n, ch in enumerate(text) if not ch.isspace()]
            pos = positions[local]
            restored = text[:pos] + missing + text[pos:]
            if compact(''.join(r['source_text'] if n != i else restored for n, r in enumerate(rows))) != source:
                return []
            rows[i]['source_text'] = restored
            return [{'shot': i+1, 'before': text, 'after': restored, 'reason': '恢复原文引用中漏抄的重复字'}]
        offset += len(part)
    return []
