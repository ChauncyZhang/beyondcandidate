import re
from pathlib import PurePath

from server.app.resume_text import normalize_resume_line, sanitize_resume_text


_EXPLICIT_NAME = re.compile(r"^(?:姓名|name)\s*[:：]\s*(.+)$", re.I)
_CJK_NAME = re.compile(r"(?:[\u3400-\u9fff]{2,4}|[\u3400-\u9fff]{1,6}·[\u3400-\u9fff·]{1,8})")
_LATIN_NAME = re.compile(r"[A-Za-z][A-Za-z'’-]{1,30}(?:\s+[A-Za-z][A-Za-z'’-]{1,30}){1,3}")
_NON_NAME_TERMS = (
    "简历", "信息", "资料", "简介", "总结", "评价", "背景", "概况", "档案", "求职", "应聘", "联系方式",
    "工作", "项目", "履历", "职业", "实践", "培训", "教育", "学历", "技能", "能力", "优势", "工程师", "经理",
    "总监", "负责人", "专员", "顾问", "开发", "算法", "产品", "设计", "财务", "销售",
    "采购", "行政", "人事", "运营", "招聘", "公司", "科技", "大学", "学院", "学校",
)
_LATIN_NON_NAME_WORDS = {
    "resume", "profile", "curriculum", "vitae", "summary", "engineer", "manager", "director",
    "developer", "designer", "candidate", "education", "experience", "skills", "scientist", "analyst",
    "architect", "consultant", "specialist", "lead", "officer", "assistant", "intern", "recruiter",
    "accountant", "finance", "sales", "marketing", "operations", "product", "project", "software",
    "hardware", "mechanical", "data",
    "quality", "assurance", "legal", "counsel", "research", "business", "strategy", "human", "resources",
    "administration", "administrative", "customer", "service", "support", "design", "account", "program",
    "career", "objective", "work", "history", "contact", "details", "employment", "academic", "background",
    "competencies", "certifications", "achievements", "languages", "interests", "references", "personal",
    "professional", "chief", "executive", "machine", "learning", "risk", "control",
}
_COMMON_CJK_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章"
    "云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安"
    "常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋"
    "茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊"
    "胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢滑"
    "裴陆荣翁荀羊甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋"
    "仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖"
    "卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍郤璩桑桂濮牛寿通边扈燕冀"
    "郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文"
    "寇广禄阙东欧利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相"
    "查后荆红游竺权逯盖益桓公"
)
_COMPOUND_CJK_SURNAMES = (
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于", "司空",
)
_FILENAME_RESUME_SUFFIX = re.compile(
    r"(?:[_\-\s]*(?:测试)?(?:个人)?简历|[_\-\s]*(?:resume|cv))$",
    re.I,
)


def _clean_identity_line(value: str) -> str:
    value = re.sub(r"</?mark>", "", value, flags=re.I)
    value = re.sub(r"^\s*#{1,6}\s*", "", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    return normalize_resume_line(value).strip(" \t-—_·•")


def _plausible_name(value: str, *, explicit: bool = False, identity_hints: tuple[str, ...] = ()) -> str | None:
    candidate = _clean_identity_line(value)
    compact = re.sub(r"\s+", "", candidate).casefold()
    if not candidate or any(term.casefold() in compact for term in _NON_NAME_TERMS):
        return None
    if _CJK_NAME.fullmatch(candidate):
        if explicit or "·" in candidate or candidate[0] in _COMMON_CJK_SURNAMES or candidate.startswith(_COMPOUND_CJK_SURNAMES):
            return candidate
    if _LATIN_NAME.fullmatch(candidate):
        words = tuple(word.casefold() for word in re.findall(r"[A-Za-z]+", candidate) if len(word) > 1)
        corroborated = any(all(word in hint for word in words) for hint in identity_hints)
        if (explicit or corroborated) and not set(words).intersection(_LATIN_NON_NAME_WORDS):
            return candidate
    return None


def extract_candidate_name(resume_text: str, *, filename: str | None = None) -> str | None:
    """Return a conservative name from resume content, never a section or role heading."""
    lines = [
        line
        for raw_line in sanitize_resume_text(resume_text).splitlines()
        if (line := _clean_identity_line(raw_line))
    ]
    for line in lines[:30]:
        match = _EXPLICIT_NAME.fullmatch(line)
        if not match:
            continue
        value = re.split(
            r"[|｜,，;；]|\s+(?=(?:性别|年龄|电话|手机|邮箱|所在地|求职意向)\s*[:：])",
            match.group(1),
            maxsplit=1,
        )[0]
        if name := _plausible_name(value, explicit=True):
            return name
    filename_hint = re.sub(r"[^a-z]", "", PurePath(filename).stem.casefold()) if filename else ""
    for index,line in enumerate(lines[:8]):
        nearby="\n".join(lines[max(0,index - 5):index] + lines[index + 1:index + 6])
        email_hints=tuple(
            re.sub(r"[^a-z]", "", match.group(1).casefold())
            for match in re.finditer(r"([\w.+-]+)@[\w.-]+\.[A-Za-z]{2,}",nearby)
        )
        identity_hints=tuple(hint for hint in (*email_hints,filename_hint) if hint)
        if name := _plausible_name(line, identity_hints=identity_hints):
            return name
    return None


def candidate_name_from_filename(filename: str) -> str:
    stem = PurePath(filename).stem.strip()
    cleaned = _FILENAME_RESUME_SUFFIX.sub("", stem).strip(" \t-—_")
    return (cleaned or stem or "Candidate")[:200]


def resolve_candidate_name(resume_text: str, filename: str) -> str:
    return (extract_candidate_name(resume_text,filename=filename) or candidate_name_from_filename(filename))[:200]
