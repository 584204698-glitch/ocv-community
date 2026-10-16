"""Verify caller-owned story annotations without models, storage or genre rules.

Returns auditable spans and explicit counts, never an artistic quality score.
Host editors supply their own annotations; this does not invent story beats.
"""
import hashlib


def verify(text, nodes, *, links=(), cycles=(), expected_sha256=None):
    """Validate exact quotes, unique IDs and temporal links against frozen text.

nodes: [{id, function, quote}], with quote occurring exactly once.
links: [{from, to, why}], ordered forward in the text.
cycles: [{setup, action, payoff}], referencing three ordered node IDs.
Offsets are Python Unicode codepoint offsets, not milliseconds or shot counts.
"""
    if not isinstance(text, str) or not text:
        raise ValueError('STORY_EVIDENCE_TEXT_INVALID')
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('STORY_EVIDENCE_TEXT_CHANGED')
    if not isinstance(nodes, (list, tuple)) or not isinstance(links, (list, tuple)) or not isinstance(cycles, (list, tuple)):
        raise ValueError('STORY_EVIDENCE_ROWS_INVALID')
    lookup, checked = {}, []
    for row in nodes:
        if not isinstance(row, dict) or set(row) != {'id', 'function', 'quote'}:
            raise ValueError('STORY_EVIDENCE_NODE_INVALID')
        if any(not isinstance(row[key], str) or not row[key].strip() for key in row):
            raise ValueError('STORY_EVIDENCE_NODE_INVALID')
        if row['id'] in lookup:
            raise ValueError('STORY_EVIDENCE_ID_DUPLICATE')
        if text.count(row['quote']) != 1:
            raise ValueError('STORY_EVIDENCE_QUOTE_NOT_UNIQUE')
        start = text.index(row['quote'])
        node = {**row, 'start_character':start, 'end_character':start+len(row['quote'])}
        lookup[row['id']] = node
        checked.append(node)

    def positions(row, keys, code):
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or row[k] not in lookup for k in keys):
            raise ValueError(code)
        values = [lookup[row[k]]['start_character'] for k in keys]
        if any(right <= left for left, right in zip(values, values[1:])):
            raise ValueError(code)

    for row in links:
        if (not isinstance(row, dict) or set(row) != {'from', 'to', 'why'}
                or not isinstance(row['why'], str) or not row['why'].strip()):
            raise ValueError('STORY_EVIDENCE_LINK_INVALID')
        positions(row, ('from', 'to'), 'STORY_EVIDENCE_LINK_INVALID')
    for row in cycles:
        if not isinstance(row, dict) or set(row) != {'setup', 'action', 'payoff'}:
            raise ValueError('STORY_EVIDENCE_CYCLE_INVALID')
        positions(row, ('setup', 'action', 'payoff'), 'STORY_EVIDENCE_CYCLE_INVALID')
    functions = {}
    for node in checked:
        functions[node['function']] = functions.get(node['function'], 0)+1
    return {'text_sha256':digest, 'nodes':checked, 'links':[dict(row) for row in links],
            'cycles':[dict(row) for row in cycles],
            'counts':{'nodes':len(checked), 'links':len(links), 'cycles':len(cycles), 'functions':functions},
            'offset_unit':'unicode_codepoints',
            'measurement_limit':'Quotes and counts verified; functions and causal meaning are caller annotations. No duration, retention or artistic quality certification.'}
