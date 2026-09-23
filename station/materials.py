"""求职/资格材料的字段分级与按角色脱敏。

字段分四个可见域：
- identity: 身份与联系信息（办理入住、处理诉求所必需）
- eligibility: 资格判断字段（户籍、毕业年限、学历、来城目的）
- service: 安排服务所必需（企业参访意向、到达安排等）
- job_search: 求职材料中的非必要信息（期望薪资、简历正文、作品集等）

除申请人本人外，任何角色都不应看到与自己职责无关的域；
团干部（驿站服务官）处理服务诉求时只能看到 identity + service，
无权查看 job_search，也无权查看 eligibility 原始材料。
"""

# key: (标签, 可见域)
FIELD_REGISTRY = {
    # identity
    "name": ("姓名", "identity"),
    "phone": ("联系电话", "identity"),
    "id_card_masked": ("证件号（脱敏）", "identity"),
    # eligibility（资格四要素 + 户籍地，供核验）
    "household_type": ("户籍类型", "eligibility"),
    "household_region": ("户籍所在地", "eligibility"),
    "graduation_date": ("毕业日期", "eligibility"),
    "degree": ("学历", "eligibility"),
    "purpose": ("来城目的", "eligibility"),
    # service（服务安排需要）
    "visit_intentions": ("企业参访意向", "service"),
    "arrival_note": ("到达安排备注", "service"),
    # job_search（非必要求职材料，默认仅本人可见）
    "target_positions": ("意向岗位", "job_search"),
    "expected_salary": ("期望薪资", "job_search"),
    "resume_summary": ("简历摘要", "job_search"),
    "portfolio_url": ("作品集链接", "job_search"),
    "work_history": ("工作经历", "job_search"),
    "reference_contacts": ("推荐人联系方式", "job_search"),
}

ELIGIBILITY_FIELDS = ("household_type", "household_region", "graduation_date", "degree", "purpose")
JOB_SEARCH_FIELDS = tuple(k for k, (_, d) in FIELD_REGISTRY.items() if d == "job_search")

# 角色 -> 可见域
ROLE_DOMAINS = {
    "applicant": {"identity", "eligibility", "service", "job_search"},
    "verifier": {"identity", "eligibility"},            # 住宿运营核验岗
    "service_officer": {"identity", "service"},        # 团干部/驿站服务官
    "duty_manager": {"identity", "eligibility", "service"},  # 值班长（人工例外责任人）
    "finance": {"identity"},                            # 财政仅看身份最小集 + 系统资格结论
    "hotel_front": {"identity"},                        # 前台仅看身份最小集
}

# 前台/财政即使在 identity 域内也只开放这些最小字段
IDENTITY_MINIMAL = {"name", "id_card_masked"}
ROLE_IDENTITY_ALLOWLIST = {
    "hotel_front": IDENTITY_MINIMAL,
    "finance": IDENTITY_MINIMAL,
}

MASK = "***无权查看***"


def field_domain(key):
    return FIELD_REGISTRY.get(key, ("", "job_search"))[1]


def can_see(role, key):
    domain = field_domain(key)
    if domain not in ROLE_DOMAINS.get(role, ()):
        return False
    allow = ROLE_IDENTITY_ALLOWLIST.get(role)
    if allow is not None and domain == "identity" and key not in allow:
        return False
    return True


def redact_fields(role, material):
    """按角色投影材料字典；不可见字段以 MASK 占位（保留键以表明该字段存在）。"""
    out = {}
    for key, value in material.items():
        if can_see(role, key):
            out[key] = value
        else:
            out[key] = MASK
    return out


def visible_snapshot(role, material):
    """只返回该角色可见字段（不暴露被隐藏字段的存在）。"""
    return {k: v for k, v in material.items() if can_see(role, k)}
