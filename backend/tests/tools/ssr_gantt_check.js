/* 甘特图 SSR 校验：用**真实 ECharts** 在 Node 里跑生成的 glue，确认 custom series 真的画出了条。
 *
 * 为什么需要它：2026-09-19 出现过「甘特图只有左侧工序名、右侧一条都没有」的线上故障。
 * 根因是 glue 的 applyFilters() 里 setOption 写成了 series:[{data:newData}]，
 * replaceMerge:["series"] 会把 custom series 整个换成**没有 renderItem** 的残缺对象，
 * 于是 init 时画好的 307 条在 applyFilters("all") 之后全部消失。
 * 这个 bug **纯数据层测试完全看不出来**（option 是好的，坏在 init 之后的第二次 setOption），
 * 只有真跑一遍 ECharts 才能发现 —— 所以留成工具。
 *
 * 用法（在仓库根目录）：
 *   node backend/tests/tools/ssr_gantt_check.js
 *   node backend/tests/tools/ssr_gantt_check.js <看板.html> <echarts.min.js>
 *
 * 判定：chart[0] 的 blue + red 条数应等于 DSH_OPT.gantt 的任务行数（甘特条数）。为 0 = 失败。
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const HTML = process.argv[2] ||
  path.join(ROOT, '输出结果', '计划_plan_run_1789818211', '计划看板.html');
const ECHARTS = process.argv[3] ||
  path.join(ROOT, 'backend', 'static', 'vendor', 'echarts.min.js');

const html = fs.readFileSync(HTML, 'utf8');
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const glue = scripts[scripts.length - 1];
if (!glue) { console.error('FAIL: 看板里找不到 glue <script>'); process.exit(1); }

// 从 glue 里抠出 DSH_OPT（平衡花括号，跳过字符串）
function extractOpt(src) {
  const at = src.indexOf('DSH_OPT');
  if (at < 0) return null;
  const b = src.indexOf('{', at);
  let depth = 0, i = b, inStr = false, q = '';
  for (; i < src.length; i++) {
    const c = src[i];
    if (inStr) { if (c === '\\') { i++; continue; } if (c === q) inStr = false; continue; }
    if (c === '"' || c === "'") { inStr = true; q = c; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  return JSON.parse(src.slice(b, i));
}

const echarts = require(ECHARTS);
// Node 里没有 canvas：给 ECharts 一个 measureText 平台实现，SSR 才能量文字宽度
if (echarts.setPlatformAPI) {
  echarts.setPlatformAPI({
    createCanvas: () => ({
      getContext: () => ({ measureText: (t) => ({ width: String(t).length * 7 }) }),
      width: 1400, height: 1500,
    }),
    measureText: (t) => ({ width: String(t == null ? '' : t).length * 7 }),
    loadImage: (s, cb) => { if (cb) cb({ width: 0, height: 0 }); return { width: 0, height: 0 }; },
  });
}

const charts = [];
const realInit = echarts.init.bind(echarts);
echarts.init = function (dom) {
  const c = realInit(null, null, { renderer: 'svg', ssr: true, width: 1400, height: 1500 });
  c.__dom = dom;
  charts.push(c);
  return c;
};

const el = (id) => ({
  id, clientWidth: 1400, clientHeight: 1500, style: {}, innerHTML: '', textContent: '',
  addEventListener() {}, appendChild() {}, setAttribute() {},
  getBoundingClientRect: () => ({ width: 1400, height: 1500, left: 0, top: 0 }),
  querySelectorAll: () => [], querySelector: () => null,
  getElementsByTagName: () => [], getAttribute: () => null,
  classList: { add() {}, remove() {} },
});
const cache = {};
const OPT = extractOpt(glue);
const expectBars = OPT && OPT.gantt ? OPT.gantt.series[0].data.length : 0;

global.echarts = echarts;                        // glue 用的是裸 echarts
global.window = { echarts, DSH_OPT: OPT, addEventListener() {}, removeEventListener() {}, devicePixelRatio: 1 };
global.document = {
  getElementById: (id) => (cache[id] = cache[id] || el(id)),
  querySelectorAll: () => [], querySelector: () => null,
  addEventListener() {}, createElement: () => el('x'),
};

try { vm.runInThisContext(glue, { filename: 'glue.js' }); }
catch (e) { console.error('FAIL: glue 抛错\n' + (e && e.stack ? e.stack.split('\n').slice(0, 5).join('\n') : e)); process.exit(1); }

const gantt = charts.find((c) => c.__dom && c.__dom.id === 'dsh-gantt');
if (!gantt) { console.error('FAIL: glue 没有初始化 dsh-gantt'); process.exit(1); }

const svg = gantt.renderToSVGString();
const blue = (svg.match(/#5470c6/gi) || []).length;
const red = (svg.match(/#ee6666/gi) || []).length;
const bars = blue + red;

console.log(`echarts ${echarts.version} | 期望甘特条数 ${expectBars} | 实画 blue=${blue} red=${red} 合计=${bars}`);
if (bars !== expectBars) {
  console.error(`FAIL: 甘特条数 ${bars} != ${expectBars}（0 表示 renderItem 被 applyFilters 抹掉了）`);
  process.exit(1);
}
console.log('OK: 甘特 custom series 正常出条');
process.exit(0);
