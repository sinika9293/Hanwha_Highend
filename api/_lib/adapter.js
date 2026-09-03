/**
 * (A) 월간 리포트 JSON을 (A)/(B) 두 API가 소비할 형태로 매핑하는 순수 함수 모듈.
 * I/O 없음 — 네트워크 없이 유닛 테스트 가능해야 한다.
 */

function groupByTier(listings) {
  const groups = { S: [], A: [], overseas: [] };
  const byId = new Map((listings.properties || []).map((p) => [p.id, p]));
  const rankings = [...(listings.rankings || [])].sort((a, b) => a.rank - b.rank);

  for (const r of rankings) {
    const prop = byId.get(r.property_id);
    if (!prop) continue;
    const tier = prop.tier === "S" || prop.tier === "A" ? prop.tier : "overseas";
    groups[tier].push({
      id: prop.id,
      name: prop.name,
      region: prop.region,
      dong: prop.dong,
      city: prop.city,
      deal_type: prop.deal_type,
      price_range: prop.price_range,
      description: prop.description,
      rank: r.rank,
      rationale: r.rationale,
    });
  }
  return groups;
}

function mdUrl(label) {
  return `https://github.com/sinika9293/Hanwha_Highend/blob/main/${label}_하이엔드주거_트렌드보고서.md`;
}

function monthlyToReportSummary(label, data) {
  const trend = data.trend || {};
  return {
    label,
    range_label: data.range_label,
    generated_on: data.generated_on,
    mdUrl: mdUrl(label),
    executive_summary: trend.executive_summary || [],
    tiers: groupByTier(data.listings || {}),
  };
}

function monthlyToTrendInsight(items) {
  const domestic = [];
  const overseas = [];
  for (const { label, data } of items) {
    const trend = data.trend || {};
    for (const text of trend.executive_summary || []) {
      domestic.push({ label, description: text });
    }
    for (const h of trend.overseas_highlights || []) {
      overseas.push({ label, name: h.keyword, description: h.description });
    }
  }
  return { domestic, overseas };
}

function monthlyToItemsInsight(section, items) {
  let intro = "";
  const domestic = [];
  const overseas = [];
  for (const { label, data } of items) {
    const sec = data[section] || {};
    if (!intro && sec.intro) intro = sec.intro;
    for (const item of sec.items || []) {
      const entry = { label, name: item.name, description: item.description };
      if (item.region === "overseas") overseas.push(entry);
      else domestic.push(entry);
    }
  }
  return { intro, domestic, overseas };
}

function globalCaseToInsight(items) {
  let intro = "";
  const deals = [];
  for (const { label, data } of items) {
    if (!intro && data.market_overview) intro = data.market_overview;
    for (const deal of data.deals || []) {
      deals.push({ label, ...deal });
    }
  }
  return { intro, deals };
}

module.exports = {
  groupByTier,
  monthlyToReportSummary,
  monthlyToTrendInsight,
  monthlyToItemsInsight,
  globalCaseToInsight,
};
