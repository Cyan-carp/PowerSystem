// @vitest-environment ./tests/vue-environment.ts
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {createRenderer,nextTick} from 'vue'
const {api,shell}=vi.hoisted(()=>({api:Object.fromEntries(['alarms','alarm','device','healthDevices','prediction','interpretations','readInterpretation'].map(k=>[k,vi.fn()])),shell:{alarmRevision:0,insightRevision:0}}))
vi.mock('../src/lib/api',()=>({api}))
vi.mock('../src/lib/store',async()=>{const {reactive}=await import('vue');return {shell:reactive(shell)}})
vi.mock('vue-router',async()=>{const {h}=await import('vue');return {RouterLink:{setup:(_:unknown,{slots}:any)=>()=>h('a',slots.default?.())}}})
vi.mock('../src/components/Drawer.vue',async()=>{const {h}=await import('vue');return {default:{setup:(_:unknown,{slots}:any)=>()=>h('div',[slots.default?.(),slots.footer?.()])}}})
import AlarmInsight from '../src/components/AlarmInsight.vue'
import {shell as liveShell} from '../src/lib/store'
type Node={type:string;text:string;children:Node[];parent:Node|null;props:Record<string,any>}
const node=(type:string,text=''):Node=>({type,text,children:[],parent:null,props:{}})
const renderer=createRenderer<Node,Node>({createElement:type=>node(type),createText:text=>node('text',text),createComment:text=>node('comment',text),setText:(el,text)=>{el.text=text},setElementText:(el,text)=>{el.text=text;el.children=[]},parentNode:el=>el.parent,nextSibling:el=>el.parent?.children[el.parent.children.indexOf(el)+1]??null,insert:(el,parent,anchor)=>{if(el.parent)el.parent.children.splice(el.parent.children.indexOf(el),1);const i=anchor?parent.children.indexOf(anchor):-1;parent.children.splice(i<0?parent.children.length:i,0,el);el.parent=parent},remove:el=>{if(el.parent)el.parent.children.splice(el.parent.children.indexOf(el),1)},patchProp:(el,key,old,value)=>{el.props[key]=value}})
const text=(el:Node):string=>(el.type==='comment'?'':el.text)+el.children.map(text).join('')
const flush=async()=>{for(let i=0;i<15;i++){await Promise.resolve();await nextTick()}}
const alarm={id:7,device_id:1,metric:'temperature',level:'major',value:82,threshold:80,status:'unhandled',triggered_at:'2026-10-30T00:00:00Z',origin:'normal'}
const insight=(active=true)=>({id:2,alarm_id:7,task_status:'completed',active,read:true,updated_at:'2026-10-30T00:00:01Z',result:{status:'answered',interpretation_mode:'fixed',protocol_version:2,conclusion:{text:'固定温度核查说明'},suggestions:[],facts:[],limitations:['固定模板，不调用API']}})
const apps:{unmount():void}[]=[]
function mount(){const root=node('root'),app=renderer.createApp(AlarmInsight);app.mount(root);apps.push(app);return root}
beforeEach(()=>{
 vi.useFakeTimers();vi.stubGlobal('window',{setTimeout,clearTimeout})
 vi.resetAllMocks();liveShell.alarmRevision=0;liveShell.insightRevision=0
 api.alarms.mockResolvedValue({list:[alarm],total:1});api.alarm.mockResolvedValue(alarm);api.device.mockResolvedValue({name:'演示设备'});api.healthDevices.mockResolvedValue({devices:[]});api.prediction.mockResolvedValue({});api.interpretations.mockResolvedValue({list:[insight()]});api.readInterpretation.mockResolvedValue({})
})
afterEach(()=>{apps.splice(0).forEach(app=>app.unmount());vi.useRealTimers();vi.unstubAllGlobals()})
it('refreshes an open interpretation when the shared alarm channel confirms recovery',async()=>{
 const root=mount();await flush();expect(text(root)).toContain('固定解读');expect(text(root)).toContain('未调用API')
 api.alarm.mockResolvedValue({...alarm,status:'recovered',recovered_at:'2026-10-30T00:10:00Z'});api.interpretations.mockResolvedValue({list:[insight(false)]})
 liveShell.alarmRevision++;await flush()
 expect(api.alarm).toHaveBeenCalledWith(7);expect(text(root)).toContain('事件已恢复')
})
it('fills an already open empty panel after a new shared alarm event',async()=>{
 api.alarms.mockResolvedValue({list:[],total:0});const root=mount();await flush();expect(text(root)).toContain('当前没有未处理告警')
 api.alarms.mockResolvedValue({list:[alarm],total:1});liveShell.alarmRevision++;await flush()
 expect(text(root)).toContain('固定温度核查说明');expect(text(root)).not.toContain('当前没有未处理告警')
})
