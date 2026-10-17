"""Recovery metadata for a completed or ambiguous review request; no replay."""
import hashlib
import json


def retryable(receipt):
    if receipt.get('status') != 'failed':
        return False
    failure = receipt.get('failure') or {}
    return ('response' in receipt
            or failure.get('kind') in {'invalid_contract', 'output_truncated'}
            or (failure.get('kind') == 'provider_http_error'
                and type(failure.get('http_status')) is int
                and 400 <= failure['http_status'] < 500
                and failure['http_status'] not in {408, 409}))


def retry_token(receipt):
    # Bind an explicit retry to this exact failed attempt, not merely its pixels.
    return hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def recovery(receipt):
    allowed = retryable(receipt)
    return dict(retry_available=allowed,
                retry_token=retry_token(receipt) if allowed else None,
                request_token=retry_token(receipt),
                outcome='known_failed' if allowed else 'unknown',
                message=('上次复核未得到有效结论，图片不代表有缺陷。可明确重试一次复核，不购买修图。'
                         if allowed else '上次复核结果尚未确认，请先核对原请求；如决定另发，须明确接受可能重复计费。不会自动调用或要求改图。'))
