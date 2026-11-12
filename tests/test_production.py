import sys, tempfile, unittest, json, urllib.error
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'drama'))
import production as p

class ProductionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.old=p.APP;p.APP=self.root
  self.job=p.Job.__new__(p.Job);self.job.folder=self.root/'output/drama_jobs/job';self.job.folder.mkdir(parents=True)
  self.job.cfg=json.loads((ROOT / 'drama/production_config.json').read_text());self.job.cfg['quality'].update(max_retries=2,stage_timeout_sec=0.1)
  self.job.state={'shots':{}};self.job.state_path=self.job.folder/'state.json';self.job.characters={};self.job.style='国风动漫';self.job.template=None
  self.shot={'shot_id':'S01','description':'仙山','width':16,'height':16,'_index':1}
  self.path=self.root/'output/result.png';a=np.indices((16,16)).sum(0)%2*255;Image.fromarray(a.astype('uint8')).convert('RGB').save(self.path)
 def tearDown(self):p.APP=self.old;self.tmp.cleanup()
 def test_terminal_failure_retries_then_succeeds(self):
  posts=[]
  def request(path,data=None):
   if path=='/api/drama/runtime':return {'backend_id':'test-backend','loaded_model_count':0}
   if path=='/queue':return {'queue_running':[],'queue_pending':[]}
   if path=='/prompt':posts.append(data);return {'prompt_id':str(len(posts))}
   if path=='/free':return {}
   pid=path.rsplit('/',1)[1]
   return {pid:{'status':{'status_str':'error' if pid=='1' else 'success','completed':True},'outputs':{'10':{'images':[{'filename':'result.png','subfolder':''}]}}}}
  self.job.request=request;self.job.run_stage(self.shot,'image')
  self.assertEqual(len(posts),2);self.assertEqual(self.job.state['shots']['S01']['image']['status'],'complete')
 def test_uncertain_submission_is_not_repeated(self):
  calls=[]
  def request(path,data=None):
   if path=='/api/drama/runtime':return {'backend_id':'test-backend','loaded_model_count':0}
   calls.append(path);raise urllib.error.URLError('lost response')
  self.job.request=request
  for _ in range(2):
   with self.assertRaises(p.Hold):self.job.run_stage(self.shot,'image')
  self.assertEqual(calls.count('/prompt'),1)
 def test_resume_last_allowed_running_attempt(self):
  st={'attempts':3,'status':'running','prompt_id':'existing'};self.job.state['shots']={'S01':{'image':st}}
  calls=[]
  def request(path,data=None):
   calls.append(path)
   return {'existing':{'status':{'status_str':'success','completed':True},'outputs':{'10':{'images':[{'filename':'result.png','subfolder':''}]}}}}
  self.job.request=request;self.job.run_stage(self.shot,'image')
  self.assertNotIn('/prompt',calls);self.assertEqual(st['status'],'complete')
 def test_resume_reports_actual_video_stage_without_resubmitting(self):
  st={'attempts':1,'status':'running','prompt_id':'existing'}
  self.job.state={'phase':'image_review','current_shot':'old','shots':{'S01':{'image':{'path':str(self.path)},'video':st}}}
  phases=[]
  def request(path,data=None):
   phases.append((self.job.state['phase'],self.job.state['current_shot']))
   self.assertEqual(path,'/history/existing')
   return {'existing':{'status':{'status_str':'success','completed':True},'outputs':{'12':{'videos':[{'filename':'result.png','subfolder':''}]}}}}
  self.job.request=request
  with patch.object(p,'video_check',return_value={'frames':17}):self.job.run_stage(self.shot,'video')
  self.assertEqual(phases,[('video','S01')]);self.assertEqual(st['attempts'],1)
 def test_retry_exhaustion_does_not_succeed(self):
  calls=[]
  def request(path,data=None):
   if path=='/api/drama/runtime':return {'backend_id':'test-backend','loaded_model_count':0}
   if path=='/queue':return {'queue_running':[],'queue_pending':[]}
   if path=='/prompt':calls.append(path);return {'prompt_id':str(len(calls))}
   if path=='/free':return {}
   pid=path.rsplit('/',1)[1];return {pid:{'status':{'status_str':'error'},'outputs':{}}}
  self.job.request=request
  with self.assertRaises(p.Hold):self.job.run_stage(self.shot,'image')
  self.assertEqual(len(calls),3);self.assertEqual(self.job.state['shots']['S01']['image']['status'],'failed')
 def test_unknown_last_attempt_recovers_without_duplicate_submission(self):
  st={'attempts':3,'status':'unknown'};self.job.state['shots']={'S01':{'image':st}};calls=[]
  metadata={'drama_job':self.job.folder.name,'shot':'S01','stage':'image','attempt':3}
  entry={'prompt':[0,'existing',{},metadata,[]],'status':{'status_str':'success','completed':True},'outputs':{'10':{'images':[{'filename':'result.png','subfolder':''}]}}}
  def request(path,data=None):
   calls.append(path)
   if path=='/queue':return {'queue_running':[],'queue_pending':[[0,'existing',{},metadata,[]]]}
   return {'existing':entry}
  self.job.request=request;self.job.run_stage(self.shot,'image')
  self.assertNotIn('/prompt',calls);self.assertEqual(st['status'],'complete')
 def test_backend_restart_retries_confirmed_terminated_job(self):
  st={'attempts':1,'status':'running','prompt_id':'old','backend_id':'old-backend'};self.job.state['shots']={'S01':{'image':st}};posts=[]
  def request(path,data=None):
   if path=='/api/drama/runtime':return {'backend_id':'new-backend','loaded_model_count':0}
   if path=='/queue':return {'queue_running':[],'queue_pending':[]}
   if path=='/free':return {}
   if path=='/prompt':posts.append(data);return {'prompt_id':'new'}
   if path=='/history/old':return {}
   return {'new':{'status':{'status_str':'success','completed':True},'outputs':{'10':{'images':[{'filename':'result.png','subfolder':''}]}}}}
  self.job.request=request;self.job.run_stage(self.shot,'image')
  self.assertEqual(len(posts),1);self.assertEqual(st['attempts'],2);self.assertEqual(st['status'],'complete')
 def test_restart_adopts_successful_saved_history_without_duplicate_generation(self):
  st={'attempts':1,'status':'running','prompt_id':'old','backend_id':'old-backend'}
  self.job.state['shots']={'S01':{'image':st}};self.job.template={}
  history={'status':{'status_str':'success','completed':True},'outputs':{'__stage_result':{'drama_stage':[{
   'path':str(self.path),'sha256':p.sha(self.path),'shot_id':'S01'}]}}}
  (self.job.folder/'S01_image_a1_history.json').write_text(json.dumps(history))
  self.job.request=lambda *a,**k:self.fail('a recorded successful stage must not be resubmitted')
  self.job.run_stage(self.shot,'image')
  self.assertEqual(st['status'],'complete');self.assertEqual(st['attempts'],1)
  self.assertEqual(st['path'],str(self.path.resolve()));self.assertEqual(st['recovered_from_history'],'S01_image_a1_history.json')
 def test_closed_gate_prevents_inference_http_request(self):
  import workflow_build
  gate=self.root/'closed_gate.json';gate.write_text(json.dumps({'build_complete':False,'inference_enabled':False}))
  with patch.object(workflow_build,'GATE',gate),patch.object(p.batch.OPENER,'open',side_effect=AssertionError('network must not be called')):
   with self.assertRaisesRegex(RuntimeError,'锁定'):p.batch.api_post('http://127.0.0.1:8188','/prompt',{'prompt':{}})
 def test_duplicate_shot_ids_rejected(self):
  folder=self.root/'board';folder.mkdir();(folder/'storyboard.json').write_text(json.dumps({'shots':[self.shot,self.shot]}))
  with self.assertRaisesRegex(ValueError,'唯一'):p.load_board(folder,self.job.cfg)
 def test_blank_image_rejected(self):
  path=self.root/'black.png';Image.new('RGB',(16,16)).save(path)
  with self.assertRaisesRegex(ValueError,'空白'):p.image_check(path)
 def test_wan_defaults_to_euler_for_existing_snapshots(self):
  config=json.loads(json.dumps(self.job.cfg));config['dynamic'].pop('sampler',None)
  graph=p.wan_graph('少年举剑','模糊','reference.png','diagnosis',0,config)
  self.assertEqual(graph['9']['inputs']['sampler_name'],'euler')
  self.assertEqual(graph['9']['inputs']['steps'],config['dynamic']['steps'])
  self.assertEqual(graph['7']['inputs']['length'],config['dynamic']['frames'])
  self.assertEqual(graph['7']['class_type'],'WanFirstLastFrameToVideo')
  self.assertEqual(graph['9']['inputs']['positive'],['7',0]);self.assertEqual(graph['9']['inputs']['negative'],['7',1]);self.assertEqual(graph['9']['inputs']['latent_image'],['7',2])
  guided=p.wan_graph('少年出剑','模糊','start.png','out',0,config,end_image='S01_end.png')
  self.assertEqual(guided['7']['inputs']['end_image'],['13',0]);self.assertEqual(guided['13']['inputs']['image'],'S01_end.png')
 def test_image_retry_uses_positive_correction_not_issue_description(self):
  st={'attempts':1,'status':'failed','quality_issues':['错误画面：角色拿着雨伞'],'correction_prompt':'少年右手握住古剑，剑尖垂下'}
  self.job.state['shots']={'S01':{'image':st}};posts=[]
  def request(path,data=None):
   if path=='/api/drama/runtime':return {'backend_id':'test-backend','loaded_model_count':0}
   if path=='/prompt':posts.append(data);return {'prompt_id':'fixed'}
   return {'fixed':{'status':{'status_str':'success','completed':True},'outputs':{'10':{'images':[{'filename':'result.png','subfolder':''}]}}}}
  self.job.request=request;self.job.run_stage(self.shot,'image')
  prompt=posts[0]['prompt']['3']['inputs']['text']
  self.assertIn(st['correction_prompt'],prompt);self.assertNotIn('雨伞',prompt)
 def test_video_retry_uses_positive_motion_correction(self):
  st={'attempts':1,'status':'failed','quality_issues':['错误动作：角色突然跑远'],'correction_prompt':'少年原地缓慢举剑，镜头固定'}
  self.shot['video_prompt']='少年原地举剑，剑刃被宽光带遮住'
  self.job.state['shots']={'S01':{'image':{'path':str(self.path)},'video':st}};posts=[]
  def request(path,data=None):
   if path=='/api/drama/runtime':return {'backend_id':'test-backend','loaded_model_count':0}
   if path=='/prompt':posts.append(data);return {'prompt_id':'fixed'}
   return {'fixed':{'status':{'status_str':'success','completed':True},'outputs':{'12':{'videos':[{'filename':'result.png','subfolder':''}]}}}}
  self.job.request=request
  with patch.object(p,'video_check',return_value={'frames':17}):self.job.run_stage(self.shot,'video')
  prompt=posts[0]['prompt']['4']['inputs']['text']
  self.assertIn(st['correction_prompt'],prompt);self.assertNotIn('突然跑远',prompt)
  self.assertNotIn(self.shot['video_prompt'],prompt)
  self.assertEqual(self.shot['video_prompt'],'少年原地举剑，剑刃被宽光带遮住')
 def test_missing_correction_falls_back_to_expected_positive_scene(self):
  response={'choices':[{'message':{'content':json.dumps({'pass':False,'issues':['拿错道具']})}}]}
  with patch.object(p.batch,'api_post',return_value=response):review=self.job.vision([self.path],'少年手持古剑')
  self.assertEqual(review['correction_prompt'],'少年手持古剑')

 def test_interrupted_execution_does_not_auto_retry(self):
  stage={'attempts':1,'status':'running','prompt_id':'interrupted'}
  self.job.state['shots']={'S01':{'image':stage}};calls=[]
  def request(path,data=None):
   calls.append(path)
   return {'interrupted':{'status':{'status_str':'error','completed':False,'messages':[['execution_interrupted',{}]]}}}
  self.job.request=request
  with self.assertRaises(p.Hold):self.job.run_stage(self.shot,'image')
  self.assertEqual(calls,['/history/interrupted']);self.assertEqual(stage['status'],'interrupted');self.assertEqual(self.job.state['status'],'stopped')
 def test_stop_marker_blocks_new_submissions(self):
  (self.job.folder/'stop_requested.json').write_text('{}')
  self.job.request=lambda *a,**k:self.fail('stopped task made a request')
  with self.assertRaises(p.Hold):self.job.run_stage(self.shot,'image')
  self.assertEqual(self.job.state['status'],'stopped')
 def test_explicit_resume_can_retry_confirmed_interruption(self):
  stage={'attempts':1,'status':'running','prompt_id':'old','resume_after_interrupt':True}
  self.job.state['shots']={'S01':{'image':stage}};posts=[]
  def request(path,data=None):
   if path=='/history/old':return {'old':{'status':{'status_str':'error','messages':[['execution_interrupted',{}]]}}}
   if path=='/free':return {}
   if path=='/queue':return {'queue_running':[],'queue_pending':[]}
   if path=='/api/drama/runtime':return {'backend_id':'test','loaded_model_count':0}
   if path=='/prompt':posts.append(data);return {'prompt_id':'new'}
   return {'new':{'status':{'status_str':'success','completed':True},'outputs':{'10':{'images':[{'filename':'result.png','subfolder':''}]}}}}
  self.job.request=request;self.job.run_stage(self.shot,'image')
  self.assertEqual(len(posts),1);self.assertEqual(stage['status'],'complete');self.assertEqual(stage['attempts'],2)
 def test_interrupted_animate2_graph_stage_retries_automatically(self):
  import workflow_graph
  key='S01_video_transfer_a1';step={'status':'running','prompt_id':'interrupted','backend_id':'test-backend','executions':1}
  self.job.state={'graph_steps':{key:step}};self.job.animate2_template={};self.job.template=None
  posts=[];expected={'path':'/output/animate2.mp4','sha256':'fixture'}
  def request(path,data=None):
   if path=='/api/drama/runtime':return {'backend_id':'test-backend','loaded_model_count':0}
   if path=='/prompt':posts.append(data);return {'prompt_id':data['prompt_id']}
   prompt_id=path.rsplit('/',1)[1]
   if prompt_id=='interrupted':
    return {'interrupted':{'status':{'status_str':'error','completed':False,'messages':[['execution_interrupted',{}]]}}}
   return {prompt_id:{'status':{'status_str':'success','completed':True},'outputs':{'__stage_result':{'drama_stage':[expected]}}}}
  self.job.request=request
  with patch.object(workflow_graph,'compile_animate2_stage',return_value={'1':{'class_type':'Fixture'}}):
   result=self.job.graph_stage('video_transfer',self.shot,1,asset={'input_image':'S01.png','driver_video':'S01.mp4'})
  self.assertEqual(result,expected);self.assertEqual(len(posts),1)
  self.assertEqual(step['status'],'complete');self.assertEqual(step['executions'],2)
 def test_delivery_copies_verified_files_and_is_idempotent(self):
  self.job.board={'title':'交付测试'};self.job.cfg['delivery']={'dir':str(self.root/'delivery'),'copy_keyframes':True}
  final=self.job.folder/'episode.mp4';final.write_bytes(b'verified-media-fixture')
  frames=self.job.folder/'keyframes';frames.mkdir();(frames/'S01.png').write_bytes(self.path.read_bytes())
  self.job.finish_delivery(final,{'frames':73});destination=Path(self.job.state['delivery_dir'])
  self.assertEqual(p.sha(destination/'episode.mp4'),p.sha(final));self.assertEqual(p.sha(destination/'keyframes/S01.png'),p.sha(self.path))
  manifest=(destination/'delivery.json').read_bytes();self.job.finish_delivery(final,{'frames':73})
  self.assertEqual(manifest,(destination/'delivery.json').read_bytes());self.assertEqual(self.job.state['status'],'complete')
 def test_delivery_preserves_modified_destination(self):
  self.job.board={'title':'交付测试'};self.job.cfg['delivery']={'dir':str(self.root/'delivery')}
  final=self.job.folder/'episode.mp4';final.write_bytes(b'verified-media-fixture');self.job.finish_delivery(final,{'frames':73})
  copied=Path(self.job.state['delivery_output']);copied.write_bytes(b'user-edited-media')
  with self.assertRaisesRegex(p.Hold,'已被修改'):self.job.finish_delivery(final,{'frames':73})
  self.assertEqual(copied.read_bytes(),b'user-edited-media');self.assertNotEqual(self.job.state['status'],'complete')

 def expanded_template(self):
  import workflow_graph as graph
  return graph,graph.capture(json.loads(graph.DEFAULT_API.read_text()))[0]
 def test_expanded_settings_follow_native_sampler_controls(self):
  graph,template=self.expanded_template()
  template['3']['inputs']['steps']=11;template['208']['inputs']['steps']=27
  settings=graph.graph_settings(template,self.job.cfg)
  self.assertEqual(settings['image']['steps'],11);self.assertEqual(settings['dynamic']['steps'],27)
 def test_expanded_keyframe_review_never_reloads_generation_models(self):
  graph,template=self.expanded_template();compiled=graph.compile_stage(template,'image_review','fixture',0,1)
  kinds={node['class_type'] for node in compiled.values()}
  self.assertIn('DramaKeyframeReview',kinds);self.assertIn('DramaStoredAsset',kinds)
  self.assertFalse(kinds.intersection({'UNETLoader','CLIPLoader','VAELoader','KSampler'}))
 def test_expanded_video_resume_uses_approved_image_and_requested_preview(self):
  graph,template=self.expanded_template();preset={'width':384,'height':672,'frames':17,'steps':12,'fps':8}
  compiled=graph.compile_stage(template,'video','fixture',0,2,asset={'input_image':'approved.png'},preset=preset)
  self.assertEqual(compiled['41']['class_type'],'LoadImage');self.assertEqual(compiled['41']['inputs']['image'],'approved.png')
  samplers=[node for node in compiled.values() if node['class_type']=='KSampler'];self.assertEqual(len(samplers),1)
  self.assertEqual(samplers[0]['inputs']['steps'],12);self.assertEqual(compiled['207']['inputs']['length'],17)
  self.assertEqual(compiled['213']['class_type'],'DramaEndpointFrames')
  self.assertEqual(compiled['210']['inputs']['images'],['213',0])
  self.assertNotIn('DramaSaveKeyframe',{node['class_type'] for node in compiled.values()})
  self.assertNotIn('end_image',compiled['207']['inputs'])
 def test_expanded_video_resume_attaches_optional_approved_end_frame(self):
  graph,template=self.expanded_template();compiled=graph.compile_stage(template,'video','fixture',0,1,asset={'input_image':'start.png','end_input_image':'end.png'})
  self.assertEqual(compiled['__end_frame']['class_type'],'LoadImage')
  self.assertEqual(compiled['207']['inputs']['end_image'],['__end_frame',0])
  self.assertEqual(compiled['213']['inputs']['end_image'],['__end_frame',0])
 def test_storyboard_end_frame_is_snapshotted_and_path_confined(self):
  import workflow_contract as contract
  folder=self.root/'input-project';folder.mkdir();(folder/'S01_end.png').write_bytes(b'frame')
  board={'shots':[{'shot_id':'S01','description':'少年持剑','end_frame':'S01_end.png'}]}
  (folder/'storyboard.json').write_text(json.dumps(board))
  payloads=contract.input_payloads(folder)
  self.assertIn('S01_end.png',payloads)
  loaded=p.load_board(folder,self.job.cfg)[1]
  self.assertEqual(loaded[0]['end_frame'],'S01_end.png')
  board['shots'][0]['end_frame']='../outside.png';(folder/'storyboard.json').write_text(json.dumps(board))
  with self.assertRaisesRegex(ValueError,'项目目录内'):contract.input_payloads(folder)
 def test_expanded_composition_executes_postproduction_without_sampling(self):
  graph,template=self.expanded_template();compiled=graph.compile_stage(template,'compose','fixture',1,1)
  kinds={node['class_type'] for node in compiled.values()}
  self.assertTrue({'DramaVoiceLines','DramaTimelineSubtitles','DramaMixSound','DramaComposeEpisode','DramaFinalReview','DramaDeliverEpisode'}<=kinds)
  self.assertFalse(kinds.intersection({'UNETLoader','CLIPLoader','VAELoader','KSampler'}))
  self.assertEqual(compiled['__stage_result']['inputs']['value'],['306',0])

if __name__=='__main__':unittest.main()
