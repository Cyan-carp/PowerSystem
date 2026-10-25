import { afterEach, expect, it, vi } from 'vitest'
import { decideAction, getRun, type RunSnapshot } from './agent-runs'
import { token } from './auth'
const run: RunSnapshot = { protocol:'assistant/v4-draft1',run_id:'run-a',session_id:'session-a',turn_id:9,status:'awaiting_approval',phase:'awaiting_approval',cancel_requested:false,requires_manual_reconciliation:false,final_answer:'',answer_version:1,result_refs:[],actions:[],artifacts:[],terminal_event:null,last_seq:3,error:null }
afterEach(()=>{vi.unstubAllGlobals();token.value=''})
it('sends only the existing approval binding and keeps unconfirmed authority visible',async()=>{
 token.value='owner-jwt';const fetcher=vi.fn().mockResolvedValue(new Response(JSON.stringify({...run,authority_sync_status:'pending_reconciliation'}),{status:202}));vi.stubGlobal('fetch',fetcher)
 const result=await decideAction(run,'action-a','approval-a','a'.repeat(64),'approve')
 const [path,init]=fetcher.mock.calls[0];expect(path).toContain('/runs/run-a/actions/action-a/decision');expect(init.headers.get('Authorization')).toBe('Bearer owner-jwt');expect(JSON.parse(init.body)).toEqual({decision:'approve',approval_id:'approval-a',plan_hash:'a'.repeat(64)});expect(result.authority_sync_status).toBe('pending_reconciliation')
})
it('rejects a decision response bound to another original turn',async()=>{
 vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({...run,turn_id:10}))))
 await expect(decideAction(run,'action-a','approval-a','a'.repeat(64),'reject')).rejects.toThrow('turn_binding_mismatch')
})
it('discards a decision response after identity changed while awaiting HTTP',async()=>{
 token.value='first';vi.stubGlobal('fetch',vi.fn().mockImplementation(async()=>{token.value='second';return new Response(JSON.stringify(run))}))
 await expect(decideAction(run,'action-a','approval-a','a'.repeat(64),'approve')).rejects.toThrow('identity_changed')
})
it('preserves only safe action display fields from authority snapshot',async()=>{
 vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({...run,actions:[{action_id:'a',status:'succeeded',display_result:'<script>plain text</script>',target_version:'0',error_code:'target_version_changed',grant:'must-not-surface',private_key:'must-not-surface'}]}))))
 const result=await getRun('run-a');expect(result.actions[0].display_result).toBe('<script>plain text</script>');expect(result.actions[0].target_version).toBe('0');expect(result.actions[0]).not.toHaveProperty('grant');expect(result.actions[0]).not.toHaveProperty('private_key')
})
