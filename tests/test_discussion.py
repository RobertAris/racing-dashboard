"""Discussion proposals remain grounded drafts; patches are validated in memory."""
import copy
import difflib
import json
from pathlib import Path
import tempfile
import unittest

from racing_dashboard.discussion import (DISCUSSION_SCHEMA, validate_discussion_response,
                                        validate_unified_diff)

COMMIT='a'*40
SOURCE='MAX_SPEED = 3.0\n\ndef speed_cap():\n    return MAX_SPEED\n'
PATH='mpc/scripts/mpc_node.py'


def context():
    return {'lap':{'id':'lap-1'},'metrics':{'evidence':[{'id':'sector-2'}]},
            'source_context':{'commit':COMMIT,'files':[{'path':PATH,'content':SOURCE}]},
            'report':{},'confirmations':[],'messages':[],'comparison':None}


def turn():
    return {'message':'Confirm the active cap before choosing a tuning experiment.',
            'questions':[{'id':'q1','key':'MAX_SPEED','question':'What MAX_SPEED ran on this lap?',
                          'suggested_answer':'3','reason':'Launch defaults do not prove deployed settings.'}],
            'proposals':[],'limitations':['Solver status was not recorded.']}


def proposal(kind='measurement'):
    return {'id':'p1','kind':kind,'title':'Record the missing diagnostic',
            'rationale':'This can distinguish causes of the observed slowdown.',
            'evidence_ids':['sector-2'],'confirmation_keys':[],
            'parameter':'','current_value':'','proposed_value':'','unit':'',
            'source_file':'','source_commit':'','diff':'',
            'validation':'Compare the same sector under comparable conditions.',
            'rollback':'Stop extra capture after reviewing the recorded intervals.'}


def parameter_turn():
    value=turn();value['questions']=[]
    value['proposals']=[{**proposal('parameter'),'parameter':'MAX_SPEED','current_value':'3.0',
                         'proposed_value':'3.2','unit':'m/s','confirmation_keys':['MAX_SPEED']}]
    return value


def patch(old=SOURCE,new=None,path=PATH):
    if new is None:new=old.replace('MAX_SPEED = 3.0','MAX_SPEED = 2.8')
    return ''.join(difflib.unified_diff(old.splitlines(keepends=True),new.splitlines(keepends=True),
                                      fromfile='a/'+path,tofile='b/'+path))


def code_turn():
    value=turn();value['questions']=[]
    value['proposals']=[{**proposal('code'),'source_file':PATH,'source_commit':COMMIT,'diff':patch(),
                         'confirmation_keys':['deployed_source_commit']}]
    return value


def confirm(value,key,answer,**extra):
    value['confirmations'].append({'key':key,'value':answer,'origin':'user_confirmed',**extra})
    return value


class DiscussionTests(unittest.TestCase):
    def test_questions_and_measurement_do_not_require_invented_runtime_configuration(self):
        value=turn();value['proposals']=[proposal()]
        checked=validate_discussion_response(value,context())
        self.assertEqual(checked,value)
        checked['questions'][0]['question']='Changed'
        self.assertNotEqual(checked['questions'],value['questions'])
        self.assertEqual(set(DISCUSSION_SCHEMA['properties']),{'message','questions','proposals','limitations'})

    def test_parameter_requires_exact_user_owned_confirmation(self):
        value=parameter_turn()
        for facts in ([],[{'key':'MAX_SPEED','value':'3','origin':'assistant'}],
                      [{'key':'MAX_SPEED','value':'3','origin':'source_default'}],
                      [{'key':'MAX_SPEED','value':'3','origin':'user_confirmed','lap_id':'later-lap'}],
                      [{'key':'MAX_SPEED','value':'4','origin':'user_confirmed'}]):
            supplied=context();supplied['confirmations']=facts
            supplied['messages']=[{'role':'user','content':'The cap is probably 3'},
                                  {'role':'assistant','response':turn()}]
            with self.subTest(facts=facts),self.assertRaises(ValueError):
                validate_discussion_response(value,supplied)
        supplied=confirm(context(),'MAX_SPEED',3,lap_id='lap-1')
        self.assertEqual(validate_discussion_response(value,supplied),value)

    def test_parameter_ranges_finiteness_names_units_and_changed_values(self):
        supplied=confirm(context(),'MAX_SPEED','3')
        invalid=[{'proposed_value':'NaN'},{'proposed_value':'Infinity'},{'proposed_value':'1e999'},
                 {'proposed_value':'15.1'},{'proposed_value':'.09'},{'proposed_value':'3'},
                 {'proposed_value':'3 m/s'},{'proposed_value':'0x10'},{'unit':'mph'},
                 {'parameter':'TK'},{'current_value':'2'}, {'confirmation_keys':[]},
                 {'diff':'shell command'}, {'source_file':PATH}]
        for changes in invalid:
            value=parameter_turn();value['proposals'][0].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate_discussion_response(value,supplied)
        for parameter in ('max_accel','profile_accel','profile_decel'):
            supplied=confirm(context(),parameter,2)
            related=['profile_accel','profile_decel'] if parameter=='max_accel' else ['max_accel']
            for key in related:confirm(supplied,key,2)
            value=parameter_turn();value['proposals'][0].update(parameter=parameter,current_value='2',
                proposed_value='2.2' if parameter=='max_accel' else '1.8',unit='m/s²',confirmation_keys=[parameter,*related])
            self.assertEqual(validate_discussion_response(value,supplied),value)
            value['proposals'][0]['proposed_value']='3.1'
            with self.assertRaises(ValueError):validate_discussion_response(value,supplied)

    def test_parameter_respects_other_confirmed_acceleration_constraints(self):
        supplied=confirm(confirm(context(),'profile_accel',1),'max_accel',1.5)
        value=parameter_turn();value['proposals'][0].update(parameter='profile_accel',current_value='1',
            proposed_value='2',unit='m/s²',confirmation_keys=['profile_accel','max_accel'])
        with self.assertRaisesRegex(ValueError,'cannot exceed'):validate_discussion_response(value,supplied)
        supplied=confirm(confirm(confirm(context(),'max_accel',3),'profile_decel',2.2),'profile_accel',1.5)
        value['proposals'][0].update(parameter='max_accel',current_value='3',proposed_value='2',confirmation_keys=['max_accel','profile_accel','profile_decel'])
        with self.assertRaisesRegex(ValueError,'cannot fall below'):validate_discussion_response(value,supplied)

    def test_acceleration_proposal_requires_related_confirmed_facts_in_references(self):
        for parameter,related in (('profile_accel',['max_accel']),('profile_decel',['max_accel']),
                                  ('max_accel',['profile_accel','profile_decel'])):
            supplied=confirm(context(),parameter,2)
            value=parameter_turn();value['proposals'][0].update(parameter=parameter,current_value='2',proposed_value='1.8',
                unit='m/s²',confirmation_keys=[parameter])
            with self.subTest(parameter=parameter),self.assertRaisesRegex(ValueError,'related native'):
                validate_discussion_response(value,supplied)
            for key in related:confirm(supplied,key,1)
            with self.assertRaisesRegex(ValueError,'related native'):validate_discussion_response(value,supplied)

    def test_code_requires_user_confirmed_exact_deployed_commit(self):
        value=code_turn()
        for supplied in (context(),confirm(context(),'deployed_source_commit','b'*40),
                         confirm(context(),'deployed_source_commit',COMMIT,lap_id='different')):
            with self.assertRaises(ValueError):validate_discussion_response(value,supplied)
        supplied=confirm(context(),'deployed_source_commit',COMMIT)
        self.assertEqual(validate_discussion_response(value,supplied),value)
        changed=copy.deepcopy(value);changed['proposals'][0]['source_commit']='b'*40
        with self.assertRaises(ValueError):validate_discussion_response(changed,supplied)
        changed=copy.deepcopy(value);changed['proposals'][0]['confirmation_keys']=[]
        with self.assertRaises(ValueError):validate_discussion_response(changed,supplied)

    def test_code_source_membership_diff_match_syntax_and_single_file_required(self):
        supplied=confirm(context(),'deployed_source_commit',COMMIT)
        invalid=[{'source_file':'../mpc_node.py'},{'source_file':'/tmp/mpc_node.py'},
                 {'source_file':'mpc//scripts/mpc_node.py'},{'source_file':'mpc/scripts/unknown.py'},
                 {'diff':patch().replace('-MAX_SPEED = 3.0','-MAX_SPEED = 4.0')},
                 {'diff':patch()+patch()}, {'diff':patch(new='def invalid(\n')},
                 {'diff':'rm -rf controller'}, {'parameter':'MAX_SPEED'}, {'unit':'m/s'}]
        for changes in invalid:
            value=code_turn();value['proposals'][0].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate_discussion_response(value,supplied)
        self.assertEqual(validate_unified_diff(patch(),PATH,SOURCE),SOURCE.replace('3.0','2.8'))
        self.assertEqual(supplied['source_context']['files'][0]['content'],SOURCE)

    def test_working_tree_base_commit_does_not_authorize_a_pinned_code_patch(self):
        supplied=confirm(context(),'deployed_source_commit',COMMIT)
        supplied['source_context'].update(mode='working_tree',snapshot_sha256='b'*64)
        with self.assertRaisesRegex(ValueError,'Use the main chat.*pinned Git revision'):
            validate_discussion_response(code_turn(),supplied)
        # The same confirmed commit still permits patches against an immutable
        # Git revision, and working-tree telemetry discussion remains available.
        supplied['source_context']['mode']='git_revision'
        self.assertEqual(validate_discussion_response(code_turn(),supplied),code_turn())
        supplied['source_context']['mode']='working_tree'
        self.assertEqual(validate_discussion_response(turn(),supplied),turn())

    def test_measurement_fields_do_not_bypass_parameter_or_code_confirmation(self):
        for field in ('parameter','current_value','proposed_value','unit','source_file','source_commit','diff'):
            value=turn();value['proposals']=[proposal()];value['proposals'][0][field]='unconfirmed'
            with self.subTest(field=field),self.assertRaises(ValueError):validate_discussion_response(value,context())

    def test_all_proposals_cite_original_evidence_and_known_confirmation_keys(self):
        supplied=context();supplied['comparison']={'metrics':{'evidence':[{'id':'later-sector'}]}}
        for change in ({'evidence_ids':[]},{'evidence_ids':['later-sector']},
                       {'evidence_ids':['sector-2','sector-2']},{'confirmation_keys':['made_up']},
                       {'confirmation_keys':[{}]}):
            value=turn();value['proposals']=[{**proposal(),**change}]
            with self.subTest(change=change),self.assertRaises(ValueError):validate_discussion_response(value,supplied)

    def test_strict_shape_bounds_and_question_keys(self):
        for change in ({'extra':'unexpected'},{'message':''},{'message':42},
                       {'questions':turn()['questions']*4},{'proposals':[proposal(),proposal()]},
                       {'limitations':[42]}):
            with self.subTest(change=change),self.assertRaises(ValueError):validate_discussion_response({**turn(),**change},context())
        for field,value in (('id','bad id'),('key','../bad'),('key','x'*81),('reason','')):
            item=turn();item['questions'][0][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):validate_discussion_response(item,context())
        value=turn();value['proposals']=[{**proposal(),'id':'q1'}]
        with self.assertRaises(ValueError):validate_discussion_response(value,context())
        value=turn();value['questions'][0]['suggested_answer']='x'*501
        with self.assertRaises(ValueError):validate_discussion_response(value,context())
        value=turn();value['message']='x'*12000;value['limitations']=['x'*3000]*20
        value['questions'][0].update(question='x'*2000,reason='x'*2000)
        value['proposals']=[{**proposal(),'rationale':'x'*5000,'validation':'x'*5000,'rollback':'x'*5000}]
        with self.assertRaisesRegex(ValueError,'75 KiB'):validate_discussion_response(value,context())


class UnifiedDiffTests(unittest.TestCase):
    def test_multiple_ordered_hunks_insertions_deletions_and_git_headers(self):
        old=''.join(f'value_{i} = {i}\n' for i in range(30))
        new=old.replace('value_2 = 2','value_2 = 3').replace('value_25 = 25','value_25 = 24')
        text=patch(old,new)
        self.assertEqual(text.count('@@ '),2)
        self.assertEqual(validate_unified_diff(text,PATH,old),new)
        self.assertEqual(validate_unified_diff(f'diff --git a/{PATH} b/{PATH}\nindex abc..def 100644\n'+text,PATH,old),new)
        for new in ('prefix = True\n'+SOURCE,SOURCE+'trailing = True\n',SOURCE.splitlines(keepends=True)[0]):
            self.assertEqual(validate_unified_diff(patch(SOURCE,new),PATH,SOURCE),new)

    def test_newline_markers_match_exact_source_bytes(self):
        old='VALUE = 1';new='VALUE = 2'
        text='--- a/'+PATH+'\n+++ b/'+PATH+'\n@@ -1 +1 @@\n-VALUE = 1\n\\ No newline at end of file\n+VALUE = 2\n\\ No newline at end of file\n'
        self.assertEqual(validate_unified_diff(text,PATH,old),new)
        with self.assertRaises(ValueError):validate_unified_diff(text,PATH,old+'\n')

    def test_wrong_hunk_coordinates_counts_context_and_noop_are_rejected(self):
        good=patch()
        for bad in (good.replace('@@ -1,4 +1,4 @@','@@ -2,4 +1,4 @@'),
                    good.replace('@@ -1,4 +1,4 @@','@@ -1,5 +1,4 @@'),
                    good.replace('@@ -1,4 +1,4 @@','@@ -1,4 +2,4 @@'),
                    good.replace(' return MAX_SPEED',' return WRONG'),
                    '--- a/'+PATH+'\n+++ b/'+PATH+'\n',
                    'Binary files differ\n',good.replace('+++ b/'+PATH,'+++ /dev/null')):
            if bad==good:continue
            with self.subTest(bad=bad[:100]),self.assertRaises(ValueError):validate_unified_diff(bad,PATH,SOURCE)

    def test_python_is_parsed_never_imported_executed_or_written(self):
        with tempfile.TemporaryDirectory() as folder:
            sentinel=Path(folder)/'must_not_exist'
            new=SOURCE+f'\nopen({str(sentinel)!r}, "w").write("should not run")\n'
            self.assertEqual(validate_unified_diff(patch(SOURCE,new),PATH,SOURCE),new)
            self.assertFalse(sentinel.exists())


if __name__=='__main__':unittest.main()
