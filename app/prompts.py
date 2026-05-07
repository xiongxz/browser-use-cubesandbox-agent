from __future__ import annotations

from .models import BrowserAgentRunRequest


FEISHU_DEFAULT_DOMAINS = [
    "*.feishu.cn",
    "*.larksuite.com",
    "*.larkoffice.com",
]


def effective_allowed_domains(request: BrowserAgentRunRequest) -> list[str]:
    if request.allowed_domains:
        return request.allowed_domains
    if request.mode == "feishu_bitable_to_form":
        return FEISHU_DEFAULT_DOMAINS.copy()
    return []


def build_task_prompt(request: BrowserAgentRunRequest) -> str:
    if request.mode == "feishu_bitable_to_form":
        assert request.bitable_url is not None
        if request.require_human_confirmation and not request.human_confirmation_granted:
            return (
                "You are operating Feishu to convert a Bitable into a questionnaire.\n"
                "Current phase: draft review before sharing.\n"
                "Goal:\n"
                f"- Open this Feishu Bitable URL: {request.bitable_url}\n"
                "- Stay focused on the current workspace and do not edit unrelated content.\n"
                "- Find the built-in capability that turns the Bitable into a questionnaire or form.\n"
                "- Open the questionnaire/form editing interface for this Bitable.\n"
                "- Capture the visible questionnaire title and visible draft questions from the editor.\n"
                "- Do not click share, do not enable form sharing, and do not finish by returning a link in this phase.\n"
                "- Stop only after you can present the draft questionnaire for human review.\n"
                "- If the page requires login or permission that is unavailable, stop and explain the exact blocker.\n"
                "- Return awaiting_human_confirmation=true together with the draft questions and form name.\n"
                "- Common labels may include Chinese or English variants such as: "
                "'转问卷', '转换为问卷', '问卷', '表单'.\n"
                "- The source user request is below.\n\n"
                f"User request: {request.query}"
            )

        confirmation_notes = request.human_confirmation_notes.strip() if request.human_confirmation_notes else ""
        return (
            "You are operating Feishu to convert a Bitable into a questionnaire.\n"
            "Current phase: publish/share after human confirmation.\n"
            "Goal:\n"
            f"- Open this Feishu Bitable URL: {request.bitable_url}\n"
            "- Stay focused on the current workspace and do not edit unrelated content.\n"
            "- Find the built-in capability that turns the Bitable into a questionnaire or form.\n"
            "- Enter the form/questionnaire editing interface for this Bitable.\n"
            "- If there are human review notes, apply the requested edits carefully before sharing.\n"
            "- Then click the top-right '分享表单' entry, enable the '开启表单分享' switch, and capture the final shareable questionnaire link.\n"
            "- Your finish condition is strict: do not stop until you have the real final questionnaire link visible and captured.\n"
            "- A successful run must end with the actual questionnaire URL in form_url. Merely opening the editor or share panel is not enough.\n"
            "- Common labels may include Chinese or English variants such as: "
            "'转问卷', '转换为问卷', '问卷', '表单', '分享表单', '开启表单分享', '发布', 'publish', 'share'.\n"
            "- If the page requires login or permission that is unavailable, stop and explain the exact blocker.\n"
            "- When you finish, return the generated questionnaire URL.\n"
            "- Do not fabricate a URL. If you cannot see the final questionnaire link, report failure.\n"
            f"- Human review notes to apply before sharing: {confirmation_notes or 'No extra edits requested; proceed with the confirmed draft.'}\n"
            "- The source user request is below.\n\n"
            f"User request: {request.query}"
        )

    prompt_lines = [
        "Use the browser to complete the user's request safely and efficiently.",
        "Prefer deterministic navigation and stop as soon as you have the final result.",
        f"User request: {request.query}",
    ]
    if request.start_url:
        prompt_lines.append(f"Start from this URL first: {request.start_url}")
    if request.allowed_domains:
        prompt_lines.append(f"Stay within these allowed domains if possible: {', '.join(request.allowed_domains)}")
    return "\n".join(prompt_lines)
