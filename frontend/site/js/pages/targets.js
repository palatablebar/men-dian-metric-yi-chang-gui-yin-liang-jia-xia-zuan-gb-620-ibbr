import { api } from '../api.js';
import { ENUMS, can, fmtMoney, fmtPct } from '../config.js';
import { badge, drawer, emptyState, errorBox, errorMessage, esc, field, statStrip, table, toast } from '../ui.js';

const state = { month: '2026-11', batchId: null, region: '' };

const STATUS_TONE = { published: 'ok', draft: 'pending', archived: 'info', running: 'warn', publishing: 'warn' };

function batchStatusBadge(row) {
  return badge(STATUS_TONE[row.status] || 'info', ENUMS.targetBatchStatus[row.status] || row.status_label || row.status);
}

function reconcileStrip(detail) {
  const check = detail.reconcile || {};
  return statStrip([
    { label: '全集团合计', value: fmtMoney(check.group_total), hint: `${detail.store_count} 家门店月目标` },
    { label: 'Σ 区域合计', value: fmtMoney(check.region_sum), hint: check.diff_region ? `差 ${fmtMoney(check.diff_region)}` : '与集团一致' },
    { label: 'Σ 门店合计', value: fmtMoney(check.store_sum), hint: check.diff_store ? `差 ${fmtMoney(check.diff_store)}` : '与集团一致' },
    {
      label: '三层对平',
      value: check.balanced ? '已对平' : '未对平',
      hint: check.balanced ? '集团 = Σ区域 = Σ门店' : '请重跑本批次',
      tone: check.balanced ? 'ok' : 'danger',
    },
  ]);
}

function transferBlock(detail) {
  const rows = detail.transfers || [];
  if (!rows.length) {
    return '<p class="muted small">本月没有跨区调整：全部门店整月计入当前归属区域。</p>';
  }
  return `<div class="stack">${rows
    .map((item) => {
      const transition = (item.transitions || [])
        .map((t) => `<span class="badge warn">${esc(t.from_region)} → ${esc(t.to_region)} · ${esc(t.effective_date)} 起</span>`)
        .join(' ');
      return `<div class="hand-note" data-testid="transfer-impact">
        <p>${transition}</p>
        <p>${esc(item.note)}</p>
      </div>`;
    })
    .join('')}</div>`;
}

function regionPanel(detail, regionData) {
  if (!state.region) {
    return `<section class="panel"><div class="panel-head"><h2>区域明细</h2><span class="meta">选择区域查看分期目标与门店贡献</span></div>
      <div class="panel-body">${emptyState({ title: '尚未选择区域', hint: '在上方区域排名表中点击一行，查看该区域的分期目标、门店贡献与跨区调整影响。' })}</div></section>`;
  }
  if (!regionData) return '';
  const summary = regionData.summary || {};
  const impacts = regionData.impacts || [];
  return `<section class="panel" data-testid="region-panel">
    <div class="panel-head">
      <h2>区域明细 · ${esc(state.region)}</h2>
      <div class="head-actions"><span class="meta">排名 #${summary.rank} · 占集团 ${fmtPct(summary.share)}</span></div>
    </div>
    <div class="panel-body stack">
      ${statStrip([
        { label: '区域目标', value: fmtMoney(summary.target), hint: '按生效日分期后归属本区域的部分' },
        { label: '覆盖门店', value: summary.store_count, hint: '含月中调入/调出的门店' },
        { label: '分期段数', value: summary.segment_count, hint: '跨区门店会拆成多段' },
      ])}
      <h3 class="sub-head">分期目标（按调整生效日拆分）</h3>
      ${table({
        rows: regionData.segments.map((item) => ({ id: item.id, ...item })),
        columns: [
          { key: 'store_name', label: '门店', render: (row) => `<b>${esc(row.store_name)}</b><div class="mono muted">${esc(row.store_code)}</div>` },
          { key: 'segment_no', label: '段', className: 'num' },
          { key: 'period', label: '归属期间', render: (row) => `<span class="mono">${esc(row.period_start)} → ${esc(row.period_end)}</span>` },
          { key: 'days', label: '天数', className: 'num' },
          { key: 'target', label: '分期目标', className: 'num', render: (row) => fmtMoney(row.target) },
          { key: 'transfer_id', label: '说明', render: (row) => (row.transfer_id ? '<span class="badge warn">调整分段</span>' : '<span class="muted">—</span>') },
        ],
      })}
      <h3 class="sub-head">门店贡献（区域内合计）</h3>
      ${table({
        rows: regionData.stores.map((item) => ({ id: item.store_id, ...item })),
        columns: [
          { key: 'store_name', label: '门店', render: (row) => `<b>${esc(row.store_name)}</b><div class="mono muted">${esc(row.store_code)}</div>` },
          { key: 'target', label: '贡献目标', className: 'num', render: (row) => fmtMoney(row.target) },
          { key: 'segment_count', label: '分段', className: 'num' },
          { key: 'periods', label: '归属期间', className: 'mono' },
        ],
      })}
      <h3 class="sub-head">跨区调整影响</h3>
      ${
        impacts.length
          ? `<div class="stack">${impacts
              .map((item) => {
                const tags = item.directions
                  .map((d) => `<span class="badge ${d === 'in' ? 'ok' : 'danger'}">${d === 'in' ? '调入本区域' : '调出本区域'}</span>`)
                  .join(' ');
                return `<div class="hand-note"><p>${tags} <b>${esc(item.store_name)}</b>（${esc(item.store_code)}）</p><p>${esc(item.note)}</p></div>`;
              })
              .join('')}</div>`
          : '<p class="muted small">本区域本月没有跨区调整影响。</p>'
      }
    </div>
  </section>`;
}

async function openUpsert(view, ctx) {
  let stores;
  try {
    stores = await api.stores.list({ limit: 200 });
  } catch (err) {
    toast(errorMessage(err), 'err');
    return;
  }
  const options = (stores.items || []).map((item) => ({ value: item.code, label: `${item.code} · ${item.name}（${item.region}）` }));
  const { root, close } = drawer({
    title: '下达门店月目标',
    subtitle: '自然键（门店 × 月份）upsert：重复提交覆盖金额，不产生重复行',
    body: `<form id="target-form" class="stack">
      ${field({ label: '门店', name: 'store_code', options })}
      ${field({ label: '月份', name: 'month', type: 'month', value: state.month })}
      ${field({ label: '目标金额（元）', name: 'target_amount', type: 'number', placeholder: '例如 6500000' })}
      <div class="form-error" id="target-error"></div>
      <button class="btn accent" type="submit">保存目标</button>
    </form>`,
  });
  root.querySelector('#target-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      const saved = await api.targets.upsert({
        store_code: form.store_code.value,
        month: form.month.value,
        target_amount: Number(form.target_amount.value || 0),
      });
      toast(`已下达 ${saved.store_code} ${saved.month} 目标 ${fmtMoney(saved.target_amount)}`);
      close();
      await targetsPage.render(view, ctx);
    } catch (err) {
      root.querySelector('#target-error').textContent = errorMessage(err);
    }
  });
}

async function pickBatch(view, ctx, batches) {
  const chosen = batches.find((item) => item.id === state.batchId) || batches[0];
  state.batchId = chosen ? chosen.id : null;
  if (!chosen) return null;
  return api.targets.getBatch(chosen.id);
}

export const targetsPage = {
  async render(view, ctx) {
    let batches;
    let storeTargets;
    try {
      [batches, storeTargets] = await Promise.all([
        api.targets.listBatches({ month: state.month, limit: 50 }),
        api.targets.listStoreTargets({ month: state.month }),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => targetsPage.render(view, ctx));
      return;
    }
    let detail = null;
    let regionData = null;
    try {
      detail = await pickBatch(view, ctx, batches.items);
      if (detail && state.region) {
        regionData = await api.targets.regionDetail(detail.id, state.region).catch(() => {
          state.region = '';
          return null;
        });
      }
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => targetsPage.render(view, ctx));
      return;
    }
    const latest = batches.items[0];
    const generateLabel = !latest ? '生成本月目标批次' : latest.status === 'draft' ? `重跑 v${latest.version}（原地替换）` : `重跑（生成 v${latest.version + 1} 草稿，不动已发布版）`;
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>目标批次 · ${esc(state.month)}</h2>
          <div class="head-actions">
            ${field({ label: '月份', name: 'month', type: 'month', value: state.month })}
            ${can(ctx.user.role, 'target:write') ? `<button class="btn sm accent" id="generate">${esc(generateLabel)}</button>` : ''}
          </div>
        </div>
        <div id="batch-rows">
        ${table({
          rows: batches.items.map((item) => ({ ...item, id: item.id })),
          rowId: (row) => (detail && row.id === detail.id ? 'batch-current' : ''),
          columns: [
            { key: 'batch_no', label: '批次', className: 'mono', render: (row) => `<b>${esc(row.batch_no)}</b>${detail && row.id === detail.id ? ' <span class="badge info">当前</span>' : ''}` },
            { key: 'version', label: '版本', render: (row) => `v${row.version}` },
            { key: 'status', label: '状态', render: batchStatusBadge },
            { key: 'store_count', label: '门店', className: 'num' },
            { key: 'line_count', label: '分期行', className: 'num' },
            { key: 'total_target', label: '集团合计', className: 'num', render: (row) => fmtMoney(row.total_target) },
            { key: 'operator_email', label: '操作人', className: 'mono' },
            {
              key: 'actions',
              label: '操作',
              render: (row) =>
                can(ctx.user.role, 'target:write') && row.status === 'draft'
                  ? `<button class="btn sm ghost" data-publish="${row.id}">发布</button>`
                  : '<span class="muted">—</span>',
            },
          ],
          empty: { title: '本月还没有目标批次', hint: can(ctx.user.role, 'target:write') ? '先下达门店月目标，再点右上角生成。' : '等待管理员下达目标并生成批次。' },
        })}
        </div>
        <div class="panel-body">
          <p class="muted small">口径：门店跨区调整<strong>生效日当天起</strong>计入新区域，生效前仍计入原区域；月目标按归属区间天数拆分，分段合计 ≡ 门店月目标。重跑同一批次原地替换不产生重复；已发布版本不被改换，重跑自动开新版本。</p>
        </div>
      </section>
      ${
        detail
          ? `<section class="panel" data-testid="reconcile-panel">
              <div class="panel-head"><h2>三层对平与区域排名</h2><span class="meta">${esc(detail.batch_no)} · ${esc(detail.status_label)}</span></div>
              <div class="panel-body stack">
                ${reconcileStrip(detail)}
                <div id="region-rows">
                ${table({
                  rows: (detail.regions || []).map((item) => ({ id: item.region, ...item })),
                  columns: [
                    { key: 'rank', label: '排名', className: 'num' },
                    { key: 'region', label: '区域', render: (row) => `<b>${esc(row.region)}</b>${state.region === row.region ? ' <span class="badge info">查看中</span>' : ''}` },
                    { key: 'target', label: '区域目标', className: 'num', render: (row) => fmtMoney(row.target) },
                    { key: 'share', label: '占集团', className: 'num', render: (row) => fmtPct(row.share) },
                    { key: 'store_count', label: '门店', className: 'num' },
                    { key: 'segment_count', label: '分期段', className: 'num' },
                    {
                      key: 'moves',
                      label: '跨区调整',
                      render: (row) =>
                        row.transfer_in || row.transfer_out
                          ? `${row.transfer_in ? `<span class="badge ok">调入 ${row.transfer_in}</span>` : ''} ${row.transfer_out ? `<span class="badge danger">调出 ${row.transfer_out}</span>` : ''}`
                          : '<span class="muted">—</span>',
                    },
                  ],
                })}
                </div>
                <h3 class="sub-head">跨区调整影响说明</h3>
                ${transferBlock(detail)}
              </div>
            </section>`
          : ''
      }
      ${detail ? regionPanel(detail, regionData) : ''}
      <section class="panel">
        <div class="panel-head">
          <h2>门店月目标（输入）</h2>
          <div class="head-actions">
            <span class="meta">${storeTargets.total} 家已下达</span>
            ${can(ctx.user.role, 'target:write') ? '<button class="btn sm accent" id="upsert-target">下达 / 调整目标</button>' : ''}
          </div>
        </div>
        ${table({
          rows: (storeTargets.items || []).map((item) => ({ id: item.id, ...item })),
          columns: [
            { key: 'store_code', label: '编码', className: 'mono' },
            { key: 'store_name', label: '门店', render: (row) => `<b>${esc(row.store_name)}</b>` },
            { key: 'region', label: '当前归属' },
            { key: 'target_amount', label: '月目标', className: 'num', render: (row) => fmtMoney(row.target_amount) },
            { key: 'updated_by', label: '下达人', className: 'mono' },
          ],
          empty: { title: '本月还没有门店目标', hint: '管理员下达目标后才能生成批次。' },
        })}
      </section>`;
    // 事件绑定
    view.querySelector('[name=month]')?.addEventListener('change', (event) => {
      state.month = event.target.value || state.month;
      state.batchId = null;
      state.region = '';
      targetsPage.render(view, ctx);
    });
    view.querySelector('#generate')?.addEventListener('click', async (event) => {
      event.target.disabled = true;
      try {
        const batch = await api.targets.generate({ month: state.month });
        state.batchId = batch.id;
        toast(`批次 ${batch.batch_no} 已生成：${batch.line_count} 行分期，对平${batch.reconcile?.balanced ? '一致' : '有差异'}`);
        await targetsPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        event.target.disabled = false;
      }
    });
    view.querySelectorAll('[data-publish]').forEach((button) => {
      button.addEventListener('click', async (event) => {
        event.stopPropagation();
        try {
          const batch = await api.targets.publish(button.dataset.publish);
          state.batchId = batch.id;
          toast(`批次 ${batch.batch_no} 已发布`);
          await targetsPage.render(view, ctx);
        } catch (err) {
          toast(errorMessage(err), 'err');
        }
      });
    });
    // 批次行点击 → 切换当前批次；区域行点击 → 查看区域明细（容器隔离，避免 id 撞行）
    view.querySelectorAll('#batch-rows tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const batchRow = batches.items.find((item) => String(item.id) === row.dataset.id);
        if (batchRow) {
          state.batchId = batchRow.id;
          targetsPage.render(view, ctx);
        }
      });
    });
    view.querySelectorAll('#region-rows tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const regionRow = (detail?.regions || []).find((item) => item.region === row.dataset.id);
        if (regionRow) {
          state.region = regionRow.region;
          targetsPage.render(view, ctx);
        }
      });
    });
    view.querySelector('#upsert-target')?.addEventListener('click', () => openUpsert(view, ctx));
  },
};

export default targetsPage;
