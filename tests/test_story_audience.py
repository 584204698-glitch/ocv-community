import copy
import json
import unittest

from backend.app import story_development as d
from backend.app.story_writer import canonical_story_brief
from tests.test_story_development import QueueCall, draft, plan, review, audience_response

STORY='小林在门边等朋友。他把约好的红伞举高，朋友却一直看向低处。原来朋友家的小狗正叼着同样的红伞，绕着他的鞋尖转圈。'

def response():
    return dict(checks=[dict(key=key, decision='keep', quote=quote, reason='与本次事件相关的具体判断。', suggestion='')
                       for key,quote in zip(('interest','naturalness','payoff'),
                                           ('小林在门边等朋友','他把约好的红伞举高','绕着他的鞋尖转圈'))], issues=[])

def evaluate(value, events=None):
    return d.review_story(canonical_story_brief('红伞'),STORY,
                          call=lambda **kwargs:json.dumps(value,ensure_ascii=False),
                          progress=(lambda stage,detail:events.append((stage,detail))) if events is not None else None)

class AudienceGateTest(unittest.TestCase):
    def test_core_failure_rebuilds_unadopted_draft_once_and_refreshes_metadata(self):
        initial=draft();raw=audience_response(review(),initial['story'])
        raw['checks'][0].update(decision='rework',suggestion='重新安排人物反应与结果。')
        changed=draft(STORY);call=QueueCall(initial,raw,changed,review())
        events=[]
        result=d.develop_story(canonical_story_brief('保留人物的选择'),plan=plan(),call=call,
                               progress=lambda stage,detail:events.append((stage,detail)))
        self.assertEqual(result['draft']['story'],STORY)
        self.assertEqual(result['draft']['derived_status'],'current')
        self.assertEqual(result['draft']['story_sha256'],result['draft']['derived_from_story_sha256'])
        self.assertEqual(result['revision_rounds'],1)
        saved=next(detail for stage,detail in events if stage=='writing' and detail['event']=='completed')
        self.assertEqual(saved['draft']['story'],initial['story'])
        self.assertNotIn('issues',call.payload(3))

    def test_repeated_core_failure_stops_instead_of_endless_rewrite(self):
        initial=draft();raw=audience_response(review(),initial['story'])
        raw['checks'][0].update(decision='rework',suggestion='重新组织事件。')
        call=QueueCall(initial,raw,initial,raw)
        result=d.develop_story(canonical_story_brief('红伞'),plan=plan(),call=call)
        self.assertEqual(result['editorial_review'],'needs_revision')
        self.assertEqual(result['revision_rounds'],1)
        self.assertEqual(len(call.requests),4)

    def test_each_rejected_axis_blocks_ready_without_model_overall_verdict(self):
        for index in range(3):
            value=response();value['checks'][index].update(decision='rework',suggestion='调整导致问题的事件。')
            with self.subTest(index=index):
                result=evaluate(value)
                self.assertEqual(result['verdict'],'needs_revision')
                self.assertEqual(result['issues'][0]['severity'],'major')

    def test_unsupported_ready_and_missing_or_duplicate_checks_fail_closed(self):
        missing=response();missing['checks'].pop()
        duplicate=response();duplicate['checks'][1]=copy.deepcopy(duplicate['checks'][0])
        forged=response();forged['checks'][0]['quote']='正文没有的漂亮情节'
        unsupported=response();unsupported['verdict']='ready'
        for value in (dict(verdict='ready',issues=[]),missing,duplicate,forged,unsupported):
            with self.subTest(value=value),self.assertRaises(ValueError):evaluate(value)

    def test_positive_evidence_is_persistable_without_changing_public_review_contract(self):
        events=[]
        self.assertEqual(evaluate(response(),events),dict(verdict='ready',issues=[]))
        completed=events[-1][1]
        self.assertEqual(len(completed['audience_checks']),3)
        self.assertIn('story_sha256',completed)

    def test_hard_constraint_blocker_cannot_be_overridden_by_three_positive_checks(self):
        value=response();value['issues']=[dict(severity='major',dimension='创作要求',quote='红伞',reason='与用户明确要求冲突。',suggestion='保留用户指定物件。')]
        self.assertEqual(evaluate(value)['verdict'],'needs_revision')

    def test_all_fields_and_untrimmed_issues_are_validated(self):
        for field,value in (('suggestion',''),('reason',''),('quote',''),('decision','excellent')):
            bad=response();bad['checks'][0].update(decision='rework',suggestion='需要改事件。');bad['checks'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):evaluate(bad)
        bad=response();bad['issues']=[dict(severity='minor',dimension='表达',quote='红伞',reason='重复',suggestion='精简')]*4
        with self.assertRaises(ValueError):evaluate(bad)

    def test_only_optional_positive_edit_may_be_omitted(self):
        value=response();del value['checks'][0]['suggestion']
        self.assertEqual(evaluate(value)['verdict'],'ready')
        value['checks'][0]['decision']='rework'
        with self.assertRaises(ValueError):evaluate(value)

if __name__=='__main__':unittest.main()
