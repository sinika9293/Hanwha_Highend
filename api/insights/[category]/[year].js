const {
  listMonthlyLabels,
  listGlobalCaseLabels,
  fetchMonthly,
  fetchGlobalCase,
} = require("../../_lib/github");
const {
  monthlyToTrendInsight,
  monthlyToItemsInsight,
  globalCaseToInsight,
} = require("../../_lib/adapter");

const VALID_CATEGORIES = new Set(["trend", "amenity", "facility", "global_case"]);

module.exports = async function handler(req, res) {
  res.setHeader("Cache-Control", "s-maxage=300, stale-while-revalidate=60");
  const { category, year } = req.query || {};

  if (!VALID_CATEGORIES.has(category)) {
    res.status(400).json({ error: `알 수 없는 category: ${category}` });
    return;
  }

  try {
    if (category === "global_case") {
      const labels = (await listGlobalCaseLabels()).filter((l) => l.startsWith(String(year)));
      const items = await Promise.all(
        labels.map(async (label) => ({ label, data: await fetchGlobalCase(label) }))
      );
      res.status(200).json(globalCaseToInsight(items));
      return;
    }

    const labels = (await listMonthlyLabels()).filter((l) => l.startsWith(String(year)));
    const items = await Promise.all(
      labels.map(async (label) => ({ label, data: await fetchMonthly(label) }))
    );

    if (category === "trend") {
      res.status(200).json(monthlyToTrendInsight(items));
    } else {
      res.status(200).json(monthlyToItemsInsight(category, items));
    }
  } catch (err) {
    res.status(502).json({
      error: "인사이트 데이터를 불러오지 못했습니다.",
      detail: String((err && err.message) || err),
    });
  }
};
