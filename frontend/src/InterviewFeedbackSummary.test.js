import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { formatSubmittedFeedbackRatings } from "./feedbackRatings.js";

const source = readFileSync(new URL("./InterviewViews.jsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

test("non-participant reviewers load submitted feedback summaries instead of my-feedback", () => {
  assert.match(source, /ownsFeedback \? controller\.getMyFeedback\(record\.id, requestOptions\) : controller\.listFeedbacks\(record\.id, requestOptions\)/);
  assert.match(source, /summaryFeedbacks\.map/);
  assert.match(source, /暂无已提交反馈/);
});

test("submitted feedback ratings include their dimension labels", () => {
  assert.deepEqual(
    formatSubmittedFeedbackRatings({
      professional: "优秀",
      problem: "一般",
      communication: "需提升",
      fit: "优秀",
    }),
    ["专业能力：优秀", "问题解决：一般", "沟通协作：需提升", "岗位匹配：优秀"],
  );
  assert.match(source, /formatSubmittedFeedbackRatings\(feedback\.ratings\)\.map/);
});

test("submitted feedback cards use a compact aligned content layout", () => {
  assert.match(source, /className="submitted-feedback-card"/);
  assert.match(styles, /\.submitted-feedback-card\s*\{[^}]*padding:\s*0 16px 14px/s);
  assert.match(styles, /\.submitted-feedback-card\s*>\s*header\s*\{[^}]*margin:\s*0 -16px/s);
  assert.match(styles, /\.submitted-feedback-card\s*>\s*p\s*\{[^}]*margin:\s*0 0 10px/s);
  assert.match(styles, /\.submitted-feedback-card\s*\.feedback-priorities\s*\{[^}]*margin:\s*12px 0 0/s);
});
