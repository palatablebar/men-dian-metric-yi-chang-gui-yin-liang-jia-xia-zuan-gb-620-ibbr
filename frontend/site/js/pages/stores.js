import { api } from '../api.js';
import { ENUMS, can, fmtMoney, fmtPct } from '../config.js';
import { bindFilters, drawer, errorBox, errorMessage, esc, field, filterBar, kv, seriesChart, table, toast } from '../ui.js';

const state = { q: '', region: '', status: '' };

const REGION_OPTIONS = ['华东', '华南', '华北', '西南', '华中'].map((item) => ({ value: item, label: item }));

function openTransfer(view, ctx, store) {
  const { root, close } = drawer({
    title: `登记跨区调整 · ${store.name}`,
    subtitle: '生效日当天起计入新区域，生效前仍计入原区域；历史目标按生效日分期',
    body: `<form id="transfer-form" class="stack">
      ${field({ label: '调入区域', name: 'to_region', value: store.region === '华东' ? '华南' : '华东', options: REGION_OPTIONS })}
      ${field({ label: '生效日期', name: 'effective_date', type: 'date', value: '2026-11-20' })}
      ${field({ label: '调整原因', name: 'reason', placeholder: '例如：片区重组，划归新区域管理' })}
      <div class="form-error" id="transfer-error"></div>
      <button class="btn accent" type="submit">保存调整</button>
    </form>`,
  });
  root.querySelector('#transfer-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      await api.transfers.create(store.id, {
        to_region: form.to_region.value,
        effective_date: form.effective_date.value,
        reason: form.reason.value.trim() || null,
      });
      toast('跨区调整已登记；重跑目标批次后按新生效日分期');
      close();
      await storesPage.render(view, ctx);
    } catch (err) {
      root.querySelector('#transfer-error').textContent = err.code === 'CONFLICT' ? '该生效日已有调整记录' : errorMessage(err);
    }
  });
}

async function openStore(view, ctx, store) {
  let series;
  let transfers;
  try {
    [series, transfers] = await Promise.all([api.stores.series(store.id), api.transfers.listForStore(store.id)]);
  } catch (err) {
    toast(errorMessage(err), 'err');
    return;
  }
  const points = series.points;
  const transferRows = transfers.items || [];
  const { root } = drawer({
    title: store.name,
    subtitle: `${store.code} · 当前归属 ${store.region} · ${store.city} · ${store.status_label}`,
    body: `${kv([
      ['房量', `${store.room_count} 间`],
      ['品牌', esc(store.brand)],
      ['店长', esc(store.manager)],
      ['90 天营收', fmtMoney(series.summary.revenue)],
      ['平均 ADR', `${series.summary.adr.toFixed(0)} 元`],
      ['平均 OCC', fmtPct(series.summary.occupancy)],
    ])}
    <h3 class="sub-head">营收日序列（可标注异常日）</h3>
    ${seriesChart(points, { height: 140 })}
    <h3 class="sub-head">跨区调整记录</h3>
    ${
      transferRows.length
        ? table({
            rows: transferRows.map((item) => ({ id: item.id, ...item })),
            columns: [
              { key: 'effective_date', label: '生效日', className: 'mono' },
              { key: 'move', label: '调整', render: (row) => `<span class="badge warn">${esc(row.from_region)} → ${esc(row.to_region)}</span>` },
              { key: 'reason', label: '原因', render: (row) => esc(row.reason || '—') },
              { key: 'created_by', label: '登记人', className: 'mono' },
            ],
          })
        : '<p class="muted small">没有跨区调整记录：该店所有月份目标整月计入当前归属区域。</p>'
    }
    <h3 class="sub-head">指标点（最近 10 条渠道明细）</h3>
    ${table({
      rows: points.slice(-10).reverse().map((point, index) => ({ id: `${point.date}-${index}`, ...point })),
      columns: [
        { key: 'date', label: '日期', className: 'mono' },
        { key: 'revenue', label: '营收', className: 'num', render: (row) => fmtMoney(row.revenue) },
        { key: 'adr', label: 'ADR', className: 'num', render: (row) => row.adr.toFixed(0) },
        { key: 'occupancy', label: 'OCC', className: 'num', render: (row) => fmtPct(row.occupancy) },
        { key: 'rooms_sold', label: '售出', className: 'num' },
        { key: 'is_weekend', label: '周末', render: (row) => (row.is_weekend ? '<span class="badge">周末</span>' : '—') },
      ],
    })}`,
    footer: `<div class="footer-actions">
      <button class="btn accent" data-anomalies>看这家店的异常</button>
      ${can(ctx.user.role, 'store:write') ? '<button class="btn ghost" data-transfer>登记跨区调整</button>' : ''}
    </div>`,
  });
  root.querySelector('[data-anomalies]')?.addEventListener('click', () => {
    location.hash = `#/anomalies?store_id=${store.id}`;
  });
  root.querySelector('[data-transfer]')?.addEventListener('click', () => openTransfer(view, ctx, store));
}

function openCreate(view, ctx) {
  const { root, close } = drawer({
    title: '登记门店',
    subtitle: '仅管理员可维护门店档案',
    body: `<form id="store-form" style="display:grid;gap:12px">
      ${field({ label: '门店编码', name: 'code', placeholder: 'LB-HZ-999' })}
      ${field({ label: '门店名称', name: 'name', placeholder: '蓝湾·光谷中心店' })}
      ${field({ label: '区域', name: 'region', value: '华中', options: ['华东', '华南', '华北', '西南', '华中'].map((item) => ({ value: item, label: item })) })}
      ${field({ label: '城市', name: 'city', placeholder: '武汉' })}
      ${field({ label: '房量', name: 'room_count', type: 'number', value: '200' })}
      ${field({ label: '店长', name: 'manager', placeholder: '店长姓名' })}
      <div class="form-error" id="store-error"></div>
      <button class="btn accent" type="submit">保存门店</button>
    </form>`,
  });
  root.querySelector('#store-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    try {
      await api.stores.create({
        code: form.code.value.trim().toUpperCase(),
        name: form.name.value.trim(),
        region: form.region.value,
        city: form.city.value.trim(),
        room_count: Number(form.room_count.value || 200),
        manager: form.manager.value.trim(),
      });
      toast('门店已登记');
      close();
      await storesPage.render(view, ctx);
    } catch (err) {
      root.querySelector('#store-error').textContent = err.code === 'CONFLICT' ? '门店编码已存在' : errorMessage(err);
    }
  });
}

export const storesPage = {
  async render(view, ctx) {
    state.q = ctx.query.q ?? state.q;
    state.region = ctx.query.region ?? state.region;
    let data;
    let regions;
    try {
      [data, regions] = await Promise.all([
        api.stores.list({ q: state.q, region: state.region, status: state.status, limit: 200 }),
        api.stores.regions(),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => storesPage.render(view, ctx));
      return;
    }
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>门店与指标</h2>
          <div class="head-actions">
            <span class="meta">${data.total} 家门店</span>
            ${can(ctx.user.role, 'store:write') ? '<button class="btn sm accent" id="new-store">登记门店</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          ${filterBar({
            id: 'store-filters',
            fields: [
              { label: '搜索', name: 'q', value: state.q, placeholder: '名称 / 编码 / 城市' },
              { label: '区域', name: 'region', value: state.region, options: [{ value: '', label: '全部' }, ...regions.items.map((item) => ({ value: item.region, label: `${item.region}（${item.count}）` }))] },
              { label: '状态', name: 'status', value: state.status, options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.storeStatus).map(([value, label]) => ({ value, label }))] },
            ],
          })}
        </div>
        ${table({
          rows: data.items,
          columns: [
            { key: 'code', label: '编码', className: 'mono' },
            { key: 'name', label: '门店', render: (row) => `<b>${esc(row.name)}</b><div class="mono muted">${esc(row.city)} · ${esc(row.brand)}</div>` },
            { key: 'region', label: '区域' },
            { key: 'room_count', label: '房量', className: 'num' },
            { key: 'manager', label: '店长' },
            { key: 'status', label: '状态', render: (row) => `<span class="badge ${row.status === 'open' ? 'ok' : row.status === 'renovating' ? 'warn' : ''}">${esc(row.status_label)}</span>` },
          ],
          empty: { title: '没有符合条件的门店', hint: '换个区域或清空搜索词。' },
        })}
      </section>`;
    bindFilters(view, 'store-filters', (values) => {
      state.q = values.q.trim();
      state.region = values.region;
      state.status = values.status;
      storesPage.render(view, ctx);
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const store = data.items.find((item) => String(item.id) === row.dataset.id);
        if (store) openStore(view, ctx, store);
      });
    });
    view.querySelector('#new-store')?.addEventListener('click', () => openCreate(view, ctx));
  },
};

export default storesPage;
