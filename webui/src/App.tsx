import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelJob,
  getAccountStats,
  getAccounts,
  getJobs,
  startHealth,
  startPromotion,
  startQuota,
  startRegistration,
  watchJob,
} from "./api";
import { accountHealthLabel, badgeTone, eventText, isJobTerminal } from "./presentation";
import type {
  AccountPage,
  AccountStats,
  BackendJob,
  JobEvent,
  RegistrationSource,
} from "./types";
import "./styles.css";

const emptyPage: AccountPage = { items: [], page: 1, pageSize: 50, total: 0 };
const emptyStats: AccountStats = { total: 0, trial: 0, registered: 0, attention: 0 };

function Badge({ value }: { value: string }) {
  return <span className={`badge badge-${badgeTone(value)}`}>{value || "—"}</span>;
}

type SidebarSection = {
  title: string;
  items: { icon: string; label: string; action?: () => void }[];
};

export default function App() {
  const [theme, setTheme] = useState<"light" | "dark">(
    () => (localStorage.getItem("workbench-theme") as "light" | "dark") || "light",
  );
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [accounts, setAccounts] = useState<AccountPage>(emptyPage);
  const [stats, setStats] = useState<AccountStats>(emptyStats);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [planType, setPlanType] = useState("");
  const [promotion, setPromotion] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState("");
  const [jobs, setJobs] = useState<BackendJob[]>([]);
  const [events, setEvents] = useState<Record<string, JobEvent[]>>({});
  const [activeJobId, setActiveJobId] = useState("");
  const [source, setSource] = useState<RegistrationSource>("pool");
  const [count, setCount] = useState(1);
  const [workers, setWorkers] = useState(4);
  const watchers = useRef<Record<string, () => void>>({});
  const regPanelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("workbench-theme", theme);
  }, [theme]);

  const loadAccounts = useCallback(async () => {
    setLoading(true);
    try {
      setAccounts(await getAccounts({
        q: query, status, planType, promotionStatus: promotion, page, pageSize,
      }));
      setNotice("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "账号读取失败");
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, planType, promotion, query, status]);

  const loadStats = useCallback(async () => {
    try {
      setStats(await getAccountStats());
    } catch { /* silent */ }
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const current = await getJobs();
      setJobs(current);
      current.filter((job) => !isJobTerminal(job)).forEach((job) => attachWatcher(job.id));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "任务读取失败");
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(loadAccounts, 180);
    return () => window.clearTimeout(timer);
  }, [loadAccounts]);

  useEffect(() => {
    void loadStats();
    const timer = window.setInterval(loadStats, 5000);
    return () => window.clearInterval(timer);
  }, [loadStats]);

  useEffect(() => {
    void loadJobs();
    const timer = window.setInterval(loadJobs, 2500);
    return () => {
      window.clearInterval(timer);
      Object.values(watchers.current).forEach((close) => close());
    };
  }, [loadJobs]);

  function attachWatcher(jobId: string) {
    if (watchers.current[jobId]) return;
    watchers.current[jobId] = watchJob(
      jobId,
      (event) => {
        setEvents((current) => ({
          ...current,
          [jobId]: [...(current[jobId] || []), event].slice(-1000),
        }));
        if (event.type === "state") {
          void loadJobs();
          void loadAccounts();
          void loadStats();
        }
      },
      () => { delete watchers.current[jobId]; void loadJobs(); },
    );
  }

  async function run(action: () => Promise<BackendJob>) {
    try {
      const job = await action();
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setActiveJobId(job.id);
      attachWatcher(job.id);
      setNotice("任务已提交");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "任务提交失败");
    }
  }

  function scrollToRegistration() {
    regPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function selectAllFiltered() {
    setSelected((current) => {
      const next = new Set(current);
      const allSelected = accounts.items.every((item) => next.has(item.id));
      accounts.items.forEach((item) => {
        if (allSelected) next.delete(item.id);
        else next.add(item.id);
      });
      return next;
    });
  }

  function clearSelection() { setSelected(new Set()); }

  async function cancelAllRunning() {
    const running = jobs.filter((job) => !isJobTerminal(job));
    for (const job of running) {
      try { await cancelJob(job.id); } catch { /* continue */ }
    }
    void loadJobs();
    setNotice("已取消所有运行中任务");
  }

  const selectedIds = useMemo(() => Array.from(selected), [selected]);
  const totalPages = Math.max(1, Math.ceil(accounts.total / pageSize));
  const activeEvents = events[activeJobId] || [];

  function toggleSelected(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  const sidebarSections: SidebarSection[] = [
    {
      title: "一键操作",
      items: [
        { icon: "👤", label: "一键注册", action: () => { setSource("pool"); scrollToRegistration(); } },
        { icon: "💬", label: "一键接码", action: () => { setSource("phone"); scrollToRegistration(); } },
        { icon: "📊", label: "账号测活", action: () => run(() => startHealth(selectedIds.length ? selectedIds : accounts.items.map((a) => a.id), workers)) },
        { icon: "🔗", label: "查优惠", action: () => run(() => startPromotion(selectedIds, workers)) },
      ],
    },
    {
      title: "支付管理",
      items: [
        { icon: "⛓", label: "打开支付链接" },
        { icon: "📋", label: "批量协议支付" },
      ],
    },
    {
      title: "邮箱管理",
      items: [
        { icon: "✉", label: "导入邮箱" },
        { icon: "📥", label: "查看收件箱" },
        { icon: "🔄", label: "邮箱换绑" },
      ],
    },
    {
      title: "账号管理",
      items: [
        { icon: "⬇", label: "一键导入" },
        { icon: "⬆", label: "导出账号" },
        { icon: "🗑", label: "删除选中", action: clearSelection },
      ],
    },
    {
      title: "项目设置",
      items: [
        { icon: "⟳", label: "刷新", action: () => { void loadAccounts(); void loadStats(); } },
        { icon: "⚙", label: "设置" },
        { icon: theme === "light" ? "☀" : "🌙", label: "切换主题", action: () => setTheme(theme === "light" ? "dark" : "light") },
        { icon: "✕", label: "取消批次", action: cancelAllRunning },
      ],
    },
  ];

  return (
    <div className={`shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">GRT</div>
          {!sidebarCollapsed && <div><strong>GPT Register Tool</strong><small>Web Workbench</small></div>}
        </div>
        <nav className="sidebar-nav">
          {sidebarSections.map((section) => (
            <div className="sidebar-section" key={section.title}>
              {!sidebarCollapsed && <div className="sidebar-section-header">{section.title}</div>}
              {section.items.map((item) => (
                <button
                  className="nav-item"
                  type="button"
                  key={item.label}
                  onClick={item.action}
                  title={item.label}
                >
                  <span className="nav-icon">{item.icon}</span>
                  {!sidebarCollapsed && <span className="nav-label">{item.label}</span>}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-spacer" />
        <div className="local-only">{!sidebarCollapsed && "仅监听 127.0.0.1"}</div>
        <button className="nav-item" type="button" onClick={() => setSidebarCollapsed((value) => !value)}>
          <span className="nav-icon">{sidebarCollapsed ? "›" : "‹"}</span>{!sidebarCollapsed && <span className="nav-label">收起侧栏</span>}
        </button>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <h1>账号工作台</h1>
            <p>账号状态、注册与健康任务</p>
          </div>
          <div className="topbar-actions">
            <button type="button" onClick={() => setTheme(theme === "light" ? "dark" : "light")}>
              {theme === "light" ? "深色" : "浅色"}
            </button>
            <button type="button" onClick={() => { void loadAccounts(); void loadStats(); }}>刷新</button>
          </div>
        </header>

        {notice && <div className="notice">{notice}</div>}

        <section className="stat-cards">
          <div className="stat-card"><span className="stat-label">总记录</span><span className="stat-value">{stats.total}</span></div>
          <div className="stat-card"><span className="stat-label">有试用</span><span className="stat-value">{stats.trial}</span></div>
          <div className="stat-card"><span className="stat-label">已注册</span><span className="stat-value">{stats.registered}</span></div>
          <div className="stat-card"><span className="stat-label">异常/待处理</span><span className="stat-value">{stats.attention}</span></div>
        </section>

        <section className="action-grid" ref={regPanelRef}>
          <div className="panel action-panel">
            <div className="panel-heading"><div><h2>注册任务</h2><p>通过共享 CLI Planner 提交</p></div></div>
            <div className="form-row">
              <label>来源<select value={source} onChange={(event) => setSource(event.target.value as RegistrationSource)}>
                <option value="pool">邮箱池</option><option value="phone">手机号</option>
                <option value="cfworker">CFWorker</option><option value="remail">ReMail</option>
                <option value="smailr">Smailr</option>
              </select></label>
              <label>数量<input type="number" min="1" max="100" value={count} onChange={(event) => setCount(Number(event.target.value))} /></label>
              <label>并发<input type="number" min="1" max="16" value={workers} onChange={(event) => setWorkers(Number(event.target.value))} /></label>
              <button className="primary" type="button" onClick={() => run(() => startRegistration({
                source, count, workers, disable2Fa: false, checkPromotion: false,
              }))}>开始注册</button>
            </div>
          </div>
          <div className="panel action-panel">
            <div className="panel-heading"><div><h2>账号健康</h2><p>已选择 {selectedIds.length} 个账号</p></div></div>
            <div className="button-row">
              <button type="button" disabled={!selectedIds.length} onClick={() => run(() => startHealth(selectedIds, workers))}>深度测活</button>
              <button type="button" disabled={!selectedIds.length} onClick={() => run(() => startPromotion(selectedIds, workers))}>套餐 / 优惠</button>
              <button type="button" disabled={selectedIds.length !== 1} onClick={() => run(() => startQuota(selectedIds[0]))}>额度</button>
            </div>
          </div>
        </section>

        <section className="panel accounts-panel">
          <div className="panel-heading accounts-heading">
            <div><h2>账号列表</h2><p>{accounts.total} 个账号，已选择 {selectedIds.length} 个</p></div>
            <div className="filters">
              <input aria-label="搜索账号" placeholder="搜索邮箱、session、token、备注" value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} />
              <select aria-label="状态筛选" value={status} onChange={(event) => { setStatus(event.target.value); setPage(1); }}>
                <option value="">全部</option><option value="active">有试用</option><option value="failed">待处理</option>
              </select>
              <input aria-label="每页条数" placeholder="每页" type="number" min="10" max="200" value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value) || 50); setPage(1); }} style={{ width: 70 }} />
              <span className="page-status">{accounts.total} 条 · 第 {page}/{totalPages} 页</span>
              <button type="button" onClick={selectAllFiltered} title="全选当前页">全选</button>
              <button type="button" onClick={clearSelection} title="清空选择">清空</button>
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="col-check"><input type="checkbox" checked={accounts.items.length > 0 && accounts.items.every((item) => selected.has(item.id))} onChange={selectAllFiltered} /></th>
                  <th>创建时间</th>
                  <th>邮箱</th>
                  <th>套餐类型</th>
                  <th>注册区</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>AT</th>
                  <th>RT</th>
                  <th>2FA</th>
                  <th>优惠状态</th>
                  <th>来源</th>
                  <th>更新时间</th>
                </tr>
              </thead>
              <tbody>
                {accounts.items.map((account) => (
                  <tr key={account.id} className={selected.has(account.id) ? "selected" : ""}>
                    <td className="col-check"><input type="checkbox" checked={selected.has(account.id)} onChange={() => toggleSelected(account.id)} /></td>
                    <td className="col-time">{account.createdAt || "—"}</td>
                    <td><strong>{account.email}</strong><small>{account.id.slice(0, 12)}</small></td>
                    <td>{account.planType || account.accountType || "—"}</td>
                    <td>{account.registrationCountry || "—"}</td>
                    <td>{account.sessionType || account.registerMethod || "—"}</td>
                    <td><Badge value={accountHealthLabel(account)} /></td>
                    <td><Badge value={account.accessTokenPresent ? "已获取" : "缺失"} /></td>
                    <td><Badge value={account.refreshTokenPresent ? "RT 有效" : "无 RT"} /></td>
                    <td><Badge value={account.totpPresent ? "已设置" : "未设置"} /></td>
                    <td><Badge value={account.promotionStatus} /></td>
                    <td>{account.mailboxProvider || account.mailboxSource || "—"}</td>
                    <td className="col-time">{account.updatedAt || "—"}</td>
                  </tr>
                ))}
                {!loading && accounts.items.length === 0 && <tr><td className="empty" colSpan={13}>暂无账号</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="pager">
            <button type="button" disabled={page <= 1} onClick={() => setPage(1)} title="首页">⏮</button>
            <button type="button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</button>
            <span>第 {page} / {totalPages} 页</span>
            <button type="button" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>下一页</button>
            <button type="button" disabled={page >= totalPages} onClick={() => setPage(totalPages)} title="末页">⏭</button>
          </div>
        </section>

        <section className="lower-grid">
          <div className="panel jobs-panel">
            <div className="panel-heading"><div><h2>后台任务</h2><p>单写任务队列</p></div></div>
            <div className="job-list">
              {jobs.map((job) => <button type="button" className={`job-row ${job.id === activeJobId ? "active" : ""}`} key={job.id} onClick={() => setActiveJobId(job.id)}>
                <div><strong>{job.kind}</strong><small>{new Date(job.createdAt).toLocaleString()}</small></div>
                <Badge value={job.state} />
                {!isJobTerminal(job) && <span className="cancel" onClick={(event) => { event.stopPropagation(); void cancelJob(job.id); }}>取消</span>}
              </button>)}
              {!jobs.length && <div className="empty">暂无任务</div>}
            </div>
          </div>
          <div className="panel log-panel">
            <div className="panel-heading"><div><h2>实时日志</h2><p>{activeJobId ? `任务 ${activeJobId.slice(0, 8)}` : "选择任务查看"}</p></div></div>
            <div className="log-stream">
              {activeEvents.map((event) => <div key={`${event.sequence}-${event.type}`}><span>{event.type}</span>{eventText(event.data)}</div>)}
              {!activeEvents.length && <div className="log-placeholder">等待任务事件…</div>}
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
