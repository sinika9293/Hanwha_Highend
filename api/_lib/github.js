/**
 * GitHub 저장소의 data/ 폴더에 접근하는 유일한 I/O 모듈.
 * Contents API로 파일 목록을 얻고, raw.githubusercontent.com에서 실제 JSON을 받아온다.
 * GITHUB_TOKEN 환경변수가 있으면 인증 요청으로 rate limit을 완화한다.
 */

const OWNER = "sinika9293";
const REPO = "Hanwha_Highend";
const DATA_DIR = "data";

const MONTHLY_PATTERN = /^(\d{4}-\d{2})_monthly\.json$/;
const GLOBAL_CASE_PATTERN = /^(\d{4}-\d{2})_global_case\.json$/;

function authHeaders() {
  const headers = { "User-Agent": "muwa-highend-report" };
  if (process.env.GITHUB_TOKEN) {
    headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  }
  return headers;
}

async function listDataFiles() {
  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/contents/${DATA_DIR}`,
    { headers: authHeaders() }
  );
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`GitHub contents API ${res.status}`);
  return res.json();
}

async function listLabelsMatching(pattern) {
  const files = await listDataFiles();
  return files
    .filter((f) => f.type === "file" && pattern.test(f.name))
    .map((f) => f.name.match(pattern)[1])
    .sort((a, b) => b.localeCompare(a));
}

function listMonthlyLabels() {
  return listLabelsMatching(MONTHLY_PATTERN);
}

function listGlobalCaseLabels() {
  return listLabelsMatching(GLOBAL_CASE_PATTERN);
}

function rawUrl(name) {
  return `https://raw.githubusercontent.com/${OWNER}/${REPO}/main/${DATA_DIR}/${encodeURIComponent(name)}`;
}

async function fetchJson(name) {
  const res = await fetch(rawUrl(name), { headers: authHeaders() });
  if (!res.ok) throw new Error(`raw fetch ${res.status}: ${name}`);
  return res.json();
}

function fetchMonthly(label) {
  return fetchJson(`${label}_monthly.json`);
}

function fetchGlobalCase(label) {
  return fetchJson(`${label}_global_case.json`);
}

module.exports = {
  OWNER,
  REPO,
  listMonthlyLabels,
  listGlobalCaseLabels,
  fetchMonthly,
  fetchGlobalCase,
};
