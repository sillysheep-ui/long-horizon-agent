import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, ChevronRight, Clock3, Compass, Database, LoaderCircle, MapPin, Plus, Send, ShieldCheck, Sparkles, X } from 'lucide-react'
import { createConversation, getConversation, getEvents, getTrip, sendMessage } from './api'
import type { Conversation, Evidence, JobEvent, Trip } from './types'

const STORAGE_KEY = 'travel-agent-conversations'
const examples = ['从上海出发，周末去杭州玩两天，喜欢自然风光和咖啡馆，预算 1500 元', '北京出发去苏州 3 天，想看园林和古镇，行程不要太赶', '带父母去成都玩 4 天，偏好美食和轻松的景点']
const stageNames: Record<string, string> = { intake: '理解需求', planning: '拆解行程', research: '查询地点', verification: '核对证据', answer: '生成方案' }
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

type SavedConversation = { id: string; title: string }

function loadSaved(): SavedConversation[] {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]') } catch { return [] }
}

function App() {
  const [input, setInput] = useState('')
  const [conversation, setConversation] = useState<Conversation | null>(null)
  const [trip, setTrip] = useState<Trip | null>(null)
  const [saved, setSaved] = useState<SavedConversation[]>(loadSaved)
  const [events, setEvents] = useState<JobEvent[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [activeTab, setActiveTab] = useState<'itinerary' | 'evidence' | 'process'>('itinerary')
  const requestId = useRef(0)
  const chatEnd = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const last = loadSaved()[0]
    if (last) void openConversation(last.id)
    // Initial history load only.
  }, [])

  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [conversation?.messages.length, busy])

  function remember(conv: Conversation) {
    setSaved(old => {
      const next = [{ id: conv.id, title: conv.title }, ...old.filter(item => item.id !== conv.id)].slice(0, 20)
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
      return next
    })
  }

  async function refreshTrip(conv: Conversation) {
    const latest = conv.trips[conv.trips.length - 1]
    if (!latest) { setTrip(null); return }
    const detail = await getTrip(latest.id)
    setTrip(detail)
  }

  async function openConversation(id: string) {
    const rid = ++requestId.current
    try {
      const conv = await getConversation(id)
      if (rid !== requestId.current) return
      setConversation(conv)
      remember(conv)
      setError('')
      await refreshTrip(conv)
    } catch (err) {
      if (rid === requestId.current) setError(err instanceof Error ? err.message : '读取对话失败')
    }
  }

  function newConversation() {
    requestId.current++
    setConversation(null)
    setTrip(null)
    setEvents([])
    setInput('')
    setError('')
  }

  async function submit() {
    const text = input.trim()
    if (text.length < 2 || busy) return
    const rid = ++requestId.current
    setBusy(true)
    setError('')
    try {
      const cid = conversation?.id ?? (await createConversation()).conversation_id
      const created = await sendMessage(cid, text)
      setInput('')
      let conv = await getConversation(cid)
      if (rid !== requestId.current) return
      setConversation(conv)
      remember(conv)
      for (let attempt = 0; attempt < 160; attempt++) {
        await sleep(1500)
        if (rid !== requestId.current) return
        try { setEvents(await getEvents(created.job_id)) } catch { /* 事件晚到可忽略 */ }
        conv = await getConversation(cid)
        if (rid !== requestId.current) return
        setConversation(conv)
        remember(conv)
        if (conv.messages.some(message => message.role === 'assistant' && message.job_id === created.job_id)) break
      }
      await refreshTrip(conv)
    } catch (err) {
      if (rid === requestId.current) setError(err instanceof Error ? err.message : '发送失败')
    } finally {
      if (rid === requestId.current) setBusy(false)
    }
  }

  const job = trip?.jobs[0]
  const processing = busy || job?.status === 'queued' || job?.status === 'running'
  const result = trip?.result
  const verified = result?.evidence?.filter(item => item.status === 'ok').length || 0
  const lastStage = [...events].reverse().find(event => event.payload.stage)?.payload.stage

  const sourceFor = (evidenceId: string, title: string) => result?.evidence?.find(item => item.id === evidenceId)?.items.find(item => item.name === title)?.url
  const evidenceFor = (evidenceId: string) => result?.evidence?.find(item => item.id === evidenceId)
  const provenanceText = (evidenceId: string) => {
    const item = evidenceFor(evidenceId)
    if (!item) return '来源未记录'
    if (item.simulated) return '模拟来源，细节未核实'
    return item.verification_level === 'address_verified' ? '地址已核实' : '来源可追溯，细节未核实'
  }
  const statusText = (item: Evidence) => item.status === 'ok' ? (item.source_kind === 'map_poi' || item.kind === 'poi' ? '地图 POI' : '网页来源') : item.status === 'empty' ? '无结果' : item.status === 'failed' ? '查询失败' : '服务不可用'
  const sourceText = (item: Evidence) => item.source || (item.kind === 'poi' ? '地图 POI' : item.kind === 'web' ? '网页来源' : '来源未知')
  const fetchedText = (item: Evidence) => item.fetched_at ? new Date(item.fetched_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '查询时间未知'
  const levelText = (item: Evidence) => item.simulated ? '模拟数据' : item.verification_level === 'address_verified' ? '地址已核实' : item.verification_level === 'source_linked' ? '仅网页来源' : '未核实'

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-icon"><Compass size={23}/></span><span>远行<span className="brand-dot">.</span><small>TRAVEL AGENT</small></span></div>
      <button className="new-trip" onClick={newConversation}><Plus size={17}/> 新建对话</button>
      <div className="side-label">最近的对话</div>
      <div className="history">{saved.length ? saved.map((item, index) => <button key={item.id} className={conversation?.id === item.id ? 'history-item active' : 'history-item'} onClick={() => void openConversation(item.id)}><MapPin size={15}/><span>{item.title || `对话 ${saved.length - index}`}</span><ChevronRight size={14}/></button>) : <p className="empty-history">还没有保存的对话</p>}</div>
      <div className="side-footer"><span className="status-dot"/> 本地工作台 <span className="version">BETA 0.2</span></div>
    </aside>

    <main className="main">
      <header className="topbar"><span>你的专属旅行规划空间</span><div className="top-pill"><Sparkles size={13}/> 多 Agent 协同规划</div></header>
      <div className="content">
        {!conversation && <div className="hero"><div className="eyebrow"><span className="eyebrow-line"/> EXPLORE WITH CONFIDENCE</div><h1>每一段旅程，<br/><em>都值得认真规划。</em></h1><p>直接说出你的出发地、目的地、时间与喜好。你可以像聊天一样改行程，我们会记住上下文，并标出尚未核实的信息。</p></div>}
        {!conversation && <section className="composer"><div className="composer-title"><Sparkles size={17}/> 从哪里开始？</div><textarea value={input} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit() } }} placeholder="例如：下周从上海出发去杭州玩两天，预算 1500 元，喜欢自然风光和咖啡馆……" rows={3}/><div className="composer-bottom"><span>描述越具体，行程越贴合你的需求</span><button className="primary-btn" disabled={busy || input.trim().length < 2} onClick={() => void submit()}>{busy ? <LoaderCircle className="spin" size={17}/> : <ArrowRight size={17}/>} 开始对话</button></div></section>}
        {!conversation && <div className="examples"><span>试试这样说</span><div>{examples.map(example => <button key={example} onClick={() => setInput(example)}>{example}<ArrowRight size={13}/></button>)}</div></div>}

        {conversation && <section className="chat">
          <div className="chat-log">
            {conversation.messages.map(message => <div key={message.id} className={`bubble ${message.role}`}>{message.content}</div>)}
            {busy && <div className="bubble assistant typing"><LoaderCircle className="spin" size={14}/> {lastStage ? stageNames[lastStage] || lastStage : '正在规划'}…</div>}
            <div ref={chatEnd}/>
          </div>
          <div className="chat-composer"><input value={input} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void submit() }} placeholder="继续说出你想调整的地方，例如：第二天轻松一些，增加咖啡馆"/><button disabled={busy || input.trim().length < 2} onClick={() => void submit()} aria-label="发送"><Send size={16}/></button></div>
        </section>}

        {error && <div className="error-box"><X size={16}/>{error}</div>}

        {trip && <section className="workspace"><div className="workspace-heading"><div><div className="section-kicker">YOUR JOURNEY</div><h2>旅行方案 <span>v{trip.version}</span></h2><p>{trip.query.split('\n')[0]}</p></div><span className={`job-badge ${job?.status || ''}`}>{processing ? <LoaderCircle size={14} className="spin"/> : job?.status === 'failed' ? <X size={14}/> : <Check size={14}/>} {job?.status === 'queued' ? '等待处理' : job?.status === 'running' ? '正在规划' : job?.status === 'failed' ? '执行失败' : result?.status === 'partial' ? '部分完成' : result?.status === 'needs_clarification' ? '需补充信息' : '已完成'}</span></div>
          <div className="tabs"><button className={activeTab === 'itinerary' ? 'selected' : ''} onClick={() => setActiveTab('itinerary')}><MapPin size={16}/> 行程方案</button><button className={activeTab === 'evidence' ? 'selected' : ''} onClick={() => setActiveTab('evidence')}><ShieldCheck size={16}/> 地点证据 {result && <b>{verified}</b>}</button><button className={activeTab === 'process' ? 'selected' : ''} onClick={() => setActiveTab('process')}><Clock3 size={16}/> 执行过程</button></div>
          {activeTab === 'itinerary' && <div className="panel">{processing && <div className="notice">正在处理当前修改。下方若有行程，它属于上一版本。</div>}{processing && !result && <div className="placeholder"><LoaderCircle className="spin" size={28}/><strong>正在为你规划旅程</strong><span>完成后，行程会自动出现在这里</span></div>}{job?.status === 'failed' && <div className="notice failure">{job.error || '任务失败，请修改需求后重试'}</div>}{result?.question && <div className="notice">{result.question}</div>}{result?.summary && <p className="summary">{result.summary}</p>}{result?.verification && <p className={result.verification.uncovered_days.length ? 'coverage warn' : 'coverage'}>{result.verification.uncovered_days.length ? `${result.verification.uncovered_days.length} 天没有可追溯来源，已在提醒中标出` : `每天都有可追溯来源 · 风险 ${result.verification.risk}`}</p>}{result?.days?.map(day => <div className="day" key={day.day}><div className="day-number">DAY {String(day.day).padStart(2, '0')}</div><div className="day-content"><h3>{day.city}</h3>{day.activities.length ? day.activities.map((activity, index) => <div className="activity" key={`${activity.evidence_id}-${index}`}><span className="activity-marker"/><div><strong>{activity.title}</strong><div className="activity-meta">{activity.start} · {activity.duration} · {activity.cost}</div><p>{activity.description}</p><span className="activity-transport">交通：{activity.transport}</span><small><ShieldCheck size={12}/> {provenanceText(activity.evidence_id)} · {activity.evidence_id}{sourceFor(activity.evidence_id, activity.title) && <a href={sourceFor(activity.evidence_id, activity.title)} target="_blank" rel="noopener noreferrer">查看来源</a>}</small></div></div>) : <p className="muted">这一天还没有可核实的地点安排</p>}</div></div>)}{result?.notes?.length ? <div className="notes"><strong>出行提醒</strong>{result.notes.map((note, index) => <p key={index}>· {note}</p>)}</div> : null}</div>}
          {activeTab === 'evidence' && <div className="panel evidence-panel">{result?.evidence?.length ? result.evidence.map(item => <div className="evidence-card" key={item.id}>
            <div className="evidence-head"><div><Database size={15}/><strong>{item.query}</strong><span>{item.city}</span></div><span className={`evidence-status ${item.status}`}>{statusText(item)}</span></div>
            <div className="evidence-meta">
              <span>来源：{sourceText(item)}</span>
              <span>查询于 {fetchedText(item)}</span>
              <span className={item.simulated || item.verification_level === 'unverified' ? 'meta-warn' : ''}>{levelText(item)}</span>
              {item.reused && <span>沿用上次查询</span>}
            </div>
            {item.items?.map((poi, index) => <p key={index}><MapPin size={13}/><b>{poi.url ? <a href={poi.url} target="_blank" rel="noopener noreferrer">{poi.name}</a> : poi.name}</b> {poi.address}</p>)}
            {item.reason && <small>原因：{item.reason}</small>}
          </div>) : <div className="placeholder">查询完成后会显示证据来源</div>}</div>}
          {activeTab === 'process' && <div className="panel process-panel">{events.length ? events.map(event => <div className="event" key={event.id}><span className={`event-dot ${event.kind}`}/><div><strong>{event.payload.stage ? stageNames[event.payload.stage] || event.payload.stage : event.kind === 'completed' ? '任务完成' : event.kind === 'failed' ? '执行失败' : '任务状态'}</strong><p>{event.payload.message}</p></div><time>{new Date(event.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time></div>) : <div className="placeholder">正在读取执行记录</div>}{result?.model_calls?.length ? <div className="model-line">模型调用：{result.model_calls.map(call => `${call.role} · ${call.provider}/${call.model}`).join(' → ')}</div> : null}</div>}
        </section>}
        <footer>远行 · 让计划有依据，让旅途更从容。<span>地点信息以查询结果为准，出行前请再次确认营业与票务。</span></footer>
      </div>
    </main>
  </div>
}

export default App
