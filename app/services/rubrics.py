CRITERIA = {
    "RESULT": ("결과물", "TASK_SOLUTION"),
    "PROMPT": ("질문 설계", "AI_USE"),
    "VERIFY": ("답변 검증", "AI_USE"),
    "IMPROVE": ("개선 과정", "AI_USE"),
}


def public_criteria(rubric: dict) -> list[dict]:
    return [
        {
            "code": code,
            "name": name,
            "dimension": dimension,
            "max_score": rubric["criterion_max_scores"][code],
        }
        for code, (name, dimension) in CRITERIA.items()
    ]


def public_requirements(rubric: dict) -> dict:
    if rubric["version"] == "LEGACY_TOTAL_V1":
        return {
            "version": rubric["version"],
            "min_total_score": rubric["min_total_score"],
        }
    return {
        "version": rubric["version"],
        "task_solution_min_score": rubric["task_solution_min_score"],
        "ai_use_min_score": rubric["ai_use_min_score"],
        "verify_min_score": rubric["verify_min_score"],
        "required_checks": [
            {"check_id": check["check_id"], "label": check["label"]}
            for check in rubric["required_checks"]
        ],
    }
