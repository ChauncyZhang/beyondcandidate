export function buildResumeDocument(candidate) {
  const skills = candidate.skills.join("、");
  const pageOne = [
    candidate.name,
    `${candidate.role} · ${candidate.company} · ${candidate.city}`,
    `手机：${candidate.phone}`,
    `邮箱：${candidate.email}`,
    "",
    "个人概述",
    candidate.summary,
  ].join("\n");
  const pageTwo = [
    "专业能力",
    skills,
    "",
    "工作经验",
    candidate.experience,
    "",
    "教育经历",
    candidate.education,
  ].join("\n");

  return {
    fileName: `${candidate.name}_简历.txt`,
    mimeType: "text/plain;charset=utf-8",
    pages: [
      { number: 1, title: "基本信息与个人概述", content: pageOne },
      { number: 2, title: "能力与经历", content: pageTwo },
    ],
    downloadText: `${pageOne}\n\n${pageTwo}\n`,
  };
}
