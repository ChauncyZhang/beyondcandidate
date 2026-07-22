export const feedbackRatingDimensions = [
  ["professional", "专业能力"],
  ["problem", "问题解决"],
  ["communication", "沟通协作"],
  ["fit", "岗位匹配"],
];

export function formatSubmittedFeedbackRatings(ratings = {}) {
  return feedbackRatingDimensions.map(([key, label]) => `${label}：${ratings[key] || "未评价"}`);
}
