import copy
import unittest
from backend.app.video_core_reference_plan import plan_core_reference_groups, reference_signature


class CoreReferencePlanTests(unittest.TestCase):
    def setUp(self):
        shot=dict(id='one',scene_reference_id='room',reference_ids=['rabbit','fox'],
            cast_plan=dict(characters=[dict(project_character_id=role,identity_sha256=role+'-v1',presence='opening')
                                      for role in ('rabbit','fox')]),
            continuity_props=[dict(id='prop_lamp',presence='opening',appearance='蓝色台灯',appearance_en='A blue lamp.')],
            continuity_states=[dict(prop_id='prop_lamp',visibility='opening',
                opening=dict(id='table',description='在桌面中央',description_en='At the center of the table.'))])
        self.record=dict(id='project',native_story={'text':'Story'},
            creation_parameters={'dynamic_text_mode':'visual_first'},shots=[copy.deepcopy(shot) for _ in range(3)])
        for index,s in enumerate(self.record['shots']):s['id']=str(index)

    def test_plan_reuses_earliest_compatible_source_without_mutation_or_file_decisions(self):
        before=copy.deepcopy(self.record)
        self.record['shots'][1]['cast_plan']['characters'].reverse()
        self.record['shots'][1]['reference_ids'].reverse()
        changed=copy.deepcopy(self.record)
        plan=plan_core_reference_groups(self.record)
        self.assertEqual([v['source_shot_id'] for v in plan],[None,'0','0'])
        self.assertEqual([v['mode'] for v in plan],['opening']*3)
        self.assertEqual(changed,self.record)
        self.assertEqual(plan[0]['signature'],reference_signature(before,before['shots'][0]))

    def test_identity_scene_state_and_visibility_changes_never_reuse_old_opening(self):
        for field,value in [('scene_reference_id','other')]:
            r=copy.deepcopy(self.record);r['shots'][1][field]=value
            self.assertIsNone(plan_core_reference_groups(r)[1]['source_shot_id'])
        for change in ('cast','state','appearance','visibility','presence','reference'):
            r=copy.deepcopy(self.record);s=r['shots'][1]
            if change=='cast':s['cast_plan']['characters'][0]['identity_sha256']='new'
            if change=='state':s['continuity_states'][0]['opening']['description']='已掉到地上'
            if change=='appearance':s['continuity_props'][0]['appearance']='红色台灯'
            if change=='visibility':s['continuity_states'][0]['visibility']='later'
            if change=='presence':s['cast_plan']['characters'][0]['presence']='later'
            if change=='reference':s['reference_ids'].append('additional')
            with self.subTest(change=change):
                edge=plan_core_reference_groups(r)[1]
                if change in {'cast','state','presence','reference'}:
                    self.assertEqual(edge['source_shot_id'],'0')
                    self.assertEqual(edge['mode'],'appearance_only')
                else:
                    self.assertIsNone(edge['source_shot_id'])

    def test_ending_actions_camera_and_voice_are_not_old_opening_state(self):
        s=self.record['shots'][1]
        s.update(native_dialogue={'scene':'Close view','turns':['new words']},voice_id='other')
        s['continuity_states'][0]['ending']={'id':'floor'}
        self.assertEqual(plan_core_reference_groups(self.record)[1]['source_shot_id'],'0')

    def test_legacy_and_disabled_scenes_do_not_gain_work_or_reference_requirements(self):
        r=copy.deepcopy(self.record);r.pop('native_story')
        self.assertEqual(plan_core_reference_groups(r),[])
        r=copy.deepcopy(self.record);r['creation_parameters']['scene_references_enabled']=False
        self.assertEqual(plan_core_reference_groups(r),[])
        self.record['shots'][1].pop('continuity_states')
        self.assertIsNone(plan_core_reference_groups(self.record)[1]['signature'])

    def test_three_characters_keep_identity_inputs_and_signature_cannot_cross_projects(self):
        for shot in self.record['shots']:
            shot['cast_plan']['characters'].append(dict(project_character_id='owl',identity_sha256='owl-v1',presence='opening'))
            shot['reference_ids'].append('owl')
        plan=plan_core_reference_groups(self.record)
        self.assertEqual(plan[1]['source_shot_id'],'0')
        self.assertEqual(len(self.record['shots'][1]['reference_ids']),3)
        other=copy.deepcopy(self.record);other['id']='another'
        self.assertNotEqual(plan[0]['signature'],plan_core_reference_groups(other)[0]['signature'])

    def test_duplicate_ids_and_incomplete_versioned_state_fail_before_any_dispatch(self):
        r=copy.deepcopy(self.record);r['shots'][1]['id']='0'
        with self.assertRaises(ValueError):plan_core_reference_groups(r)
        r=copy.deepcopy(self.record);r['shots'][1]['continuity_states'][0]['opening'].pop('description')
        with self.assertRaises(ValueError):plan_core_reference_groups(r)

    def test_repaired_prop_and_changed_cast_use_earliest_appearance_without_mutation(self):
        for shot in self.record['shots'][1:]:
            shot['cast_plan']['characters']=shot['cast_plan']['characters'][:1]
            shot['continuity_states'][0]['opening']=dict(id='repaired',description='修补后仍未亮',
                description_en='The same unlit lamp with the approved repair.')
            shot['action']='Repair the lamp without changing its shape or color.'
            shot['native_dialogue']={'turns':['Keep our original words.']}
        before=copy.deepcopy(self.record)
        plan=plan_core_reference_groups(self.record)
        self.assertEqual([row['source_shot_id'] for row in plan],[None,'0','0'])
        self.assertEqual([row['mode'] for row in plan],['opening','appearance_only','appearance_only'])
        self.assertEqual(before,self.record)

    def test_appearance_requires_full_visible_coverage_and_exact_ids_and_bilingual_design(self):
        for change in ('id','english','later','missing_state','extra'):
            r=copy.deepcopy(self.record);shot=r['shots'][1]
            shot['cast_plan']['characters']=shot['cast_plan']['characters'][:1]
            if change=='id':
                shot['continuity_props'][0]['id']='prop_other'
                shot['continuity_states'][0]['prop_id']='prop_other'
            if change=='english':shot['continuity_props'][0]['appearance_en']='A patterned lamp.'
            if change=='later':shot['continuity_props'][0]['presence']='later'
            if change=='missing_state':shot.pop('continuity_states')
            if change=='extra':
                prop=copy.deepcopy(shot['continuity_props'][0]);prop['id']='prop_book'
                state=copy.deepcopy(shot['continuity_states'][0]);state['prop_id']='prop_book'
                shot['continuity_props'].append(prop);shot['continuity_states'].append(state)
            with self.subTest(change=change):
                self.assertIsNone(plan_core_reference_groups(r)[1]['source_shot_id'])

    def test_an_inherited_image_is_never_planned_as_a_new_source(self):
        self.record['shots'][0]['core_reference_used']={'source_shot_id':'old'}
        plan=plan_core_reference_groups(self.record)
        self.assertEqual([row['source_shot_id'] for row in plan],[None,None,'1'])
