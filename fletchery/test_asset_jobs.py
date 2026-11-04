"""Asset jobs require operator grants and retain normal code/reviewer boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
from asset_jobs import grant_for,permitted,prompt_note
from agents import AgentSpec
from clis import CodexRuntime,OpenCodeRuntime
from config import Config
from repo import Repo

class AssetJobs(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.worktrees=self.root/'tasks';self.worktrees.mkdir()
  self.wt=self.worktrees/'991-hair';self.wt.mkdir()
  self.grants={'991':{'paths':['Art/Hair/','Review991/']}}
 def test_grant_is_from_operator_and_bound_to_task_root(self):
  self.assertIsNone(grant_for(None,self.worktrees,self.wt))
  self.assertIsNone(grant_for(self.grants,self.root,self.wt))
  self.assertIsNone(grant_for(self.grants,self.worktrees,self.worktrees/'992-hair'))
  grant=grant_for(self.grants,self.worktrees,self.wt)
  self.assertTrue(permitted(grant,'Art/Hair/Waves.png'))
  self.assertFalse(permitted(grant,'Content/Other.uasset'))
  self.assertFalse(permitted(grant,'Art/Hair/../Other.png'))
  self.assertIsNone(grant_for({'991':{'paths':['../'] }},self.worktrees,self.wt))
  self.assertIsNone(grant_for({'991':{'paths':['Art/*'] }},self.worktrees,self.wt))
  self.assertIn('binary file type alone',prompt_note(grant))
 def test_codex_authoring_is_task_scoped_and_reviewers_stay_read_only(self):
  rt=CodexRuntime(AgentSpec('codex',kind='codex'));rt.asset_tasks=self.grants;rt.worktree_root=self.worktrees
  prompt=self.root/'prompt.md';prompt.write_text('task')
  with patch('clis.find_codex',return_value='codex'):
   for role,want in [('worker','danger-full-access'),('fixer','danger-full-access'),('reviewer','read-only'),('planner','read-only')]:
    args,_,_=rt.command(role,prompt,self.wt,self.root)
    self.assertEqual(args[args.index('-s')+1],want)
   args,_,_=rt.command('worker',prompt,self.worktrees/'992-hair',self.root)
   self.assertEqual(args[args.index('-s')+1],'workspace-write')
 def test_opencode_allows_installed_mcp_only_for_authorized_write_jobs(self):
  rt=OpenCodeRuntime(AgentSpec('deepseek',kind='opencode'));rt.asset_tasks=self.grants;rt.worktree_root=self.worktrees
  prompt=self.root/'prompt.md';prompt.write_text('task')
  with patch('clis.find_opencode',return_value='opencode'):
   for role,want in [('worker','allow'),('reviewer','deny'),('planner','deny')]:
    _,_,env=rt.command(role,prompt,self.wt,self.root)
    perms=json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
    self.assertEqual(perms['external_directory'],want)
 def repo(self,grants=None):
  r=Repo(self.root,self.worktrees,'main',asset_tasks=grants)
  r.changed_files=lambda _:['Art/Hair/Waves.png']
  r.lfs_tracked=lambda _,names:names
  return r
 def test_default_and_unrelated_binary_changes_are_refused(self):
  for grants in (None,{'991':{'paths':['Content/']}}):
   r=self.repo(grants);r.git=Mock()
   with self.assertRaises(RuntimeError):r.push(self.wt,'codex/hair')
   r.git.assert_not_called()
 def test_assets_use_delivery_helper_and_failure_does_not_publish_git(self):
  r=self.repo(self.grants);r.git=Mock()
  helper=self.root/'Tools/CI/PushWithLfs.ps1';helper.parent.mkdir(parents=True);helper.write_text('helper')
  with patch('repo.subprocess.run',return_value=Mock(returncode=0,stdout='',stderr='')) as run:
   r.push(self.wt,'codex/hair')
   self.assertIn(str(helper),run.call_args.args[0]);self.assertEqual(run.call_args.kwargs['cwd'],self.wt)
  with patch('repo.subprocess.run',return_value=Mock(returncode=1,stdout='upload failed',stderr='')):
   with self.assertRaisesRegex(RuntimeError,'upload failed'):r.push(self.wt,'codex/hair')
  r.git.assert_not_called()
 def test_deletion_grant_refuses_modification_and_never_skips_lfs_hook(self):
  r=self.repo({'991':{'paths':['Art/Hair/'],'mode':'delete'}})
  r.git=Mock(return_value='Art/Hair/Waves.png')
  with self.assertRaisesRegex(RuntimeError,'Deletion-only'):r.push(self.wt,'codex/hair')
  r.git=Mock(return_value='')
  r.push(self.wt,'codex/hair')
  self.assertEqual(r.git.call_args.args,('push','-u','origin','HEAD:codex/hair'))
 def test_profile_roundtrip(self):
  p=self.root/'project.json';p.write_text(json.dumps({'workers':{'assetTasks':self.grants}}))
  cfg=Config.load(p);self.assertEqual(cfg.asset_tasks,self.grants)
  cfg.save(asset_tasks={});self.assertEqual(json.loads(p.read_text())['workers']['assetTasks'],{})

if __name__=='__main__':unittest.main()
