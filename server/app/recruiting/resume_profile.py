import re
from collections.abc import Iterable

from server.app.resume_text import is_obfuscation_marker, normalize_resume_line, sanitize_resume_text


_SECTION_ALIASES = {
    "summary": ("简介", "个人简介", "个人总结", "个人优势", "核心优势", "自我评价", "职业概述", "职业简介", "profile", "summary", "professionalsummary", "aboutme", "about", "objective"),
    "skills": ("技能", "专业技能", "核心技能", "核心能力", "技术能力", "技术栈", "技能清单", "skills", "technicalskills", "competencies", "corecompetencies"),
    "experience": ("工作经历", "工作经验", "任职经历", "实习经历", "实践经历", "项目经历", "professionalexperience", "workexperience", "experience", "employmenthistory", "careerhistory", "projectexperience", "projects"),
    "education": ("教育", "教育经历", "教育背景", "教育及培训", "学历信息", "education", "academicbackground", "academicqualifications", "qualifications"),
}
_HEADING_LOOKUP = {
    re.sub(r"[\s:：_-]+", "", alias).casefold(): section
    for section, aliases in _SECTION_ALIASES.items()
    for alias in aliases
}
_CONTACT_PATTERN = re.compile(r"(?:\b1[3-9]\d{9}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})")
_BULLET_PATTERN = re.compile(r"^[\s\-•·▪●○◆◇►▸*\uf0b7]+")
_SKILL_PREFIX = re.compile(r"^(?:(?:语言[、及/]*)?框架|语言|训练技术|工具|技术工具|软件工具|数据库|平台|模型|熟练(?:掌握|使用)?|掌握|熟悉|了解|擅长|技能|技术栈)\s*[:：]?\s*", re.I)
_NON_SKILL_LABELS = {"工具", "技术工具", "软件工具", "语言", "框架", "训练技术", "数据库", "平台", "模型"}
_BOSS_METADATA_PATTERN = re.compile(r"^(?:个人信息|联系方式|求职信息|性别|年龄|电话|微信号|工作时长|求职意向|期望城市)\s*[:：]?")
_BOSS_PROFILE_METADATA = re.compile(r"(?:\d{1,2}\s*年工作经验|求职意向|期望薪资|期望城市|(?:男|女)\s*\|?\s*\d{1,2}\s*岁)")
_CANDIDATE_NAME_ONLY = re.compile(r"^[\u3400-\u9fff·]{2,8}$")
_EDUCATION_SIGNAL = re.compile(r"(?:大学|学院|学校|本科|大专|硕士|博士|中专|高中)")
_DEGREE_SIGNAL = re.compile(r"(?:博士|硕士|本科|大专|中专|高中)")
_INSTITUTION_WITH_DEGREE = re.compile(r"(?:大学|学院|学校)\s*(?:博士|硕士|本科|大专|中专|高中)")
_DATE_SIGNAL = re.compile(r"(?:(?:19|20)\d{2}|\d{2})\s*(?:年|[./-])")
_TIMELINE_ONLY = re.compile(r"^(?:(?:19|20)\d{2}\s*年?\s*\d{0,2}\s*(?:月|[./-])?\s*){2,}$")
_URL_PATTERN = re.compile(r"(?:https?://|www\.|\b(?:github|gitee|gitlab|linkedin)\.com/)", re.I)
_RESPONSIBILITY_PATTERN = re.compile(
    r"^(?:\d+[.、)]\s*)?(?:负责|组织|审核|协助|跟进|维护|处理|管理|编制|完成|执行|参与|统筹|开展|对接|"
    r"财务处理|往来核算|成本管理|税务管理|资金管理|费用审核)"
)
_CJK_COMPACT_TERMS = (
    "平台产品经理", "产品经理", "项目经理", "技术负责人", "智能制造",
    "软件工程师", "算法工程师", "前端工程师", "后端工程师", "大模型工程师",
)
_KNOWN_SKILLS = (
    "Python", "Java", "C++", "C#", "Golang", "Go", "JavaScript", "TypeScript", "React", "Vue",
    "PyTorch", "TensorFlow", "Transformers", "HuggingFace", "FastAPI", "Django", "Flask", "Spring Boot",
    "RAG", "Agent", "LangChain", "LlamaIndex", "LLM", "NLP", "Embedding", "Prompt Engineering", "MCP",
    "OpenAI", "Claude", "DeepSeek", "Llama", "SFT", "RLHF", "LoRA", "Docker", "Kubernetes", "K8s",
    "Linux", "SQL", "MySQL", "PostgreSQL", "Redis", "MongoDB", "Elasticsearch", "Kafka", "Spark",
    "AWS", "Azure", "GCP", "PEFT", "TRL", "DPO", "PPO", "GRPO", "QLoRA", "bitsandbytes",
    "Excel", "Word", "WPS", "金蝶", "用友",
)
_MARKDOWN_SECTION_PATTERN = re.compile(
    r"\*\*\s*(" + "|".join(
        sorted(
            (re.escape(alias) for aliases in _SECTION_ALIASES.values() for alias in aliases),
            key=len,
            reverse=True,
        )
    ) + r")\s*\*\*",
    re.I,
)


def _profile_lines(text: str) -> Iterable[str]:
    """Split layout-parser Markdown without coupling profile rules to one PDF template."""
    for raw_line in (text or "").splitlines():
        line = re.sub(r"</?mark>", "", raw_line, flags=re.I)
        line = _MARKDOWN_SECTION_PATTERN.sub(lambda match: f"\n{match.group(1)}\n", line)
        yield from line.splitlines()


def _clean_line(value: str) -> str:
    cleaned = re.sub(r"</?mark>", "", value, flags=re.I)
    cleaned = re.sub(r"^\s*#{1,6}\s*", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"^_([^_]+)_$", r"\1", cleaned.strip())
    cleaned = normalize_resume_line(_BULLET_PATTERN.sub("", cleaned))
    for term in _CJK_COMPACT_TERMS:
        cleaned = re.sub(r"\s*".join(map(re.escape, term)), term, cleaned)
    return cleaned


def _section_heading(line: str) -> tuple[str | None, str]:
    for separator in ("：", ":"):
        if separator in line:
            heading, remainder = line.split(separator, 1)
            key = re.sub(r"[\s_-]+", "", heading).casefold()
            if key in _HEADING_LOOKUP:
                return _HEADING_LOOKUP[key], _clean_line(remainder)
    key = re.sub(r"[\s:：_-]+", "", line).casefold()
    return (_HEADING_LOOKUP.get(key), "")


def _join_lines(lines: Iterable[str], *, limit: int) -> str | None:
    values: list[str] = []
    for line in lines:
        for segment in re.split(r"[；;]", line):
            value = segment.strip()
            compact = re.sub(r"\s+", "", value).casefold()
            if not value or is_obfuscation_marker(value) or _CONTACT_PATTERN.search(value) or _URL_PATTERN.search(value) or "website:" in compact or "website：" in compact:
                continue
            values.append(value)
    if not values:
        return None
    return "；".join(values)[:limit].rstrip("；")


def _join_summary_lines(lines: Iterable[str], *, limit: int) -> str | None:
    values: list[str] = []
    for line in lines:
        value = line.strip()
        compact = re.sub(r"\s+", "", value).casefold()
        if not value or is_obfuscation_marker(value) or _CONTACT_PATTERN.search(value) or _URL_PATTERN.search(value) or _TIMELINE_ONLY.fullmatch(compact) or "website:" in compact or "website：" in compact:
            continue
        values.append(value)
    if not values:
        return None
    return "".join(values)[:limit].rstrip("；")


def _format_education_line(line: str) -> str:
    parts = re.split(r"([；;])", line)
    for index in range(0, len(parts), 2):
        if _INSTITUTION_WITH_DEGREE.search(parts[index]):
            parts[index] = _DEGREE_SIGNAL.sub(lambda match: f" {match.group(0)} ", parts[index])
            parts[index] = re.sub(r"\s+", " ", parts[index]).strip()
    return "".join(parts)


def _deduplicate(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        canonical = value.casefold()
        if canonical in seen:
            continue
        seen.add(canonical)
        result.append(value)
    return result


def _skills_from_sections(lines: list[str], full_text: str) -> list[str]:
    tokens: list[str] = []
    for line in lines:
        cleaned = _SKILL_PREFIX.sub("", line)
        cleaned = re.sub(r"[()（）]", "、", cleaned)
        for token in re.split(r"[、,，;；/|]+", cleaned):
            value = token.strip(" .。()（）")
            compact = re.sub(r"\s+", "", value).casefold()
            normalized = next((skill for skill in _KNOWN_SKILLS if re.sub(r"\s+", "", skill).casefold() == compact), value)
            if normalized and normalized not in _NON_SKILL_LABELS and len(normalized) <= 40 and not _CONTACT_PATTERN.search(normalized) and not _URL_PATTERN.search(normalized):
                tokens.append(normalized)
    if not tokens:
        for skill in _KNOWN_SKILLS:
            if re.search(rf"(?<![A-Za-z0-9+#]){re.escape(skill)}(?![A-Za-z0-9+#])", full_text, re.I):
                tokens.append(skill)
    return _deduplicate(tokens)[:20]


def _recover_boss_trailing_heading_sections(
    cleaned_lines: list[str],
    sections: dict[str, list[str]],
) -> None:
    """Recover BOSS three-column PDFs whose section labels are extracted last."""
    tail_start = len(cleaned_lines)
    tail_sections: set[str] = set()
    for index in range(len(cleaned_lines) - 1, -1, -1):
        heading, remainder = _section_heading(cleaned_lines[index])
        if not heading or remainder:
            break
        tail_start = index
        tail_sections.add(heading)

    if not {"summary", "experience", "education"}.issubset(tail_sections):
        return

    body = cleaned_lines[:tail_start]
    if not body:
        return

    metadata_start = len(body)
    for index in range(max(0, len(body) - 8), len(body)):
        line = body[index]
        if _CONTACT_PATTERN.search(line) or _BOSS_PROFILE_METADATA.search(line):
            metadata_start = index
            if index > 0 and _CANDIDATE_NAME_ONLY.fullmatch(body[index - 1]):
                metadata_start -= 1
            break
    content = body[:metadata_start]

    education_lines = [
        line for line in content
        if _EDUCATION_SIGNAL.search(line) and _DATE_SIGNAL.search(line)
    ]
    dated_experience_lines = [
        line for line in content
        if _DATE_SIGNAL.search(line)
        and not _EDUCATION_SIGNAL.search(line)
        and not _TIMELINE_ONLY.fullmatch(re.sub(r"\s+", "", line))
    ]
    responsibility_index = next(
        (index for index, line in enumerate(content) if _RESPONSIBILITY_PATTERN.match(line)),
        len(content),
    )
    dated_before_responsibilities = [
        index for index, line in enumerate(content[:responsibility_index])
        if _DATE_SIGNAL.search(line)
    ]
    summary_start = dated_before_responsibilities[-1] + 1 if dated_before_responsibilities else 0
    summary_lines = [
        line for line in content[summary_start:responsibility_index]
        if not _EDUCATION_SIGNAL.search(line)
        and not _BOSS_PROFILE_METADATA.search(line)
        and not _CANDIDATE_NAME_ONLY.fullmatch(line)
    ]
    responsibility_lines = [
        line for line in content[responsibility_index:]
        if line not in education_lines
        and not _BOSS_PROFILE_METADATA.search(line)
        and not _CANDIDATE_NAME_ONLY.fullmatch(line)
    ]

    if summary_lines and not sections["summary"]:
        sections["summary"] = summary_lines
    if (dated_experience_lines or responsibility_lines) and not sections["experience"]:
        sections["experience"] = dated_experience_lines + responsibility_lines
    if education_lines and not sections["education"]:
        sections["education"] = education_lines


def extract_resume_profile(text: str, *, include_metadata: bool = False) -> dict[str, object]:
    """Extract display-safe profile fields from parsed resume text without returning raw text."""
    text = sanitize_resume_text(text)
    sections: dict[str, list[str]] = {name: [] for name in _SECTION_ALIASES}
    heading_positions: dict[str, list[int]] = {name: [] for name in _SECTION_ALIASES}
    current: str | None = None
    cleaned_lines: list[str] = []
    for raw_line in _profile_lines(text):
        line = _clean_line(raw_line)
        if not line:
            continue
        cleaned_lines.append(line)
        heading, remainder = _section_heading(line)
        if heading:
            heading_positions[heading].append(len(cleaned_lines) - 1)
            current = heading
            if remainder:
                sections[current].append(remainder)
            continue
        if current:
            sections[current].append(line)

    if heading_positions["summary"]:
        summary_index = heading_positions["summary"][0]
        preceding: list[str] = []
        nearby_before_summary = cleaned_lines[max(0, summary_index - 12):summary_index]
        for line in reversed(nearby_before_summary):
            if _section_heading(line)[0] or _BOSS_METADATA_PATTERN.match(line):
                if preceding:
                    break
                continue
            preceding.append(line)
        legacy_summary = list(reversed(preceding[:4]))
        has_boss_metadata = any(_BOSS_METADATA_PATTERN.match(line) for line in nearby_before_summary)

        experience_index = heading_positions["experience"][0] if heading_positions["experience"] else None
        is_trailing_legacy_layout = experience_index is not None and 1 <= experience_index - summary_index <= 6
        if len(legacy_summary) >= 2 and has_boss_metadata and is_trailing_legacy_layout:
            sections["summary"] = legacy_summary
            assert experience_index is not None
            if summary_index < experience_index:
                sections["experience"] = cleaned_lines[summary_index + 1:experience_index] + sections["experience"]

    if heading_positions["education"]:
        education_index = heading_positions["education"][0]
        preceding = cleaned_lines[max(0, education_index - 3):education_index]
        education_fallback = [line for line in preceding if _EDUCATION_SIGNAL.search(line) and _DATE_SIGNAL.search(line)]
        if education_fallback:
            sections["education"] = education_fallback + [
                line for line in sections["education"] if line not in education_fallback
            ]
            sections["experience"] = [line for line in sections["experience"] if line not in education_fallback]

    _recover_boss_trailing_heading_sections(cleaned_lines, sections)

    heading_indexes = [index for indexes in heading_positions.values() for index in indexes]
    first_heading_index = min(heading_indexes) if heading_indexes else len(cleaned_lines)
    preamble = cleaned_lines[:first_heading_index]
    if not sections["education"]:
        sections["education"] = [
            line for line in preamble
            if _EDUCATION_SIGNAL.search(line)
            and (_DATE_SIGNAL.search(line) or _DEGREE_SIGNAL.search(line))
            and not _CONTACT_PATTERN.search(line)
        ][:3]
    if not sections["summary"]:
        summary_preamble = [
            line for line in preamble
            if len(re.sub(r"\s+", "", line)) >= 20
            and not _CONTACT_PATTERN.search(line)
            and not _URL_PATTERN.search(line)
            and not _BOSS_PROFILE_METADATA.search(line)
            and not _EDUCATION_SIGNAL.search(line)
            and not _CANDIDATE_NAME_ONLY.fullmatch(line)
        ]
        if summary_preamble:
            sections["summary"] = summary_preamble[:2]

    full_text = "\n".join(cleaned_lines)
    experience = _join_lines(sections["experience"][:8], limit=600)
    explicit_summary = _join_summary_lines(sections["summary"], limit=320)
    summary = explicit_summary or _join_lines(sections["experience"][:2], limit=320)
    education = _join_lines((_format_education_line(line) for line in sections["education"][:5]), limit=400)
    skills = _skills_from_sections(sections["skills"], full_text)
    populated = sum((bool(summary), bool(skills), bool(experience), bool(education)))
    status = "ready" if populated == 4 else "partial" if populated else "unavailable"
    profile = {
        "summary": summary,
        "skills": skills,
        "experience": experience,
        "education": education,
        "status": status,
    }
    if include_metadata:
        profile["summary_origin"] = "resume" if explicit_summary else "generated" if summary else None
        profile["source"] = "rules"
    return profile
