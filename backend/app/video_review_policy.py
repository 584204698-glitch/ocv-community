"""Model quality checks are optional; asset/request integrity is always required.

Only an explicit strict policy enables model verdicts as production gates.
Missing/legacy settings must preserve ordinary generation and export behavior.
Manual review endpoints are independent of this production policy.
"""


def strict_quality_review(record):
    return (record.get('creation_parameters') or {}).get('quality_review_policy') in {'strict', 'media'}


def strict_story_review(record):
    """Editorial suggestions are not media defects or permission to rewrite."""
    return (record.get('creation_parameters') or {}).get('quality_review_policy') == 'strict'
