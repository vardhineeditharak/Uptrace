// Uptrace — Better Stack Midnight SRE Console Controller with Per-Monitor Scheduling

let currentResults = [];
let activeCategory = 'all';
let activeEnvironment = localStorage.getItem('uptrace_env') || 'all';

// Presets Dictionary with Custom Ping Intervals & Methods
const PRESETS = {
    render_prod: {
        name: 'My Render Web App (Prod)',
        environment: 'production',
        type: 'http',
        method: 'GET',
        category: 'Web Applications',
        url: 'https://my-app.onrender.com/health',
        expected_status: '200, 301, 302',
        interval_seconds: 60,
        timeout: 8,
        headers: '',
        body: '',
        keep_alive_note: 'Prevents Render 15-min free tier inactivity spin-down'
    },
    vercel_head: {
        name: 'Vercel Edge Deployment (Prod)',
        environment: 'production',
        type: 'http',
        method: 'HEAD',
        category: 'Web Applications',
        url: 'https://jsonplaceholder.typicode.com/posts/1',
        expected_status: '200',
        interval_seconds: 300,
        timeout: 5,
        headers: '',
        body: '',
        keep_alive_note: 'Lightweight HEAD health ping for Next.js edge deployment'
    },
    graphql_post: {
        name: 'GraphQL API Gateway (Prod)',
        environment: 'production',
        type: 'http',
        method: 'POST',
        category: 'APIs & Microservices',
        url: 'https://httpbin.org/post',
        expected_status: '200',
        interval_seconds: 300,
        timeout: 6,
        headers: '{"Content-Type": "application/json"}',
        body: '{"query": "{ health { status } }"}',
        keep_alive_note: 'POST payload assertion for GraphQL gateway'
    },
    supabase_prod: {
        name: 'Supabase PostgreSQL (Prod)',
        environment: 'production',
        type: 'http',
        method: 'GET',
        category: 'Databases (Keep-Alive)',
        url: 'https://xyzcompany.supabase.co/rest/v1/',
        expected_status: '200, 401',
        interval_seconds: 21600,
        timeout: 6,
        headers: '',
        body: '',
        keep_alive_note: 'Prevents Supabase 7-day project pausing'
    },
    postgres_prod: {
        name: 'Primary PostgreSQL Port 5432',
        environment: 'production',
        type: 'tcp_db',
        category: 'Databases (Keep-Alive)',
        host: 'db.mycompany.com',
        port: 5432,
        interval_seconds: 3600,
        timeout: 5,
        keep_alive_note: 'Direct TCP socket keep-alive ping for Postgres'
    },
    localhost_dev: {
        name: 'Local Dev Server (localhost:3000)',
        environment: 'development',
        type: 'http',
        method: 'GET',
        category: 'Web Applications',
        url: 'http://localhost:3000/api/health',
        expected_status: '200',
        interval_seconds: 120,
        timeout: 4,
        headers: '',
        body: '',
        keep_alive_note: 'Local development server monitoring'
    },
    redis_staging: {
        name: 'Staging Redis Cache (TCP 6379)',
        environment: 'staging',
        type: 'tcp_db',
        category: 'Databases (Keep-Alive)',
        host: 'staging-redis.internal',
        port: 6379,
        interval_seconds: 900,
        timeout: 5,
        keep_alive_note: 'Staging Redis connectivity monitor'
    }
};

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    initEnvironmentSwitcher();
    fetchAuthState();
    fetchLatestStatus();
    startSchedulerPoller();
});

function setupEventListeners() {
    // Environment switcher
    const envButtons = document.querySelectorAll('.env-btn');
    envButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            envButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeEnvironment = btn.getAttribute('data-env');
            localStorage.setItem('uptrace_env', activeEnvironment);
            updateFilteredView();
        });
    });

    // Category tabs
    const tabButtons = document.querySelectorAll('.tab-btn');
    tabButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            tabButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            activeCategory = btn.getAttribute('data-category');
            renderServicesGrid();
        });
    });
}

function initEnvironmentSwitcher() {
    const activeBtn = document.querySelector(`.env-btn[data-env="${activeEnvironment}"]`);
    if (activeBtn) {
        document.querySelectorAll('.env-btn').forEach(b => b.classList.remove('active'));
        activeBtn.classList.add('active');
    }
}

// Check current user authentication status via /api/me
async function fetchAuthState() {
    const authContainer = document.getElementById('authNavContainer');
    if (!authContainer) return;

    try {
        const res = await fetch('/api/me');
        const data = await res.json();

        if (data.authenticated && data.user) {
            const user = data.user;
            const avatarUrl = user.avatar_url || 'https://github.com/identicons/uptrace.png';
            const username = user.name || user.username || 'SRE Engineer';
            
            authContainer.innerHTML = `
                <div class="user-profile-badge" title="Logged in as ${escapeHtml(username)}">
                    <img src="${escapeHtml(avatarUrl)}" alt="Avatar" class="user-avatar" onerror="this.src='https://github.com/github.png'">
                    <span class="user-name">${escapeHtml(username)}</span>
                    <a href="/logout" class="btn-signout" title="Sign Out">Sign Out</a>
                </div>
            `;
        } else {
            authContainer.innerHTML = `
                <a href="/login" class="btn btn-secondary btn-github-login" id="githubLoginBtn" title="Sign in with GitHub">
                    <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor">
                        <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>
                    </svg>
                    <span>Sign In</span>
                </a>
            `;
        }
    } catch (err) {
        console.debug('Auth fetch note:', err);
    }
}

// Background poller to refresh UI status smoothly every 8 seconds
function startSchedulerPoller() {
    setInterval(async () => {
        try {
            const res = await fetch('/api/status-latest');
            const data = await res.json();
            currentResults = data.results || [];
            updateFilteredView(data.timestamp);
        } catch (err) {
            console.debug('Polling note:', err);
        }
    }, 8000);
}

function formatInterval(seconds) {
    const sec = parseInt(seconds) || 300;
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.round(sec / 60)}m`;
    if (sec < 86400) return `${Math.round(sec / 3600)}h`;
    return `${Math.round(sec / 86400)}d`;
}

// Fetch cached latest status on initial page load
async function fetchLatestStatus() {
    try {
        const res = await fetch('/api/status-latest');
        const data = await res.json();
        currentResults = data.results || [];
        updateFilteredView(data.timestamp);
    } catch (err) {
        console.warn('Could not fetch cached status, running full check...', err);
        runLiveHealthCheck();
    }
}

// Execute live check across all endpoints on demand
async function runLiveHealthCheck(isBackground = false) {
    const refreshBtn = document.getElementById('refreshBtn');
    if (!isBackground && refreshBtn) {
        refreshBtn.disabled = true;
        refreshBtn.innerHTML = 'Pinging...';
    }

    try {
        const res = await fetch('/api/health-check');
        const data = await res.json();
        currentResults = data.results || [];
        updateFilteredView(data.timestamp);
        if (!isBackground) {
            showToast('All keep-alive checks executed');
        }
    } catch (err) {
        console.error('Health check error:', err);
        showToast('Health check failed to connect to backend', 'error');
    } finally {
        if (!isBackground && refreshBtn) {
            refreshBtn.disabled = false;
            refreshBtn.innerHTML = 'Ping All';
        }
    }
}

// Updates environment badge counts and filtered view
function updateFilteredView(timestamp) {
    const allCount = currentResults.length;
    const prodCount = currentResults.filter(r => (r.environment || 'production') === 'production').length;
    const devCount = currentResults.filter(r => (r.environment || 'production') === 'development').length;
    const stagingCount = currentResults.filter(r => (r.environment || 'production') === 'staging').length;

    document.getElementById('allCountBadge').textContent = allCount;
    document.getElementById('prodCountBadge').textContent = prodCount;
    document.getElementById('devCountBadge').textContent = devCount;
    document.getElementById('stagingCountBadge').textContent = stagingCount;

    let envResults = currentResults;
    if (activeEnvironment !== 'all') {
        envResults = currentResults.filter(r => (r.environment || 'production') === activeEnvironment);
    }

    const total = envResults.length;
    const activeMonitors = envResults.filter(r => r.status !== 'paused');
    const healthy = activeMonitors.filter(r => r.status === 'healthy').length;
    const unhealthy = activeMonitors.length - healthy;
    const dbCount = envResults.filter(r => (r.category || '').toLowerCase().includes('database') || r.type === 'tcp_db').length;
    const appCount = envResults.filter(r => (r.category || '').toLowerCase().includes('web') || (r.category || '').toLowerCase().includes('api')).length;

    let overallUptime = 100.0;
    if (envResults.length > 0) {
        const uptimes = envResults.map(r => (r.uptime_stats && r.uptime_stats.uptime_24h) || 100);
        overallUptime = (uptimes.reduce((a, b) => a + b, 0) / uptimes.length).toFixed(1);
    }

    const envLabel = activeEnvironment === 'all' ? 'All' : activeEnvironment.toUpperCase();
    document.getElementById('uptimeCardTitle').textContent = `${envLabel} 24h Uptime`;
    document.getElementById('overallUptime').textContent = `${overallUptime}%`;
    document.getElementById('uptimeCardFooter').textContent = `Across ${total} ${activeEnvironment === 'all' ? '' : activeEnvironment} monitor(s)`;

    document.getElementById('healthyCount').textContent = healthy;
    document.getElementById('dbCount').textContent = dbCount;
    document.getElementById('appCount').textContent = appCount;

    const incidentBanner = document.getElementById('incidentBanner');
    if (unhealthy === 0) {
        if (incidentBanner) incidentBanner.classList.add('hidden');
    } else {
        if (incidentBanner) {
            incidentBanner.classList.remove('hidden');
            document.getElementById('incidentBannerText').textContent = 
                `${unhealthy} service(s) in ${envLabel} currently experiencing degradation. Automated alerts dispatched.`;
        }
    }

    if (timestamp) {
        document.getElementById('lastCheckedTime').textContent = timestamp;
    }

    renderServicesGrid();
}

// Render cards into services grid
function renderServicesGrid() {
    const grid = document.getElementById('servicesGrid');
    if (!grid) return;

    let filtered = currentResults;
    if (activeEnvironment !== 'all') {
        filtered = currentResults.filter(r => (r.environment || 'production') === activeEnvironment);
    }

    if (activeCategory !== 'all') {
        filtered = filtered.filter(r => r.category === activeCategory);
    }

    if (filtered.length === 0) {
        grid.innerHTML = `
            <div class="loading-state">
                <p>No monitors found for Environment: <strong>${activeEnvironment.toUpperCase()}</strong> | Category: <strong>${activeCategory}</strong></p>
                <button class="btn btn-add" style="margin-top: 14px;" onclick="openAddMonitorModal()">+ Add Monitor</button>
            </div>
        `;
        return;
    }

    grid.innerHTML = '';
    filtered.forEach(svc => {
        grid.appendChild(createServiceCard(svc));
    });
}

function createServiceCard(svc) {
    const card = document.createElement('div');
    card.className = `service-card ${svc.status}`;

    let statusBadgeClass = 'healthy';
    let statusText = 'Operational';

    if (svc.status === 'unhealthy') {
        statusBadgeClass = 'unhealthy';
        statusText = 'Degraded';
    } else if (svc.status === 'error') {
        statusBadgeClass = 'error';
        statusText = 'Down';
    } else if (svc.status === 'paused') {
        statusBadgeClass = 'paused';
        statusText = 'Paused';
    }

    let latencyClass = 'latency-good';
    if (svc.latency_ms > 1000) latencyClass = 'latency-bad';
    else if (svc.latency_ms > 400) latencyClass = 'latency-warn';

    const scheduleFormatted = formatInterval(svc.interval_seconds || 300);

    const env = (svc.environment || 'production').toLowerCase();
    const envBadgeClass = env === 'production' ? 'prod' : (env === 'development' ? 'dev' : 'staging');
    const envBadgeText = env.toUpperCase();

    // Protocol & Method Badges
    let methodBadgeHtml = '';
    if (svc.type === 'tcp_db') {
        methodBadgeHtml = `<span class="card-type-tag">TCP DB</span>`;
    } else {
        const method = (svc.method || 'GET').toUpperCase();
        const methodClass = `method-${method.toLowerCase()}`;
        methodBadgeHtml = `<span class="method-badge ${methodClass}">${method}</span>`;
    }

    // SSL Badge
    let sslBadgeHtml = '';
    if (svc.ssl_info) {
        const isExpiring = svc.ssl_info.days_left < 14;
        sslBadgeHtml = `<span class="ssl-badge ${isExpiring ? 'expiring' : ''}">SSL: ${svc.ssl_info.days_left}d</span>`;
    }

    // Uptime blocks (30 blocks)
    const stats = svc.uptime_stats || { uptime_24h: 100.0, blocks: Array(30).fill('up') };
    const blocksHtml = (stats.blocks || []).map(b => `<div class="uptime-block ${b}" title="Status: ${b.toUpperCase()}"></div>`).join('');

    card.innerHTML = `
        <div>
            <div class="card-top">
                <div class="card-top-left">
                    <span class="env-badge ${envBadgeClass}">${envBadgeText}</span>
                    ${methodBadgeHtml}
                    <span class="schedule-badge" title="Ping Schedule: Every ${scheduleFormatted}">⏱️ ${scheduleFormatted}</span>
                    ${sslBadgeHtml}
                </div>
                <div class="card-top-right">
                    <span class="card-badge ${statusBadgeClass}">● ${statusText}</span>
                    <div class="card-actions">
                        <button class="action-icon-btn" onclick="togglePauseMonitor('${svc.id}')" title="${svc.status === 'paused' ? 'Resume Monitor' : 'Pause Monitor'}">
                            ${svc.status === 'paused' ? '▶' : '⏸'}
                        </button>
                        <button class="action-icon-btn" onclick="openEditMonitorModal('${svc.id}')" title="Edit Monitor & Schedule">
                            ✎
                        </button>
                        <button class="action-icon-btn delete-btn" onclick="deleteMonitor('${svc.id}')" title="Delete Monitor">
                            ✕
                        </button>
                    </div>
                </div>
            </div>

            <h3 class="card-title">${escapeHtml(svc.name)}</h3>
            <div class="card-target">${escapeHtml(svc.target || '')}</div>
            
            ${svc.keep_alive_note ? `
            <div class="keep-alive-banner">
                <span>⚡</span>
                <span>${escapeHtml(svc.keep_alive_note)}</span>
            </div>` : ''}

            <!-- 30-Check History Bar -->
            <div class="uptime-section">
                <div class="uptime-header">
                    <span>Uptime (30 checks)</span>
                    <span class="uptime-pct">${stats.uptime_24h}%</span>
                </div>
                <div class="uptime-bar">
                    ${blocksHtml}
                </div>
            </div>
        </div>

        <div>
            <div class="card-metrics">
                <div class="metric-col">
                    <span class="label">Response</span>
                    <span class="val ${latencyClass}">${svc.latency_ms || 0}ms</span>
                </div>
                <div class="metric-col">
                    <span class="label">Status</span>
                    <span class="val">${escapeHtml(String(svc.status_code || '-'))}</span>
                </div>
                <div class="metric-col">
                    <span class="label">Schedule</span>
                    <span class="val" style="font-size: 11px;">Every ${scheduleFormatted}</span>
                </div>
            </div>

            ${svc.error && svc.status !== 'paused' ? `<div class="card-error">Error: ${escapeHtml(svc.error)}</div>` : ''}
        </div>
    `;

    return card;
}

// --- QUICK PRESETS ---
function applyPreset(presetKey) {
    const preset = PRESETS[presetKey];
    if (!preset) return;

    openAddMonitorModal();
    document.getElementById('monName').value = preset.name;
    document.getElementById('monEnvironment').value = preset.environment;
    document.getElementById('monType').value = preset.type;
    document.getElementById('monCategory').value = preset.category;
    document.getElementById('monInterval').value = String(preset.interval_seconds || 300);
    document.getElementById('monTimeout').value = preset.timeout;
    document.getElementById('monKeepAliveNote').value = preset.keep_alive_note;

    if (preset.type === 'tcp_db') {
        document.getElementById('monHost').value = preset.host;
        document.getElementById('monPort').value = preset.port;
    } else {
        document.getElementById('monMethod').value = preset.method || 'GET';
        document.getElementById('monUrl').value = preset.url;
        document.getElementById('monExpectedStatus').value = preset.expected_status;
        document.getElementById('monHeaders').value = preset.headers || '';
        document.getElementById('monBody').value = preset.body || '';
    }

    handleMonitorTypeChange();
    handleMethodChange();
    showToast(`Preset loaded with ${formatInterval(preset.interval_seconds)} ping schedule`);
}

// --- MODAL & CRUD HANDLERS ---

function openAddMonitorModal() {
    document.getElementById('modalTitle').textContent = '+ Add New Monitor';
    document.getElementById('editMonitorId').value = '';
    document.getElementById('monName').value = '';
    document.getElementById('monEnvironment').value = activeEnvironment !== 'all' ? activeEnvironment : 'production';
    document.getElementById('monType').value = 'http';
    document.getElementById('monMethod').value = 'GET';
    document.getElementById('monCategory').value = 'Web Applications';
    document.getElementById('monInterval').value = '300';
    document.getElementById('monUrl').value = '';
    document.getElementById('monHost').value = '';
    document.getElementById('monPort').value = '5432';
    document.getElementById('monExpectedStatus').value = '200';
    document.getElementById('monHeaders').value = '';
    document.getElementById('monBody').value = '';
    document.getElementById('monTimeout').value = '6';
    document.getElementById('monKeepAliveNote').value = '';
    
    handleMonitorTypeChange();
    handleMethodChange();
    document.getElementById('monitorModal').classList.remove('hidden');
}

function openEditMonitorModal(svcId) {
    const svc = currentResults.find(s => s.id === svcId);
    if (!svc) return;

    document.getElementById('modalTitle').textContent = 'Edit Monitor & Ping Schedule';
    document.getElementById('editMonitorId').value = svc.id;
    document.getElementById('monName').value = svc.name || '';
    document.getElementById('monEnvironment').value = svc.environment || 'production';
    document.getElementById('monType').value = svc.type || 'http';
    document.getElementById('monCategory').value = svc.category || 'Web Applications';
    document.getElementById('monInterval').value = String(svc.interval_seconds || 300);
    
    if (svc.type === 'tcp_db') {
        const parts = (svc.target || '').split(':');
        document.getElementById('monHost').value = parts[0] || '';
        document.getElementById('monPort').value = parts[1] || '5432';
    } else {
        document.getElementById('monMethod').value = (svc.method || 'GET').toUpperCase();
        document.getElementById('monUrl').value = svc.target || '';
        document.getElementById('monExpectedStatus').value = svc.expected_status ? (Array.isArray(svc.expected_status) ? svc.expected_status.join(', ') : svc.expected_status) : '200';
        document.getElementById('monHeaders').value = svc.headers ? JSON.stringify(svc.headers) : '';
        document.getElementById('monBody').value = svc.body || '';
    }

    document.getElementById('monTimeout').value = svc.timeout || 6;
    document.getElementById('monKeepAliveNote').value = svc.keep_alive_note || '';

    handleMonitorTypeChange();
    handleMethodChange();
    document.getElementById('monitorModal').classList.remove('hidden');
}

function closeMonitorModal() {
    document.getElementById('monitorModal').classList.add('hidden');
}

function handleMonitorTypeChange() {
    const type = document.getElementById('monType').value;
    const httpGroup = document.getElementById('httpFieldsGroup');
    const tcpGroup = document.getElementById('tcpFieldsGroup');
    const urlInput = document.getElementById('monUrl');
    const hostInput = document.getElementById('monHost');

    if (type === 'tcp_db') {
        httpGroup.classList.add('hidden');
        tcpGroup.classList.remove('hidden');
        urlInput.removeAttribute('required');
        hostInput.setAttribute('required', 'required');
    } else {
        httpGroup.classList.remove('hidden');
        tcpGroup.classList.add('hidden');
        urlInput.setAttribute('required', 'required');
        hostInput.removeAttribute('required');
    }
}

function handleMethodChange() {
    const method = document.getElementById('monMethod').value;
    const bodyGroup = document.getElementById('bodyFieldGroup');
    // Show body field prominently for POST, PUT, PATCH, DELETE
    if (bodyGroup) {
        if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
            bodyGroup.style.display = 'block';
        } else {
            bodyGroup.style.display = 'block'; // Keep accessible for all HTTP methods
        }
    }
}

async function handleSaveMonitor(e) {
    e.preventDefault();
    const saveBtn = document.getElementById('saveMonitorBtn');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';

    const editId = document.getElementById('editMonitorId').value;
    const type = document.getElementById('monType').value;

    const payload = {
        name: document.getElementById('monName').value.trim(),
        environment: document.getElementById('monEnvironment').value,
        type: type,
        category: document.getElementById('monCategory').value,
        interval_seconds: parseInt(document.getElementById('monInterval').value) || 300,
        keep_alive_note: document.getElementById('monKeepAliveNote').value.trim(),
        timeout: parseInt(document.getElementById('monTimeout').value) || 6
    };

    if (type === 'tcp_db') {
        payload.host = document.getElementById('monHost').value.trim();
        payload.port = parseInt(document.getElementById('monPort').value) || 5432;
    } else {
        payload.method = document.getElementById('monMethod').value.toUpperCase();
        payload.url = document.getElementById('monUrl').value.trim();
        payload.expected_status = document.getElementById('monExpectedStatus').value.trim();
        payload.headers = document.getElementById('monHeaders').value.trim();
        payload.body = document.getElementById('monBody').value;
    }

    try {
        const url = editId ? `/api/monitors/${encodeURIComponent(editId)}` : '/api/monitors';
        const method = editId ? 'PUT' : 'POST';

        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await res.json();
        if (res.ok) {
            closeMonitorModal();
            showToast(`Monitor ${editId ? 'updated' : 'created'} (${payload.type === 'http' ? payload.method : 'TCP DB'}, ${formatInterval(payload.interval_seconds)})`);
            runLiveHealthCheck();
        } else {
            showToast(`Error: ${data.error || 'Failed to save monitor'}`, 'error');
        }
    } catch (err) {
        console.error('Save monitor error:', err);
        showToast('Network error while saving monitor', 'error');
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save Monitor';
    }
}

async function togglePauseMonitor(svcId) {
    try {
        const res = await fetch(`/api/monitors/${encodeURIComponent(svcId)}/toggle`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            const isPaused = data.monitor && data.monitor.paused;
            showToast(`Monitor ${isPaused ? 'paused' : 'resumed'}`);
            runLiveHealthCheck();
        }
    } catch (err) {
        console.error('Toggle error:', err);
        showToast('Failed to toggle monitor state', 'error');
    }
}

async function deleteMonitor(svcId) {
    const svc = currentResults.find(s => s.id === svcId);
    const name = svc ? svc.name : svcId;
    if (!confirm(`Are you sure you want to delete "${name}"?`)) return;

    try {
        const res = await fetch(`/api/monitors/${encodeURIComponent(svcId)}`, { method: 'DELETE' });
        if (res.ok) {
            showToast(`Monitor "${name}" deleted`);
            runLiveHealthCheck();
        } else {
            showToast('Failed to delete monitor', 'error');
        }
    } catch (err) {
        console.error('Delete error:', err);
        showToast('Network error while deleting', 'error');
    }
}

// Trigger Git auto commit and push
async function triggerGitCommit() {
    const btn = document.getElementById('gitSyncBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = 'Committing...';
    }

    try {
        const res = await fetch('/api/trigger-git-commit', { method: 'POST' });
        const data = await res.json();
        
        if (data.status === 'success') {
            showToast(data.message || 'Committed and pushed status to GitHub');
        } else if (data.status === 'skipped') {
            showToast('Git commit skipped (ENABLE_GIT_AUTO_COMMIT=false in .env)', 'info');
        } else {
            showToast(`Git: ${data.message || data.error || 'Check completed'}`, 'info');
        }
    } catch (err) {
        console.error('Git trigger error:', err);
        showToast('Git commit request failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = 'Push to GitHub';
        }
    }
}

// Toast notification helper
function showToast(message, type = 'success') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(10px)';
        toast.style.transition = 'all 0.2s ease';
        setTimeout(() => toast.remove(), 200);
    }, 3500);
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}