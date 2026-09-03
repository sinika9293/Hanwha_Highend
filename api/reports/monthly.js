const { listMonthlyLabels, fetchMonthly } = require("../_lib/github");
const { monthlyToReportSummary } = require("../_lib/adapter");

module.exports = async function handler(req, res) {
  res.setHeader("Cache-Control", "s-maxage=300, stale-while-revalidate=60");
  try {
    const { month } = req.query || {};
    const labels = await listMonthlyLabels();

    if (month) {
      if (!labels.includes(month)) {
        res.status(404).json({ error: `월간 리포트 데이터를 찾을 수 없습니다: ${month}` });
        return;
      }
      const data = await fetchMonthly(month);
      res.status(200).json(monthlyToReportSummary(month, data));
      return;
    }

    const reports = await Promise.all(
      labels.map(async (label) => monthlyToReportSummary(label, await fetchMonthly(label)))
    );
    res.status(200).json({ reports });
  } catch (err) {
    res.status(502).json({
      error: "월간 리포트 데이터를 불러오지 못했습니다.",
      detail: String((err && err.message) || err),
    });
  }
};
