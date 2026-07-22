import manifest from "../../../test-data/ux-08-resumes/manifest.json";

function middleScore(range) {
  const values = String(range).match(/\d+/g)?.map(Number) || [];
  return values.length === 2 ? Math.round((values[0] + values[1]) / 2) : null;
}

function typeLabel(format) {
  if (format === "docx") return "DOCX";
  if (format === "txt") return "TXT";
  return "PDF";
}

function recommendation(score, parseStatus) {
  if (parseStatus === "failed") return "待重试";
  if (score >= 82) return "优先沟通";
  if (score >= 68) return "人工复核";
  return "暂不优先";
}

export const syntheticResumeFiles = manifest.map((item) => {
  const score = middleScore(item.expectedScoreRange);
  return {
    id: item.id,
    name: item.filename,
    candidate: item.name,
    email: item.email,
    phone: item.phone,
    size: "合成样本",
    type: typeLabel(item.format),
    valid: true,
    targetPosition: item.targetPosition,
    expectedParseStatus: item.expectedParseStatus,
    expectedLlmStatus: item.expectedLlmStatus || "success",
    scenarioTags: item.scenarioTags,
    skills: item.skills,
    missing: item.missing,
    ruleScore: score,
    llmScore: score == null ? null : Math.max(0, score - 3),
    recommendation: recommendation(score, item.expectedParseStatus),
    matched: item.skills.join("、") || "待解析",
    risk: item.missing,
    synthetic: true,
  };
});

export function syntheticResumeFilesFor(position) {
  return syntheticResumeFiles.filter((file) => file.targetPosition === position);
}
