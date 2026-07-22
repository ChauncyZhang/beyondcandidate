from server.app.recruiting.resume_profile import extract_resume_profile


def test_extract_resume_profile_reads_common_chinese_sections_without_contact_details() -> None:
    profile = extract_resume_profile(
        """
        郭莹雪
        手机：13800000000
        个人简介
        8 年研发经验，负责过企业级 Agent 平台从设计到上线。
        专业技能
        Python、FastAPI、RAG、Docker、Kubernetes
        工作经历
        2021.03-至今 某科技公司 技术负责人
        负责 AI Agent 平台和知识库产品交付
        教育经历
        2012.09-2016.06 浙江大学 计算机科学与技术 本科
        """
    )

    assert profile == {
        "summary": "8 年研发经验，负责过企业级 Agent 平台从设计到上线。",
        "skills": ["Python", "FastAPI", "RAG", "Docker", "Kubernetes"],
        "experience": "2021.03-至今 某科技公司 技术负责人；负责 AI Agent 平台和知识库产品交付",
        "education": "2012.09-2016.06 浙江大学 计算机科学与技术 本科",
        "status": "ready",
    }
    assert "13800000000" not in str(profile)


def test_extract_resume_profile_reports_partial_data_instead_of_placeholder_copy() -> None:
    profile = extract_resume_profile("技能\nPython, SQL")

    assert profile["skills"] == ["Python", "SQL"]
    assert profile["summary"] is None
    assert profile["experience"] is None
    assert profile["education"] is None
    assert profile["status"] == "partial"


def test_extract_resume_profile_uses_experience_as_summary_and_repairs_pdf_skill_spacing() -> None:
    profile = extract_resume_profile(
        """
        技能
        用户调研、工具、Py thon、Ja v aScrip t
        工作经历
        平台 产品经 理，小米科技 | 2024.08 - 至今
        负责 AI Agent 产品设计与交付
        教育经历
        景观建筑硕士；W ebsite: www.sno wguo.c om；密歇根安娜堡大学；2021.09 - 2024.05
        """
    )

    assert profile["summary"] == "平台产品经理，小米科技 | 2024.08 - 至今；负责 AI Agent 产品设计与交付"
    assert profile["skills"] == ["用户调研", "Python", "JavaScript"]
    assert profile["education"] == "景观建筑硕士；密歇根安娜堡大学；2021.09 - 2024.05"


def test_extract_resume_profile_ignores_repeated_pdf_obfuscation_markers() -> None:
    marker = "bf63fd04e3f2ddac1HJ-3Ni8EFBSwYm9V_6cWOGnn_HZMhll"
    profile = extract_resume_profile(
        f"""
        个人简介
        负责企业财务系统搭建和流程优化。
        {marker}
        {marker}
        工作经历
        2022.01-至今 某科技公司 财务经理
        {marker}
        {marker}
        教育经历
        2014.09-2018.06 某大学 会计学 本科
        {marker}
        {marker}
        """
    )

    assert profile["summary"] == "负责企业财务系统搭建和流程优化。"
    assert profile["experience"] == "2022.01-至今 某科技公司 财务经理"
    assert profile["education"] == "2014.09-2018.06 某大学 会计学 本科"
    assert marker not in str(profile)


def test_extract_resume_profile_repairs_legacy_boss_trailing_section_headings() -> None:
    profile = extract_resume_profile(
        """
        求 职 信 息
        工 作 时 长 ： 22 年
        求 职 意 向 ： 财 务 经 理 / 主 管
        期 望 城 市 ： 深 圳
        熟 练 操 作 上 市 公 司 生 产 制 造 业 全 盘 帐 务 处 理 工 作 。
        具 有 良 好 的 职 业 道 德 和 职 业 操 守 ， 严 谨 细 致 ， 善 于 沟 通 。
        个 人 优 势
        深 圳 博 雅 英 杰 电 子 有 限 公 司 财 务 经 理 / 主 管 2 0 1 8 . 0 8 - 至 今
        1 、 负 责 上 市 公 司 全 盘 帐 务 处 理 。
        工 作 经 历
        2 、 建 立 健 全 财 务 系 统 。
        四 川 大 学 大 专 会 计 1 9 9 6 - 1 9 9 8
        教 育 经 历
        """
    )

    assert profile["summary"] == "熟练操作上市公司生产制造业全盘帐务处理工作。具有良好的职业道德和职业操守，严谨细致，善于沟通。"
    assert profile["experience"] == "深圳博雅英杰电子有限公司财务经理/主管 2018.08 - 至今；1、负责上市公司全盘帐务处理。；2、建立健全财务系统。"
    assert profile["education"] == "四川大学 大专 会计 1996-1998"


def test_extract_resume_profile_prepends_school_before_trailing_education_heading() -> None:
    profile = extract_resume_profile(
        """
        工作经历
        某公司 采购专员
        深圳信息职业技术学院 大专 室内环境检测与控制技术 2017-2020
        教育经历
        在校担任书法协会组织部副部长，负责活动筹备工作。
        """
    )

    assert profile["experience"] == "某公司 采购专员"
    assert profile["education"] == "深圳信息职业技术学院 大专 室内环境检测与控制技术 2017-2020；在校担任书法协会组织部副部长，负责活动筹备工作。"


def test_extract_resume_profile_keeps_normal_sections_separate_with_noisy_pdf_text() -> None:
    marker = "c1b27f8d4f1852011HF93dq8FIRQwY-6UPqaWOWjlvbRNxlm1W~~"
    profile = extract_resume_profile(
        f"""
        {marker}
        2024年09月2022年09月2020年09月2017年08月2024年01月2025年11月
        刘晨
        13800000000 liu@example.com
        GitHub: github.com/example
        求职意向
        AI 工程师
        个人总结
        具备从零搭建 ReAct 智能体和多 Agent 协作系统的工程经验。
        熟练掌握模型微调、RAG 与工具调用。
        {marker}
        教育经历
        2022年09月-2026年05月 威斯康星大学 计算机科学 硕士
        2017年09月-2021年06月 北京理工大学 应用物理学 本科
        技术栈
        语言、框架：Python、PyTorch、HuggingFace（Transformers/PEFT）
        训练技术：SFT、LoRA、DPO
        工作经历
        2025年11月-2026年02月 某科技公司 AI 工程师
        负责 Agent 平台研发和评测体系建设。
        """
    )

    assert profile["summary"] == "具备从零搭建 ReAct 智能体和多 Agent 协作系统的工程经验。熟练掌握模型微调、RAG 与工具调用。"
    assert profile["experience"] == "2025年11月-2026年02月 某科技公司 AI 工程师；负责 Agent 平台研发和评测体系建设。"
    assert profile["education"] == "2022年09月-2026年05月 威斯康星大学 计算机科学 硕士；2017年09月-2021年06月 北京理工大学 应用物理学 本科"
    assert profile["skills"] == ["Python", "PyTorch", "HuggingFace", "Transformers", "PEFT", "SFT", "LoRA", "DPO"]
    assert marker not in str(profile)
    assert "GitHub" not in str(profile)
    assert "2024年09月2022年09月" not in str(profile)


def test_extract_resume_profile_supports_english_section_headings() -> None:
    profile = extract_resume_profile(
        """
        PROFESSIONAL SUMMARY
        Machine learning engineer building production retrieval systems.
        TECHNICAL SKILLS
        Python | FastAPI | PostgreSQL
        EMPLOYMENT HISTORY
        2021 - Present Example Labs, Senior ML Engineer
        EDUCATION
        2017 - 2021 Example University, B.Sc. Computer Science
        """
    )

    assert profile["summary"] == "Machine learning engineer building production retrieval systems."
    assert profile["skills"] == ["Python", "FastAPI", "PostgreSQL"]
    assert profile["experience"] == "2021 - Present Example Labs, Senior ML Engineer"
    assert profile["education"] == "2017 - 2021 Example University, B.Sc. Computer Science"
    assert profile["status"] == "ready"


def test_extract_resume_profile_normalizes_layout_parser_markdown() -> None:
    profile = extract_resume_profile(
        """
        # **候选人甲**
        candidate@example.test | 求职意向：AI 工程师 **个人总结** 具备企业级 Agent 平台研发和交付经验。
        ## <mark>教育背景</mark>
        **示例大学** - 计算机科学 硕士 2022年09月 - 2024年06月
        ## **技术栈**
        - **语言、框架：** Python、PyTorch、FastAPI
        ## <mark>工作经历</mark>
        ### **示例科技** - AI 工程师
        2024年07月 - 至今
        - 负责大模型应用、RAG 与 Agent 工作流建设。 **项目经历**
        - 从零实现多智能体协作平台。
        """
    )

    assert profile["summary"] == "具备企业级 Agent 平台研发和交付经验。"
    assert set(profile["skills"]) >= {"Python", "PyTorch", "FastAPI"}
    assert "示例科技 - AI 工程师" in profile["experience"]
    assert "从零实现多智能体协作平台" in profile["experience"]
    assert profile["education"] == "示例大学 - 计算机科学 硕士 2022年09月 - 2024年06月"
    assert profile["status"] == "ready"


def test_extract_resume_profile_recovers_preamble_summary_and_untitled_education() -> None:
    profile = extract_resume_profile(
        """
        # 候选人乙
        示例大学 · 软件工程 · 2023 届 | 全日制 · 本科 | 工作年限：3 年
        具备用户体验设计背景，专注于 AI 驱动企业级平台的构建与规模化落地。
        ## 工作经历
        示例公司 平台产品经理 2024.08 - 至今
        负责 AI Agent 产品设计与交付。
        ## 专业技能
        Python、RAG、Agent
        """
    )

    assert profile["summary"] == "具备用户体验设计背景，专注于 AI 驱动企业级平台的构建与规模化落地。"
    assert profile["education"] == "示例大学 · 软件工程 · 2023 届 | 全日制 · 本科 | 工作年限：3 年"
    assert profile["experience"] == "示例公司 平台产品经理 2024.08 - 至今；负责 AI Agent 产品设计与交付。"
    assert set(profile["skills"]) >= {"Python", "RAG", "Agent"}
    assert profile["status"] == "ready"


def test_extract_resume_profile_does_not_infer_education_from_summary_sentence() -> None:
    profile = extract_resume_profile(
        """
        个人总结
        毕业后持续从事平台研发，具备硕士阶段形成的系统分析能力。
        教育经历
        2018.09-2021.06 某大学 软件工程 硕士
        """
    )

    assert profile["summary"] == "毕业后持续从事平台研发，具备硕士阶段形成的系统分析能力。"
    assert profile["education"] == "2018.09-2021.06 某大学 软件工程 硕士"


def test_extract_resume_profile_recovers_boss_columns_when_headings_are_at_the_end() -> None:
    profile = extract_resume_profile(
        """
        深圳市甲科技有限公司 会计兼跟单 2023.05-至今
        深圳市乙科技有限公司 会计 2012.08-2023.05
        武汉某大学外经贸学院 大专 会计 2009-2012
        熟练使用金蝶、用友、excel、word、wps等办公软件及常用函数统计分析，做事细心严谨，对工作认真负责，
        有较强的责任心和执行力，善于学习，有良好的沟通能力和团队精神。
        -负责应收应付账款核算管理，并定期出具账龄分析表；
        -负责订单报价及接单，跟进生产和物流；
        候选人甲
        女 | 34岁
        14年工作经验 | 求职意向：会计 | 期望城市：深圳
        个人优势
        工作经历
        教育经历
        """
    )

    assert profile["summary"] == (
        "熟练使用金蝶、用友、excel、word、wps等办公软件及常用函数统计分析，做事细心严谨，对工作认真负责，"
        "有较强的责任心和执行力，善于学习，有良好的沟通能力和团队精神。"
    )
    assert profile["experience"] == (
        "深圳市甲科技有限公司 会计兼跟单 2023.05-至今；"
        "深圳市乙科技有限公司 会计 2012.08-2023.05；"
        "负责应收应付账款核算管理，并定期出具账龄分析表；"
        "负责订单报价及接单，跟进生产和物流"
    )
    assert profile["education"] == "武汉某大学外经贸学院 大专 会计 2009-2012"
    assert set(profile["skills"]) >= {"金蝶", "用友", "Excel", "Word", "WPS"}
    assert "候选人甲" not in str(profile)
