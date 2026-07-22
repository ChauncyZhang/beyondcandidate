import test from "node:test";
import assert from "node:assert/strict";
import { buildResumeDocument } from "./resumeDocument.js";

const candidate = {
  name: "李嘉明",
  role: "AI 算法工程师",
  company: "字节",
  city: "北京",
  phone: "138****2468",
  email: "lij***@mail.com",
  summary: "负责过企业级 RAG 和 Agent 项目。",
  skills: ["Python", "PyTorch", "RAG"],
  experience: "5 年算法与大模型应用经验",
  education: "北京邮电大学 · 计算机硕士",
};

test("builds candidate-specific resume metadata and two preview pages", () => {
  const document = buildResumeDocument(candidate);

  assert.equal(document.fileName, "李嘉明_简历.txt");
  assert.equal(document.mimeType, "text/plain;charset=utf-8");
  assert.equal(document.pages.length, 2);
  assert.match(document.pages[0].content, /李嘉明/);
  assert.match(document.pages[0].content, /AI 算法工程师/);
  assert.match(document.pages[1].content, /Python、PyTorch、RAG/);
});

test("includes summary, experience, education, and skills in download text", () => {
  const { downloadText } = buildResumeDocument(candidate);

  assert.match(downloadText, /负责过企业级 RAG 和 Agent 项目/);
  assert.match(downloadText, /5 年算法与大模型应用经验/);
  assert.match(downloadText, /北京邮电大学 · 计算机硕士/);
  assert.match(downloadText, /Python、PyTorch、RAG/);
});
