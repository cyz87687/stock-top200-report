#!/usr/bin/env node
// 页面行为自测（jsdom 真实加载 index.html 并执行页面 JS）
// 用法：node test_page_ui.js          → 测 index.html
//       node test_page_ui.js xxx.html → 测指定文件
// 依赖：npm i jsdom（若缺失可设 NODE_PATH 指向已有安装）
// ⚠️ 时序陷阱：必须等 applyFilters 真正跑过一次（出现 hidden-row）再测交互，
//    否则 initFilters 尚未绑定，首次 change 会被静默丢弃 → 误报「排序失效」。
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const DIR = __dirname;
const FILE = process.argv[2] ? path.resolve(process.argv[2]) : path.join(DIR, 'index.html');
const html = fs.readFileSync(FILE, 'utf8');

const errors = [];
const C = { g: '\x1b[32m', r: '\x1b[31m', y: '\x1b[33m', b: '\x1b[36m', d: '\x1b[2m', x: '\x1b[0m' };
let pass = 0, fail = 0;
const failList = [];
function ok(name, cond, extra = '') {
  if (cond) { pass++; console.log(`  ${C.g}✅${C.x} ${name}${extra ? ' ' + C.d + extra + C.x : ''}`); }
  else { fail++; failList.push(name + (extra ? ' | ' + extra : '')); console.log(`  ${C.r}❌ ${name}${C.x} ${extra}`); }
}
function sec(t) { console.log(`\n${C.b}【${t}】${C.x}`); }

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'file:///Users/yzreal/WorkBuddy/2026-07-24-18-07-15/stock-top200-report/index.html',
});
const { window } = dom;
const doc = window.document;
window.addEventListener('error', e => errors.push('window.error: ' + (e.message || e)));
const origError = window.console.error;
window.console.error = (...a) => { errors.push('console.error: ' + a.join(' ')); };

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // ⚠️ 必须等 DOMContentLoaded 真正触发（标志：applyFilters 已跑过 → 出现 hidden-row），
  //   只等「行存在」不够：那时 initFilters 尚未绑定，首次 change 会被丢弃。
  const inited = () => doc.querySelectorAll('#stockTable tbody tr.expandable.hidden-row').length > 0;
  for (let i = 0; i < 120 && !inited(); i++) await sleep(25);
  if (!inited()) { console.log('❌ 页面初始化未完成（applyFilters 未执行），后续测试不可信'); process.exit(2); }

  console.log('='.repeat(68));
  console.log('  页面行为自测  jsdom  ·  ' + path.basename(FILE));
  console.log('='.repeat(68));

  const allRows = () => Array.from(doc.querySelectorAll('#stockTable tbody tr.expandable'));
  // 正确判据：applyFilters 用 classList.add('hidden-row') 隐藏
  const visible = () => allRows().filter(r => !r.classList.contains('hidden-row') && r.style.display !== 'none');
  const fire = (el, type) => el.dispatchEvent(new window.Event(type, { bubbles: true }));
  const click = el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const dataOf = (r, k) => {
    const short = k.replace('score_', '');
    return parseFloat(r.dataset['s' + short[0].toUpperCase() + short.slice(1)]);
  };

  // ---------- 1 结构 ----------
  sec('1. 页面结构');
  ['sec-decision', 'sec-mf-pick', 'sec-list', 'sec-focus-pool', 'sec-notes'].forEach(s =>
    ok(s, doc.querySelectorAll('#' + s).length === 1));
  ok('表格存在', !!doc.querySelector('#stockTable'));
  const thN = doc.querySelectorAll('#stockTable thead tr th').length;
  const tdN = allRows()[0].querySelectorAll('td').length;
  ok('表头列数 = 数据行列数', thN === tdN, `th=${thN} td=${tdN}`);

  // ---------- 2 生效维口径 ----------
  sec('2. 生效维口径');
  // 维度数从排序下拉**动态读取**（v4.16 = 8 生效维），禁再写死 7/8/9（历史漂移源）
  const sortSel = doc.querySelector('#sortSelect');
  const opts = Array.from(sortSel.options).map(o => o.value);
  const scoreOpts = opts.filter(v => v.startsWith('score_'));
  const DIM_N = scoreOpts.length;
  ok(`每行 ${DIM_N} 个分数格`, allRows().every(r => r.querySelectorAll('.s-score').length === DIM_N),
    `不达标行数 ${allRows().filter(r => r.querySelectorAll('.s-score').length !== DIM_N).length}`);
  const wtext = doc.querySelector('#sec-decision').textContent;
  // ⚠️ 不写死具体权重（调权即过时）；权重条按 .ws-cell/.ws-pct 逐格解析（唯一口径
  //    来源 = _build_weight_strip）。只读 .ws-pct 元素：cell 的 textContent 会把
  //    「低波20」标签与「20.0%」粘连成 2020.0%（无分隔符），曾致解析伪值。
  //    校验：格数=生效维数 + 合计 100% + 每格 (0,100)。
  const wsCells = Array.from(doc.querySelectorAll('#sec-decision .weight-strip .ws-cell'));
  const wpcts = wsCells.map(c => {
    const t = (c.querySelector('.ws-pct') || {}).textContent || '';
    const m = t.match(/(\d+\.\d)%/);
    return m ? parseFloat(m[1]) : NaN;
  });
  const wsum = wpcts.reduce((a, b) => a + (isNaN(b) ? 0 : b), 0);
  ok(`权重条 ${DIM_N} 个生效维`, wsCells.length === DIM_N && wpcts.every(v => !isNaN(v)),
    `解析到 ${wsCells.length} 格: ${wpcts.join(',')}`);
  // 容差 0.5：页面权重显示为 1 位小数（_w*100:.1f），9 格各 ±0.05 舍入残差
  //   累计上界 ±0.45（实测 v4.15 显示合计 100.1）。真实合计恒为 100%（Σw=1），
  //   本项防的是「格子丢了/权重真漂移」而非舍入。
  ok('权重合计 100%（含舍入残差）', Math.abs(wsum - 100) < 0.5, wpcts.join('+') + '=' + wsum.toFixed(2));
  ok('每个权重均在 (0,100)', wpcts.every(v => v > 0 && v < 100), wpcts.join(','));
  ok('权重条归零说明', wtext.includes('未参与评分'));
  ok('模型结构结论已渲染', wtext.includes('模型结构'));
  ok('有效维数已披露', /\d+\.\d+ \/ \d+/.test(wtext), (wtext.match(/有效维数 [\d.]+ \/ \d+/) || [])[0]);
  ok('两种量纲已说明', wtext.includes('两种量纲别混读'));

  // ---------- 3 data 属性 ----------
  sec('3. data-s-* 排序数据源');
  const firstTag = allRows()[0].outerHTML.match(/<tr[^>]*>/)[0];
  const names = firstTag.match(/data-[a-z0-9_-]+(?==)/g) || [];
  const cnt = {}; names.forEach(n => cnt[n] = (cnt[n] || 0) + 1);
  const dups = Object.entries(cnt).filter(([, v]) => v > 1);
  ok('无重复 data 属性', dups.length === 0, dups.map(d => d[0] + '×' + d[1]).join(','));

  // 与排序下拉同源 = 当前生效维（v4.15：fund/quality/lowvol20/tech_neg/bias60/rsi_rev/ep/size_growth/dominant）
  const KEYS = scoreOpts;
  const miss = [], bad = [];
  KEYS.forEach(k => {
    allRows().forEach(r => {
      const v = r.dataset['s' + k.replace('score_', '').replace(/^./, c => c.toUpperCase())];
      if (v === undefined) miss.push(k);
      else if (v !== '' && (parseFloat(v) < -1e-3 || parseFloat(v) > 10.001)) bad.push(k + '=' + v);
    });
  });
  ok(`${DIM_N} 维 data 属性齐全`, miss.length === 0, miss.length ? miss.slice(0, 3).join(',') : '');
  ok('归一化值域 0-10', bad.length === 0, bad.slice(0, 3).join(','));

  // ---------- 4 排序 ----------
  sec('4. 排序（真实 change 事件）');
  // 地板值仅防「整体降级成空壳」，不写死维数（v4.15=9 / v4.16=8，调权会变）
  ok(`score_* 选项 = ${DIM_N}（与行分数格一致）`, scoreOpts.length === DIM_N && DIM_N >= 5, scoreOpts.join(','));
  ok('无 score_news', !opts.includes('score_news'));
  const setSort = v => { sortSel.value = v; fire(sortSel, 'change'); };

  setSort('total');
  const tv = visible().map(r => parseFloat(r.querySelector('.s-total').dataset.value));
  ok('total 降序单调', tv.every((v, i) => i === 0 || v <= tv[i - 1] + 1e-9), `前4: ${tv.slice(0, 4).join(' > ')} 共${tv.length}条`);

  let sortBad = [];
  scoreOpts.forEach(f => {
    setSort(f);
    const vis = visible();
    const vals = vis.map(r => dataOf(r, f)).filter(v => !isNaN(v));
    const head = dataOf(vis[0], f);
    const mx = Math.max(...vals);
    const monoOk = vals.every((v, i) => i === 0 || v <= vals[i - 1] + 1e-9);
    if (Math.abs(head - mx) > 1e-6 || !monoOk) sortBad.push(`${f}(Top1=${head},max=${mx},mono=${monoOk})`);
  });
  ok(`${scoreOpts.length} 维排序全部正确`, sortBad.length === 0, sortBad.join(' '));
  setSort('total');

  // ---------- 5 筛选 ----------
  sec('5. 筛选');
  const rf = doc.querySelector('#ratingFilter');
  rf.value = 'S'; fire(rf, 'change');
  const sVis = visible();
  ok('评级筛选 S', sVis.length > 0 && sVis.every(r => r.dataset.rating === 'S'), `n=${sVis.length}`);
  rf.value = ''; fire(rf, 'change');
  ok('评级复位后恢复', visible().length === parseInt(doc.querySelector('#pageSize').value), `n=${visible().length}`);

  const si = doc.querySelector('#searchInput');
  const kw = allRows()[0].dataset.name.slice(0, 2);
  si.value = kw; fire(si, 'input');
  await sleep(420); // debounce 300ms
  const kVis = visible();
  ok('搜索过滤', kVis.length > 0 && kVis.every(r => (r.dataset.name || '').includes(kw) || (r.dataset.code || '').includes(kw)),
    `「${kw}」→ ${kVis.length} 条`);
  si.value = ''; fire(si, 'input');
  await sleep(420);
  ok('搜索复位后恢复', visible().length === parseInt(doc.querySelector('#pageSize').value), `n=${visible().length}`);

  // ---------- 6 展开详情 ----------
  sec('6. 行展开详情（生效维渲染）');
  const r0 = allRows()[0];
  click(r0);
  await sleep(80);
  const detail = r0.nextElementSibling;
  const isDetail = !!detail && detail.classList.contains('detail-row');
  ok('详情行创建', isDetail);
  if (isDetail) {
    const dt = detail.textContent;
    ['估值锚', 'EP估值', '质量', '低波20', '技术面反号', '乖离低位', 'RSI反转', '小盘成长', '行业龙头'].forEach(d =>
      ok(`  含「${d}」`, dt.includes(d)));
    ok('  参考维标注「不计分」', dt.includes('不计分'));
    // v4.8 起详情行**不再写死权重%**（曾硬编码 30%/17.5% 两度过期），
    //   权重唯一口径 = 页首权重条，详情只放指针文案。
    ok('  指向权重条唯一口径', dt.includes('各维权重见页首') || dt.includes('唯一口径来源'));
    ok('  无 undefined/NaN/None', !/undefined|\bNaN\b|>None<|\(None/.test(dt),
      (dt.match(/undefined|NaN|None/g) || []).slice(0, 3).join(','));
    const grid = detail.querySelectorAll('div[style*="grid-template-columns"] > div');
    ok(`  ${DIM_N} 生效维 + 4 参考维格子齐全`, grid.length >= DIM_N + 4, `格子 ${grid.length}`);
    // 与 data 属性交叉校验：详情里估值锚数值 == data-s-fund
    const f10 = parseFloat(r0.dataset.sFund).toFixed(1);
    ok('  估值锚数值与 data 一致', dt.includes('(' + f10 + '/10)'), `data=${f10}`);
    // 二次点击应折叠
    click(r0); await sleep(50);
    ok('  二次点击折叠', !r0.nextElementSibling || !r0.nextElementSibling.classList.contains('show'));
  }

  // ---------- 7 分页 ----------
  sec('7. 分页');
  const pi = doc.querySelector('#pageInfo');
  ok('分页控件', !!pi && !!doc.querySelector('#nextPage') && !!doc.querySelector('#prevPage'));
  const p0 = pi.textContent.trim();
  click(doc.querySelector('#nextPage'));
  const p1 = pi.textContent.trim();
  ok('下一页生效', p0 !== p1, `${p0} → ${p1}`);
  click(doc.querySelector('#prevPage'));
  ok('上一页生效', pi.textContent.trim() === p0, `回到 ${pi.textContent.trim()}`);
  const totPages = parseInt((pi.textContent.match(/\/\s*(\d+)/) || [])[1] || 0);
  ok('总页数 > 1', totPages > 1, `${totPages} 页`);

  // ---------- 8 列开关 ----------
  sec('8. 列显示开关');
  const checks = Array.from(doc.querySelectorAll('.col-check'));
  ok('列开关存在', checks.length > 0, `${checks.length} 个`);
  if (checks.length) {
    const cb = checks[0], col = cb.dataset.col;
    const els = () => Array.from(doc.querySelectorAll('.col-' + col));
    cb.checked = false; fire(cb, 'change');
    ok('取消勾选隐藏列', els().length > 0 && els().every(e => e.style.display === 'none'), `${col} ${els().length} 个`);
    cb.checked = true; fire(cb, 'change');
    ok('恢复勾选显示列', els().every(e => e.style.display !== 'none'));
  }

  // ---------- 9 快捷筛选 ----------
  sec('9. 快捷筛选');
  const qs = Array.from(doc.querySelectorAll('.quick-btn, [data-quick]'));
  ok('按钮存在', qs.length > 0, `${qs.length} 个`);
  const qerr = [];
  qs.forEach(b => { try { click(b); } catch (e) { qerr.push(b.textContent.trim() + ':' + e.message); } });
  ok('全部可点击无异常', qerr.length === 0, qerr.join(';'));
  const active = qs.filter(b => /active|on/.test(b.className)).length;
  ok('点击后有激活态', active > 0, `激活 ${active} 个`);

  // ---------- 10 值与文案 ----------
  sec('10. 值域与文案');
  const visText = (() => {
    const clone = doc.body.cloneNode(true);
    clone.querySelectorAll('script,style').forEach(e => e.remove());
    return clone.textContent;
  })();
  ok('可见文本无「十维」', !visText.includes('十维'));
  ok('可见文本无「量价反转」', !visText.includes('量价反转'));
  ok('无 custom-html-error', !html.includes('custom-html-error'));

  const scores = Array.from(doc.querySelectorAll('#stockTable tbody .s-score')).map(e => parseFloat(e.textContent));
  ok('分数格全部 0-10 非 NaN', scores.every(v => !isNaN(v) && v >= 0 && v <= 10.0001),
    `共 ${scores.length}，异常 ${scores.filter(v => isNaN(v) || v < 0 || v > 10.0001).length}`);
  ok(`分数格 = 行数×${DIM_N}`, scores.length === allRows().length * DIM_N, `${scores.length} = ${allRows().length}×${DIM_N}`);

  const totals = Array.from(doc.querySelectorAll('#stockTable tbody .s-total')).map(e => parseFloat(e.dataset.value));
  ok('total 值域 0-100', totals.every(v => v >= 0 && v <= 100) && totals.length === allRows().length,
    `min=${Math.min(...totals).toFixed(2)} max=${Math.max(...totals).toFixed(2)} n=${totals.length}`);

  // 归零维不得出现在分数条
  const sbTitles = Array.from(doc.querySelectorAll('#stockTable tbody .s-score')).slice(0, DIM_N).map(e => e.getAttribute('title') || '');
  const banned = ['消息面', '题材', '板块动量', '超跌', '量能', '超买', '关注度'];
  ok('分数条 tooltip 无归零维', !sbTitles.some(t => banned.some(b => t.includes(b))), sbTitles.map(t => t.split(' ')[0]).join(','));

  // ---------- 11 JS 运行时 ----------
  sec('11. JS 运行时');
  const realErr = errors.filter(e => !/Not implemented|jsdom|getContext/i.test(e));
  ok('无 JS 运行时错误', realErr.length === 0, realErr.slice(0, 2).join(' | '));

  console.log('\n' + '='.repeat(68));
  console.log(`  结果: ${C.g}${pass} 通过${C.x} / ${fail ? C.r : C.g}${fail} 失败${C.x}`);
  if (failList.length) {
    console.log(`\n${C.r}失败清单：${C.x}`);
    failList.forEach(f => console.log('  · ' + f));
  }
  console.log('='.repeat(68));
  process.exit(fail ? 1 : 0);
})();
