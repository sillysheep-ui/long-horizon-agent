import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, ChevronRight, Clock3, Compass, Database, LoaderCircle, MapPin, Plus, RotateCcw, Send, ShieldCheck, Sparkles, X } from 'lucide-react'
import { createTrip, getEvents, getTrip, reviseTrip } from './api'
import type { JobEvent, Trip } from './types'

const STORAGE_KEY = 'travel-agent-trip-ids'
const examples = ['从上海出发，周末去杭州玩两天，喜欢自然风光和咖啡馆，预算 1500 元', '北京出发去苏州 3 天，想看园林和古镇，行程不要太赶', '带父母去成都玩 4 天，偏好美食和轻松的景点']
const stageNames: Record<string, string> = { intake: '理解需求', planning: '拆解行程', research: '查询地点', verification: '核对证据', answer: '生成方案' }

function App() {
  const [query, setQuery] = useState('')
  const [revision, setRevision] = useState('')
  const [trip, setTrip] = useState<Trip | null>(null)
  const [ids, setIds] = useState<string[]>(() => { try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]') } catch { return [] } })
  const [events, setEvents] = useState<JobEvent[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [activeTab, setActiveTab] = useState<'itinerary' | 'evidence' | 'process'>('itinerary')
  const eventJob = useRef('')
  const latestId = useRef(0)
  const openRequest = useRef(0)

  async function openTrip(id: string) {
    const requestId = ++openRequest.current
    try {
      const next = await getTrip(id)
      if (requestId !== openRequest.current) return
      setTrip(next); setError(''); setActiveTab('itinerary')
    } catch (err) { if (requestId === openRequest.current) setError(err instanceof Error ? err.message : '读取失败') }
  }

  useEffect(() => {
    if (ids[0]) void openTrip(ids[0])
    // Initial history load only.
  }, [])

  const job = trip?.jobs[0]
  const processing = job?.status === 'queued' || job?.status === 'running'

  useEffect(() => {
    if (!job || !trip) return
    if (eventJob.current !== job.id) { eventJob.current = job.id; latestId.current = 0; setEvents([]) }
    let stopped = false
    async function poll() {
      if (!job || !trip || stopped) return
      try {
        const incoming = await getEvents(job.id, latestId.current)
        if (stopped) return
        if (incoming.length) { latestId.current = incoming[incoming.length - 1].id; setEvents(old => [...old, ...incoming]) }
        const updated = await getTrip(trip.id)
        if (!stopped) setTrip(updated)
      } catch (err) { if (!stopped) setError(err instanceof Error ? err.message : '更新失败') }
    }
    void poll()
    if (!processing) return () => { stopped = true }
    const timer = window.setInterval(() => void poll(), 1500)
    return () => { stopped = true; window.clearInterval(timer) }
  }, [trip?.id, job?.id, processing])

  async function submit() {
    if (query.trim().length < 5) { setError('请用至少 5 个字描述旅行需求'); return }
    setBusy(true); setError('')
    try {
      const created = await createTrip(query.trim())
      const next = [created.trip_id, ...ids.filter(id => id !== created.trip_id)].slice(0, 20)
      setIds(next); localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
      await openTrip(created.trip_id); setQuery('')
    } catch (err) { setError(err instanceof Error ? err.message : '提交失败') }
    finally { setBusy(false) }
  }

  async function submitRevision() {
    if (!trip || revision.trim().length < 2) return
    setBusy(true); setError('')
    try { await reviseTrip(trip.id, revision.trim()); setRevision(''); await openTrip(trip.id); setActiveTab('process') }
    catch (err) { setError(err instanceof Error ? err.message : '修改失败') }
    finally { setBusy(false) }
  }

  const result = trip?.result
  const verified = result?.evidence?.filter(item => item.status === 'ok').length || 0
  const sourceFor = (evidenceId: string, title: string) => result?.evidence?.find(item => item.id === evidenceId)?.items.find(item => item.name === title)?.url

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-icon"><Compass size={23}/></span><span>远行<span className="brand-dot">.</span><small>TRAVEL AGENT</small></span></div>
      <button className="new-trip" onClick={() => { openRequest.current++; setTrip(null); setEvents([]); setError('') }}><Plus size={17}/> 新建旅行计划</button>
      <div className="side-label">最近的行程</div>
      <div className="history">{ids.length ? ids.map((id, index) => <button key={id} className={trip?.id === id ? 'history-item active' : 'history-item'} onClick={() => void openTrip(id)}><MapPin size={15}/><span>{id === trip?.id ? trip.query.split('\n')[0].slice(0, 18) : `旅行计划 ${ids.length - index}`}</span><ChevronRight size={14}/></button>) : <p className="empty-history">还没有保存的行程</p>}</div>
      <div className="side-footer"><span className="status-dot"/> 本地工作台 <span className="version">BETA 0.1</span></div>
    </aside>

    <main className="main">
      <header className="topbar"><span>你的专属旅行规划空间</span><div className="top-pill"><Sparkles size={13}/> 多 Agent 协同规划</div></header>
      <div className="content">
        <div className="hero"><div className="eyebrow"><span className="eyebrow-line"/> EXPLORE WITH CONFIDENCE</div><h1>每一段旅程，<br/><em>都值得认真规划。</em></h1><p>告诉我你的出发地、目的地、时间与喜好。我们会拆解需求、查询真实地点，并标出尚未核实的信息。</p></div>
        <section className="composer"><div className="composer-title"><Sparkles size={17}/> 从这里开始</div><textarea value={query} onChange={event => setQuery(event.target.value)} placeholder="例如：下周从上海出发去杭州玩两天，预算 1500 元，喜欢自然风光和咖啡馆……" rows={3}/><div className="composer-bottom"><span>描述越具体，行程越贴合你的需求</span><button className="primary-btn" disabled={busy} onClick={() => void submit()}>{busy ? <LoaderCircle className="spin" size={17}/> : <ArrowRight size={17}/>} 生成旅行方案</button></div></section>
        {!trip && <div className="examples"><span>试试这样说</span><div>{examples.map(example => <button key={example} onClick={() => setQuery(example)}>{example}<ArrowRight size={13}/></button>)}</div></div>}
        {error && <div className="error-box"><X size={16}/>{error}</div>}
        {trip && <section className="workspace"><div className="workspace-heading"><div><div className="section-kicker">YOUR JOURNEY</div><h2>旅行方案 <span>v{trip.version}</span></h2><p>{trip.query.split('\n')[0]}</p></div><span className={`job-badge ${job?.status || ''}`}>{processing ? <LoaderCircle size={14} className="spin"/> : job?.status === 'failed' ? <X size={14}/> : <Check size={14}/>} {job?.status === 'queued' ? '等待处理' : job?.status === 'running' ? '正在规划' : job?.status === 'failed' ? '执行失败' : result?.status === 'partial' ? '部分完成' : result?.status === 'needs_clarification' ? '需补充信息' : '已完成'}</span></div>
          <div className="tabs"><button className={activeTab === 'itinerary' ? 'selected' : ''} onClick={() => setActiveTab('itinerary')}><MapPin size={16}/> 行程方案</button><button className={activeTab === 'evidence' ? 'selected' : ''} onClick={() => setActiveTab('evidence')}><ShieldCheck size={16}/> 地点证据 {result && <b>{verified}</b>}</button><button className={activeTab === 'process' ? 'selected' : ''} onClick={() => setActiveTab('process')}><Clock3 size={16}/> 执行过程</button></div>
          {activeTab === 'itinerary' && <div className="panel">{processing && <div className="notice">正在处理当前修改。下方若有行程，它属于上一版本。</div>}{processing && !result && <div className="placeholder"><LoaderCircle className="spin" size={28}/><strong>正在为你规划旅程</strong><span>完成后，行程会自动出现在这里</span></div>}{job?.status === 'failed' && <div className="notice failure">{job.error || '任务失败，请修改需求后重试'}</div>}{result?.question && <div className="notice">{result.question}</div>}{result?.summary && <p className="summary">{result.summary}</p>}{result?.days?.map(day => <div className="day" key={day.day}><div className="day-number">DAY {String(day.day).padStart(2, '0')}</div><div className="day-content"><h3>{day.city}</h3>{day.activities.length ? day.activities.map((activity, index) => <div className="activity" key={`${activity.evidence_id}-${index}`}><span className="activity-marker"/><div><strong>{activity.title}</strong><p>{activity.description}</p><small><ShieldCheck size={12}/> 已关联可追溯来源 · {activity.evidence_id}{sourceFor(activity.evidence_id, activity.title) && <a href={sourceFor(activity.evidence_id, activity.title)} target="_blank" rel="noopener noreferrer">查看来源</a>}</small></div></div>) : <p className="muted">这一天还没有可核实的地点安排</p>}</div></div>)}{result?.notes?.length ? <div className="notes"><strong>出行提醒</strong>{result.notes.map((note, index) => <p key={index}>· {note}</p>)}</div> : null}</div>}
          {activeTab === 'evidence' && <div className="panel evidence-panel">{result?.evidence?.length ? result.evidence.map(item => <div className="evidence-card" key={item.id}><div className="evidence-head"><div><Database size={15}/><strong>{item.query}</strong><span>{item.city}</span></div><span className={`evidence-status ${item.status}`}>{item.status === 'ok' ? (item.kind === 'poi' ? '地图 POI' : '网页来源') : item.status === 'empty' ? '无结果' : '不可用'}</span></div>{item.items?.map((poi, index) => <p key={index}><MapPin size={13}/><b>{poi.url ? <a href={poi.url} target="_blank" rel="noopener noreferrer">{poi.name}</a> : poi.name}</b> {poi.address}</p>)}{item.reason && <small>{item.reason}</small>}{item.source && <small>来源：{item.source}{item.reused ? ' · 沿用最近查询' : ''}</small>}</div>) : <div className="placeholder">查询完成后会显示证据来源</div>}</div>}
          {activeTab === 'process' && <div className="panel process-panel">{events.length ? events.map(event => <div className="event" key={event.id}><span className={`event-dot ${event.kind}`}/><div><strong>{event.payload.stage ? stageNames[event.payload.stage] || event.payload.stage : event.kind === 'completed' ? '任务完成' : event.kind === 'failed' ? '执行失败' : '任务状态'}</strong><p>{event.payload.message}</p></div><time>{new Date(event.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time></div>) : <div className="placeholder">正在读取执行记录</div>}{result?.model_calls?.length ? <div className="model-line">模型调用：{result.model_calls.map(call => `${call.role} · ${call.provider}/${call.model}`).join(' → ')}</div> : null}</div>}
          <div className="revision"><div><RotateCcw size={17}/><strong>需要调整？</strong><span>直接说出你想修改的地方</span></div><div className="revision-input"><input value={revision} onChange={event => setRevision(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void submitRevision() }} placeholder="例如：第二天轻松一些，增加咖啡馆"/><button disabled={busy || revision.trim().length < 2} onClick={() => void submitRevision()} aria-label="提交修改"><Send size={16}/></button></div></div>
        </section>}
        <footer>远行 · 让计划有依据，让旅途更从容。<span>地点信息以查询结果为准，出行前请再次确认营业与票务。</span></footer>
      </div>
    </main>
  </div>
}

export default App
