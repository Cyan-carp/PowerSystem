import { describe,expect,it } from 'vitest'
import { insightView } from '../src/lib/interpretation'
import type { Interpretation } from '../src/lib/api'

const item=(result:Interpretation['result'],task_status:Interpretation['task_status']='completed'):Interpretation=>({id:1,event_key:'alarm:1',alarm_id:1,category:'device_alarm',level:'major',occurred_at:'2026-10-30T00:00:00Z',task_status,reason:'',result,evidence:[],updated_at:'2026-10-30T00:00:01Z',read:false,active:true})
describe('interpretation source',()=>{
 it('labels a fixed result explicitly without claiming a model response',()=>{
  const v=insightView(item({status:'answered',analysis_status:'fixed',interpretation_mode:'fixed',template_version:'alarm-fixed/v1',model:'',conclusion:{text:'固定温度核查说明'},suggestions:[{text:'核对散热与采样'}],limitations:['固定模板，不调用模型'],facts:[]}))
  expect(v.state).toBe('ready');expect(v.fixed).toBe(true);expect(v.model).toBe('');expect(v.note).toContain('未调用模型API');expect(v.conclusion).toEqual(['固定温度核查说明'])
 })
 it('retains the existing real model result path for manual demos',()=>{
  const v=insightView(item({status:'answered',analysis_status:'answered',model:'deepseek-flash',conclusion:{text:'模型的演示解读'},limitations:[]}))
  expect(v.state).toBe('ready');expect(v.fixed).toBe(false);expect(v.model).toBe('deepseek-flash');expect(v.note).toContain('模型')
 })
 it('does not display an old answer while a task is running',()=>{
  const v=insightView(item({status:'answered',interpretation_mode:'fixed',conclusion:{text:'旧结果'}},'running'))
  expect(v.state).toBe('pending');expect(v.conclusion).toEqual([]);expect(v.note).not.toContain('模型正在')
 })
})
