"""Evidence-bound audience checks. This validates contracts, not artistic truth."""
from __future__ import annotations

CHECKS = {'interest': '观看兴趣', 'naturalness': '人物行为', 'payoff': '结尾兑现'}

AUDIENCE_REVIEW_PROMPT = '''你是短片的试读编辑，不是作者的辩护人。你只看到创作要求和正文，看不到作者的选稿理由。
先作为目标观众读完，再做三项独立判断。不要先决定“合格”再凑理由，不输出分数或预测流量。

interest（观看兴趣）：指出读者具体想体验的细节、人物反应、幽默、感官变化或发现。用一句任何故事都能套的“想知道能否成功”不足以通过。已经猜到结果不必然无趣，过程的表现也可以吸引人。
naturalness（人物行为）：以这个题材建立的规则和人物性格判断行动。不要求童话遵守现实物理，也不要求人物总选择最有效办法；但若不做一个显然更自然的动作，只为给作者凑情节，且正文没提供性格或处境依据，应返回 rework。也检查关键动作前后的人物、物件与状态是否接得上；区分普通叙事省略和必须靠读者另编一段才能成立的缺口。
payoff（结尾兑现）：指出结果具体增加了什么体验：一个好笑的反应、关系变化、发现、情绪或前文细节的新意义。不要把任务做完、解释物件位置、核对数量或补一句感悟自动当满足。安静收尾和开放结尾可以通过，不强制反转。

尺度是“一键生成后可用的故事”，不是文学精品或爆款。完整自然、过程有一定趣味或情绪发展就可以保留。绘本的游戏感、想象、节奏性重复、交谈和小发现都可以成立；不因温柔、平静、可预期而否定，也不因有愿望、动作、收尾就自动通过。
评价整篇带来的体验，不孤立夸一句比喻或一个末尾小动作来替平淡的主体辩护。理由要与正文相称：普通反应不应被拔高成强烈情绪；若删去辞藻只剩一件琐事反复操作，指出过程缺少什么发展。不要自行增加用户没给出的宏大主题或硬性创作限制，也不要把制作方便、人物少、场景少当作好故事的依据。

三项中任一项明显不足就填 rework；不能用另一项的优点抵消。可以通过的作品不必完美，普通省略和不影响核心体验的措辞只放 minor issues。不要为展示严格而虚构问题。
每项必须引用正文中连续出现的非空原句并具体解释；不能把相隔几段的句子拼成一个 quote，也不能补字或省略中间文字。rework 的 suggestion 说明需要调整的事件或反应，不只要求换形容词、补一句“因为”或加一个时间限制。无关紧要的问题不要变成大改要求。
另外最多三项 issues 检查硬性要求、事实、角色造型、指代和时序；逐字引用正文，不依据未提供的资产编造问题。已有用户文稿只给建议，不擅自改动。

只输出严格JSON，恰好含 checks、issues，不输出整体 verdict。
checks 恰好三项，每项恰好含 key（interest/naturalness/payoff，各一次）、decision（keep/rework）、quote（逐字正文，最多2000字）、reason（具体依据，最多1600字）、suggestion（最多1600字；keep 时空字符串）。
issues 每项恰好含 severity（major/minor）、dimension（最多120字）、quote（逐字正文，最多2000字）、reason、suggestion（各最多1600字）；无额外问题时为[]。
'''


def parse_audience_review(value, story, *, string, exact, validate_review):
    code = 'STORY_AUDIENCE_REVIEW_INVALID'
    exact(value, {'checks', 'issues'}, code)
    rows = value['checks']
    if not isinstance(rows, list) or len(rows) != 3:
        raise ValueError(code)
    checks, seen, blockers = [], set(), []
    for row in rows:
        # A positive check needs no edit. Some providers omit its empty field;
        # accept only this omission, never fill evidence or a rejected remedy.
        if isinstance(row, dict) and row.get('decision') == 'keep' and 'suggestion' not in row:
            row = {**row, 'suggestion': ''}
        exact(row, {'key', 'decision', 'quote', 'reason', 'suggestion'}, code)
        key = string(row['key'], code, 30)
        decision = string(row['decision'], code, 20)
        if key not in CHECKS or key in seen or decision not in {'keep', 'rework'}:
            raise ValueError(code)
        seen.add(key)
        quote = string(row['quote'], code, 2000, preserve=True)
        if quote not in story:
            raise ValueError('STORY_REVIEW_QUOTE_NOT_FOUND')
        reason = string(row['reason'], code, 1600)
        suggestion = string(row['suggestion'], code, 1600, empty=decision == 'keep')
        checks.append(dict(key=key, decision=decision, quote=quote, reason=reason, suggestion=suggestion))
        if decision == 'rework':
            blockers.append(dict(severity='major', dimension=CHECKS[key], quote=quote,
                                 reason=reason, suggestion=suggestion))
    issues = value['issues']
    if not isinstance(issues, list):
        raise ValueError(code)
    # Validate every model issue before trimming, including any hidden fourth item.
    existing = validate_review({'verdict': 'needs_revision' if any(
        isinstance(i, dict) and i.get('severity') == 'major' for i in issues) else 'ready', 'issues': issues}, story)
    combined = [i for i in existing['issues'] if i['severity'] == 'major'] + blockers
    combined += [i for i in existing['issues'] if i['severity'] == 'minor']
    verdict = 'needs_revision' if blockers or existing['verdict'] == 'needs_revision' else 'ready'
    return validate_review({'verdict': verdict, 'issues': combined[:3]}, story), checks
