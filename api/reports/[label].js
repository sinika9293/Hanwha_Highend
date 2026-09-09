const { listMonthlyLabels, fetchMonthly, fetchGlobalCase } = require("../_lib/github");
const { monthlyToReportDetail } = require("../_lib/adapter");

module.exports = async function handler(req, res) {
  res.setHeader("Cache-Control", "s-maxage=300, stale-while-revalidate=60");
  try {
    const { label } = req.query || {};
    const labels = await listMonthlyLabels();
    if (!label || !labels.includes(label)) {
      res.status(404).json({ error: `리포트 데이터를 찾을 수 없습니다: ${label}` });
      return;
    }

    const monthlyData = await fetchMonthly(label);
    let globalCaseData = {};
    try {
      globalCaseData = await fetchGlobalCase(label);
    } catch {
      globalCaseData = {};
    }

    res.status(200).json(monthlyToReportDetail(label, monthlyData, globalCaseData));
  } catch (err) {
    res.status(502).json({
      error: "리포트 상세 데이터를 불러오지 못했습니다.",
      detail: String((err && err.message) || err),
    });
  }
};
